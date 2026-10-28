"""Generated replay and CPU process checks; these do not qualify hardware accuracy."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pytest

from ninefbg.spectral_data import SYNC_RULE, align_force, fit_normalization, load_dataset, training_selection
from ninefbg.spectral_model import AXES, UNITS, build_network, load_bundle, masked_losses, validate_metadata, metadata_digest
from ninefbg.spectral_training import SpectralTrainingManager
from ninefbg.experiment import coordinate_definition, default_point_grid


def spectral_archive(userdata, name="replay_session_1", *, sample_offset=0., trials_count=4):
    """Explicitly synthetic fixture with 25 Hz spectra / approximately 30 Hz force."""
    path = Path(userdata) / "calibration_sessions" / name
    path.mkdir(parents=True)
    axis = [1500. + .1 * index for index in range(512)]
    preprocessing = {"state": "raw_sdk_uint16_counts", "integration_us_requested": 70}
    raw_s, raw_f, baselines, trials = [], [], [], []
    spectrum_id = force_id = 0
    for trial_number in range(1, trials_count + 1):
        bid, tid = f"B{trial_number:04d}", f"T{trial_number:04d}"
        point = f"P{(trial_number - 1) % 2 + 1:02d}"
        start = 1_000_000_000 + (trial_number - 1) * 6_000_000_000
        s0 = [1000. + index / 10 for index in range(512)]
        ids, stamps = [], []
        for index in range(12):
            spectrum_id += 1
            stamp = start + index * 40_000_000
            ids.append(spectrum_id)
            stamps.append(stamp)
            raw_s.append({"frame_id": spectrum_id, "stream_epoch": "spectrum-epoch", "host_rx_monotonic_ns": stamp,
                          "time_quality": "host_rx_approx", "source_kind": "hardware", "valid": False,
                          "intensity": list(s0), "wavelength_nm": axis, "preprocessing": preprocessing,
                          "trial_id": None, "baseline_id": bid, "point_id": point, "phase": "unloaded"})
        baseline = {"baseline_id": bid, "S0": s0, "wavelength_nm": axis, "source_frame_ids": ids,
                    "source_stamps_ns": stamps, "spectral_reference_id": "S0-" + bid,
                    "force_zero": [1., 2., 3., .01, .02, .03], "force_noise_axes": [.01] * 6,
                    "optical_epoch": "spectrum-epoch", "force_epoch": "force-epoch", "unloaded_confirmed": True,
                    "requested_monotonic_ns": start, "effective_monotonic_ns": start + 500_000_000,
                    "trigger": "manual_unloaded_confirmation"}
        baselines.append(baseline)
        for index in range(96):
            spectrum_id += 1
            stamp = start + 600_000_000 + index * 40_000_000
            phase = "preload" if index < 20 else "loading" if index < 80 else "recovery"
            raw_s.append({"frame_id": spectrum_id, "stream_epoch": "spectrum-epoch", "host_rx_monotonic_ns": stamp,
                          "time_quality": "host_rx_approx", "source_kind": "hardware", "valid": False,
                          "intensity": [value + sample_offset + index / 20 for value in s0],
                          "wavelength_nm": axis, "preprocessing": preprocessing,
                          "trial_id": tid, "baseline_id": bid, "point_id": point, "phase": phase})
        for index in range(117):
            force_id += 1
            stamp = start + 600_000_000 + index * 33_333_333
            contact = 20 * 40_000_000 <= stamp - (start + 600_000_000) < 80 * 40_000_000
            corrected = [.2, -.3, .4, .002, -.003, .004] if contact else [.01, -.01, .005, .0001, -.0001, .0002]
            raw_f.append({"frame_id": force_id, "stream_epoch": "force-epoch", "host_rx_monotonic_ns": stamp,
                          "time_quality": "host_rx_approx", "source_kind": "hardware", "axes": AXES, "units": UNITS,
                          "force_values": [a + b for a, b in zip(corrected, baseline["force_zero"])],
                          "trial_id": tid, "baseline_id": bid, "point_id": point,
                          "phase": "loading" if contact else "preload"})
        trials.append({"trial_id": tid, "baseline_id": bid, "point_id": point, "status": "completed",
                       "manual_mark": [], "quality": {"accepted": True}})
    values = {"raw_spectra.jsonl": raw_s, "raw_force.jsonl": raw_f, "events.jsonl": [], "results.jsonl": []}
    manifest = {"kind": "9fbg_calibration_experiment_v1", "complete": True,
                "settings": {"finger_id": "synthetic-fixture-only", "batch": "A", "point_grid": default_point_grid(),
                             "coordinate_definition": coordinate_definition(), "sample_rate_hz": 25,
                             "acquisition_protocol": "timed_wrench_v1", "conditions": {"fixation": "fixture", "temperature_C": 23},
                             "timed_wrench_criteria": {"sync_rule": SYNC_RULE, "contact_force_norm_N": .1}},
                "counts": {name.removesuffix(".jsonl"): len(rows) for name, rows in values.items()},
                "files": {}, "final": {"baseline_history": baselines, "trials": trials}}
    for name_, rows in values.items():
        file = path / name_
        file.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        manifest["files"][name_] = hashlib.sha256(file.read_bytes()).hexdigest()
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _rewrite(path, filename, rows):
    file = path / filename
    file.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["files"][filename] = hashlib.sha256(file.read_bytes()).hexdigest()
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _wait(manager, predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.status()
        if predicate(state):
            return state
        time.sleep(.04)
    pytest.fail("训练子进程超时：" + repr(manager.status()))


def test_raw_512_windows_masked_labels_and_fixed_split_without_peak_rows(tmp_path):
    path = spectral_archive(tmp_path)
    data = load_dataset(tmp_path, [path.name])
    assert data["x"].shape == (4 * 81, 16, 512)
    assert data["x"].dtype == np.float32
    assert data["wrench_mask"].mean() > .98  # 25/30 Hz receive-time bracketing replay.
    assert data["position_mask"].sum() < data["wrench_mask"].sum()
    assert np.any(data["wrench"][:, 1] < 0)
    for row in data["windows"]:
        assert len(set(row["frame_ids"])) == 16
        assert row["sync"].get("method") != "nearest_diagnostic_only"
    train = {row["trial_id"] for row, split in zip(data["windows"], data["split_labels"]) if split == "train"}
    validation = {row["trial_id"] for row, split in zip(data["windows"], data["split_labels"]) if split == "validation"}
    assert train and validation and not train & validation
    original_assignments = dict(data["split"]["assignments"])
    second = spectral_archive(tmp_path, "replay_session_2", sample_offset=20000)
    extended = load_dataset(tmp_path, [path.name, second.name])
    assert all(extended["split"]["assignments"][key] == value for key, value in original_assignments.items())
    normal_x, normal_y = fit_normalization(data)
    assert normal_x["fitted_split"] == normal_y["fitted_split"] == "train"
    assert normal_y["scale"][0] >= .05 and normal_y["scale"][3] >= .001
    assert normal_x["unique_frames"] < len(train) * 81 * 16


def test_source_hash_active_archive_and_reference_tampering_rejected(tmp_path):
    path = spectral_archive(tmp_path)
    with (path / "raw_force.jsonl").open("a") as stream:
        stream.write("{}\n")
    with pytest.raises(ValueError, match="哈希"):
        load_dataset(tmp_path, [path.name])
    (path / "manifest.json").unlink()
    manager = SpectralTrainingManager(tmp_path)
    with pytest.raises(ValueError, match="活动 JSONL"):
        manager.start([path.name])
    manager.close()


def test_gap_duplicate_identity_and_cross_boundary_do_not_fill_windows(tmp_path):
    path = spectral_archive(tmp_path)
    rows = [json.loads(line) for line in (path / "raw_spectra.jsonl").read_text().splitlines()]
    selected = [index for index, row in enumerate(rows) if row.get("trial_id") == "T0001"]
    rows[selected[40]]["frame_id"] = rows[selected[39]]["frame_id"]
    _rewrite(path, "raw_spectra.jsonl", rows)
    data = load_dataset(tmp_path, [path.name])
    assert data["rejected"]["spectrum_source_reference_or_config"] == 1
    assert data["x"].shape[0] < 4 * 81
    spectrum = {"host_rx_monotonic_ns": 1_020_000_000, "trial_id": "T1", "baseline_id": "B1"}
    force_rows = [{"host_rx_monotonic_ns": stamp, "trial_id": trial, "baseline_id": "B1", "stream_epoch": "F",
                   "frame_id": index, "force_values": [1.] * 6, "source_kind": "hardware", "time_quality": "host_rx_approx"}
                  for index, (stamp, trial) in enumerate(((1_000_000_000, "T1"), (1_040_000_000, "T2")))]
    assert align_force(spectrum, force_rows, [row["host_rx_monotonic_ns"] for row in force_rows],
                       {"force_epoch": "F", "force_zero": [0.] * 6})["valid"] is False


def test_shared_unloaded_source_trials_never_cross_split(tmp_path):
    path = spectral_archive(tmp_path)
    manifest = json.loads((path / "manifest.json").read_text())
    first = manifest["final"]["baseline_history"][0]
    second = manifest["final"]["baseline_history"][2]
    for key in ("S0", "wavelength_nm", "source_frame_ids", "source_stamps_ns", "spectral_reference_id"):
        second[key] = deepcopy(first[key])
    second["reused_from_baseline_id"] = first["baseline_id"]
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    data = load_dataset(tmp_path, [path.name])
    split_by_trial = {row["trial_id"]: split for row, split in zip(data["windows"], data["split_labels"])}
    assert split_by_trial[path.name + ":T0001"] == split_by_trial[path.name + ":T0003"]


def test_next_s0_recovery_sources_connect_previous_supervised_trial(tmp_path):
    path = spectral_archive(tmp_path)
    manifest = json.loads((path / "manifest.json").read_text())
    rows = [json.loads(line) for line in (path / "raw_spectra.jsonl").read_text().splitlines()]
    recovery = [row for row in rows if row["trial_id"] == "T0001" and row["phase"] == "recovery"]
    next_baseline = manifest["final"]["baseline_history"][2]
    next_baseline.update(S0=np.median([row["intensity"] for row in recovery], axis=0).tolist(),
                         source_frame_ids=[row["frame_id"] for row in recovery],
                         source_stamps_ns=[row["host_rx_monotonic_ns"] for row in recovery],
                         trigger="automatic_unloaded_recovery")
    # B1's original S0 sources and B3's new sources have no intersection, but
    # B3's S0 sources are part of T1's supervised recovery sequence.
    assert not set(next_baseline["source_frame_ids"]) & set(manifest["final"]["baseline_history"][0]["source_frame_ids"])
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    data = load_dataset(tmp_path, [path.name])
    split_by_trial = {row["trial_id"]: split for row, split in zip(data["windows"], data["split_labels"])}
    assert split_by_trial[path.name + ":T0001"] == split_by_trial[path.name + ":T0003"]


def test_timed_spectrum_request_rate_must_be_recorded_as_25hz(tmp_path):
    path = spectral_archive(tmp_path)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["settings"]["sample_rate_hz"] = 100
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="25 Hz"):
        load_dataset(tmp_path, [path.name])


def target_protocol_archive(userdata, name="target_replay_session"):
    path = spectral_archive(userdata, name)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["settings"]["acquisition_protocol"] = "force_target_wrench_v1"
    manifest["settings"]["force_target_wrench_criteria"] = manifest["settings"].pop("timed_wrench_criteria")
    for trial in manifest["final"]["trials"]:
        trial["quality"]["criteria_version"] = "force_target_wrench_quality_v1"
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name_ in ("raw_spectra.jsonl", "raw_force.jsonl"):
        rows = [json.loads(line) for line in (path / name_).read_text().splitlines()]
        for row in rows:
            if row["phase"] == "preload":
                row["phase"] = "waiting_contact"
            elif row["phase"] == "loading":
                row["phase"] = "release"  # The saved target point is still fixed while unloading.
        _rewrite(path, name_, rows)
    return path


def test_target_protocol_release_mask_waiting_force_only_and_saving_excluded(tmp_path):
    path = target_protocol_archive(tmp_path)
    rows = [json.loads(line) for line in (path / "raw_spectra.jsonl").read_text().splitlines()]
    trial_rows = [row for row in rows if row["trial_id"] == "T0001"]
    end = trial_rows[-9]["host_rx_monotonic_ns"]
    for row in trial_rows[-8:]:
        row["phase"] = "saving"
    _rewrite(path, "raw_spectra.jsonl", rows)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["final"]["trials"][0]["data_end_monotonic_ns"] = end
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    data = load_dataset(tmp_path, [path.name])
    phases = np.asarray([row["phase"] for row in data["windows"]])
    assert data["position_mask"][phases == "release"].any()
    assert not data["position_mask"][phases == "waiting_contact"].any()
    assert not data["position_mask"][phases == "recovery"].any()
    assert data["wrench_mask"][phases == "waiting_contact"].any()
    assert data["wrench_mask"][phases == "recovery"].any()
    assert "saving" not in phases
    assert data["rejected"]["after_trial_data_end"] == 8
    assert all(row["stamp"] <= end for row in data["windows"] if row["trial_id"].endswith(":T0001"))


def test_long_target_waiting_windows_train_cap_follows_split_and_preserves_evaluation(tmp_path):
    # Focused selection fixture: raw loader/CPU integration is exercised below
    # and in the real ExperimentManager integration, without copying a network.
    windows = [{"trial_id": "trial-a", "protocol": "force_target_wrench_v1", "phase": phase}
               for phase, count in (("waiting_contact", 500), ("loading", 20), ("release", 10), ("recovery", 25))
               for _ in range(count)]
    windows += [{"trial_id": "trial-v", "protocol": "force_target_wrench_v1", "phase": "waiting_contact"}] * 100
    data = {"windows": windows, "split_labels": np.asarray(["train"] * 555 + ["validation"] * 100),
            "position_mask": np.asarray([False] * 500 + [True] * 30 + [False] * 125),
            "wrench_mask": np.ones(655, dtype=bool)}
    selected, policy = training_selection(data)
    assert len(selected) == 60
    assert data["position_mask"][selected].sum() == 30
    assert all(data["split_labels"][index] == "train" for index in selected)
    assert policy["trials"][0]["force_only_by_phase"] == {"waiting_contact": 15, "recovery": 15}
    assert np.array_equal(selected, training_selection(data)[0])
    assert len(data["windows"]) == 655  # Validation remains complete and naturally distributed.
    data["windows"] = [{**row, "protocol": "timed_wrench_v1"} for row in windows]
    assert len(training_selection(data)[0]) == 555  # The fixed-duration protocol retains its old behavior.


def test_cpu_two_heads_empty_position_loss_and_finite_gradients():
    torch = pytest.importorskip("torch")
    torch.set_num_threads(2)
    model = build_network()
    x = torch.randn(3, 16, 512)
    p, y = model(x)
    assert p.shape == (3, 25) and y.shape == (3, 6)
    lp, ly = masked_losses(p, y, torch.tensor([0, 1, 2]), torch.randn(3, 6),
                           torch.zeros(3, dtype=torch.bool), torch.ones(3, dtype=torch.bool))
    assert lp.item() == 0 and torch.isfinite(ly)
    (lp + ly).backward()
    assert all(parameter.grad is not None and torch.isfinite(parameter.grad).all() for parameter in model.parameters())


def test_cpu_worker_pause_reasons_cancel_reap_and_successful_candidate(tmp_path):
    pytest.importorskip("torch")
    path = spectral_archive(tmp_path)
    manager = SpectralTrainingManager(tmp_path)
    manager.start([path.name], {"method": "spectral_gru", "epochs": 1000, "patience": 1000})
    manager.pause("user")
    manager.pause("acquisition")
    state = _wait(manager, lambda state: state["status"] == "paused")
    assert state["busy"] and state["pause_reasons"] == ["acquisition", "user"]
    assert manager.resume("acquisition")["pause_reasons"] == ["user"]
    time.sleep(.1)
    assert manager.status()["status"] == "paused"
    manager.cancel()
    state = _wait(manager, lambda state: state["status"] == "cancelled")
    assert state["progress_percent"] < 100
    manager.close()
    assert manager.process is None
    manager = SpectralTrainingManager(tmp_path)
    manager.start([path.name], {"epochs": 2, "batch_size": 16})
    state = _wait(manager, lambda state: not state["busy"])
    assert state["status"] == "completed", state
    assert state["progress_percent"] == 100 and state["model_id"]
    model_path = tmp_path / "spectral_models" / state["model_id"]
    metadata, model = load_bundle(model_path)
    assert metadata["formal_ready"] is False
    assert metadata["evaluation"]["independent_validation"] is True
    assert metadata["trained_point_ids"] == ["P01", "P02"]
    assert metadata["input_normalization"]["fitted_split"] == "train"
    for mutate, message in ((lambda item: item["window_rule"].update(target="window_mean"), "窗口规则"),
                            (lambda item: item["sync_rule"].update(maximum_bracket_ms=99), "同步规则"),
                            (lambda item: item["input_normalization"].update(fitted_split="validation"), "训练集合"),
                            (lambda item: item["acquisition_provenance"]["physical_definition"]["point_grid"][0].update(col=4), "点阵坐标")):
        tampered = deepcopy(metadata)
        mutate(tampered)
        tampered["metadata_sha256"] = tampered["model_sha256"] = metadata_digest(tampered)
        with pytest.raises(ValueError, match=message):
            validate_metadata(tampered)
    weights = model_path / "weights.pt"
    with weights.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="权重哈希"):
        load_bundle(model_path)
    manager.close()


def test_close_reaps_paused_worker_and_options_reject_invalid(tmp_path):
    pytest.importorskip("torch")
    path = spectral_archive(tmp_path)
    manager = SpectralTrainingManager(tmp_path)
    with pytest.raises(ValueError, match="参数"):
        manager.start([path.name], {"cpu_threads": 0})
    manager.start([path.name], {"epochs": 1000})
    manager.pause()
    _wait(manager, lambda state: state["status"] == "paused")
    manager.close()
    assert manager.status()["status"] == "cancelled"
    assert manager.process is None
