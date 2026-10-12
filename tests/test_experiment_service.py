"""Experiment/service integration against hardware-shaped in-memory fakes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import pytest

from ninefbg import devices
from ninefbg.analysis import load_rows
from ninefbg.reprocess import FEATURE_PROCESSING, reprocess_session
from ninefbg.service import AcquisitionService
from ninefbg.tracking import PeakTracker
from test_display_service import fake_hardware
from test_service import until


@pytest.fixture
def experiment_hardware(fake_hardware, monkeypatch):
    optical, forces, Force = fake_hardware
    original_optical_read = devices.BaySpecSource.read
    original_force_read = Force.read
    Force.values = [0, 0, -1, 0, 0, 0]

    def optical_read(source):
        if getattr(source, "fail", False):
            raise TimeoutError("测试光谱中断")
        return {**original_optical_read(source), "source_kind": "hardware"}

    def force_read(source):
        frame = original_force_read(source)
        if frame is None:
            return None
        return {**frame, "source_kind": "hardware", "force_values": deepcopy(Force.values),
                "axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"],
                "units": ["N", "N", "N", "N·m", "N·m", "N·m"]}

    monkeypatch.setattr(devices.BaySpecSource, "read", optical_read)
    monkeypatch.setattr(Force, "read", force_read)
    return optical, forces, Force


def initialized_service(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "not-created")
    assert not service.userdata.exists()
    service.connect(record=False, connect_force=True, integration_us=20, sample_rate_hz=100,
                    config={"stable_frames": 3})
    until(lambda: service.latest_force is not None and service.metrics["processed"] > 0)
    service.reference(True)
    until(lambda: service.snapshot()["tracker"]["reference_version"] == 1 and
          all(peak["valid"] for peak in service.snapshot()["tracker"]["peaks"]))
    assert isinstance(service.snapshot()["tracker"]["reference_nm"], list), "Use the real PeakTracker reference representation"
    assert not service.userdata.exists(), "Hardware-shaped display/ref operations must not create recording directories"
    return service


def settings():
    return {"finger_id": "actual-tracker-fixture", "batch": "A", "force_sign": -1,
            "force_direction_confirmed": True, "coordinates_confirmed": True,
            "conditions": {"test_fixture": True}}


def records(path, name):
    return [json.loads(line) for line in (path / name).read_text(encoding="utf-8").splitlines()]


def test_display_queue_skips_but_experiment_records_all_raw_and_real_reference(tmp_path, experiment_hardware, monkeypatch):
    _, _, Force = experiment_hardware
    actual_process = PeakTracker.process

    def slow_process(tracker, frame):
        time.sleep(.02)
        return actual_process(tracker, frame)

    monkeypatch.setattr(PeakTracker, "process", slow_process)
    service = initialized_service(tmp_path)
    accepted_spectra, accepted_forces = [], []
    experiment = service.experiment
    raw_spectrum_hook, raw_force_hook = experiment.on_spectrum, experiment.on_force

    def audit_spectrum(frame):
        with experiment.lock:
            if experiment.running:
                accepted_spectra.append(frame["frame_id"])
            return raw_spectrum_hook(frame)

    def audit_force(frame):
        with experiment.lock:
            if experiment.running:
                accepted_forces.append(frame["frame_id"])
            return raw_force_hook(frame)

    monkeypatch.setattr(experiment, "on_spectrum", audit_spectrum)
    monkeypatch.setattr(experiment, "on_force", audit_force)
    try:
        service.experiment_start(settings())
        assert service.recording is False and service.snapshot()["session"] is None
        assert not (service.userdata / "sessions").exists()
        service.experiment_baseline(True)
        until(lambda: service.snapshot()["experiment"]["baseline"]["ready"], seconds=8)
        baseline = service.snapshot()["experiment"]["baseline"]
        assert len(baseline["lambda0_nm"]) == 9 and len(baseline["force_zero"]) == 6
        assert baseline["reference_version"] == 2 and baseline["force_span_ms"] >= 400
        service.experiment_trial("start")
        assert service.tracker.status()["reference_frozen"] is True
        before_loading = service.experiment.counts["results"]
        service.experiment_trial("loading")
        Force.values = [0, 0, -2, 0, 0, 0]
        until(lambda: service.experiment.counts["results"] >= before_loading + 3)
        before_hold = service.experiment.counts["results"]
        service.experiment_trial("hold")
        until(lambda: service.experiment.counts["results"] >= before_hold + 3)
        service.experiment_trial("removed")
        assert service.tracker.status()["press_active"] is False
        service.experiment_trial("recovery")
        service.experiment_trial("finish")
        assert not service.snapshot()["experiment"]["baseline"]["ready"]
        service.experiment_stop()
        path = experiment.writer.path
        raw = records(path, "raw_spectra.jsonl")
        raw_force = records(path, "raw_force.jsonl")
        fitted = records(path, "results.jsonl")
        assert [row["frame_id"] for row in raw] == accepted_spectra
        assert [row["frame_id"] for row in raw_force] == accepted_forces
        assert all(len(row["wavelength_nm"]) == len(row["intensity"]) == 512 for row in raw)
        assert all(len(row["force_values"]) == 6 for row in raw_force)
        assert len(raw) > len(fitted) and service.metrics["display_skipped"] > 0
        assert service.frames.maxsize == 2, "Experiment recording must not repurpose the bounded display queue"
        manifest = experiment.get_session(experiment.session_id)
        assert manifest["complete"] and manifest["final"]["counts"]["trials_completed"] == 1
        assert manifest["acquisition_audit"]["tracker_config"]["stable_frames"] == 3
        assert manifest["final"]["baseline_history"][0]["reference_version"] == 2
        assert manifest["formal_training_qualified"] is False
        assert all(row["point_id"] is None for row in fitted if row["phase"] not in {"loading", "hold"})
        service.disconnect()
        monkeypatch.setattr(PeakTracker, "process", actual_process)
        immutable = {name: hashlib.sha256((path / name).read_bytes()).hexdigest() for name in manifest["files"]}
        replayed = reprocess_session(path, manifest)
        assert len(replayed) == len(raw) > len(fitted)
        assert [row["frame_id"] for row in replayed] == [row["frame_id"] for row in raw]
        contact = [row for row in replayed if row["valid"] and row["phase"] in {"loading", "hold"}]
        assert {row["phase"] for row in contact} == {"loading", "hold"}
        raw_by_id = {row["frame_id"]: row for row in raw}
        force_by_id = {row["frame_id"]: row for row in raw_force}
        for row in contact:
            source = raw_by_id[row["frame_id"]]
            assert row["baseline_id"] == source["baseline_id"] == baseline["baseline_id"]
            assert row["trial_id"] == source["trial_id"] == "T0001"
            assert row["selected_point_id"] == row["point_id"] == "P01"
            assert row["reference_version"] == baseline["reference_version"]
            assert len(row["x_pm"]) == 9
            assert row["force_N"] == pytest.approx(-(row["sync"]["force_values"][2] - baseline["force_zero"][2]))
            assert [peak["id"] for peak in row["result"]["peaks"]] == [f"FBG{i}" for i in range(1, 10)]
            assert row["feature_processing"] == FEATURE_PROCESSING
            assert all(force_by_id[key]["baseline_id"] == source["baseline_id"] and force_by_id[key]["trial_id"] == source["trial_id"] for key in row["sync"]["force_frame_ids"])
        assert any(row["force_N"] == pytest.approx(1) for row in contact)
        loaded, provenance = load_rows(service.userdata, [experiment.session_id])
        assert any(row["phase"] == "loading" for row in loaded)
        assert any(row["phase"] == "hold" for row in loaded)
        assert all(row["feature_processing"] == FEATURE_PROCESSING and row["valid"] for row in loaded)
        assert provenance["sources"][0]["offline_processed_rows"] == len(raw)
        assert immutable == {name: hashlib.sha256((path / name).read_bytes()).hexdigest() for name in manifest["files"]}
    finally:
        service.disconnect()


@pytest.mark.parametrize("disconnect", ["force", "all"])
def test_controlled_disconnect_ends_experiment_and_keeps_display_independent(tmp_path, experiment_hardware, disconnect):
    optical, _, _ = experiment_hardware
    service = initialized_service(tmp_path)
    try:
        service.experiment_start(settings())
        until(lambda: service.experiment.counts["raw_spectra"] >= 2)
        if disconnect == "force":
            service.disconnect_force()
            before = service.metrics["received"]
            until(lambda: service.metrics["received"] > before + 2)
            assert service.running and optical[0].connected
        else:
            service.disconnect()
            assert not service.running
        assert service.experiment.snapshot()["running"] is False
        assert service.experiment.writer.closed
        assert service.experiment.get_session(service.experiment.session_id)["complete"]
    finally:
        service.disconnect()


@pytest.mark.parametrize("failed", ["force", "optical"])
def test_reader_failure_ends_experimental_recording_without_hanging(tmp_path, experiment_hardware, failed):
    optical, _, Force = experiment_hardware
    service = initialized_service(tmp_path)
    try:
        service.experiment_start(settings())
        until(lambda: service.experiment.counts["raw_spectra"] >= 2)
        if failed == "force":
            Force.fail = True
        else:
            optical[0].fail = True
        until(lambda: service.experiment.snapshot()["running"] is False)
        assert service.experiment.writer.closed
        assert "设备异常" in service.experiment.get_session(service.experiment.session_id)["final"]["reason"]
        if failed == "force":
            assert not service.stop_event.is_set() and optical[0].connected
        else:
            assert service.stop_event.is_set()
    finally:
        service.disconnect()


def test_guided_real_tracker_auto_finish_release_new_reference_and_failed_retry(tmp_path, experiment_hardware, monkeypatch):
    _, _, Force = experiment_hardware
    actual_process = PeakTracker.process
    press_phase_errors = []

    def audit_process(tracker, frame):
        fitted = actual_process(tracker, frame)
        if "按压阶段未通过" in fitted.get("message", ""):
            press_phase_errors.append({"frame_id": frame.get("frame_id"), "phase": frame.get("phase"),
                                       "message": fitted["message"]})
        return fitted

    monkeypatch.setattr(PeakTracker, "process", audit_process)
    # Match the requested 100 Hz optical cadence instead of this fixture's
    # intentionally fast 4 ms transport used to stress the display queue.
    read = devices.BaySpecSource.read

    def requested_cadence(source):
        time.sleep(.006)
        return read(source)

    monkeypatch.setattr(devices.BaySpecSource, "read", requested_cadence)
    service = initialized_service(tmp_path)
    manager = service.experiment
    actions = []
    standard_trial = service.experiment_trial

    def trial(action):
        actions.append(action)
        return standard_trial(action)

    def guided_step():
        return manager.snapshot()["guided"]["step"]

    def bin_count(trial_id, index):
        with manager.lock:
            return manager._guided_rounds[trial_id]["force_bins"].get(index, 0)

    try:
        service.experiment_start({**settings(), "guided_acquisition": True, "repetitions_per_point": 3})
        assert manager.point_id is None and guided_step() == "select_point"
        with pytest.raises(ValueError, match="确认"):
            service.experiment_baseline(True)
        manager.select_point("P01")
        service.experiment_baseline(True)
        until(lambda: manager.snapshot()["baseline"]["ready"], seconds=8)
        first_baseline = manager.snapshot()["baseline"]
        assert first_baseline["reference_version"] == 2
        trial("start")
        first_trial = manager.trial_id
        assert service.tracker.status()["press_active"]
        until(lambda: guided_step() == "loading", seconds=5)
        for index, normal in enumerate((.5, 1, 1.5, 2, 2.5)):
            previous = bin_count(first_trial, index)
            Force.values = [0, 0, -1-normal, 0, 0, 0]
            until(lambda: bin_count(first_trial, index) >= previous + 8, seconds=5)
        Force.values = [0, 0, -4, 0, 0, 0]
        until(lambda: guided_step() == "unloading", seconds=5)
        assert manager.phase == "hold" and service.tracker.status()["press_active"]
        Force.values = [0, 0, -1, 0, 0, 0]
        until(lambda: manager.trial_id is None, seconds=5)
        until(lambda: not service.tracker.status()["press_active"], seconds=5)
        guided = manager.snapshot()["guided"]
        assert guided["step"] == "round_complete" and guided["point_accepted"] == 1
        assert guided["last_quality"]["accepted"], guided["last_quality"]
        assert guided["last_quality"]["metrics"]["valid_spectra"] >= 40
        assert all(count >= 8 for count in guided["last_quality"]["metrics"]["force_bin_counts"].values())
        assert service.capture_phase == "recovery"
        with pytest.raises(ValueError):
            trial("start")
        until(lambda: len(service.snapshot()["tracker"]["peaks"]) == 9 and all(peak["valid"] and not peak.get("stale") for peak in service.snapshot()["tracker"]["peaks"]), seconds=5)
        assert not press_phase_errors, press_phase_errors
        with pytest.raises(ValueError, match="新的明确无载"):
            trial("start")
        service.experiment_baseline(True)
        until(lambda: manager.snapshot()["baseline"]["ready"], seconds=8)
        second_baseline = manager.snapshot()["baseline"]
        assert second_baseline["baseline_id"] != first_baseline["baseline_id"]
        assert second_baseline["reference_version"] == 3
        assert not service.tracker.status()["press_active"]
        assert guided_step() == "ready"

        # A second real automatic round exceeds 3.3 N: preserve all evidence,
        # require unloading, keep accepted count unchanged and exclude its trial.
        trial("start")
        failed_trial = manager.trial_id
        until(lambda: guided_step() == "loading", seconds=5)
        Force.values = [0, 0, -4.4, 0, 0, 0]
        until(lambda: guided_step() == "unloading", seconds=5)
        Force.values = [0, 0, -1, 0, 0, 0]
        until(lambda: manager.trial_id is None, seconds=5)
        until(lambda: not service.tracker.status()["press_active"], seconds=5)
        assert not press_phase_errors, press_phase_errors
        guided = manager.snapshot()["guided"]
        assert guided["step"] == "retry" and guided["point_accepted"] == 1 and guided["point_attempts"] == 2
        assert not guided["last_quality"]["accepted"]
        assert any("3.3" in reason for reason in guided["last_quality"]["reasons"])
        assert set(actions) == {"start"}, "No manual loading/hold/removed/recovery/finish may drive guided acquisition"
        service.experiment_stop()
        service.disconnect()
        path, session_id = manager.writer.path, manager.session_id
        manifest = manager.get_session(session_id)
        trials = {row["trial_id"]: row for row in manifest["final"]["trials"]}
        assert manifest["complete"] and manifest["final"]["guided"]["total_accepted"] == 1
        assert trials[first_trial]["quality"]["accepted"] and not trials[first_trial]["bad"]
        assert not trials[failed_trial]["quality"]["accepted"] and trials[failed_trial]["bad"]
        assert trials[failed_trial]["status"] == "completed"
        assert any(row["trial_id"] == failed_trial for row in records(path, "raw_spectra.jsonl"))
        assert any(row["trial_id"] == failed_trial for row in records(path, "raw_force.jsonl"))
        immutable = {name: hashlib.sha256((path / name).read_bytes()).hexdigest() for name in manifest["files"]}
        loaded, provenance = load_rows(service.userdata, [session_id])
        assert any(row["source_trial_id"] == first_trial and row["phase"] in {"loading", "hold"} for row in loaded)
        assert all(row["source_trial_id"] != failed_trial for row in loaded)
        assert provenance["sources"][0]["offline_processed_rows"] == manifest["counts"]["raw_spectra"]
        assert immutable == {name: hashlib.sha256((path / name).read_bytes()).hexdigest() for name in manifest["files"]}
        assert not press_phase_errors, press_phase_errors
    finally:
        service.disconnect()
