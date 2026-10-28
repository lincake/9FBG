import json
from pathlib import Path
import struct
import time
import os
import subprocess
import sys

import numpy as np
import pytest

from ninefbg.devices import BaySpecSource, DeviceLease, PX6DSource, audit_wavelength_axis, command_packet, crc8, load_device_config, parse_force_frame, _helper_kill_on_close_job, _close_job


def test_actual_factory_axis_integrity_and_nonuniformity():
    source = BaySpecSource(load_device_config()["bayspec"])
    source._load_axis()
    assert len(source._axis) == 512
    assert source._axis_audit["uniform"] is False
    assert source._axis_audit["axis_sha256"] == "afc4cfe6cc3f35f3d968e91478f16e0d71c3587af0ead3e8767a2d58db94ef82"
    assert source._axis_audit["physical_device_identity_verified"] is False


def test_axis_rejects_bad_lengths_nan_and_unsorted():
    for axis in ([1, 2], [float("nan")] * 512, [1] * 512, list(range(512))[::-1]):
        with pytest.raises(ValueError):
            audit_wavelength_axis(axis)


def test_sdk_frame_preserves_raw_points_and_honest_time_identity():
    source = BaySpecSource(load_device_config()["bayspec"])
    source._load_axis()
    source.stream_epoch = "test-epoch"
    source._consume_message({"type": "status", "device_count": 1, "device_index": 0, "spectrum_points": 512, "is_open_ok": 1, "vid": "0xBA8C", "pid": "0x1688"})
    values = list(range(512))
    for i in range(2):
        source._consume_message({"type": "spectrum", "counts": values, "frame_index": i, "snapshot_result": 1, "timestamp_ms": 100, "monotonic_ms": 5}, 1000000000+i*10000000, 999000000+i*10000000)
    first, second = source.read(), source.read()
    assert first["intensity"] == values
    assert first["wavelength_nm"] == source._axis
    assert first["device_sequence"] is first["device_timestamp_raw"] is None
    assert first["frame_id_source"] == "local_receive_sequence"
    assert first["local_receive_id"] == first["frame_id"]
    assert first["time_quality"] == "host_rx_approx"
    assert first["preprocessing"]["smoothing_applied"] is False
    assert first["preprocessing"]["saturation_level"] == 65535
    assert first["frame_id"] != second["frame_id"]
    diag = source.diagnostics()
    assert diag["identical_value_count"] == 1
    assert diag["duplicate_hardware_frames"] is None
    assert diag["actual_new_sensor_frame_rate_hz"] is None
    assert diag["receive_rate_hz"] == pytest.approx(100)
    with pytest.raises(ValueError):
        source._consume_message({"type": "spectrum", "counts": values, "frame_index": 1, "snapshot_result": 1})


def test_sdk_queue_overflow_stops_without_overwriting_accepted_raw():
    source = BaySpecSource({**load_device_config()["bayspec"], "queue_limit": 2})
    source._load_axis()
    source._header = {"device_count": 1}
    for i in range(3):
        source._consume_message({"type": "spectrum", "counts": [i]*512, "frame_index": i, "snapshot_result": 1})
    assert source.diagnostics()["queue_drops"] == 0
    assert source.diagnostics()["overflow_stops"] == 1
    assert source.diagnostics()["capture_incomplete"]
    assert [source.read()["frame_id"] for _ in range(3)] == [1, 2, 3]
    with pytest.raises(RuntimeError, match="队列已满"):
        source.read()


@pytest.mark.parametrize("warning_before_status", [False, True])
def test_sdk_timing_fallback_warning_preserves_valid_raw_and_diagnostics(warning_before_status):
    source = BaySpecSource(load_device_config()["bayspec"])
    source._load_axis()
    status = {"type": "status", "device_count": 1, "device_index": 0, "spectrum_points": 512, "is_open_ok": 1, "vid": "0xBA8C", "pid": "0x1688"}
    active = {"cadence_wait_mechanism": "waitable_timer_high_resolution", "high_resolution_waitable_timer_active": True, "cadence_wait_fallback_reason": "", "cadence_wait_max_stop_check_ms": 2}
    fallback = {**active, "cadence_wait_mechanism": "stop_event_wait_fallback", "high_resolution_waitable_timer_active": False, "cadence_wait_fallback_reason": "高分辨率等待失败，Win32 错误 87"}
    warning = {"type": "timing_warning", "message": "已退回可中止普通等待，实际节拍需检查", **fallback}
    values = list(range(512))
    first = None
    initial_diag = None
    if not warning_before_status:
        source._consume_message({**status, **active})
        source._consume_message({"type": "spectrum", "counts": values, "frame_index": 0, "snapshot_result": 1})
        first = source.read()
        initial_diag = source.diagnostics()
    source._consume_message(warning)
    if warning_before_status:
        # Older status messages may lack timing fields; keep an earlier warning.
        source._consume_message(status)
    source._consume_message({"type": "spectrum", "counts": values, "frame_index": 1, "snapshot_result": 1, **fallback})
    frame = source.read()
    diag = source.diagnostics()
    assert frame["intensity"] == values and frame["wavelength_nm"] == source._axis
    assert {key: frame[key] for key in fallback} == fallback
    assert {key: diag["sdk_status_raw"][key] for key in fallback} == fallback
    assert diag["last_timing_warning"] == warning
    assert diag["last_sdk_warning"] is None and diag["invalid_frames"] == 0
    assert diag["last_error"] is None and not diag["capture_incomplete"]
    assert source._fatal_error is None and not source._stop.is_set()
    if first is not None:
        assert {key: first[key] for key in active} == active
        assert {key: initial_diag["sdk_status_raw"][key] for key in active} == active
        assert {key: initial_diag["device_identity"][key] for key in active} == active
        assert initial_diag["sdk_status_raw"] is not diag["sdk_status_raw"]


def test_sdk_memory_drain_preserves_tail_without_read_or_watchdog(monkeypatch):
    source = BaySpecSource({**load_device_config()["bayspec"], "queue_limit": 2})
    source._load_axis()
    source._header = {"device_count": 1}
    for i in range(3):
        source._consume_message({"type": "spectrum", "counts": [i]*512, "frame_index": i, "snapshot_result": 1})
    source.close()
    monkeypatch.setattr(source, "read", lambda: pytest.fail("drain must not call read"))
    assert [frame["frame_id"] for frame in source.drain_received()] == [1, 2, 3]
    assert source.drain_received() == []
    assert source.metrics.received == 3


def test_sdk_eof_exposes_error_after_drain():
    source = BaySpecSource()
    class FakeStream:
        def readline(self): return ""
    class FakeProcess:
        stdout = FakeStream()
        def poll(self): return 0
    source.process = FakeProcess()
    source._read_worker()
    with pytest.raises(RuntimeError, match="退出"):
        source.read()


def test_sdk_rejects_incomplete_header_and_non_raw_counts():
    source = BaySpecSource(load_device_config()["bayspec"])
    source._load_axis()
    with pytest.raises(ValueError):
        source._consume_message({"type": "status", "device_count": 2})
    source._header = {"device_count": 1}
    with pytest.raises(ValueError):
        source._consume_message({"type": "spectrum", "counts": [2.5]*512, "frame_index": 0, "snapshot_result": 1})


def test_exclusive_lease_is_shared_across_instances_and_released(tmp_path, monkeypatch):
    monkeypatch.setattr("ninefbg.devices._existing_sdk_helpers", lambda: [])
    first = DeviceLease("bayspec", tmp_path, legacy_ports=[])
    second = DeviceLease("bayspec", tmp_path, legacy_ports=[])
    first.acquire()
    with pytest.raises(RuntimeError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_orphan_sdk_helper_blocks_new_hardware_open(tmp_path, monkeypatch):
    monkeypatch.setattr("ninefbg.devices._existing_sdk_helpers", lambda: [{"pid": 123, "parent_alive": False}])
    lease = DeviceLease("bayspec", tmp_path, legacy_ports=[])
    with pytest.raises(RuntimeError, match="父进程已退出"):
        lease.acquire()
    assert lease.stream is None


def test_px6d_vendor_packet_crc_and_force_units():
    assert command_packet(7).hex() == "aa557f0701d0"
    assert command_packet(5).hex() == "aa557f0501fa"
    expected = [-.0223, -.2217, -.0929, -.0054, -.0009, .0043]
    body = bytes.fromhex("AA 55 7F 03") + struct.pack("<6f", *expected)
    packet = body + bytes((crc8(body),))
    assert parse_force_frame(packet) == pytest.approx(expected)
    with pytest.raises(ValueError):
        parse_force_frame(packet[:-1] + bytes((packet[-1] ^ 1,)))
    body = bytes.fromhex("AA 55 7F 03") + struct.pack("<6f", float("nan"), 0, 0, 0, 0, 0)
    with pytest.raises(ValueError):
        parse_force_frame(body + bytes((crc8(body),)))


def test_px6d_different_response_address_is_exposed_not_assumed_echo():
    body = bytes.fromhex("AA 55 01 03") + struct.pack("<6f", 1, 2, 3, 4, 5, 6)
    packet = body + bytes((crc8(body),))
    assert parse_force_frame(packet) == [1, 2, 3, 4, 5, 6]
    with pytest.raises(ValueError, match="响应地址"):
        parse_force_frame(packet, verify_address=True)
    with pytest.raises(ValueError, match="CRC"):
        parse_force_frame(packet[:-1] + bytes((packet[-1] ^ 1,)))


def test_px6d_invalid_raw_packet_is_retained_in_diagnostics():
    body = bytes.fromhex("AA 55 7F 03") + struct.pack("<6f", 1, 2, 3, 4, 5, 6)
    packet = body + bytes((crc8(body) ^ 1,))
    class FakePort:
        is_open = True
        def reset_input_buffer(self): pass
        def write(self, data): return len(data)
        def flush(self): pass
    source = PX6DSource({"poll_hz": 50, "device_id": 127})
    source.port = FakePort()
    source._packet = lambda length, timeout: packet
    with pytest.raises(ValueError, match="CRC"):
        source.read()
    diag = source.diagnostics()
    assert diag["last_failed_packet_hex"] == packet.hex()
    assert diag["last_failed_packet"]["calculated_crc"] == crc8(body)
    assert diag["last_failed_packet"]["received_crc"] == packet[-1]


def test_sdk_watchdog_surfaces_stalled_reader_and_closes(monkeypatch):
    source = BaySpecSource()
    source._opened_ns = time.perf_counter_ns()-3_000_000_000
    source.process = object()
    closed = []
    monkeypatch.setattr(source, "close", lambda: closed.append(True))
    with pytest.raises(RuntimeError, match="未返回新有效"):
        source.read()
    assert closed


def test_px6d_frame_returns_acquisition_interval_and_raw_packet():
    body = bytes.fromhex("AA 55 7F 03") + struct.pack("<6f", 1, 2, 3, 4, 5, 6)
    packet = body + bytes((crc8(body),))
    class FakePort:
        is_open = True
        def reset_input_buffer(self): pass
        def write(self, data):
            assert data == command_packet(5)
            return len(data)
        def flush(self): pass
    source = PX6DSource({"poll_hz": 50, "device_id": 127})
    source.port = FakePort()
    source.stream_epoch = "fake"
    source._packet = lambda length, timeout: packet
    frame = source.read()
    assert frame["force_values"] == [1, 2, 3, 4, 5, 6]
    assert frame["read_start_ns"] <= frame["host_rx_monotonic_ns"] == frame["read_end_ns"]
    assert frame["device_sequence"] is None and frame["time_quality"] == "host_rx_approx"
    assert frame["raw_packet_hex"] == packet.hex()
    assert frame["units"] == ["N", "N", "N", "N·m", "N·m", "N·m"]


def test_sdk_invalid_settings_do_not_claim_lease_or_start_hardware(monkeypatch):
    source = BaySpecSource({"integration_us": True})
    monkeypatch.setattr(source.lease, "acquire", lambda: pytest.fail("lease claimed"))
    with pytest.raises(ValueError):
        source.open()


@pytest.mark.skipif(os.name != "nt", reason="Windows 子进程退出保护")
def test_windows_job_close_terminates_child_without_hardware():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    job = None
    try:
        job = _helper_kill_on_close_job(process.pid)
        assert process.poll() is None
        _close_job(job)
        job = None
        assert process.wait(timeout=3) is not None
    finally:
        if job is not None:
            _close_job(job)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
