"""Service coordination checks with synthetic sources, never hardware evidence."""
from copy import deepcopy
from pathlib import Path
import threading
import time

import pytest

from ninefbg.service import AcquisitionService
from test_experiment import clock, context
from test_force_target_spectral_integration import _start_recording
from test_timed_wrench import tick


def wait_for(predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("采集保护协调等待超时")


def test_checkpoint_ack_holds_training_pause_and_reference_until_actual_save(tmp_path, clock, monkeypatch):
    class PausedTraining:
        def __init__(self):
            self.lock = threading.RLock()
            self.reasons = {"user"}

        def status(self):
            with self.lock:
                return {"method": "spectral_gru", "busy": True, "status": "paused",
                        "pause_reasons": sorted(self.reasons)}

        def pause(self, reason):
            with self.lock:
                self.reasons.add(reason)

        def resume(self, reason):
            with self.lock:
                self.reasons.discard(reason)

        def close(self):
            pass

    manager = _start_recording(tmp_path, clock)
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    service.experiment = manager
    trainer = PausedTraining()
    service.analysis.spectral_training = trainer
    service.analysis.training = trainer.status()
    service.analysis.busy = True
    allow_ack = threading.Event()
    actual_ready = manager.writer.checkpoint_ready
    monkeypatch.setattr(manager.writer, "checkpoint_ready", lambda token: allow_ack.is_set() and actual_ready(token))
    invalidations = []
    monkeypatch.setattr(service.analysis.spectral_runtime, "invalidate", lambda reason, **kwargs: invalidations.append(reason))
    try:
        manager.trial("start", context(clock.now))
        service._ensure_spectral_coordinator()
        wait_for(lambda: manager.phase == "preload")
        for number in range(500, 551):
            tick(manager, clock, number)
        wait_for(lambda: manager.phase == "waiting_contact")
        assert trainer.status()["pause_reasons"] == ["acquisition", "user"]
        trial_id = manager.trial_id
        with pytest.raises(ValueError, match="未达到"):
            service.experiment_trial("finish")
        assert manager.phase == "waiting_contact" and manager.trial_id == trial_id
        assert manager.running and not manager._timed["trial"]["finish_requested"]
        assert not invalidations
        for number in range(30):
            tick(manager, clock, 1000 + number, [4.1, 0, 0, 0, 0, 0], intensity_delta=1)
        assert manager.phase == "release"
        manager.trial("finish", context(clock.now))
        original = deepcopy(manager.baseline)
        for number in range(30):
            if manager.phase == "saving":
                break
            tick(manager, clock, 2000 + number)
        assert manager.phase == "saving"
        token = manager._timed["trial"]["checkpoint_token"]
        wait_for(lambda: actual_ready(token))
        assert manager.trial_id and manager.baseline == original
        assert trainer.status()["pause_reasons"] == ["acquisition", "user"]
        assert not invalidations
        allow_ack.set()
        wait_for(lambda: manager.trial_id is None)
        wait_for(lambda: trainer.status()["pause_reasons"] == ["user"])
        assert manager.baseline["baseline_id"] != original["baseline_id"]
        assert invalidations == ["本轮已保存并建立下一轮参考，重新预热"]
        assert service.analysis.busy and trainer.status()["status"] == "paused"
    finally:
        service._spectral_coord_stop.set()
        if service._spectral_coordinator:
            service._spectral_coordinator.join(2)
        manager.stop()
        service.analysis.close()


def test_old_prediction_cannot_compare_against_new_round_zero(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    prediction = {"valid": True, "reference_id": "old-S0", "wrench_pred": [1] * 6, "point_id": "P01"}
    try:
        service._spectral_comparison({"prediction": prediction}, None,
            {"running": True, "baseline": {"spectral_reference": {"reference_id": "new-S0"}}})
        assert not prediction["valid"] and prediction["status"] == "预热"
        assert prediction["wrench_pred"] is None and prediction["point_id"] is None
        assert prediction["wrench_truth"] is None and not prediction["comparison_ready"]
    finally:
        service.analysis.close()


@pytest.mark.parametrize("protocol", ["timed_wrench_v1", "force_target_wrench_v1"])
def test_full_spectrum_protocol_requires_actual_25_hz_without_hot_device_change(tmp_path, protocol):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    try:
        with pytest.raises(ValueError, match="25 Hz"):
            service.experiment_start({"acquisition_protocol": protocol, "sample_rate_hz": 25})
        assert not service.sources and not service.running
        assert not service.experiment.running and not tmp_path.joinpath("calibration_sessions").exists()
    finally:
        service.analysis.close()
