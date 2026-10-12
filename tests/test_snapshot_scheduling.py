"""Slow snapshot serialization must not hold the live force reader's lock."""
from pathlib import Path
import threading
import time

from ninefbg import service as module
from ninefbg.service import AcquisitionService
from ninefbg.sync import ForceSynchronizer
from test_acquisition_scheduling import ForceSource, MemoryWriter


def test_force_reader_continues_while_snapshot_json_conversion_is_blocked(tmp_path, monkeypatch):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "absent")
    service.writer = MemoryWriter()
    service.synchronizer = ForceSynchronizer()
    source = ForceSource()
    cleaning, release = threading.Event(), threading.Event()
    original = module.clean
    snapshots = []

    def blocked_clean(value):
        cleaning.set()
        assert release.wait(3)
        return original(value)

    monkeypatch.setattr(module, "clean", blocked_clean)
    snapshot = threading.Thread(target=lambda: snapshots.append(service.snapshot()))
    reader = threading.Thread(target=service._reader, args=(source, "force"))
    snapshot.start()
    try:
        assert cleaning.wait(1)
        reader.start()
        assert source.thirty_frames.wait(1), "snapshot JSON conversion blocked fresh force reads"
        assert len(service.synchronizer.frames) >= 29
        assert snapshots == []
    finally:
        service.stop_event.set()
        release.set()
        snapshot.join(3)
        if reader.ident is not None:
            reader.join(3)
    assert not snapshot.is_alive() and not reader.is_alive()
    assert snapshots and not (tmp_path / "absent").exists()


def reference_service(tmp_path):
    service = AcquisitionService(Path(__file__).resolve().parents[1], tmp_path / "absent")
    service.running = True
    service.mode = "hardware"
    service.tracker._reference_version = 1
    service.tracker._reference = [1500.0] * 9
    service.tracker._reference_history = [{"version": 1, "reference_nm": [1500.0] * 9}]
    service.latest = reference_result(service, 1)
    return service


def reference_result(service, version):
    return {"frame": {"frame_id": version, "source_kind": "hardware",
                      "host_rx_monotonic_ns": time.perf_counter_ns()},
            "tracker": {"frame_id": version, "reference_version": version,
                        "peaks": [{"id": f"FBG{i}", "valid": True, "stale": False,
                                   "drift_nm": version * .001} for i in range(1, 10)],
                        "algorithm_version": service.tracker.algorithm_version,
                        "config_version": service.tracker.config_version}}


def commit_reference(service, version):
    service.tracker._reference_version = version
    service.tracker._reference = [1500.0 + version] * 9
    service.tracker._reference_history.append({"version": version,
                                               "reference_nm": list(service.tracker._reference)})


def test_reference_cannot_change_between_snapshot_tracker_and_latest_capture(tmp_path):
    service = reference_service(tmp_path)
    service_lock_requested, allow_service_lock = threading.Event(), threading.Event()
    mutation_attempted, mutation_completed = threading.Event(), threading.Event()
    original_lock = service.lock
    snapshots = []

    class GatedLock:
        def __enter__(self):
            if threading.current_thread().name == "snapshot-capture":
                service_lock_requested.set()
                assert allow_service_lock.wait(3)
            original_lock.acquire()
            return self

        def __exit__(self, *args):
            original_lock.release()

    service.lock = GatedLock()

    def update_reference():
        assert service_lock_requested.wait(2)
        mutation_attempted.set()
        with service.tracker_lock:
            commit_reference(service, 2)
            with service.lock:
                service.latest = reference_result(service, 2)
        mutation_completed.set()

    capture = threading.Thread(target=lambda: snapshots.append(service.snapshot()), name="snapshot-capture")
    mutation = threading.Thread(target=update_reference)
    capture.start()
    mutation.start()
    try:
        assert mutation_attempted.wait(1)
        assert not mutation_completed.wait(.05), "reference changed between snapshot's two captures"
    finally:
        allow_service_lock.set()
        capture.join(3)
        mutation.join(3)
    assert not capture.is_alive() and not mutation.is_alive()
    assert mutation_completed.is_set()
    assert snapshots[0]["tracker"]["reference_version"] == 1
    assert snapshots[0]["frame"]["frame_id"] == 1
    assert snapshots[0]["tracker"]["reference_history"][-1]["version"] == 1
    assert all(peak["valid"] for peak in snapshots[0]["tracker"]["peaks"])
    assert not (tmp_path / "absent").exists()


def test_old_processed_frame_cannot_be_valid_under_new_reference_before_publication(tmp_path):
    service = reference_service(tmp_path)
    committed, allow_publication = threading.Event(), threading.Event()

    def update_before_publication():
        # Mirror _process: tracker commit precedes publication under service.lock.
        with service.tracker_lock:
            commit_reference(service, 2)
        committed.set()
        assert allow_publication.wait(3)
        with service.lock:
            service.latest = reference_result(service, 2)

    worker = threading.Thread(target=update_before_publication)
    worker.start()
    try:
        assert committed.wait(1)
        snapshot = service.snapshot()
        tracker = snapshot["tracker"]
        assert tracker["reference_version"] == 2
        assert tracker["reference_history"][-1]["version"] == 2
        assert snapshot["frame"]["frame_id"] == 1
        assert tracker.get("display_stale") is True
        assert all(not peak["valid"] and peak["stale"] for peak in tracker["peaks"])
        assert all("旧参考" in peak["failure_reason"] for peak in tracker["peaks"])
    finally:
        allow_publication.set()
        worker.join(3)
    assert not worker.is_alive()
    fresh = service.snapshot()
    assert fresh["frame"]["frame_id"] == 2
    assert fresh["tracker"]["reference_version"] == 2
    assert not fresh["tracker"].get("display_stale")
    assert all(peak["valid"] for peak in fresh["tracker"]["peaks"])
    assert not (tmp_path / "absent").exists()
