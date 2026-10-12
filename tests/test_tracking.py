import copy

import numpy as np
import pytest

from ninefbg.tracking import PeakTracker


def make_frame(sequence, *, missing=None, shift=None, noise=0.3, nonuniform=False,
               phase="unloaded", source_kind="simulation", sigma_samples=2.2):
    axis = np.linspace(1500.0, 1590.0, 512)
    if nonuniform:
        axis = axis + 0.015 * np.sin(np.arange(512) / 11)
    centers = np.interp(np.arange(40, 473, 48)[:9] + .13, np.arange(512), axis)
    sigma = np.median(np.diff(axis)) * sigma_samples
    y = np.full(512, 230.0)
    rng = np.random.default_rng(sequence + 400)
    for index, center in enumerate(centers):
        if index == missing:
            continue
        center += (shift or {}).get(index, 0)
        y += (1000 + 35 * index) * np.exp(-0.5 * ((axis - center) / sigma) ** 2)
    y += rng.normal(0, noise, 512)
    return {"wavelength_nm": axis.tolist(), "intensity": y.tolist(), "frame_id": sequence,
            "host_rx_monotonic_ns": 10_000_000_000 + sequence * 10_000_000,
            "timestamp_source": "host_rx_approx", "time_quality": {"physical_time_verified": False},
            "stream_epoch": "epoch-A", "device_sequence": sequence,
            "preprocessing": {"raw_available": True, "smoothing_enabled": False,
                              "description": "合成测试样本", "saturation_level": 65535},
            "phase": phase, "source_kind": source_kind}


def initialize(tracker, count=4, **kwargs):
    tracker.request_reference(True)
    output = None
    for index in range(count):
        output = tracker.process(make_frame(index, **kwargs))
    assert output["state"] == "tracking", output["message"]
    return output


def test_confirmation_required_and_no_peak_count_fabrication():
    tracker = PeakTracker({"stable_frames": 4})
    for sequence in range(6):
        result = tracker.process(make_frame(sequence))
    assert not tracker.status()["initialized"]
    assert result["state"] == "awaiting_confirmation"
    with pytest.raises(ValueError, match="确认无载荷"):
        tracker.request_reference(False)
    tracker.request_reference(True)
    for sequence in range(6, 12):
        result = tracker.process(make_frame(sequence, missing=4))
    assert result["locator_diagnostics"]["candidate_count"] == 8
    assert "不补足" in result["message"]
    assert not tracker.status()["initialized"]


def test_measured_nonuniform_axis_and_raw_fit_output():
    tracker = PeakTracker({"stable_frames": 4})
    result = initialize(tracker, nonuniform=True)
    assert result["axis_diagnostics"]["uniform"] is False
    assert len(result["windows"]) == 9
    assert all(row["id"] == f"FBG{i + 1}" for i, row in enumerate(result["peaks"]))
    assert all(row["valid"] and row["fit_uses_smoothed_data"] is False for row in result["peaks"])
    assert all(row["residual"] < 1 for row in result["peaks"])
    assert result["raw_spectrum"]["intensity"] == make_frame(3, nonuniform=True)["intensity"]
    assert result["drift_range_verified"] is False
    assert all(window["confirmed_drift_nm"] is None for window in result["windows"])


def test_missing_peak_retains_identity_and_marks_old_value():
    tracker = PeakTracker({"stable_frames": 4})
    original = initialize(tracker)
    result = tracker.process(make_frame(4, missing=3))
    assert [row["id"] for row in result["peaks"]] == [f"FBG{i + 1}" for i in range(9)]
    missing = result["peaks"][3]
    assert not missing["valid"] and missing["stale"]
    assert missing["age_ms"] == 10.0
    assert missing["value_frame_id"] == 3
    assert missing["center_nm"] == original["peaks"][3]["center_nm"]
    assert all(row["valid"] for i, row in enumerate(result["peaks"]) if i != 3)
    assert result["windows"] == original["windows"]


def test_reference_freezes_during_press_and_zero_does_not_reassign():
    tracker = PeakTracker({"stable_frames": 4})
    original = initialize(tracker)
    tracker.begin_press()
    with pytest.raises(ValueError, match="冻结"):
        tracker.request_reference(True)
    step = original["axis_diagnostics"]["median_step_nm"]
    result = tracker.process(make_frame(4, shift={2: step * .8}))
    assert result["reference_version"] == 1 and result["reference_frozen"]
    assert result["peaks"][2]["drift_nm"] > step * .7
    tracker.end_press()
    tracker.request_reference(True)
    for sequence in range(5, 9):
        result = tracker.process(make_frame(sequence, shift={2: step * .8}))
    assert result["reference_version"] == 2
    assert abs(result["peaks"][2]["drift_nm"]) < step * .01
    assert result["windows"] == original["windows"]
    state = tracker.export_state()
    assert len(state["reference_history"]) == 2
    assert state["reference_history"][0]["reference_nm"] == original["reference_nm"]


def test_duplicate_bad_time_and_axis_changes_cannot_authorize_reference():
    tracker = PeakTracker({"stable_frames": 4})
    tracker.request_reference(True)
    tracker.process(make_frame(0))
    bad = tracker.process(make_frame(0))
    assert bad["state"] == "input_invalid" and "重复" in bad["message"]
    assert bad["stable_frame_count"] == 0
    for sequence in range(1, 5):
        tracker.process(make_frame(sequence))
    assert tracker.status()["initialized"]
    changed = make_frame(5)
    changed["wavelength_nm"][0] -= .001
    result = tracker.process(changed)
    assert not tracker.status()["initialized"]
    assert result["reference_pending"] is False
    assert len(tracker.export_state()["reference_history"]) == 1


def test_saturation_overlap_and_jump_reject_current_measurement():
    tracker = PeakTracker({"stable_frames": 4})
    original = initialize(tracker)
    frame = make_frame(4)
    peak_index = 40 + 48 * 4
    frame["intensity"][peak_index] = 65535
    result = tracker.process(frame)
    assert not result["peaks"][4]["valid"]
    assert result["peaks"][4]["stale"]
    assert "饱和" in result["peaks"][4]["failure_reason"]
    frame = make_frame(5)
    axis = np.asarray(frame["wavelength_nm"])
    other = original["peaks"][1]["center_nm"] + original["axis_diagnostics"]["median_step_nm"] * 13
    frame["intensity"] = (np.asarray(frame["intensity"]) + 800 * np.exp(-.5 * ((axis - other) / .30) ** 2)).tolist()
    result = tracker.process(frame)
    assert not result["peaks"][1]["valid"]
    assert "多个" in result["peaks"][1]["failure_reason"]
    step = original["axis_diagnostics"]["median_step_nm"]
    result = tracker.process(make_frame(6, shift={7: step * 4}))
    assert not result["peaks"][7]["valid"]
    assert "跳变" in result["peaks"][7]["failure_reason"]


def test_sparse_samples_reduce_parameters_and_expose_availability():
    tracker = PeakTracker({"stable_frames": 4})
    result = initialize(tracker, noise=0.01, sigma_samples=.65)
    assert all(row["fit_parameter_count"] < 4 for row in result["peaks"])
    assert all("固定峰宽" in row["fit_availability"] for row in result["peaks"])
    assert all(row["fit_sample_count"] < 7 for row in result["peaks"])


def test_source_timestamp_metadata_preserved_and_smoothed_input_blocked():
    tracker = PeakTracker({"stable_frames": 4})
    frame = make_frame(0)
    frame["preprocessing"]["smoothing_enabled"] = True
    result = tracker.process(frame)
    assert result["state"] == "input_invalid"
    assert "原始高斯拟合被阻断" in result["message"]
    frame = make_frame(1)
    result = tracker.process(frame)
    for field in ("frame_id", "host_rx_monotonic_ns", "timestamp_source", "time_quality", "stream_epoch"):
        assert result[field] == frame[field]
    assert "fit_completion_timestamp" not in result
    result["time_quality"]["physical_time_verified"] = True
    assert frame["time_quality"]["physical_time_verified"] is False


def test_unstable_and_invalid_frames_break_consecutive_stability():
    tracker = PeakTracker({"stable_frames": 4})
    tracker.request_reference(True)
    tracker.process(make_frame(0))
    tracker.process(make_frame(1))
    frame = make_frame(2, shift={0: .15})
    result = tracker.process(frame)
    assert result["stable_frame_count"] == 0
    assert not tracker.status()["initialized"]
    frame = make_frame(3)
    frame["intensity"][50] = float("nan")
    assert tracker.process(frame)["state"] == "input_invalid"
    for sequence in range(4, 8):
        result = tracker.process(make_frame(sequence))
    assert result["state"] == "tracking"


def test_epoch_change_requires_new_explicit_confirmation():
    tracker = PeakTracker({"stable_frames": 4})
    initialize(tracker)
    frame = make_frame(4)
    frame["stream_epoch"] = "epoch-B"
    result = tracker.process(frame)
    assert not tracker.status()["initialized"]
    assert not result["reference_pending"]
    assert not result["windows"]
    with pytest.raises(ValueError, match="初始化"):
        tracker.begin_press()


def test_config_validation_and_snapshot_ownership():
    with pytest.raises(ValueError, match="确认记录"):
        PeakTracker({"confirmed_drift_nm": .5})
    tracker = PeakTracker({"stable_frames": 4})
    original = initialize(tracker)
    snapshot = tracker.export_state()
    snapshot["reference_history"][0]["reference_nm"][0] = 0
    assert tracker.export_state()["reference_history"][0]["reference_nm"][0] != 0
    copied = copy.deepcopy(original)
    tracker.process(make_frame(4))
    assert original == copied


def test_runtime_config_mutation_and_inconsistent_smoothing_are_blocked():
    tracker = PeakTracker({"stable_frames": 4})
    initialize(tracker)
    tracker.config["relative_prominence"] = .08
    result = tracker.process(make_frame(4))
    assert result["state"] == "input_invalid"
    assert "配置已改变" in result["message"]
    assert all(not row["valid"] for row in result["peaks"])
    tracker = PeakTracker()
    frame = make_frame(0)
    frame["preprocessing"]["smoothing_applied"] = True
    result = tracker.process(frame)
    assert result["state"] == "input_invalid"


def test_press_before_reference_ready_cancels_confirmation_and_requires_new_one():
    tracker = PeakTracker({"stable_frames": 4})
    tracker.request_reference(True)
    tracker.process(make_frame(0))
    tracker.process(make_frame(1))
    with pytest.raises(ValueError, match="初始化"):
        tracker.begin_press()
    rejected = tracker.process(make_frame(2, phase="press"))
    assert rejected["state"] == "input_invalid"
    assert "本帧拒绝" in rejected["message"]
    assert tracker.status()["reference_pending"] is False
    assert tracker.status()["stable_frame_count"] == 0
    for sequence in range(3, 10):
        tracker.process(make_frame(sequence))
    assert tracker.status()["initialized"] is False
    assert tracker.export_state()["reference_history"] == []
    tracker.request_reference(True)
    for sequence in range(10, 14):
        result = tracker.process(make_frame(sequence))
    assert result["state"] == "tracking"
    assert result["reference_version"] == 1
