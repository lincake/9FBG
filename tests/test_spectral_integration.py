"""Actual recorder -> immutable loader -> CPU worker -> live runtime integration.

All frames are synthetic and explicitly remain research test fixtures. These
checks exercise source schemas and do not qualify physical accuracy/timing.
"""
from copy import deepcopy
import json
import time

import numpy as np
import pytest

from ninefbg.analysis import AnalysisController, load_rows
from ninefbg.spectral_data import load_dataset
from ninefbg.spectral_training import SpectralTrainingManager
from ninefbg.spectral_runtime import SpectralRuntime
from ninefbg.service import AcquisitionService
from test_experiment import clock, context, result, spectrum
from test_timed_wrench import enter_main, start, tick
from test_experiment_api import idle_server, post


def recorded_session(userdata, clock):
    manager = start(userdata, clock, sample_rate_hz=25)
    number = enter_main(manager, clock)
    for _ in range(75):
        tick(manager, clock, number, [.4, -.3, -.2, .001, -.002, .003], intensity_delta=1)
        number += 1
    for _ in range(25):
        tick(manager, clock, number)
        number += 1
    assert manager.trial_id is None
    assert manager.snapshot()["guided"]["last_quality"]["accepted"]
    # Even permanently failed peak extraction has an auditable results stream.
    invalid = result(clock.now, number)
    invalid.update(peaks=[], valid=False)
    manager.on_result(invalid, {"valid": False, "reason": "synthetic fixture: nine-peak fit intentionally unavailable"})
    reference = deepcopy(manager.baseline["spectral_reference"])
    manager.stop()
    return manager.session_id, reference


def wait_for(predicate, description, timeout=30):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError("等待超时：" + description)


def test_real_timed_recorder_schema_cpu_candidate_load_and_pure_optical_runtime(tmp_path, clock):
    pytest.importorskip("torch")
    session_a, _ = recorded_session(tmp_path, clock)
    session_b, reference = recorded_session(tmp_path, clock)
    for session_id in (session_a, session_b):
        manifest = json.loads((tmp_path/"calibration_sessions"/session_id/"manifest.json").read_text(encoding="utf-8"))
        assert manifest["complete"]
        assert set(manifest["files"]) == {"raw_spectra.jsonl", "raw_force.jsonl", "events.jsonl", "results.jsonl"}
        assert manifest["final"]["baseline_history"][0]["lambda0_nm"] is None
    data = load_dataset(tmp_path, [session_a, session_b])
    assert data["x"].shape[1:] == (16, 512)
    assert np.any(data["wrench"][:, 0] > .3) and np.any(data["wrench"][:, 1] < -.2)
    assert data["wrench_mask"].mean() > .98
    assert set(data["split_labels"]) == {"train", "validation"}
    with pytest.raises(ValueError, match="spectral_gru"):
        load_rows(tmp_path, [session_a])
    controller = AnalysisController(tmp_path)
    trainer = SpectralTrainingManager(tmp_path)
    try:
        trainer.start([session_a, session_b], {"epochs": 1, "batch_size": 32})
        trained = wait_for(lambda: trainer.status() if not trainer.status()["busy"] else None, "CPU 训练完成")
        assert trained["status"] == "completed", trained
        assert trained["progress_percent"] == 100
        assert controller.model_id is None  # Publishing a candidate never applies it.
        loaded = controller.load(trained["model_id"])
        assert loaded["model_kind"] == "spectral_gru"
        assert loaded["formal_ready"] is False
        assert trained["report"]["independent_validation"] is True
        controller.spectral_runtime.set_reference(reference)
        for number in range(16):
            timestamp = clock.advance(40)
            controller.on_spectrum(spectrum(timestamp, 9000+number))
        inference = wait_for(lambda: controller.spectral_runtime.snapshot() if controller.spectral_runtime.snapshot()["metrics"]["completed"] else None,
                             "纯光谱推理完成", timeout=5)
        prediction = inference["prediction"]
        assert prediction["valid"]
        assert prediction["window_frame_ids"] == list(range(9000, 9016))
        assert len(prediction["wrench_pred"]) == 6
        assert prediction["wrench_truth"] is None and not prediction["comparison_ready"]
        frozen = deepcopy(inference["metrics"])
        for _ in range(20):
            controller.snapshot()
        assert controller.spectral_runtime.snapshot()["metrics"] == frozen
        controller.on_spectrum({**spectrum(clock.advance(40), 9016), "stream_epoch": "reconnected"})
        assert controller.spectral_runtime.snapshot()["reference"] is None
        assert not controller.spectral_runtime.snapshot()["prediction"]["valid"]
    finally:
        trainer.close()
        controller.close()


def test_new_training_api_preserves_local_origin_and_idle_state(tmp_path):
    import urllib.error
    userdata = tmp_path/"no-measurement-data"
    with idle_server(userdata) as (service, url):
        for route in ("/api/training/pause", "/api/training/resume", "/api/training/cancel"):
            with pytest.raises(urllib.error.HTTPError) as error:
                post(url, route, {})
            assert error.value.code == 400
            with pytest.raises(urllib.error.HTTPError) as error:
                post(url, route, {}, origin="https://external.invalid")
            assert error.value.code == 403
        assert not service.running and not service.sources
        assert not userdata.exists()


@pytest.mark.parametrize("terminal_status", ["cancelled", "completed"])
def test_service_pause_acknowledgement_clears_only_its_acquisition_reason(tmp_path, clock, terminal_status):
    from pathlib import Path
    import threading

    class PausedTraining:
        """A paused worker ACK, retaining independent user ownership."""
        def __init__(self):
            self.lock = threading.RLock()
            self.reasons = {"user"}
            self.worker_status = "paused"

        def status(self):
            with self.lock:
                return {"method": "spectral_gru", "busy": self.worker_status == "paused", "status": self.worker_status,
                        "pause_reasons": sorted(self.reasons)}

        def pause(self, reason):
            with self.lock:
                self.reasons.add(reason)

        def resume(self, reason):
            with self.lock:
                self.reasons.discard(reason)

        def close(self):
            pass

    manager = start(tmp_path, clock, sample_rate_hz=25)
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    trainer = PausedTraining()
    service.experiment = manager
    service.analysis.spectral_training = trainer
    service.analysis.training = trainer.status()
    service.analysis.busy = True
    try:
        manager.trial("start", context(clock.now))
        assert manager.phase == "waiting_training"
        for _ in range(10):
            assert manager.snapshot()["phase"] == "waiting_training"
        service._ensure_spectral_coordinator()
        wait_for(lambda: manager.phase == "preload", "采集暂停确认")
        assert trainer.status()["pause_reasons"] == ["acquisition", "user"]
        with pytest.raises(ValueError, match="模型训练"):
            service.history_change(["not-a-real-source"])
        manager.trial("abort", context(clock.now))
        wait_for(lambda: trainer.status()["pause_reasons"] == ["user"], "仅解除采集保护暂停")
        assert service.analysis.busy
        assert trainer.status()["status"] == "paused"
        manager.baseline_request(True, context(clock.now))
        for number in range(12):
            tick(manager, clock, 5000+number)
        assert manager.baseline["ready"]
        trainer.worker_status = terminal_status
        manager.trial("start", context(clock.now))
        wait_for(lambda: manager.phase == "preload", "训练已取消/完成后采集继续")
        assert service.analysis.busy is False
        assert trainer.status()["status"] == terminal_status
        manager.trial("abort", context(clock.now))
    finally:
        service._spectral_coord_stop.set()
        if service._spectral_coordinator:
            service._spectral_coordinator.join(2)
        manager.stop()
        service.analysis.close()


def test_raw_receive_keeps_recording_while_inference_worker_is_blocked(tmp_path):
    from pathlib import Path
    import threading
    from test_acquisition_scheduling import MemoryWriter
    from test_spectral_runtime import setup_runtime

    entered, release, received = threading.Event(), threading.Event(), threading.Event()
    def blocked_predictor(*args):
        entered.set()
        assert release.wait(3)
        return [1.]+[0.]*24, [.2, 0., 0., 0., 0., 0.]

    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path/"untouched")
    runtime, frames, _ = setup_runtime(blocked_predictor)
    service.analysis.spectral_runtime = runtime
    service.analysis.bundle = {"model_kind": "spectral_gru"}
    service.mode, service.running = "hardware", True
    service.writer = MemoryWriter()
    service.metrics = {"received": 0, "duplicates": 0, "identical_payloads": 0, "max_queue_depth": 0}
    thread = None
    try:
        for frame in frames:
            service._receive(frame)
        assert entered.wait(1)
        def receive_more():
            for number in range(16, 24):
                frame = {**frames[-1], "frame_id": number,
                         "host_rx_monotonic_ns": frames[-1]["host_rx_monotonic_ns"]+(number-15)*40_000_000}
                service._receive(frame)
            received.set()
        thread = threading.Thread(target=receive_more)
        thread.start()
        assert received.wait(1), "原始光谱接收等待了推理网络计算"
        assert len(service.writer.rows["raw_spectra"]) == 24
        assert runtime.snapshot()["metrics"]["submitted"] == 24
        assert runtime.snapshot()["metrics"]["skipped_requests"] >= 7
        assert not (tmp_path/"untouched").exists()
    finally:
        release.set()
        if thread:
            thread.join(3)
        service.analysis.close()


def attach_synthetic_runtime(controller, reference):
    runtime = SpectralRuntime(predictor=lambda *args: ([1.]+[0.]*24, [.2, 0., 0., 0., 0., 0.]))
    metadata = {"wavelength_nm": reference["wavelength_nm"], "preprocessing": reference["preprocessing"],
                "trained_point_ids": ["P01"], "model_sha256": "a"*64,
                "model_id": "Ssynthetic", "window_rule": {"max_gap_ms": 120}}
    runtime.apply(metadata, object())
    runtime.set_reference(reference)
    controller.spectral_runtime = runtime
    controller.bundle = {**metadata, "model_kind": "spectral_gru"}
    controller.inference_active = True
    return runtime


@pytest.mark.parametrize("action", ["start", "abort", "finish"])
def test_timed_commands_clear_inference_window_preserve_S0_and_stop_revokes_it(tmp_path, clock, monkeypatch, action):
    from pathlib import Path
    manager = start(tmp_path, clock, sample_rate_hz=25)
    if action in {"abort", "finish"}:
        number = enter_main(manager, clock)
        if action == "finish":
            for _ in range(75):
                tick(manager, clock, number, [.4, 0, 0, 0, 0, 0])
                number += 1
            assert manager.phase == "recovery"
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    service.experiment = manager
    monkeypatch.setattr(service, "snapshot", lambda: context(clock.now))
    monkeypatch.setattr(service, "_ensure_spectral_coordinator", lambda: None)
    reference = deepcopy(manager.baseline["spectral_reference"])
    runtime = attach_synthetic_runtime(service.analysis, reference)
    try:
        for number in range(16):
            runtime.submit(spectrum(clock.advance(40), 8000+number))
        wait_for(lambda: runtime.snapshot()["prediction"]["valid"], "边界前推理完成", timeout=3)
        service.experiment_trial(action)
        boundary = runtime.snapshot()
        assert not boundary["prediction"]["valid"]
        assert not runtime.frames and runtime.completed_window is None
        assert boundary["reference"]["reference_id"] == reference["reference_id"]
        for number in range(15):
            runtime.submit(spectrum(clock.advance(40), 9000+number))
        assert not runtime.snapshot()["prediction"]["valid"]
        assert len(runtime.frames) == 15
        runtime.submit(spectrum(clock.advance(40), 9015))
        wait_for(lambda: runtime.snapshot()["prediction"]["valid"], "边界后独立窗口完成", timeout=3)
        assert runtime.snapshot()["prediction"]["window_frame_ids"] == list(range(9000, 9016))
        service.experiment_stop()
        assert runtime.snapshot()["reference"] is None
        assert not runtime.snapshot()["prediction"]["valid"]
    finally:
        manager.stop()
        service.analysis.close()


def test_experiment_S0_cannot_authorize_independent_manual_validation(tmp_path, clock):
    manager = start(tmp_path, clock, sample_rate_hz=25)
    controller = AnalysisController(tmp_path)
    reference = {**deepcopy(manager.baseline["spectral_reference"]), "trigger": "automatic_unloaded_recovery"}
    runtime = attach_synthetic_runtime(controller, reference)
    try:
        state = controller.snapshot(experiment={"running": False}, source_kind="hardware")
        assert state["spectral_reference"]["reference_id"] == reference["reference_id"]
        assert state["test_reference"]["ready"] is False
        assert state["prediction"]["comparison_ready"] is False
        # An explicit new independent operator confirmation can authorize a
        # newly collected S0 even without PX6D, but cannot claim truth checking.
        controller.request_truth_zero({**context(clock.now), "force_connected": False})
        for number in range(12):
            controller.on_spectrum(spectrum(clock.advance(40), 9500+number))
        independent = controller.snapshot(experiment={"running": False}, source_kind="hardware")
        assert independent["test_reference"]["ready"] is True
        assert independent["spectral_reference"]["trigger"] == "operator_unloaded_confirmation"
        assert not independent["truth_zero"]["ready"]
        assert independent["prediction"]["comparison_ready"] is False
    finally:
        controller.close()
        manager.stop()


def test_training_start_API_forwards_false_adaptation_and_gap_keeps_legacy_dispatch_and_rejects_unknown(tmp_path, monkeypatch):
    import urllib.error
    from ninefbg.spectral_training import checked_options
    calls = []
    with idle_server(tmp_path/"no-training-data") as (service, url):
        def fake_start(session_ids, method="linear", options=None):
            if method == "spectral_gru":
                checked_options(options)  # The real worker option validator.
            calls.append((session_ids, method, deepcopy(options)))
            return {"accepted": True, "method": method}
        monkeypatch.setattr(service, "training_start", fake_start)
        for adaptation in (False, True):
            payload = {"session_ids": ["ended-source"], "method": "spectral_gru", "epochs": 1,
                       "max_spectrum_gap_ms": 120, "adapt_legacy_unloaded": adaptation}
            with post(url, "/api/training/start", payload) as response:
                assert response.status == 200
                assert json.load(response)["accepted"]
            assert calls[-1] == (["ended-source"], "spectral_gru",
                                 {"epochs": 1, "max_spectrum_gap_ms": 120, "adapt_legacy_unloaded": adaptation})
        for requested, expected in ((None, "linear"), ("linear", "linear"), ("mlp", "mlp")):
            payload = {"session_ids": ["legacy-source"]}
            if requested is not None:
                payload["method"] = requested
            with post(url, "/api/training/start", payload) as response:
                assert json.load(response)["method"] == expected
            assert calls[-1] == (["legacy-source"], expected, {})
        accepted_count = len(calls)
        for field in ("interop_threads", "unknown_parameter"):
            with pytest.raises(urllib.error.HTTPError) as error:
                post(url, "/api/training/start", {"session_ids": ["ended-source"], "method": "spectral_gru", field: 1})
            assert error.value.code == 400
            assert "未知字段" in json.load(error.value)["error"]
            assert len(calls) == accepted_count
        assert not service.running and not service.sources
        assert not (tmp_path/"no-training-data").exists()
