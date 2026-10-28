"""Compact recorder → immutable loader → CPU model, with synthetic sources only."""
from copy import deepcopy
import hashlib
import json
import time

import numpy as np
import pytest

from ninefbg.drift_features import FBG_IDS, make_drift_frame
from ninefbg.experiment import ExperimentManager
from ninefbg.peak_drift_data import FILES, RECORDING_SCHEMA
from ninefbg.spectral_data import load_dataset
from ninefbg.spectral_model import load_bundle, metadata_digest, validate_metadata
from test_drift_pipeline import _rewrite, _new_synthetic_split, worker
from test_experiment import clock, context, force, result, settings, spectrum, sync


def compact_archive(root, clock, *, center_offset=0., invalid_initial=True):
    manager = ExperimentManager(root)
    active = context(clock.now)
    active["tracker"].update(peaks=[], display_stale=True)
    manager.start(settings(acquisition_protocol="force_target_wrench_v1", force_direction_confirmed=False,
                           sample_rate_hz=25, target_force_norm_N=4, max_force_norm_N=5,
                           capture_mode="peak_drift", input_kind="delta_lambda_9"), active)
    manager.baseline_request(True, active)
    number = 100

    def tick(wrench=None, invalid=False):
        nonlocal number
        stamp = clock.advance(40)
        raw = spectrum(stamp, number)
        values = [0., 0., -1., 0., 0., 0.]
        if wrench:
            values = [a+b for a,b in zip(values,wrench)]
        manager.on_force(force(stamp, number, values))
        manager.on_spectrum(raw)
        fitted = result(stamp, number)
        fitted["peaks"] = [{"id": fbg, "valid": True, "stale": False, "value_frame_id": number,
                            "center_nm": raw["wavelength_nm"][45+index*45]+center_offset+
                            (wrench[0]*.001 if wrench else 0.)} for index,fbg in enumerate(FBG_IDS)]
        if invalid:
            fitted["peaks"][0]["id"] = None
        fitted["drift_features"] = make_drift_frame(fitted, raw)
        manager.on_result(fitted, sync(number,number,values))
        number += 1

    try:
        for _ in range(12):
            tick(invalid=invalid_initial)
        assert manager.baseline["ready"]
        manager.select_point("P01")
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
        deadline = time.monotonic()+5
        while not manager.recording_checkpoint_acknowledged() and time.monotonic() < deadline:
            time.sleep(.01)
        assert manager.trial_id is None, manager.snapshot()
        path = manager.writer.path
        manager.stop()
        return path
    finally:
        manager.stop()


def read(path, name):
    return [json.loads(line) for line in (path/name).read_text(encoding="utf-8").splitlines()]


def tree_hashes(path):
    return {p.relative_to(path).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def test_compact_real_recorder_only_nine_values_no_intensity_and_loader_fixed_reference(tmp_path,clock):
    path = compact_archive(tmp_path,clock)
    manifest = json.loads((path/"manifest.json").read_text(encoding="utf-8"))
    assert manifest["recording_schema"] == RECORDING_SCHEMA and set(manifest["files"]) == FILES
    assert len(manifest["static_wavelength_nm"]) == 512
    assert not (path/"raw_spectra.jsonl").exists() and not (path/"results.jsonl").exists()
    for name in FILES:
        assert '"intensity"' not in (path/name).read_text(encoding="utf-8")
        assert '"S0"' not in (path/name).read_text(encoding="utf-8")
        assert '"wavelength_nm"' not in (path/name).read_text(encoding="utf-8")
    data = load_dataset(tmp_path,[path.name],{"method":"drift_gru"})
    assert data["x"].shape[1:] == (16,9) and data["x"].dtype == np.float32
    assert data["acquisition_provenance"]["sources"][0]["raw_spectra"] == 0
    contact = np.flatnonzero(data["position_mask"])
    np.testing.assert_allclose(data["x"][contact[-1],-1],[4.1]*9,atol=1e-5)
    assert data["references"][0]["lambda0_nm"] == manifest["final"]["trials"][0]["drift_reference"]["lambda0_nm"]
    assert len(set(data["split_labels"])) == 1
    with pytest.raises(ValueError,match="不能训练 512"):
        load_dataset(tmp_path,[path.name],{"method":"spectral_gru"})


@pytest.mark.parametrize("mutation",["hash","time","axis","reference","drift","missing_source","force","zero"])
def test_compact_loader_rejects_corrupted_hash_identity_reference_and_force_evidence(tmp_path,clock,mutation):
    path = compact_archive(tmp_path,clock)
    if mutation == "hash":
        with (path/"peak_frames.jsonl").open("a",encoding="utf-8") as stream:
            stream.write("{}\n")
    elif mutation in {"time","axis","missing_source","drift"}:
        name = "optical_sources.jsonl" if mutation == "missing_source" else "peak_frames.jsonl"
        rows = read(path,name)
        if mutation == "missing_source":
            rows.pop(20)
        elif mutation == "time":
            rows[20]["result"]["drift_features"]["host_rx_monotonic_ns"] += 1
        elif mutation == "axis":
            rows[20]["result"]["drift_features"]["axis_sha256"] = "f"*64
        else:
            row = next(row for row in rows if row.get("delta_lambda_pm") is not None)
            row["delta_lambda_pm"][0] += 1
        _rewrite(path,name,rows)
    else:
        manifest = json.loads((path/"manifest.json").read_text(encoding="utf-8"))
        trial = manifest["final"]["trials"][0]
        if mutation == "reference":
            trial["drift_reference"]["lambda0_nm"][0] += .01
        elif mutation == "force":
            trial["quality"]["metrics"]["unloaded_evidence"]["force_source_stamps_ns"][0] += 1
        else:
            manifest["final"]["baseline_history"][0]["force_zero"][0] += .1
        (path/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(tmp_path,[path.name],{"method":"drift_gru"})


def test_compact_cpu_worker_save_load_finetune_and_strict_model_source_format(tmp_path,clock):
    torch = pytest.importorskip("torch")
    first = compact_archive(tmp_path,clock)
    before = tree_hashes(first)
    trained = worker(tmp_path,[first.name],{"method":"drift_gru","epochs":1})
    assert trained["status"] == "completed", trained
    parent, network = load_bundle(tmp_path/"spectral_models"/trained["model_id"])
    assert parent["input_kind"] == "delta_lambda_9" and len(parent["input_normalization"]["mean"]) == 9
    assert parent["acquisition_provenance"]["sources"][0]["recording_schema"] == RECORDING_SCHEMA
    with torch.inference_mode():
        assert network(torch.zeros(1,16,9))[1].shape == (1,6)
    invalid = deepcopy(parent)
    invalid["acquisition_provenance"]["sources"][0].pop("recording_schema")
    invalid["metadata_sha256"] = invalid["model_sha256"] = metadata_digest(invalid)
    with pytest.raises(ValueError):
        validate_metadata(invalid)
    second = compact_archive(tmp_path,clock,center_offset=.01)
    child = worker(tmp_path,[second.name],{"method":"drift_gru","training_mode":"finetune",
                       "base_model_id":parent["model_id"],"epochs":1})
    assert child["status"] == "completed", child
    metadata,_ = load_bundle(tmp_path/"spectral_models"/child["model_id"])
    assert metadata["input_normalization"] == parent["input_normalization"]
    assert metadata["base_model_id"] == parent["model_id"]
    assert tree_hashes(first) == before


def test_original_nine_raw_parent_accepts_new_compact_same_peak_definition_without512_weights(tmp_path,clock):
    pytest.importorskip("torch")
    from test_drift_pipeline import drift_archive
    old = drift_archive(tmp_path,clock)
    trained = worker(tmp_path,[old.name],{"method":"drift_gru","epochs":1})
    assert trained["status"] == "completed", trained
    compact = compact_archive(tmp_path,clock)
    child = worker(tmp_path,[compact.name],{"method":"drift_gru","training_mode":"finetune",
                       "base_model_id":trained["model_id"],"epochs":1})
    assert child["status"] == "completed", child
    metadata,_ = load_bundle(tmp_path/"spectral_models"/child["model_id"])
    assert len(metadata["acquisition_provenance"]["sources"]) == 2
    merged = load_dataset(tmp_path,[old.name,compact.name],{"method":"drift_gru"})
    assert merged["x"].shape[1:] == (16,9)
    assert {source.get("recording_schema") for source in merged["acquisition_provenance"]["sources"]} == {None,RECORDING_SCHEMA}
