"""Report/manager isolation using real controllers and synthetic source frames.

No instrument, training worker, saved model or user dataset is opened here.
"""
from copy import deepcopy
from pathlib import Path
import queue
import threading
import time

import pytest

import ninefbg.analysis as analysis_module
import ninefbg.spectral_training as training_module
from ninefbg.analysis import AnalysisController
from ninefbg.spectral_training import SpectralTrainingManager
from test_spectral_runtime import setup_runtime, wait_result


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(.005)
    raise AssertionError("模拟报告状态等待超时")


@pytest.fixture
def spectral_controller(tmp_path):
    controller = AnalysisController(tmp_path)
    controller.spectral_runtime.close()
    runtime, frames, reference = setup_runtime()
    requested = reference["effective_ns"] - 1
    reference = {**reference, "requested_ns": requested, "trigger": "operator_unloaded_confirmation"}
    runtime.set_reference({**reference, "reference_id": "independent-fixture"})
    for frame in frames:
        runtime.submit(frame)
    wait_result(runtime)
    controller.spectral_runtime = runtime
    controller.bundle = {"model_kind": "spectral_gru", "model_sha256": "a" * 64,
                         "trained_point_ids": ["P01"], "evaluation": {"synthetic_rows": list(range(20000))}}
    controller.inference_active = True
    controller.model_id = "Sfixture"
    controller.test_reference = {"requested_ns": requested, "epoch": "optical", "ready": True, "pending": False}
    try:
        yield controller
    finally:
        controller.close()


def pending_force_zero(controller):
    now = time.perf_counter_ns()
    values = [1.0, -2.0, 3.0, .01, -.02, .03]
    controller.truth_zero = {"ready": False, "pending": True, "epoch": "force-fixture",
                             "requested_ns": now - 500_000_000}
    controller._zero_samples = [(now - 440_000_000 + i * 40_000_000, values[:], i) for i in range(11)]
    controller._zero_seen = {("force-fixture", i) for i in range(11)}
    frame = {"frame_id": 11, "stream_epoch": "force-fixture", "host_rx_monotonic_ns": now,
             "source_kind": "hardware", "time_quality": "host_rx_approx", "force_values": values[:],
             "axes": ["Fx", "Fy", "Fz", "Mx", "My", "Mz"], "units": ["N", "N", "N", "N·m", "N·m", "N·m"]}
    return frame


def start_blocked_clean(controller, monkeypatch, force=None):
    entered, release = threading.Event(), threading.Event()
    results, failures = [], []
    original = analysis_module.clean

    def gated(value):
        if isinstance(value, dict) and value.get("model_kind") in {"spectral_gru", "full"}:
            if value["model_kind"] == "spectral_gru":
                assert value["prediction"]["valid"] and value["prediction"]["point_id"] == "P01"
                assert value["test_reference"]["ready"], "代际测试必须先截取有效旧推理与独立参考"
            entered.set()
            assert release.wait(4), "模拟报告复制必须由测试释放"
        return original(value)

    monkeypatch.setattr(analysis_module, "clean", gated)

    def snapshot():
        try:
            results.append(controller.snapshot(force=force, source_kind="hardware"))
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=snapshot)
    worker.start()
    assert entered.wait(2)
    return worker, release, results, failures


@pytest.mark.parametrize("model_kind", ["spectral_gru", "full"])
def test_slow_report_payload_copy_does_not_block_actual_six_axis_zero_collection(spectral_controller, monkeypatch, model_kind):
    controller = spectral_controller
    if model_kind == "full":
        # A completed full-spectrum training report also exists before applying
        # its model, while the nine-peak/no-inference branch serves the UI.
        controller.bundle = {"model_kind": "full", "model_sha256": "b" * 64}
        controller.inference_active = False
        controller.training = {"method": "spectral_gru", "report": {"synthetic_rows": list(range(20000))}}
        controller.job_progress = deepcopy(controller.training)
    frame = pending_force_zero(controller)
    worker, release, results, failures = start_blocked_clean(controller, monkeypatch, frame)
    done = threading.Event()

    def receive():
        controller.on_force(frame)
        done.set()

    reader = threading.Thread(target=receive)
    reader.start()
    blocked_at = time.monotonic()
    try:
        assert done.wait(.12), "报告复制持有六轴读取需要的 analysis.lock"
        assert controller.truth_zero["ready"] and not controller.truth_zero["pending"]
        assert controller.truth_zero["offsets"] == frame["force_values"]
        assert controller.truth_zero["source_frame_ids"][-1] == frame["frame_id"]
        assert controller.truth_zero["source_timestamps_ns"][-1] == frame["host_rx_monotonic_ns"]
        assert controller.truth_zero["noise_mad"] == [0] * 6
        threading.Event().wait(max(0, .35 - (time.monotonic() - blocked_at)))
        assert worker.is_alive() and not release.is_set(), "六轴成功处理时报告仍应阻塞至少 350 ms"
    finally:
        release.set()
        reader.join(2)
        worker.join(2)
    assert not failures and len(results) == 1
    assert not reader.is_alive() and not worker.is_alive()


@pytest.mark.parametrize("action", ["stop", "invalidate", "reference"])
def test_model_or_reference_changes_during_copy_cannot_republish_old_valid_prediction(spectral_controller, monkeypatch, action):
    controller = spectral_controller
    frame = pending_force_zero(controller)
    worker, release, results, failures = start_blocked_clean(controller, monkeypatch, frame)
    try:
        if action == "stop":
            controller.stop_inference()
        elif action == "invalidate":
            controller.spectral_runtime.invalidate("模拟真实源时段改变")
        else:
            old = controller.spectral_runtime.reference
            controller.spectral_runtime.set_reference({**old, "reference_id": "new-reference-fixture"})
    finally:
        release.set()
        worker.join(2)
    assert not failures and len(results) == 1 and not worker.is_alive()
    result = results[0]
    assert not result["prediction"]["valid"]
    assert result["prediction"]["point_id"] is None and not result["prediction"]["comparison_ready"]
    assert not result["test_reference"]["ready"]
    assert result["inference_active"] is (action != "stop")
    if action == "stop":
        assert result["prediction"]["status"] == "已结束"
    result["report"]["synthetic_rows"][0] = "client-edited"
    assert controller.bundle["evaluation"]["synthetic_rows"][0] == 0


def test_slow_changed_training_status_does_not_block_actual_on_force(spectral_controller, tmp_path, monkeypatch):
    controller = spectral_controller
    manager = SpectralTrainingManager(tmp_path)
    controller.spectral_training = manager
    controller.training = {"method": "spectral_gru"}
    frame = pending_force_zero(controller)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original = manager.status_if_changed

    def gated(revision):
        entered.set()
        assert release.wait(4)
        return original(revision)

    monkeypatch.setattr(manager, "status_if_changed", gated)
    refresh = threading.Thread(target=controller._refresh_spectral_training)
    refresh.start()
    reader = threading.Thread(target=lambda: (controller.on_force(frame), done.set()))
    try:
        assert entered.wait(2)
        reader.start()
        assert done.wait(.12), "训练状态复制持有六轴读取需要的 analysis.lock"
        assert controller.truth_zero["ready"] and controller.truth_zero["offsets"] == frame["force_values"]
        assert refresh.is_alive() and not release.is_set()
    finally:
        release.set()
        reader.join(2)
        refresh.join(2)
    assert not refresh.is_alive() and not reader.is_alive()


class FakeChannel(queue.Queue):
    def close(self):
        pass

    def cancel_join_thread(self):
        pass


class FakeProcess:
    pid = 12345
    exitcode = 0

    def __init__(self, **kwargs):
        self.alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        pass

    def close(self):
        assert not self.alive

    def terminate(self):
        self.alive = False


class FakeContext:
    Queue = FakeChannel
    Process = FakeProcess


def test_production_training_revision_cache_copies_completed_report_once_and_tracks_reap_and_next_job(spectral_controller, tmp_path, monkeypatch):
    controller = spectral_controller
    manager = SpectralTrainingManager(tmp_path)
    controller.spectral_training = manager
    controller.training = {"method": "spectral_gru"}
    monkeypatch.setattr("ninefbg.spectral_finetuning.prepare_training", lambda root, ids, options, **kwargs: (list(ids), {}))
    monkeypatch.setattr("ninefbg.spectral_finetuning.status_fields", lambda plan: {})
    monkeypatch.setattr(training_module.multiprocessing, "get_context", lambda *args: FakeContext())
    first = manager.start(["synthetic-session"], {"epochs": 1})
    process = manager.process
    report = {"rows": [{"i": i, "wrench": [float(i)] * 6} for i in range(20000)]}
    manager.events.put({"status": "completed", "progress_percent": 100, "model_id": "Ssynthetic-first", "report": report})
    wait_for(lambda: manager._state["status"] == "completed")
    copies, calls = [], []
    original_copy, original_status = training_module.deepcopy, manager.status

    def observed_copy(value, *args, **kwargs):
        if value is manager._state:
            copies.append(manager._status_revision)
        return original_copy(value, *args, **kwargs)

    def observed_status():
        calls.append(manager._status_revision)
        return original_status()

    monkeypatch.setattr(training_module, "deepcopy", observed_copy)
    monkeypatch.setattr(manager, "status", observed_status)
    try:
        for _ in range(60):
            controller._refresh_spectral_training()
        assert len(copies) == len(calls) == 1, "未变更的完整评估不应在每个采集协调周期复制"
        assert controller.busy and controller.training["status"] == "completed", "终态消息到达后仍须等待工作进程回收"
        snapshot = controller.snapshot(source_kind="hardware")
        snapshot["training"]["report"]["rows"][0]["wrench"][0] = -999
        snapshot["job_progress"]["report"]["rows"][1]["i"] = -999
        assert controller.training["report"]["rows"][0]["wrench"][0] == 0
        assert controller.job_progress["report"]["rows"][1]["i"] == 1
        assert manager._state["report"]["rows"][0]["wrench"][0] == 0
        assert len(copies) == len(calls) == 1
        process.alive = False
        wait_for(lambda: manager.process is None)
        controller._refresh_spectral_training()
        assert not controller.busy and not controller.training["busy"]
        assert len(copies) == len(calls) == 2, "回收工作进程必须发布新的 busy 状态"
        second = manager.start(["next-synthetic-session"], {"epochs": 1})
        assert second["job_id"] != first["job_id"]
        controller._refresh_spectral_training()
        assert controller.busy and controller.training["job_id"] == second["job_id"]
        assert controller.training["report"] is None and controller.training["model_id"] is None
        manager.pause("acquisition")
        controller._refresh_spectral_training()
        assert controller.training["status"] == "pause_requested"
        assert controller.training["pause_reasons"] == ["acquisition"]
        manager.resume("acquisition")
        controller._refresh_spectral_training()
        assert controller.training["status"] == "running" and not controller.training["pause_reasons"]
        manager.cancel()
        controller._refresh_spectral_training()
        assert controller.training["status"] == "cancelling"
        manager.process.alive = False
        wait_for(lambda: manager.process is None)
        controller._refresh_spectral_training()
        assert controller.training["status"] == "cancelled" and not controller.busy
        third = manager.start(["publication-synthetic-session"], {"epochs": 1})
        controller._refresh_spectral_training()
        manager.events.put({"status": "completed", "progress_percent": 100,
                            "model_id": "Ssynthetic-published-next", "report": {"generation": 3}})
        wait_for(lambda: manager._state.get("model_id") == "Ssynthetic-published-next")
        controller._refresh_spectral_training()
        assert controller.training["job_id"] == third["job_id"]
        assert controller.training["model_id"] == "Ssynthetic-published-next"
        assert controller.training["report"] == {"generation": 3}
        assert controller.job_progress["report"] == {"generation": 3}
    finally:
        if manager.process is not None:
            manager.process.alive = False
            wait_for(lambda: manager.process is None)
        manager.close()
