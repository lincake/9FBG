"""Real reader threads must remain independent of the optical fitting lock.

All streams below are hardware-shaped synthetic fixtures. No instrument is
opened and display mode must leave even the temporary userdata path absent.
"""
from collections import deque
from copy import deepcopy
from pathlib import Path
import threading
import time

import pytest

from ninefbg import devices
from ninefbg.service import AcquisitionService, DisplayWriter
from ninefbg.sync import ForceSynchronizer
from ninefbg.tracking import PeakTracker
from test_service import spectra, until


ROOT = Path(__file__).resolve().parents[1]


class MemoryWriter(DisplayWriter):
    """Observe production submission order without creating recording files."""

    def __init__(self):
        super().__init__()
        self.audit = []
        self.rows = {kind: deque(maxlen=512) for kind in ("raw_force", "raw_spectra", "results")}
        self.audit_lock = threading.Lock()

    def submit(self, kind, value):
        super().submit(kind, value)
        if kind in self.rows:
            with self.audit_lock:
                self.rows[kind].append(deepcopy(value))
                self.audit.append((kind, value.get("frame_id")))


class ForceSource:
    """A continuous receive clock, with each read producing a distinct frame."""

    def __init__(self, config=None):
        self.number = 0
        self.connected = False
        self.thirty_frames = threading.Event()

    def open(self):
        self.connected = True

    def read(self):
        time.sleep(.003)
        self.number += 1
        if self.number >= 30:
            self.thirty_frames.set()
        return {"frame_id": self.number, "force_values": [0, 0, -1, 0, 0, 0],
                "host_rx_monotonic_ns": time.perf_counter_ns(), "stream_epoch": "scheduling-force",
                "time_quality": "host_rx_approx", "source_kind": "hardware"}

    def diagnostics(self):
        return {"connected": self.connected, "received_frames": self.number,
                "stream_epoch": "scheduling-force"}

    def close(self):
        self.connected = False


def test_force_reader_commits_each_frame_and_stops_while_fitting_lock_is_held(tmp_path, monkeypatch):
    service = AcquisitionService(ROOT, tmp_path / "uncreated")
    service.writer = MemoryWriter()
    service.synchronizer = ForceSynchronizer()
    # The 0.4.0 regression only occurs in the guided workspace, even while
    # waiting for a session: its on_force hook unconditionally takes tracker_lock.
    service.experiment.settings = {"guided_acquisition": True}
    source = ForceSource()
    entered, release = threading.Event(), threading.Event()
    local_stop = threading.Event()
    original_force = service.experiment.on_force
    original_analysis = service.analysis.on_force
    original_add = service.synchronizer.add

    def on_force(frame):
        service.writer.audit.append(("on_force", frame["frame_id"]))
        return original_force(frame)

    def analysis_force(frame):
        service.writer.audit.append(("analysis", frame["frame_id"]))
        return original_analysis(frame)

    def sync_add(frame):
        service.writer.audit.append(("sync", frame["frame_id"]))
        return original_add(frame)

    monkeypatch.setattr(service.experiment, "on_force", on_force)
    monkeypatch.setattr(service.analysis, "on_force", analysis_force)
    monkeypatch.setattr(service.synchronizer, "add", sync_add)

    def hold_fitting_lock():
        with service.tracker_lock:
            entered.set()
            release.wait(3)

    fitting = threading.Thread(target=hold_fitting_lock, name="test-held-fit")
    reader = threading.Thread(target=service._reader, args=(source, "force", local_stop), name="test-force")
    fitting.start()
    try:
        assert entered.wait(1)
        reader.start()
        assert source.thirty_frames.wait(.8), "六轴读取被九峰拟合锁阻塞，无法继续获取新帧"
        # At most the current read can still be between transport and commit.
        assert len(service.synchronizer.frames) >= 29
        assert service.latest_force["frame_id"] >= 29
        assert len(service.writer.rows["raw_force"]) >= 29
        assert service.writer.audit[:8] == [(kind, frame_id) for frame_id in (1, 2)
                                          for kind in ("raw_force", "on_force", "analysis", "sync")]
        local_stop.set()
        reader.join(.8)
        assert not reader.is_alive(), "六轴单独停止不应等待拟合锁释放"
        assert fitting.is_alive(), "必须在拟合线程仍持锁时完成六轴退出"
        assert not source.connected and service.force_error is None
        assert len(service.writer.rows["raw_force"]) == len(service.synchronizer.frames) == source.number
    finally:
        local_stop.set()
        release.set()
        fitting.join(2)
        if reader.ident is not None:
            reader.join(2)
    assert not reader.is_alive() and not fitting.is_alive()
    assert not service.userdata.exists()


def test_blocked_real_peak_tracker_keeps_force_brackets_current_and_shutdown_drains(tmp_path, monkeypatch):
    optical_sources, force_sources = [], []
    blocked, release, block_next = threading.Event(), threading.Event(), threading.Event()
    actual_process = PeakTracker.process

    class OpticalSource:
        def __init__(self, config):
            self.number = 0
            self.connected = False
            self.template = spectra(1)[0]
            self.frames = deque(maxlen=512)
            optical_sources.append(self)

        def open(self):
            self.connected = True

        def read(self):
            time.sleep(.01)
            self.number += 1
            frame = {**deepcopy(self.template), "frame_id": self.number,
                     "host_rx_monotonic_ns": time.perf_counter_ns(),
                     "stream_epoch": "scheduling-optical", "source_kind": "hardware"}
            self.frames.append(frame)
            return frame

        def diagnostics(self):
            return {"connected": self.connected, "received_frames": self.number}

        def close(self):
            self.connected = False

    class ObservedForce(ForceSource):
        def __init__(self, config):
            super().__init__(config)
            force_sources.append(self)

    def blocked_process(tracker, frame):
        if block_next.is_set() and not blocked.is_set():
            blocked.set()
            if not release.wait(4):
                raise AssertionError("测试必须释放九峰拟合线程")
        # Preserve the actual tracker, fitting, quality checks and reference gate.
        return actual_process(tracker, frame)

    monkeypatch.setattr(devices, "BaySpecSource", OpticalSource)
    monkeypatch.setattr(devices, "PX6DSource", ObservedForce)
    monkeypatch.setattr(PeakTracker, "process", blocked_process)
    monkeypatch.setattr("ninefbg.service.DisplayWriter", MemoryWriter)
    service = AcquisitionService(ROOT, tmp_path / "uncreated")
    service.connect(record=False, connect_force=True, integration_us=20, sample_rate_hz=100,
                    config={"stable_frames": 3})
    stop = None
    try:
        # This is explicit confirmation of synthetic fixture state, never a
        # substitute for an operator's physical no-contact confirmation.
        service.reference(True)
        until(lambda: service.tracker.status()["reference_version"] == 1 and
              (service.latest_sync or {}).get("valid") is True)
        service.experiment.settings = {"guided_acquisition": True}
        force = force_sources[0]
        block_next.set()
        assert blocked.wait(2)
        first_count = force.number
        first_optical_count = optical_sources[0].number
        until(lambda: optical_sources[0].number >= first_optical_count + 12, seconds=.8)
        assert not release.is_set(), "帧接收检查必须发生在实际 PeakTracker 持锁期间"
        # A real optical receive timestamp inside the blocked interval must
        # still have two fresh force samples. The old implementation creates
        # a >40 ms force gap here because on_force waits for tracker_lock.
        cutoff = time.perf_counter_ns() - 35_000_000
        optical_frame = next(frame for frame in reversed(tuple(optical_sources[0].frames))
                             if frame["host_rx_monotonic_ns"] <= cutoff)
        with service.lock:
            aligned = service.synchronizer.align(optical_frame)
            newest_force_ns = service.latest_force["host_rx_monotonic_ns"]
        assert aligned["valid"] is True, (aligned.get("reason"), aligned.get("delta_ms"), aligned)
        assert aligned["method"] in {"linear_receive_time", "exact_receive_time"}
        assert force.number >= first_count + 20
        assert newest_force_ns > optical_frame["host_rx_monotonic_ns"]
        assert service.frames.qsize() <= 2 and service.metrics["display_skipped"] > 0
        assert service.metrics["dropped"] == 0
        assert service.tracker.status()["reference_version"] == 1

        stop = threading.Thread(target=service.disconnect, name="test-controlled-stop")
        stop.start()
        until(lambda: service.stop_event.is_set(), seconds=1)
        until(lambda: all(not worker.is_alive() for worker in service.workers), seconds=1)
        assert stop.is_alive(), "受控停止应等待仍在执行的拟合，再排空处理队列"
        release.set()
        stop.join(4)
        assert not stop.is_alive() and not service.processor.is_alive()
        assert not service.running and not service.stopping
        assert service.frames.empty() and service.frames.unfinished_tasks == 0
        assert all(not source.connected for source in optical_sources + force_sources)
        assert service.error is None and service.force_error is None
        assert service.tracker.status()["reference_version"] == 1
        assert service.writer.closed and not service.userdata.exists()
    finally:
        release.set()
        if stop is not None:
            stop.join(5)
        service.disconnect()


def test_guided_tail_keeps_press_frozen_and_new_reference_rejects_old_receive_time(tmp_path, monkeypatch):
    service = AcquisitionService(ROOT, tmp_path / "uncreated")
    service.writer = MemoryWriter()
    service.running, service.mode = True, "hardware"
    service.metrics = {"received": 0, "processed": 0, "duplicates": 0, "identical_payloads": 0,
                       "max_queue_depth": 0, "display_skipped": 0, "dropped": 0}
    service.tracker = PeakTracker({"stable_frames": 3})
    service.tracker.request_reference(True)
    initialized = spectra(3)
    # Keep fixture receive times strictly in the past before the release event.
    start = time.perf_counter_ns() - 100_000_000
    for index, frame in enumerate(initialized):
        frame.update(frame_id=index + 1, host_rx_monotonic_ns=start + index * 10_000_000,
                     phase="unloaded", source_kind="hardware")
        service.tracker.process(frame)
    assert service.tracker.status()["reference_version"] == 1
    service.tracker.begin_press()
    service.capture_phase = "press"
    service.phase_events = [(0, "press")]
    service.experiment.settings = {"guided_acquisition": True}
    service.experiment.running = True
    service.experiment.trial_id = None
    service.latest = {"frame": initialized[-1], "tracker": service.tracker.status()}
    observed = []
    monkeypatch.setattr(service.experiment, "on_spectrum", lambda frame: True)

    def on_result(result, sync):
        observed.append((result["frame_id"], result["reference_version"],
                         service.tracker.status()["press_active"], result["state"]))

    monkeypatch.setattr(service.experiment, "on_result", on_result)
    service._release_completed_guided_press()
    release_ns = service.guided_release_after_ns
    assert release_ns is not None and service.capture_phase == "recovery"
    assert service.tracker.status()["press_active"] is True
    with pytest.raises(ValueError, match="参考冻结"):
        service.tracker.request_reference(True)

    tail, recovery = deepcopy(initialized[0]), deepcopy(initialized[0])
    tail.update(frame_id=10, host_rx_monotonic_ns=release_ns - 1)
    recovery.update(frame_id=11, host_rx_monotonic_ns=release_ns + 1)
    service._receive(tail)
    service._receive(recovery)
    service.process_done.set()
    service._process()
    assert observed[:2] == [(10, 1, True, "tracking"), (11, 1, True, "tracking")]
    assert service.guided_release_after_ns is None
    assert not service.tracker.status()["press_active"]
    assert service.tracker.status()["reference_version"] == 1
    assert [row["phase"] for row in service.writer.rows["raw_spectra"]] == ["press", "recovery"]

    service.experiment.running = False
    service.reference(True)
    reference_cutoff = service.reference_min_rx_ns
    old = deepcopy(initialized[0])
    old.update(frame_id=20, host_rx_monotonic_ns=reference_cutoff - 1)
    service._receive(old)
    service._process()
    assert observed[-1] == (20, 1, False, "reference_waiting")
    assert service.tracker.status()["reference_pending"] is True
    assert service.pending_confirmation is True
    assert service.tracker.status()["reference_version"] == 1

    for index in range(3):
        fresh = deepcopy(initialized[0])
        fresh.update(frame_id=21 + index, host_rx_monotonic_ns=reference_cutoff + index * 10_000_000 + 1)
        service._receive(fresh)
    service._process()
    assert service.tracker.status()["reference_version"] == 2
    assert not service.tracker.status()["reference_pending"]
    history = service.tracker.export_state()["reference_history"]
    assert history[-1]["frame_ids"] == [21, 22, 23]
    assert all(row[1] >= 1 for row in observed)
    assert service.frames.unfinished_tasks == 0 and not service.userdata.exists()
