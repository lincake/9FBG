"""Recorder-to-training checks with synthetic source frames, not hardware acceptance."""
from copy import deepcopy
import json
import time

import numpy as np
import pytest

from ninefbg.experiment import ExperimentManager, list_sessions
from ninefbg.spectral_data import load_dataset, training_selection
from test_experiment import clock, context, force, result, settings, spectrum
from test_spectral_pipeline import _rewrite, target_protocol_archive
from test_timed_wrench import tick


def _saved_direct_session(userdata, clock, rounds=1):
    manager = ExperimentManager(userdata)
    active = context(clock.now)
    manager.start(settings(acquisition_protocol="force_target_wrench_v1", sample_rate_hz=25), active)
    manager.baseline_request(True, active)
    number = 1000
    try:
        for _ in range(12):
            timestamp = clock.advance(40)
            manager.on_force(force(timestamp, number))
            manager.on_spectrum(spectrum(timestamp, number))
            number += 1
        manager.select_point("P01")
        original_zero, original_s0 = list(manager.baseline["force_zero"]), list(manager.baseline["S0"])
        for round_index in range(rounds):
            manager.trial("start", context(clock.now))
            assert manager.training_pause_acknowledged()
            zero = [a-b for a, b in zip(manager.baseline["force_zero"], original_zero)]
            spectral_delta = manager.baseline["S0"][0]-original_s0[0]
            for _ in range(51):
                tick(manager, clock, number, zero, intensity_delta=spectral_delta)
                number += 1
            assert manager.phase == "waiting_contact"
            press = [a+b for a, b in zip(zero, [4.1, -.2, .1, .002, -.003, .004])]
            for _ in range(20):
                tick(manager, clock, number, press, intensity_delta=spectral_delta+1)
                number += 1
            manager.trial("finish", context(clock.now))
            # Deliberately not near the previous force zero or recovered S0.
            # A manual finish is an assertion, not measured recovery evidence.
            next_zero = [.3*(round_index+1), -.1, .2, .001, -.002, .003]
            tick(manager, clock, number, next_zero, intensity_delta=9*(round_index+1))
            number += 1
            assert manager.phase == "saving", manager.snapshot()
            token = manager._timed["trial"]["checkpoint_token"]
            deadline = time.monotonic()+5
            while not manager.writer.checkpoint_ready(token) and time.monotonic() < deadline:
                time.sleep(.005)
            assert manager.writer.checkpoint_ready(token)
            assert manager.recording_checkpoint_acknowledged()
            assert manager.snapshot()["guided"]["last_quality"]["accepted"]
        invalid = result(clock.now, number+100000)
        invalid.update(peaks=[], valid=False)
        manager.on_result(invalid, {"valid": False, "reason": "synthetic: peak fit unavailable"})
        manager.stop()
        manifest_path = manager.writer.path/"manifest.json"
        return manager.session_id, manifest_path
    finally:
        manager.stop()


def test_direct_zero_sources_preload_masks_and_previous_round_zero_are_immutable(tmp_path, clock):
    session, manifest_path = _saved_direct_session(tmp_path, clock, rounds=2)
    before = {path.name: path.read_bytes() for path in manifest_path.parent.iterdir() if path.is_file()}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {trial["quality"]["criteria_version"] for trial in manifest["final"]["trials"]} == {"force_target_wrench_quality_v2"}
    baselines = manifest["final"]["baseline_history"]
    assert baselines[0]["force_zero"] == [0, 0, -1, 0, 0, 0]
    assert baselines[1]["force_zero"] == pytest.approx([.3, -.1, -.8, .001, -.002, .003])
    assert len(baselines[1]["spectral_reference"]["source_frame_ids"]) == 1
    assert baselines[1]["spectral_reference"]["S0"][0] == baselines[0]["spectral_reference"]["S0"][0]+9
    data = load_dataset(tmp_path, [session])
    phases = np.asarray([row["phase"] for row in data["windows"]])
    preload = phases == "preload"
    assert preload.sum() >= 70  # Both real 2-second lead-ins become causal windows.
    assert data["wrench_mask"][preload].all()
    assert not data["position_mask"][preload].any()
    assert np.allclose(data["wrench"][preload], 0)
    for trial_id in ("T0001", "T0002"):
        contact = np.asarray([row["trial_id"].endswith(":"+trial_id) for row in data["windows"]]) & data["position_mask"]
        assert contact.any()
        assert np.allclose(data["wrench"][contact], [4.1, -.2, .1, .002, -.003, .004])
    selected, policy = training_selection(data)
    assert any(data["windows"][index]["phase"] == "preload" for index in selected)
    assert all(item["force_only_by_phase"]["preload"] > 0 for item in policy["trials"])
    assert len({row["group_id"] for row in data["windows"]}) == 1  # Shared next S0 cannot leak across splits.
    summary = next(row for row in list_sessions(tmp_path) if row["session_id"] == session)
    assert summary["accepted_trial_count"] == 2 and summary["has_training_data"]
    assert summary["formal_training_qualified"] is False
    assert all((manifest_path.parent/name).read_bytes() == content for name, content in before.items())


def test_quality_v1_and_v2_sessions_can_be_loaded_together_without_rewriting_history(tmp_path, clock):
    legacy = target_protocol_archive(tmp_path, "legacy_quality_v1")
    current, current_path = _saved_direct_session(tmp_path, clock)
    current_manifest = json.loads(current_path.read_text(encoding="utf-8"))
    legacy_manifest = json.loads((legacy/"manifest.json").read_text(encoding="utf-8"))
    reference = current_manifest["final"]["baseline_history"][0]["spectral_reference"]
    # Give both generated versions the same physical device definition. The
    # archived v1 quality/reference semantics remain unchanged.
    for key in ("finger_id", "point_grid", "coordinate_definition", "conditions"):
        legacy_manifest["settings"][key] = deepcopy(current_manifest["settings"][key])
    for baseline in legacy_manifest["final"]["baseline_history"]:
        baseline["wavelength_nm"] = list(reference["wavelength_nm"])
    (legacy/"manifest.json").write_text(json.dumps(legacy_manifest), encoding="utf-8")
    rows = [json.loads(line) for line in (legacy/"raw_spectra.jsonl").read_text().splitlines()]
    for row in rows:
        row.update(wavelength_nm=list(reference["wavelength_nm"]), preprocessing=deepcopy(reference["preprocessing"]))
    _rewrite(legacy, "raw_spectra.jsonl", rows)
    original = (legacy/"manifest.json").read_bytes()
    data = load_dataset(tmp_path, [legacy.name, current])
    assert {row["session_id"] for row in data["acquisition_provenance"]["sources"]} == {legacy.name, current}
    assert (legacy/"manifest.json").read_bytes() == original
    legacy_summary = next(row for row in list_sessions(tmp_path) if row["session_id"] == legacy.name)
    assert legacy_summary["accepted_trial_count"] == 4 and not legacy_summary["formal_training_qualified"]


@pytest.mark.parametrize("mutation,message", [
    (lambda manifest: manifest["final"]["trials"][0]["quality"].update(criteria_version="force_target_wrench_quality_v3"), "质量规则版本"),
    (lambda manifest: manifest["final"]["trials"][0]["quality"].update(criteria_version="force_target_wrench_quality_v1"), "质量规则版本"),
    (lambda manifest: manifest["settings"]["force_target_wrench_criteria"].update(automatic_unloaded_recovery_required=True), "规则与版本"),
    (lambda manifest: manifest["settings"]["force_target_wrench_criteria"].update(preload_duration_s=1), "规则与版本"),
    (lambda manifest: manifest["final"]["trials"][0]["quality"]["metrics"]["unloaded_evidence"].update(schema="timed_unloaded_evidence_v1"), "人工卸载声明"),
    (lambda manifest: manifest["final"]["trials"][0]["quality"]["metrics"]["unloaded_evidence"].update(operator_confirmed=False), "人工卸载声明"),
    (lambda manifest: manifest["final"]["trials"][0]["quality"]["metrics"]["unloaded_evidence"].update(automatic_unloaded_recovery_verified=True), "人工卸载声明"),
    (lambda manifest: manifest["final"]["trials"][0]["quality"]["metrics"].update(physical_accuracy_qualified=True), "精度验收"),
    (lambda manifest: manifest["final"]["trials"][0]["quality"]["metrics"].update(preload_recorded_duration_s=1.99), "人工卸载声明"),
    (lambda manifest: manifest["final"]["trials"][0]["quality"]["metrics"]["unloaded_evidence"].update(force_frame_ids=["invented-source"]), "真实源帧"),
    (lambda manifest: manifest["final"]["trials"][0]["quality"]["metrics"]["unloaded_evidence"].update(spectrum_source_stamps_ns=[1]), "结束边界"),
])
def test_v2_unknown_rules_false_recovery_claims_and_invented_zero_sources_are_rejected(tmp_path, clock, mutation, message):
    session, path = _saved_direct_session(tmp_path, clock)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutation(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_dataset(tmp_path, [session])


def test_unknown_quality_version_is_not_listed_as_training_data(tmp_path, clock):
    session, path = _saved_direct_session(tmp_path, clock)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["final"]["trials"][0]["quality"]["criteria_version"] = "force_target_wrench_quality_v3"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    summary = next(row for row in list_sessions(tmp_path) if row["session_id"] == session)
    assert summary["accepted_trial_count"] == 0 and not summary["has_training_data"]


@pytest.mark.parametrize("mutation,message", [
    (lambda baseline: baseline["force_zero"].__setitem__(0, 999), "六轴零偏不等于"),
    (lambda baseline: baseline.update(force_frame_ids=["invented-source"]), "基线与上一轮"),
    (lambda baseline: baseline["unloaded_evidence"].update(zero_sampling_monotonic_ns=0), "基线与上一轮"),
    (lambda baseline: baseline.update(force_epoch="different-force-epoch"), "真实源帧"),
    (lambda baseline: baseline.update(automatic_unloaded_recovery_verified=True), "基线与上一轮"),
    (lambda baseline: baseline.pop("unloaded_evidence"), "基线缺少人工卸载声明"),
    (lambda baseline: baseline["unloaded_evidence"].pop("schema"), "基线缺少人工卸载声明"),
    (lambda baseline: baseline.update(quality_version="force_target_wrench_quality_v1"), "基线与上一轮"),
])
def test_applied_next_zero_must_equal_its_own_real_single_force_source(tmp_path, clock, mutation, message):
    session, path = _saved_direct_session(tmp_path, clock, rounds=2)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutation(manifest["final"]["baseline_history"][1])
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_dataset(tmp_path, [session])
