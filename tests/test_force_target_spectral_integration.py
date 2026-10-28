"""Real recorder schemas with synthetic frames; not a physical acceptance test."""
from copy import deepcopy
import json
import time

import numpy as np
import pytest

from ninefbg.analysis import AnalysisController
from ninefbg.experiment import ExperimentManager
from ninefbg.spectral_data import load_dataset, training_selection
from ninefbg.spectral_model import load_bundle
from ninefbg.spectral_training import SpectralTrainingManager
from test_experiment import clock, context, force, result, settings, spectrum
from test_timed_wrench import tick


def _wait(predicate, description, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    pytest.fail("等待超时：" + description)


def _start_recording(userdata, clock):
    manager = ExperimentManager(userdata)
    active = context(clock.now)
    active["tracker"]["peaks"] = []
    active["tracker"]["display_stale"] = True
    manager.start(settings(acquisition_protocol="force_target_wrench_v1", force_direction_confirmed=False,
                           sample_rate_hz=25, target_force_norm_N=4, max_force_norm_N=5), active)
    manager.baseline_request(True, active)
    for number in range(12):
        timestamp = clock.advance(40)
        manager.on_force(force(timestamp, 100 + number))
        manager.on_spectrum(spectrum(timestamp, 100 + number))
    assert manager.baseline["ready"]
    manager.select_point("P01")
    return manager


def _record_variable_trial(manager, clock, number, *, wait_frames, contact_frames):
    manager.trial("start", context(clock.now))
    assert manager.phase == "waiting_training"
    assert manager.training_pause_acknowledged()
    assert manager.phase == "preload"
    for _ in range(51):
        tick(manager, clock, number)
        number += 1
    assert manager.phase == "waiting_contact"
    for _ in range(wait_frames):
        tick(manager, clock, number)
        number += 1
    assert manager.phase == "waiting_contact"
    for _ in range(contact_frames):
        tick(manager, clock, number, [4.1, -.2, .1, .002, -.003, .004], intensity_delta=1)
        number += 1
    assert manager.phase == "release" and manager.trial_id
    # The target is a prompt: even a >3-second contact remains recording.
    for _ in range(5):
        tick(manager, clock, number)
        number += 1
    manager.trial("finish", context(clock.now))
    assert manager.phase == "recovery"
    for _ in range(30):
        if manager.phase == "saving":
            break
        tick(manager, clock, number)
        number += 1
    assert manager.phase == "saving", manager.snapshot()
    before_ack = deepcopy(manager.baseline)
    # Saving-latency frames remain in raw evidence but cannot extend the trial.
    for _ in range(3):
        tick(manager, clock, number)
        number += 1
    assert manager.phase == "saving" and manager.baseline == before_ack
    token = manager._timed["trial"]["checkpoint_token"]
    _wait(lambda: manager.writer.checkpoint_ready(token), "原始数据真正 flush/fsync 检查点")
    assert manager.recording_checkpoint_acknowledged()
    assert manager.trial_id is None and manager.baseline["ready"]
    assert manager.snapshot()["guided"]["last_quality"]["accepted"]
    assert manager._trials[f"T{manager._trial_number:04d}"]["checkpoint_saved"]
    return number


def _finish_session(manager, clock):
    invalid = result(clock.now, 990000)
    invalid.update(peaks=[], valid=False)
    manager.on_result(invalid, {"valid": False, "reason": "synthetic: nine-peak fit unavailable"})
    manager.stop()
    manifest = json.loads((manager.writer.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"]
    return manager.session_id, manifest


def test_variable_target_recorder_to_fixed_split_balanced_cpu_candidate_and_load(tmp_path, clock):
    pytest.importorskip("torch")
    first = _start_recording(tmp_path, clock)
    try:
        number = _record_variable_trial(first, clock, 1000, wait_frames=400, contact_frames=20)
        # The next trial uses the previous trial's first post-finish source S0.
        _record_variable_trial(first, clock, number, wait_frames=30, contact_frames=120)
        session_a, manifest_a = _finish_session(first, clock)
    finally:
        first.stop()
    second = _start_recording(tmp_path, clock)
    try:
        _record_variable_trial(second, clock, 1000, wait_frames=50, contact_frames=35)
        session_b, _ = _finish_session(second, clock)
    finally:
        second.stop()
    duration = [(trial["main_end_monotonic_ns"] - trial["main_start_monotonic_ns"]) / 1e9
                for trial in manifest_a["final"]["trials"]]
    assert duration[0] < 3 < duration[1]
    assert manifest_a["final"]["trials"][0]["quality"]["metrics"]["contact_raw_spectra"] < 60
    data = load_dataset(tmp_path, [session_a, session_b])
    phases = np.asarray([row["phase"] for row in data["windows"]])
    assert "saving" not in phases
    assert not data["position_mask"][np.isin(phases, ["waiting_contact", "recovery"])].any()
    assert data["position_mask"][phases == "release"].any()
    trial_split = {row["trial_id"]: split for row, split in zip(data["windows"], data["split_labels"])}
    assert trial_split[session_a + ":T0001"] == trial_split[session_a + ":T0002"]
    assert trial_split[session_a + ":T0001"] != trial_split[session_b + ":T0001"]
    selected, policy = training_selection(data)
    assert len(selected) <= policy["source_train_windows"]
    assert all(trial["force_only_selected"] <= trial["contact_windows"] for trial in policy["trials"])
    controller = AnalysisController(tmp_path)
    trainer = SpectralTrainingManager(tmp_path)
    try:
        trainer.start([session_a, session_b], {"epochs": 1, "batch_size": 16})
        state = _wait(lambda: trainer.status() if not trainer.status()["busy"] else None, "变长轮 CPU 训练")
        assert state["status"] == "completed", state.get("error")
        assert controller.model_id is None and controller.bundle is None
        metadata, _ = load_bundle(tmp_path / "spectral_models" / state["model_id"])
        assert metadata["training_sampling"]["schema"] == "spectral_train_sampling_v1"
        assert metadata["evaluation"]["independent_validation"]
        assert metadata["evaluation"]["validation_distribution"] == "complete_natural_distribution"
        loaded = controller.load(state["model_id"])
        assert loaded["model_kind"] == "spectral_gru" and not loaded["formal_ready"]
    finally:
        trainer.close()
        controller.close()
