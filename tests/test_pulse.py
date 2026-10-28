"""Synthetic contracts only; these tests make no pulse accuracy claim."""
from copy import deepcopy
from pathlib import Path
import queue
import threading
import time

import pytest

from ninefbg.pulse import FBG_IDS, PulseBuffer
from ninefbg.service import AcquisitionService, DisplayWriter
import ninefbg.pulse as pulse_module


def source_result(frame_id=1, stamp=None):
    frame = {"source_kind": "hardware", "frame_id": frame_id, "stream_epoch": "pulse-test",
             "host_rx_monotonic_ns": stamp or time.perf_counter_ns(), "time_quality": "host_rx_approx",
             "valid": True, "stale": False}
    reference = [1500 + index for index in range(9)]
    result = {**frame, "state": "tracking", "reference_pending": False,
              "algorithm_version": "original-fixed-identity", "config_version": "fixed-config",
              "reference_version": 1, "reference_nm": reference,
              "peaks": [{"id": fbg, "center_nm": reference[index] + .001 * (index + 1),
                         "value_frame_id": frame_id, "valid": True, "stale": False}
                        for index, fbg in enumerate(FBG_IDS)]}
    return frame, result


def test_values_derive_from_same_frame_centers_and_fixed_reference_with_explicit_ids():
    buffer = PulseBuffer()
    frame, result = source_result()
    result["peaks"].reverse()
    buffer.append(frame, result)
    sample = buffer.read()["samples"][0]
    assert sample["fbg_ids"] == FBG_IDS
    assert sample["values_pm"] == pytest.approx(range(1, 10), abs=1e-8)
    assert sample["valid"] and all(sample["valid_channels"])
    assert sample["t_ns"] == frame["host_rx_monotonic_ns"]
    assert "intensity" not in sample and "peaks" not in sample
    result["reference_nm"][0] = 0
    sample["values_pm"][0] = -99
    assert buffer.read()["samples"][0]["values_pm"][0] == pytest.approx(1, abs=1e-8)


@pytest.mark.parametrize("change", [
    {"valid": False}, {"stale": True}, {"value_frame_id": 0},
    {"value_frame_id": True}, {"center_nm": float("nan")}, {"center_nm": float("inf")},
])
def test_lost_or_old_peak_is_null_without_reusing_last_value(change):
    buffer = PulseBuffer()
    frame, result = source_result()
    buffer.append(frame, result)
    frame, result = source_result(2)
    result["peaks"][3].update(change)
    buffer.append(frame, result)
    sample = buffer.read(1)["samples"][0]
    assert not sample["valid"] and not sample["valid_channels"][3]
    assert sample["values_pm"][3] is None
    assert sum(sample["valid_channels"]) == 8
    assert sample["values_pm"][0] == pytest.approx(1, abs=1e-8)


@pytest.mark.parametrize("raw_change,result_change", [
    ({"source_kind": "replay"}, {}), ({"valid": False}, {}), ({"stale": True}, {}),
    ({"time_quality": "unknown"}, {}), ({"host_rx_monotonic_ns": None}, {}),
    ({}, {"frame_id": 2}), ({}, {"frame_id": True}), ({}, {"stream_epoch": "other"}),
    ({}, {"host_rx_monotonic_ns": 123}), ({}, {"reference_pending": True}),
    ({}, {"reference_version": 0}), ({}, {"reference_nm": []}), ({}, {"state": "input_invalid"}),
    ({}, {"valid": False}), ({}, {"stale": True}),
])
def test_untrusted_source_or_result_binding_never_produces_measurement(raw_change, result_change):
    buffer = PulseBuffer()
    frame, result = source_result()
    frame.update(raw_change)
    result.update(result_change)
    buffer.append(frame, result)
    sample = buffer.read()["samples"][0]
    assert sample["values_pm"] == [None] * 9
    assert not sample["valid"] and sample["reason"]


def test_duplicate_or_missing_fixed_identity_invalidates_all_channels():
    buffer = PulseBuffer()
    frame, result = source_result()
    result["peaks"][0]["id"] = "FBG2"
    buffer.append(frame, result)
    assert buffer.read()["samples"][0]["values_pm"] == [None] * 9


def test_future_source_reception_time_is_not_a_current_measurement():
    buffer = PulseBuffer()
    buffer.append(*source_result(stamp=time.perf_counter_ns() + 2_000_000_000))
    sample = buffer.read()["samples"][0]
    assert not sample["valid"] and sample["values_pm"] == [None] * 9


def test_slow_history_copy_does_not_block_processing_append(monkeypatch):
    buffer = PulseBuffer()
    buffer.append(*source_result())
    entered, release, appended = threading.Event(), threading.Event(), threading.Event()
    original = pulse_module.deepcopy
    def slow_copy(value):
        if isinstance(value, list) and value and isinstance(value[0], dict) and "seq" in value[0]:
            entered.set()
            assert release.wait(3)
        return original(value)
    monkeypatch.setattr(pulse_module, "deepcopy", slow_copy)
    reader = threading.Thread(target=buffer.read)
    reader.start()
    assert entered.wait(1)
    def append():
        buffer.append(*source_result(2))
        appended.set()
    writer = threading.Thread(target=append)
    writer.start()
    try:
        assert appended.wait(1), "慢只读历史复制不得持有追加锁"
    finally:
        release.set()
        reader.join(2)
        writer.join(2)
    assert buffer.sequence == 2


def test_bounded_overflow_pagination_and_new_service_cursor_reset_are_explicit():
    buffer = PulseBuffer(capacity=3)
    for index in range(1, 7):
        buffer.append(*source_result(index))
    page = buffer.read(after=1, limit=2)
    assert page["oldest_seq"] == 4 and page["truncated"]
    assert [sample["seq"] for sample in page["samples"]] == [4, 5]
    assert page["cursor"] == 5
    assert buffer.read(after=0)["truncated"] is False
    assert buffer.read(5)["samples"][0]["seq"] == 6
    assert buffer.read(6)["samples"] == []
    reset = PulseBuffer().read(6)
    assert reset["reset_required"] and reset["truncated"] and reset["cursor"] == 0


@pytest.mark.parametrize("after,limit", [(-1, 1), (True, 1), (1.0, 1), ("0", 1), (2**53, 1), (0, 0), (0, 4097), (0, True)])
def test_invalid_cursor_and_limits_are_rejected(after, limit):
    with pytest.raises(ValueError):
        PulseBuffer().read(after, limit)


def make_service(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    frame, result = source_result()
    service.running, service.mode = True, "hardware"
    service.latest = {"frame": deepcopy(frame), "tracker": result}
    service.tracker._anchors = list(result["reference_nm"])
    service.tracker._reference = list(result["reference_nm"])
    service.tracker._reference_version = result["reference_version"]
    result["algorithm_version"] = service.tracker.algorithm_version
    result["config_version"] = service.tracker.config_version
    service.pulse_buffer.append(frame, result)
    return service


def test_read_only_feed_does_not_call_ui_analysis_or_tracker_status_or_open_hardware(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("只读脉搏接口不得调用重型快照、记录或硬件连接")
    for target, name in [(service, "snapshot"), (service.analysis, "snapshot"),
                         (service.tracker, "status"), (service, "connect"), (service, "reference")]:
        monkeypatch.setattr(target, name, forbidden)
    response = service.pulse_samples()
    assert response["schema"] == "nine_fbg_pulse_samples_v1"
    assert response["blocked"] is False and response["age_ms"] < 1000
    assert len(response["samples"]) == 1
    assert service.sources == [] and service.writer is None


@pytest.mark.parametrize("condition", ["stopped", "stopping", "recording", "experiment", "inference", "pending", "stale", "binding"])
def test_current_source_and_protected_workflows_explicitly_block_measurement(tmp_path, condition):
    service = make_service(tmp_path)
    if condition == "stopped":
        service.running = False
    elif condition == "stopping":
        service.stopping = True
    elif condition == "recording":
        service.recording = True
    elif condition == "experiment":
        service.experiment.running = True
    elif condition == "inference":
        service.analysis.inference_active = True
    elif condition == "pending":
        service.tracker._pending_reference = True
    elif condition == "stale":
        with service.pulse_buffer.lock:
            service.pulse_buffer.samples[-1]["t_ns"] -= 2_000_000_000
    elif condition == "binding":
        service.tracker._reference_version += 1
    result = service.pulse_samples()
    assert result["blocked"] and result["reason"]


def test_processor_appends_existing_result_without_starting_new_reader(tmp_path, monkeypatch):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    frame, result = source_result()
    service.mode = "hardware"
    service.frames = queue.Queue()
    service.frames.put((frame, frame["host_rx_monotonic_ns"]))
    service.process_done.set()
    service.metrics = {"processed": 0}
    service.writer = DisplayWriter()
    monkeypatch.setattr(service.tracker, "process", lambda value: deepcopy(result))
    service._process_frames(None)
    page = service.pulse_buffer.read()
    assert page["cursor"] == 1 and page["samples"][0]["frame_id"] == frame["frame_id"]
    assert service.metrics["processed"] == 1 and service.sources == [] and service.workers == []


def initial_pulse_service(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    service.running, service.mode = True, "hardware"
    return service


def test_pulse_initialization_is_available_before_peaks_and_needs_no_px6d(tmp_path, monkeypatch):
    service = initial_pulse_service(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("脉搏初始化不得连接设备")
    monkeypatch.setattr(service, "connect", forbidden)
    monkeypatch.setattr(service, "connect_force", forbidden)
    state = service.pulse_samples()
    assert state["can_initialize"] and state["blocked"] and state["samples"] == []
    assert not service.tracker._anchors and service.latest_force is None
    before = time.perf_counter_ns()
    result = service.pulse_reference(True)
    assert not result["can_initialize"] and service.tracker._pending_reference
    assert service.reference_min_rx_ns >= before and service.pending_confirmation
    assert service.capture_phase == "unloaded"
    assert service.tracker._reference_version == 0  # stable real frames must still establish it
    assert service.sources == [] and service.writer is None and service.workers == []


@pytest.mark.parametrize("confirmed", [False, None, 0, 1, "true"])
def test_pulse_reference_requires_literal_explicit_unloaded_confirmation(tmp_path, monkeypatch, confirmed):
    service = initial_pulse_service(tmp_path)
    calls = []
    monkeypatch.setattr(service, "reference", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="确认"):
        service.pulse_reference(confirmed)
    assert calls == [] and not service.tracker._pending_reference


@pytest.mark.parametrize("condition", ["recording", "experiment", "inference", "busy", "stopping", "not_running",
                                        "stop_event", "replay", "press", "pending", "initialized"])
def test_pulse_reference_rejects_protected_conditions_before_existing_reference(tmp_path, monkeypatch, condition):
    service = initial_pulse_service(tmp_path)
    if condition in {"recording", "stopping"}:
        setattr(service, condition, True)
    elif condition == "experiment":
        service.experiment.running = True
    elif condition in {"inference", "busy"}:
        setattr(service.analysis, "inference_active" if condition == "inference" else "busy", True)
    elif condition == "not_running":
        service.running = False
    elif condition == "stop_event":
        service.stop_event.set()
    elif condition == "replay":
        service.mode = "replay"
    elif condition == "press":
        service.tracker._press = True
    elif condition == "pending":
        service.tracker._pending_reference = True
    elif condition == "initialized":
        service.tracker._anchors = [1500 + index for index in range(9)]
    calls = []
    monkeypatch.setattr(service, "reference", lambda *args: calls.append(args))
    assert not service.pulse_samples()["can_initialize"]
    with pytest.raises(ValueError):
        service.pulse_reference(True)
    assert calls == [] and service.tracker._reference_version == 0


def test_pulse_reference_reuses_existing_request_with_command_lock(tmp_path, monkeypatch):
    service = initial_pulse_service(tmp_path)
    original = service.reference
    called = []
    def existing(confirmed):
        assert service.command_lock._is_owned()
        called.append(confirmed)
        return original(confirmed)
    monkeypatch.setattr(service, "reference", existing)
    service.pulse_reference(True)
    assert called == [True]
