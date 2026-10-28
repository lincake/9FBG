"""Connection automation uses existing fits, with separate no-load provenance."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import time

import numpy as np
import pytest

from ninefbg import devices
from ninefbg.service import AcquisitionService
from ninefbg.tracking import AUTO_REFERENCE_SOURCE, MANUAL_REFERENCE_SOURCE, PeakTracker, digest
from test_service import until
from test_tracking import make_frame


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def controlled_optical(monkeypatch):
    instances = []

    class Optical:
        fail_open = False

        def __init__(self, config):
            self.connected = False
            self.frames = queue.Queue()
            self.number = 0
            self.epoch = f"auto-connect-fixture-{len(instances)}"
            instances.append(self)

        def open(self):
            if self.fail_open:
                raise RuntimeError("测试光谱连接失败")
            self.connected = True

        def read(self):
            try:
                frame = self.frames.get(timeout=.01)
            except queue.Empty:
                return None
            # Pace synthetic hardware frames so display coalescing is not the
            # behavior under test. The source stamp still belongs to read().
            time.sleep(.02)
            frame.setdefault("host_rx_monotonic_ns", time.perf_counter_ns())
            return frame

        def emit(self, count=1, *, stamp=None, shift=None):
            for _ in range(count):
                frame = make_frame(self.number, phase="unknown", source_kind="hardware", shift=shift)
                frame.pop("host_rx_monotonic_ns")
                # No fabricated physical frame sequence: our fixture only has
                # a local identity, like the real adapter's current SDK.
                frame["device_sequence"] = None
                frame["stream_epoch"] = self.epoch
                frame["time_quality"] = "host_rx_approx"
                if stamp is not None:
                    frame["host_rx_monotonic_ns"] = stamp
                self.frames.put(frame)
                self.number += 1

        def diagnostics(self):
            return {"connected": self.connected, "stream_epoch": self.epoch}

        def close(self):
            self.connected = False

    monkeypatch.setattr(devices, "BaySpecSource", Optical)
    return instances, Optical


def test_connect_automatic_reference_uses_fresh_stable_frames_once_and_records_truthful_provenance(tmp_path, controlled_optical):
    sources, _ = controlled_optical
    service = AcquisitionService(ROOT, tmp_path)
    service.connect(config={"stable_frames": 3}, connect_force=False, auto_reference=True)
    try:
        pending = service.snapshot()["tracker"]
        assert pending["reference_pending"]
        assert pending["pending_reference_source"] == AUTO_REFERENCE_SOURCE
        assert "自动" in pending["message"]
        source = sources[-1]
        source.emit(stamp=service.reference_min_rx_ns - 1)
        until(lambda: service.metrics["processed"] == 1)
        assert service.latest["tracker"]["state"] == "reference_waiting"
        assert service.tracker.status()["stable_frame_count"] == 0
        source.emit(3)
        until(lambda: service.tracker.status()["initialized"])
        first = deepcopy(service.tracker.export_state()["reference_history"][0])
        assert first["reference_source"] == AUTO_REFERENCE_SOURCE
        assert first["unloaded_confirmed"] is False
        assert first["frame_ids"] == [1, 2, 3]
        assert first["stable_frame_count"] == 3
        assert first["stream_epoch"] == source.epoch
        assert first["algorithm_version"] == service.tracker.algorithm_version
        assert first["config_version"] == service.tracker.config_version
        assert first["axis_sha256"] == service.latest["tracker"]["axis_diagnostics"]["axis_sha256"]
        source.emit(5, shift={0: .1})
        until(lambda: service.metrics["processed"] == 9)
        assert service.tracker.status()["reference_version"] == 1
        assert service.tracker.export_state()["reference_history"] == [first]
        assert service.capture_phase == "unknown" and service.pending_confirmation is False
        assert service.snapshot()["tracker"]["reference_unloaded_confirmed"] is False
        assert service.analysis.test_reference["ready"] is False
        assert service.experiment.baseline.get("ready") is not True
        with pytest.raises(ValueError, match="确认无载荷"):
            service.reference(False)
    finally:
        service.disconnect()
    raw = [json.loads(line) for line in (service.writer.path / "raw_spectra.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(row["phase"] == "unknown" and row["unloaded_confirmed"] is False for row in raw)
    events = [json.loads(line) for line in (service.writer.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    requests = [event for event in events if event["event"] == "optical_connect_auto_reference_requested"]
    assert len(requests) == 1 and requests[0]["unloaded_confirmed"] is False
    assert not any(event["event"] == "unloaded_confirmation" for event in events)


def test_auto_reference_reconnect_is_new_generation_and_manual_reference_still_confirms(tmp_path, controlled_optical):
    sources, _ = controlled_optical
    service = AcquisitionService(ROOT, tmp_path / "uncreated")
    first_epoch = None
    for connection in range(2):
        service.connect(config={"stable_frames": 3}, record=False, connect_force=False, auto_reference=True)
        try:
            assert service.tracker.status()["reference_version"] == 0
            assert service.tracker.status()["reference_pending"]
            source = sources[-1]
            source.emit(3)
            until(lambda: service.tracker.status()["initialized"])
            state = service.tracker.export_state()
            assert len(state["reference_history"]) == 1
            assert state["reference_history"][0]["stream_epoch"] != first_epoch
            first_epoch = source.epoch
            if connection:
                anchors, windows = state["identity_anchor_nm"], state["windows"]
                service.reference(True)
                source.emit(3)
                until(lambda: service.tracker.status()["reference_version"] == 2)
                state = service.tracker.export_state()
                assert state["reference_history"][-1]["reference_source"] == MANUAL_REFERENCE_SOURCE
                assert state["reference_history"][-1]["unloaded_confirmed"] is True
                assert state["identity_anchor_nm"] == anchors and state["windows"] == windows
        finally:
            service.disconnect()
    assert not service.userdata.exists()


def test_failed_optical_connection_never_commits_reference(tmp_path, controlled_optical):
    _, Optical = controlled_optical
    Optical.fail_open = True
    service = AcquisitionService(ROOT, tmp_path / "uncreated")
    service.connect(record=False, connect_force=False, auto_reference=True)
    try:
        until(lambda: service.stop_event.is_set())
        assert "测试光谱连接失败" in service.error
        assert not service.tracker.status()["initialized"]
        assert service.tracker.export_state()["reference_history"] == []
        assert service.pending_confirmation is False and service.capture_phase == "unknown"
    finally:
        service.disconnect()
    assert not service.userdata.exists()


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_auto_reference_option_validates_before_creating_files_or_devices(tmp_path, controlled_optical, value):
    sources, _ = controlled_optical
    service = AcquisitionService(ROOT, tmp_path / "uncreated")
    with pytest.raises(ValueError, match="必须为布尔值"):
        service.connect(auto_reference=value)
    assert not sources and not service.running and not service.userdata.exists()


def test_auto_reference_is_hardware_only_and_legacy_connect_remains_manual(tmp_path, controlled_optical):
    sources, _ = controlled_optical
    service = AcquisitionService(ROOT, tmp_path / "uncreated")
    with pytest.raises(ValueError, match="回放"):
        service.connect("replay", [make_frame(0)], auto_reference=True)
    assert not sources and not service.running and not service.userdata.exists()
    service.connect(record=False, connect_force=False)
    try:
        sources[-1].emit(4)
        until(lambda: service.metrics["processed"] == 4)
        assert not service.tracker.status()["initialized"]
        assert not service.tracker.status()["reference_pending"]
    finally:
        service.disconnect()


def test_auto_reference_preserves_nonuniform_coordinates_and_requires_consecutive_valid_stability():
    tracker = PeakTracker({"stable_frames": 3})
    tracker.request_connection_reference()
    for sequence in range(2):
        tracker.process(make_frame(sequence, nonuniform=True))
    unstable = tracker.process(make_frame(2, shift={0: .15}, nonuniform=True))
    assert unstable["stable_frame_count"] == 0 and unstable["reference_pending"]
    assert "自动九峰参考" in unstable["message"] and "无载" not in unstable["message"]
    invalid = make_frame(3, nonuniform=True)
    invalid["intensity"][0] = float("nan")
    assert tracker.process(invalid)["state"] == "input_invalid"
    missing = tracker.process(make_frame(4, missing=4, nonuniform=True))
    assert missing["locator_diagnostics"]["candidate_count"] == 8
    assert missing["reference_pending"] and not tracker.status()["initialized"]
    for sequence in range(5, 8):
        result = tracker.process(make_frame(sequence, nonuniform=True))
    assert result["reference_version"] == 1 and result["reference_unloaded_confirmed"] is False
    assert result["axis_diagnostics"]["uniform"] is False
    assert [row["id"] for row in result["peaks"]] == [f"FBG{i + 1}" for i in range(9)]
    assert all(row["fit_uses_smoothed_data"] is False for row in result["peaks"])
    assert tracker.export_state()["reference_history"][0]["frame_ids"] == [5, 6, 7]
    with pytest.raises(ValueError, match="尚未处理"):
        tracker.request_connection_reference()


def test_auto_reference_cannot_cross_changed_axis_or_silently_changed_config():
    tracker = PeakTracker({"stable_frames": 3})
    tracker.request_connection_reference()
    tracker.process(make_frame(0))
    changed = make_frame(1)
    changed["wavelength_nm"][0] -= .001
    result = tracker.process(changed)
    assert not result["reference_pending"] and not tracker.status()["initialized"]
    for sequence in range(2, 7):
        tracker.process(make_frame(sequence))
    assert tracker.export_state()["reference_history"] == []
    tracker = PeakTracker({"stable_frames": 3})
    tracker.request_connection_reference()
    tracker.process(make_frame(0))
    tracker.config["relative_prominence"] = .08
    for sequence in range(1, 5):
        result = tracker.process(make_frame(sequence))
    assert result["state"] == "input_invalid" and "配置已改变" in result["message"]
    assert result["reference_pending"] and not tracker.status()["initialized"]
    assert tracker.export_state()["reference_history"] == []


def test_automatic_reference_uses_identical_fits_on_actual_512_point_calibration_axis():
    calibration = json.loads((ROOT / "assets/sdk/wavelength_axis_F1520808_512.json").read_text(encoding="utf-8-sig"))
    axis = np.asarray(calibration["axis_nm"])
    assert len(axis) == 512
    centers = np.interp(np.arange(40, 473, 48)[:9] + .13, np.arange(512), axis)
    sigma = np.median(np.diff(axis)) * 2.2
    values = np.full(512, 230.0)
    for index, center in enumerate(centers):
        values += (1000 + 35 * index) * np.exp(-.5 * ((axis - center) / sigma) ** 2)
    manual, automatic = PeakTracker({"stable_frames": 3}), PeakTracker({"stable_frames": 3})
    manual.request_reference(True)
    automatic.request_connection_reference()
    for sequence in range(3):
        frame = make_frame(sequence, phase="unknown")
        frame.update(wavelength_nm=axis.tolist(), intensity=values.tolist())
        manual_result, automatic_result = manual.process(frame), automatic.process(frame)
    assert automatic_result["reference_version"] == 1
    assert automatic_result["algorithm_version"] == manual_result["algorithm_version"] == "gaussian_background_fixed_identity_1.0.0"
    assert automatic_result["config_version"] == manual_result["config_version"]
    assert automatic_result["axis_diagnostics"]["axis_sha256"] == manual_result["axis_diagnostics"]["axis_sha256"] == digest(axis.tolist())
    assert automatic_result["reference_nm"] == manual_result["reference_nm"]
    assert automatic_result["peaks"] == manual_result["peaks"]
    assert automatic_result["windows"] == manual_result["windows"]
    assert automatic_result["raw_spectrum"]["wavelength_nm"] == axis.tolist()
    assert automatic_result["raw_spectrum"]["intensity"] == values.tolist()


@pytest.mark.parametrize("confirmation", [False, None, 0, 1, "true"])
def test_manual_reference_still_requires_literal_unloaded_confirmation(confirmation):
    tracker = PeakTracker()
    with pytest.raises(ValueError, match="确认无载荷"):
        tracker.request_reference(confirmation)
    assert not tracker.status()["reference_pending"]
