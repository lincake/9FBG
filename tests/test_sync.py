import pytest

from ninefbg.sync import ForceSynchronizer


def force(ms, value=0, epoch="force-1", fid=None):
    return {"host_rx_monotonic_ns": int(ms*1e6), "force_values": [value]*6, "stream_epoch": epoch, "frame_id": fid if fid is not None else ms, "time_quality": "host_rx_approx"}


def spec(ms, epoch="optical-1"):
    return {"host_rx_monotonic_ns": int(ms*1e6), "stream_epoch": epoch, "frame_id": ms, "time_quality": "host_rx_approx", "fit_completed_ns": 9999999999999}


def test_exact_or_bracket_only_and_never_fit_finish_time():
    sync = ForceSynchronizer()
    sync.add(force(100, 0))
    assert sync.align(spec(100))["valid"]
    sync.add(force(120, 10))
    result = sync.align(spec(110))
    assert result["valid"] and result["force_values"] == [5]*6
    assert result["time_quality"] == "host_rx_approx"
    assert result["method"] == "linear_receive_time"
    assert sync.align(spec(121))["valid"] is False


def test_large_gap_and_missing_acquisition_time_reject():
    sync = ForceSynchronizer(max_gap_ms=40)
    sync.add(force(100))
    sync.align(spec(100))
    sync.add(force(200))
    assert not sync.align(spec(150))["valid"]
    assert not sync.align({"fit_completed_ns": 100000000})["valid"]


def test_reconnection_never_interpolates_previous_epoch():
    sync = ForceSynchronizer()
    sync.add(force(100, 1))
    sync.align(spec(100))
    sync.add(force(120, 2, epoch="force-2"))
    result = sync.align(spec(110))
    assert not result["valid"]
    assert sync.diagnostics()["stream_resets"] == 1
    sync.add(force(140, 3, epoch="force-2"))
    assert sync.align(spec(130))["force_values"] == [2.5]*6
    assert not sync.align(spec(135, epoch="optical-2"))["valid"]


def test_identical_values_are_not_duplicate_frames():
    sync = ForceSynchronizer()
    assert sync.add(force(100, 1))
    assert sync.add(force(120, 1))
    assert not sync.add(force(120, 1))
    assert not sync.add(force(115, 2))
    assert sync.diagnostics()["duplicate_transport_frames"] == 2


def test_invalid_force_time_quality_or_values_rejected():
    sync = ForceSynchronizer()
    bad = force(100)
    bad["force_values"] = [float("nan")]*6
    assert not sync.add(bad)
    bad = force(100)
    bad["time_quality"] = "unknown"
    assert not sync.add(bad)
