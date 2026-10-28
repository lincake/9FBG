"""Hardware-shaped fakes verify display lifecycle without opening an instrument."""
from copy import deepcopy
from pathlib import Path
import time
import threading

import pytest

from ninefbg import devices
from ninefbg.service import AcquisitionService, optical_request_settings
from test_service import spectra, until


@pytest.fixture
def fake_hardware(monkeypatch):
    optical, forces = [], []

    class Optical:
        def __init__(self, config):
            self.config = dict(config)
            self.connected = False
            self.number = 0
            self.frame = spectra(1)[0]
            optical.append(self)
        def open(self):
            self.connected = True
        def read(self):
            time.sleep(.004)
            self.number += 1
            return {**deepcopy(self.frame), "frame_id": self.number,
                    "host_rx_monotonic_ns": time.perf_counter_ns(), "stream_epoch": "optical-fixture"}
        def diagnostics(self):
            return {"connected": self.connected, "received_frames": self.number,
                    "wavelength_axis": {"point_count": 512}, "configuration": self.config}
        def close(self):
            self.connected = False

    class Force:
        fail = False
        emit = True
        def __init__(self, config):
            self.connected = False
            self.number = 0
            self.epoch = "force-fixture-" + str(len(forces))
            forces.append(self)
        def open(self):
            self.connected = True
        def read(self):
            time.sleep(.003)
            if self.fail:
                raise TimeoutError("测试六轴超时")
            if not self.emit:
                return None
            self.number += 1
            return {"force_values": [1, 2, 3, .1, .2, .3], "frame_id": self.number,
                    "host_rx_monotonic_ns": time.perf_counter_ns(), "stream_epoch": self.epoch,
                    "time_quality": "host_rx_approx"}
        def diagnostics(self):
            return {"connected": self.connected, "received_frames": self.number,
                    "port_identity": {"port": "FAKE"}, "stream_epoch": self.epoch}
        def close(self):
            self.connected = False

    monkeypatch.setattr(devices, "BaySpecSource", Optical)
    monkeypatch.setattr(devices, "PX6DSource", Force)
    return optical, forces, Force


def test_display_only_has_no_files_and_force_connects_independently(tmp_path, fake_hardware):
    optical, forces, Force = fake_hardware
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "uncreated")
    service.connect(record=False, connect_force=False, integration_us=20, sample_rate_hz=100)
    try:
        until(lambda: service.metrics["processed"] >= 2)
        assert not forces, "Optical-only connect must never instantiate PX6D"
        assert optical[0].config["integration_us"] == 20
        assert optical[0].config["interval_ms"] == 10
        assert optical[0].config["trigger_hz"] == 100
        assert service.snapshot()["session"] is None
        assert service.snapshot()["recording"] is False
        assert not service.userdata.exists()
        service.connect_force()
        until(lambda: service.snapshot()["force_connected"] and service.latest_force is not None)
        with pytest.raises(ValueError, match="已连接"):
            service.connect_force()
        old_epoch = service.latest_force["stream_epoch"]
        service.disconnect_force()
        assert not service.snapshot()["force_connected"]
        assert service.snapshot()["force"]["stale"]
        assert service.snapshot()["sync"]["valid"] is False
        assert not service.synchronizer.frames
        before = service.metrics["received"]
        until(lambda: service.metrics["received"] > before + 2)
        assert optical[0].connected and service.running
        Force.emit = False
        service.connect_force()
        until(lambda: service.snapshot()["force_connected"])
        assert service.snapshot()["force"]["stale"], "A new connection cannot make an old force frame current"
        Force.emit = True
        until(lambda: service.latest_force["stream_epoch"] != old_epoch)
        assert len(service.sources) == 2
        assert len(service.snapshot()["force_diagnostic_history"]) == 1
    finally:
        service.disconnect()
    assert all(not worker.is_alive() for worker in service.workers)
    assert not service.userdata.exists()


def test_legacy_hardware_connect_keeps_recording_and_default_requests(tmp_path, fake_hardware):
    optical, forces, _ = fake_hardware
    root = Path(__file__).resolve().parents[1]
    original_config = (root / "config" / "device.json").read_bytes()
    service = AcquisitionService(root, tmp_path)
    service.connect()
    try:
        until(lambda: service.latest_force is not None and service.metrics["processed"] > 0)
        assert len(forces) == 1
        assert optical[0].config["interval_ms"] == 40
        assert service.snapshot()["recording"] is True
        assert service.snapshot()["session"]["path"] == str(service.writer.path)
    finally:
        service.disconnect()
    assert (service.writer.path / "manifest.json").exists()
    assert (root / "config" / "device.json").read_bytes() == original_config


def test_force_failure_can_reconnect_without_stopping_optical(tmp_path, fake_hardware):
    optical, forces, Force = fake_hardware
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    service.connect(record=False, connect_force=False)
    try:
        Force.fail = True
        service.connect_force()
        until(lambda: service.force_worker is not None and not service.force_worker.is_alive())
        assert service.force_error == "测试六轴超时"
        assert not service.stop_event.is_set()
        assert service.snapshot()["stopped"] is False
        assert optical[0].connected
        assert service.snapshot()["sync"]["valid"] is False
        Force.fail = False
        service.connect_force()
        until(lambda: service.snapshot()["force_connected"] and service.latest_force is not None)
        assert service.force_error is None
        assert service.error is None
    finally:
        service.disconnect()


def test_runtime_optical_failure_exposes_stop_without_discarding_reference_or_raw_frame(
        tmp_path, fake_hardware, monkeypatch):
    optical, _, _ = fake_hardware
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "uncreated")
    service.connect(record=False, connect_force=False, auto_reference=True,
                    config={"stable_frames": 3})
    try:
        until(lambda: len(service.snapshot()["tracker"].get("reference_nm") or []) == 9)
        reference = service.snapshot()["tracker"]["reference_nm"]

        def fail_read():
            raise TimeoutError("光谱设备超过 2 秒未返回新有效采集帧")

        monkeypatch.setattr(optical[0], "read", fail_read)
        until(lambda: service.stop_event.is_set() and not service.workers[0].is_alive())
        state = service.snapshot()
        assert state["stopped"] is True
        assert state["running"] is True  # Controlled disconnect still owns the session.
        assert "超过 2 秒" in state["error"]
        assert state["tracker"]["reference_nm"] == reference
        assert state["tracker"]["reference_version"] == 1
        assert state["tracker"]["display_stale"] is True
        assert state["tracker"]["peaks"]
        assert all(not peak["valid"] and peak["stale"] for peak in state["tracker"]["peaks"])
        assert len(state["frame"]["intensity"]) == 512
        assert state["frame"]["intensity"] == optical[0].frame["intensity"]
        assert not optical[0].connected
        assert not service.userdata.exists()
    finally:
        service.disconnect()
    assert service.snapshot()["running"] is False


def test_force_connection_requires_active_hardware_and_options_validate_first(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "uncreated")
    with pytest.raises(ValueError, match="请先连接光谱"):
        service.connect_force()
    with pytest.raises(ValueError, match="整数毫秒"):
        service.connect(record=False, connect_force=False, sample_rate_hz=30)
    assert not service.userdata.exists() and not service.running
    service.connect("replay", spectra(1), record=False, connect_force=False)
    try:
        with pytest.raises(ValueError, match="请先连接光谱"):
            service.connect_force()
    finally:
        service.disconnect()
    assert not service.userdata.exists()


@pytest.mark.parametrize("rate,interval", [(25, 40), (50, 20), (100, 10), (200, 5), (1, 1000)])
def test_optical_request_frequency_maps_to_exact_integer_interval(rate, interval):
    assert optical_request_settings(20, rate) == {"integration_us": 20, "interval_ms": interval, "trigger_hz": rate}


def test_fast_display_skips_old_queued_frames_and_shutdown_balances_tasks(tmp_path, fake_hardware, monkeypatch):
    from ninefbg.tracking import PeakTracker
    entered, release = threading.Event(), threading.Event()
    process = PeakTracker.process

    def blocked_process(tracker, frame):
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "Test must release the first fitting frame"
        return process(tracker, frame)

    monkeypatch.setattr(PeakTracker, "process", blocked_process)
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "uncreated")
    service.connect(record=False, connect_force=False, integration_us=20, sample_rate_hz=100)
    try:
        assert entered.wait(2)
        until(lambda: service.metrics["received"] >= 20)
        assert service.frames.maxsize == 2 and service.frames.qsize() <= 2
        assert service.metrics["max_queue_depth"] <= 2
        assert service.metrics["display_skipped"] > 0
        assert service.metrics["dropped"] == 0
        assert service.error is None
        assert service.frames.unfinished_tasks <= 3  # Two queued plus one in fitting.
        stop = threading.Thread(target=service.disconnect)
        stop.start()
        until(lambda: service.stop_event.is_set())
        release.set()
        stop.join(5)
        assert not stop.is_alive()
        assert service.frames.empty() and service.frames.unfinished_tasks == 0
        assert service.metrics["received"] == service.metrics["processed"] + service.metrics["display_skipped"]
        assert all(not worker.is_alive() for worker in service.workers)
        assert not service.processor.is_alive()
        assert not service.userdata.exists()
    finally:
        release.set()
        service.disconnect()


@pytest.mark.parametrize("mode,stale", [("hardware", True), ("replay", False)])
def test_hardware_display_age_uses_original_receive_time_not_processing_time(tmp_path, mode, stale):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path)
    service.running = True
    service.mode = mode
    now = time.perf_counter_ns()
    service.latest = {"received_ns": now,
                      "frame": {"frame_id": 1, "host_rx_monotonic_ns": now-2_000_000_000},
                      "tracker": {"peaks": [{"id": "FBG1", "valid": True, "stale": False}]}}
    state = service.snapshot()
    assert (state["tracker"].get("display_stale") is True) is stale
    if mode == "hardware":
        assert state["tracker"]["display_age_ms"] >= 2000
        assert state["tracker"]["peaks"][0]["stale"]
    else:
        assert state["tracker"]["display_age_ms"] < 1000
