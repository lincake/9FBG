"""Synthetic fixed-source nine-peak replays; never hardware accuracy evidence."""
from copy import deepcopy
import hashlib
import json
import queue
import time

import numpy as np
import pytest

from ninefbg.drift_features import FBG_IDS, make_drift_frame
from ninefbg.spectral_data import fit_normalization, load_dataset, training_selection
from ninefbg.spectral_finetuning import prepare_training
from ninefbg.spectral_model import (DRIFT_ARCHITECTURE, build_network, load_bundle,
                                    metadata_digest, validate_metadata)
from ninefbg.spectral_training import checked_options, _worker, SpectralTrainingManager
from test_experiment import clock, context, force, result, spectrum, sync
from test_force_target_spectral_integration import _start_recording
from test_spectral_finetuning import Events, saved_parent


def _rewrite(path, name, rows):
    file = path/name
    file.write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in rows), encoding="utf-8")
    manifest = json.loads((path/"manifest.json").read_text(encoding="utf-8"))
    manifest["files"][name] = hashlib.sha256(file.read_bytes()).hexdigest()
    (path/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def _new_synthetic_split(root):
    # Only generated tmp_path ledgers: these mutations are alternative fixtures,
    # not edits to a real source already accepted into a persisted split.
    for ledger in (root/"spectral_splits").glob("*.json"):
        ledger.unlink()


def drift_archive(root, clock, *, center_offset=0., include_features=True):
    """Exercise the real v2 recorder, with declared synthetic fitted centers."""
    manager = _start_recording(root, clock)
    number = 1000

    def tick(wrench=None):
        nonlocal number
        stamp = clock.advance(40)
        values = [0., 0., -1., 0., 0., 0.]
        if wrench:
            values = [a+b for a, b in zip(values, wrench)]
        raw = spectrum(stamp, number)
        manager.on_force(force(stamp, number, values))
        manager.on_spectrum(raw)
        fitted = result(stamp, number)
        shift = (wrench[0] if wrench else 0.) * .001
        fitted["peaks"] = [{"id": fbg, "valid": True, "stale": False, "value_frame_id": number,
                            "center_nm": raw["wavelength_nm"][45+index*45] + center_offset + shift}
                           for index, fbg in enumerate(FBG_IDS)]
        if include_features:
            fitted["drift_features"] = make_drift_frame(fitted, raw)
        manager.on_result(fitted, sync(number, number, values))
        number += 1

    try:
        manager.trial("start", context(clock.now))
        assert manager.training_pause_acknowledged()
        for _ in range(51):
            tick()
        assert manager.phase == "waiting_contact"
        for _ in range(8):
            tick()
        for _ in range(24):
            tick([4.1, -.2, .1, .002, -.003, .004])
        assert manager.phase == "release"
        for _ in range(5):
            tick()
        manager.trial("finish", context(clock.now))
        for _ in range(30):
            tick()
            if manager.phase == "saving":
                break
        assert manager.phase == "saving"
        token = manager._timed["trial"]["checkpoint_token"]
        deadline = time.monotonic()+4
        while not manager.writer.checkpoint_ready(token) and time.monotonic() < deadline:
            time.sleep(.01)
        assert manager.recording_checkpoint_acknowledged()
        assert manager._trials["T0001"]["quality"]["accepted"]
        path = manager.writer.path
        manager.stop()
        return path
    finally:
        manager.stop()


def worker(root, selected, raw_options):
    options = checked_options(raw_options)
    merged, plan = prepare_training(root, selected, options, explicit_options=raw_options)
    events = Events()
    _worker(str(root), "synthetic-nine-peak", merged, options, queue.Queue(), events, plan)
    values = []
    while not events.empty():
        values.append(events.get_nowait())
    return values[-1]


def test_nine_input_actual_centers_pm_fixed_preload_no_future_target_or_chart_reference(tmp_path, clock):
    path = drift_archive(tmp_path, clock)
    data = load_dataset(tmp_path, [path.name], {"method": "drift_gru"})
    assert data["x"].shape[1:] == (16, 9)
    assert data["x"].dtype == np.float32
    assert data["input_kind"] == "delta_lambda_9"
    reference = data["references"][0]
    assert len(reference["lambda0_nm"]) == 9 and len(reference["source_frame_ids"]) >= 12
    assert "S0" not in reference
    assert all(row["stamp"] >= reference["effective_ns"] for row in data["windows"])
    contact = np.flatnonzero(data["position_mask"])
    assert len(contact) > 10
    np.testing.assert_allclose(data["x"][contact[-1], -1], [4.1]*9, atol=1e-5)
    assert len(set(row["trial_id"] for row in data["windows"])) == 1
    # The saved chart reference is unrelated and deliberately changed. Peaks
    # and drift_features, not result.reference_nm or x_pm, define this input.
    saved = [json.loads(line) for line in (path/"results.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in saved:
        row["x_pm"] = [987654.]*9
        row["result"]["reference_nm"] = [1.]*9
        row["result"]["drift_nm"] = [999.]*9
    _rewrite(path, "results.jsonl", saved)
    _new_synthetic_split(tmp_path)
    later = load_dataset(tmp_path, [path.name], {"method": "drift_gru"})
    np.testing.assert_array_equal(data["x"], later["x"])


def test_missing_peaks_break_sequence_and_never_fill_zero_or_hold_last(tmp_path, clock):
    path = drift_archive(tmp_path, clock)
    original = load_dataset(tmp_path, [path.name], {"method": "drift_gru"})
    saved = [json.loads(line) for line in (path/"results.jsonl").read_text(encoding="utf-8").splitlines()]
    row = [row for row in saved if row["phase"] == "release"][5]
    invalid_id = row["frame_id"]
    row["result"]["peaks"][0]["valid"] = False
    raw = next(json.loads(line) for line in (path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()
               if json.loads(line)["frame_id"] == invalid_id)
    row["result"]["drift_features"] = make_drift_frame(row["result"], raw)
    _rewrite(path, "results.jsonl", saved)
    _new_synthetic_split(tmp_path)
    data = load_dataset(tmp_path, [path.name], {"method": "drift_gru"})
    assert len(data["x"]) < len(original["x"])
    assert data["rejected"]["nine_peak_invalid_or_config"] == 1
    assert all(invalid_id not in row["frame_ids"] for row in data["windows"])


def test_historical_v2_peak_results_adapt_but_missing_preload_or_mismatched_feature_rejected(tmp_path, clock):
    path = drift_archive(tmp_path, clock, include_features=False)
    assert load_dataset(tmp_path, [path.name], {"method": "drift_gru"})["x"].shape[2] == 9
    saved = [json.loads(line) for line in (path/"results.jsonl").read_text(encoding="utf-8").splitlines()]
    raw_by_id = {row["frame_id"]: row for row in map(json.loads, (path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines())}
    saved[0]["result"]["drift_features"] = make_drift_frame(saved[0]["result"], raw_by_id[saved[0]["frame_id"]])
    saved[0]["result"]["drift_features"]["centers_nm"][0] += .01
    _rewrite(path, "results.jsonl", saved)
    with pytest.raises(ValueError, match="保存的九路"):
        load_dataset(tmp_path, [path.name], {"method": "drift_gru"})
    saved[0]["result"].pop("drift_features")
    _rewrite(path, "results.jsonl", saved)
    manifest = json.loads((path/"manifest.json").read_text(encoding="utf-8"))
    manifest["final"]["trials"][0]["quality"]["metrics"]["press_prompt_monotonic_ns"] = None
    (path/"manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(tmp_path, [path.name], {"method": "drift_gru"})


def test_worker_save_load_shapes_train_only_normalization_and_same_kind_finetune(tmp_path, clock, monkeypatch):
    torch = pytest.importorskip("torch")
    old = drift_archive(tmp_path, clock)
    result = worker(tmp_path, [old.name], {"method": "drift_gru", "epochs": 1, "batch_size": 32})
    assert result["status"] == "completed", result
    parent, network = load_bundle(tmp_path/"spectral_models"/result["model_id"])
    assert parent["architecture"] == {**DRIFT_ARCHITECTURE, "contact_outputs": 1}
    assert parent["method"] == "drift_gru" and parent["input_kind"] == "delta_lambda_9"
    assert len(parent["input_normalization"]["mean"]) == 9
    assert parent["input_normalization"]["fitted_split"] == "train"
    assert all(scale >= .1 for scale in parent["input_normalization"]["scale"])
    with torch.inference_mode():
        logits, wrench, contact_logits = network(torch.zeros((2, 16, 9)))
        assert logits.shape == (2, 25) and wrench.shape == (2, 6) and contact_logits.shape == (2,)
        with pytest.raises(ValueError):
            network(torch.zeros((2, 16, 512)))
    for key, update in (("input_normalization", {"mean": [0.]*512}),
                        ("window_rule", {"max_gap_ms": 121}),
                        ("peak_definition", {"config_version": ""})):
        invalid = deepcopy(parent)
        invalid[key].update(update)
        invalid["metadata_sha256"] = invalid["model_sha256"] = metadata_digest(invalid)
        with pytest.raises(ValueError):
            validate_metadata(invalid)
    new = drift_archive(tmp_path, clock, center_offset=.01)
    before = {key: value.clone() for key, value in network.state_dict().items()}
    monkeypatch.setattr(torch.optim.Adam, "step", lambda *args, **kwargs: None)
    child_result = worker(tmp_path, [new.name], {"method": "drift_gru", "training_mode": "finetune",
                          "base_model_id": parent["model_id"], "epochs": 1, "batch_size": 32})
    assert child_result["status"] == "completed", child_result
    child, child_network = load_bundle(tmp_path/"spectral_models"/child_result["model_id"])
    assert child["input_normalization"] == parent["input_normalization"]
    assert all(torch.equal(value, child_network.state_dict()[key]) for key, value in before.items())
    assert set(parent["training_trial_ids"]).issubset(child["training_trial_ids"])
    assert child["base_model_id"] == parent["model_id"]
    # Changing absolute wavelength zero per session does not change per-round drift.
    data = load_dataset(tmp_path, [old.name, new.name], {"method": "drift_gru"})
    selected, _ = training_selection(data)
    normal_x, _ = fit_normalization(data, selected)
    assert len(normal_x["mean"]) == 9


def test_old_512_parent_rejected_before_new_worker_and_old_network_kept(tmp_path, clock):
    old, new, parent = saved_parent(tmp_path)
    options = checked_options({"method": "drift_gru", "training_mode": "finetune", "base_model_id": parent["model_id"]})
    with pytest.raises(ValueError, match="不能互相继承"):
        prepare_training(tmp_path, [new.name], options)
    torch = pytest.importorskip("torch")
    with torch.inference_mode():
        assert build_network()(torch.zeros(1, 16, 512))[1].shape == (1, 6)
    with pytest.raises(ValueError, match="120 ms"):
        checked_options({"method": "drift_gru", "max_spectrum_gap_ms": 200})


def test_actual_peak_stream_gap_does_not_use_intermediate_raw_spectra_as_inputs(tmp_path, clock):
    path = drift_archive(tmp_path, clock)
    saved = [json.loads(line) for line in (path/"results.jsonl").read_text(encoding="utf-8").splitlines()]
    contact = [row for row in saved if row["phase"] == "release"]
    removed = {row["frame_id"] for row in contact[5:10]}
    _rewrite(path, "results.jsonl", [row for row in saved if row["frame_id"] not in removed])
    data = load_dataset(tmp_path, [path.name], {"method": "drift_gru"})
    assert data["rejected"]["spectrum_gap_or_order"] == 1
    assert all(not set(row["frame_ids"]) & removed for row in data["windows"])
    raw = {row["frame_id"]: row["host_rx_monotonic_ns"] for row in map(json.loads,
           (path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines())}
    assert all(0 < raw[b]-raw[a] <= 120_000_000 for row in data["windows"]
               for a, b in zip(row["frame_ids"], row["frame_ids"][1:]))


def test_nine_normalization_ignores_independent_validation_and_test_values(tmp_path, clock):
    first = drift_archive(tmp_path, clock)
    second = drift_archive(tmp_path, clock, center_offset=.05)
    data = load_dataset(tmp_path, [first.name, second.name], {"method": "drift_gru"})
    assert data["split"]["group_mode"] == "session"
    assert (data["split_labels"] == "validation").any()
    selected, _ = training_selection(data)
    expected = fit_normalization(data, selected)
    nontrain = data["split_labels"] != "train"
    data["x"][nontrain] = 1.e8
    data["wrench"][nontrain] = -1.e8
    assert fit_normalization(data, selected) == expected
    train_trials = {row["trial_id"] for row, label in zip(data["windows"], data["split_labels"]) if label == "train"}
    heldout_trials = {row["trial_id"] for row, label in zip(data["windows"], data["split_labels"]) if label == "validation"}
    assert not train_trials & heldout_trials


def test_nine_manager_spawn_pause_cancel_and_next_complete_keep_method(tmp_path, clock):
    pytest.importorskip("torch")
    path = drift_archive(tmp_path, clock)
    manager = SpectralTrainingManager(tmp_path)

    def wait(predicate):
        deadline = time.monotonic()+25
        while time.monotonic() < deadline:
            state = manager.status()
            if predicate(state):
                return state
            time.sleep(.02)
        pytest.fail("九路训练子进程超时："+repr(manager.status()))

    try:
        started = manager.start([path.name], {"method": "drift_gru", "epochs": 1000})
        assert started["method"] == "drift_gru" and "寻峰漂移" in started["method_label"]
        manager.pause("acquisition")
        state = wait(lambda state: state["status"] == "paused")
        assert state["method"] == "drift_gru" and state["pause_reasons"] == ["acquisition"]
        manager.cancel()
        state = wait(lambda state: not state["busy"])
        assert state["status"] == "cancelled"
        manager.start([path.name], {"method": "drift_gru", "epochs": 1})
        state = wait(lambda state: not state["busy"])
        assert state["status"] == "completed", state
        assert state["method"] == "drift_gru"
        metadata, _ = load_bundle(tmp_path/"spectral_models"/state["model_id"])
        assert metadata["input_kind"] == "delta_lambda_9"
    finally:
        manager.close()


def test_preload_reference_uses_same_continuous_tail_after_early_loss_as_online(tmp_path, clock):
    path = drift_archive(tmp_path, clock)
    saved = [json.loads(line) for line in (path/"results.jsonl").read_text(encoding="utf-8").splitlines()]
    raw_by_id = {row["frame_id"]: row for row in map(json.loads,
                 (path/"raw_spectra.jsonl").read_text(encoding="utf-8").splitlines())}
    preload = [row for row in saved if row["phase"] == "preload"]
    assert len(preload) >= 40
    for row in preload[:20]:
        for peak in row["result"]["peaks"]:
            peak["center_nm"] += .05
    preload[20]["result"]["peaks"][0]["valid"] = False
    for row in preload:
        row["result"]["drift_features"] = make_drift_frame(row["result"], raw_by_id[row["frame_id"]])
    _rewrite(path, "results.jsonl", saved)
    data = load_dataset(tmp_path, [path.name], {"method": "drift_gru"})
    reference = data["references"][0]
    expected_ids = [row["frame_id"] for row in preload[21:]]
    assert reference["source_frame_ids"] == expected_ids
    np.testing.assert_allclose(reference["lambda0_nm"],
                               preload[-1]["result"]["drift_features"]["centers_nm"])
    contact = np.flatnonzero(data["position_mask"])
    np.testing.assert_allclose(data["x"][contact[-1], -1], [4.1]*9, atol=1e-5)
