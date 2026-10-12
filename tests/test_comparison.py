import copy

from ninefbg.comparison import compare_algorithms, training_gate
from ninefbg.tracking import ALGORITHM_VERSION, TEMPLATE_ALGORITHM_VERSION
from test_tracking import make_frame


def recorded_frames(real=False):
    frames = []
    for sequence in range(16):
        phase = "unloaded" if sequence < 8 else "press"
        shift = {} if sequence < 8 else {2: min(sequence - 7, 4) * .02}
        frame = make_frame(sequence, phase=phase, source_kind="hardware" if real else "simulation", shift=shift)
        if sequence == 0:
            frame["unloaded_confirmed"] = True
        frames.append(frame)
    return frames


def test_comparison_uses_same_raw_frames_and_freezes_template():
    frames = recorded_frames()
    before = copy.deepcopy(frames)
    report = compare_algorithms(frames, {"stable_frames": 4})
    assert frames == before
    assert report["same_original_frames"] is True
    assert report["original_frame_count"] == 16
    gaussian = report["algorithms"][ALGORITHM_VERSION]
    template = report["algorithms"][TEMPLATE_ALGORITHM_VERSION]
    assert len(gaussian["frames"]) == len(template["frames"]) > 8
    assert [row["frame_id"] for row in gaussian["frames"]] == [row["frame_id"] for row in template["frames"]]
    assert report["template_press_versions"] == [1]
    assert all(row["template_reference_version"] == 1 for row in template["frames"])
    assert template["summary"]["valid_rate"] > .95
    assert gaussian["summary"]["valid_rate"] > .95
    last = template["frames"][-1]["peaks"][2]
    assert abs(last["drift_nm"] - .08) < .02
    assert report["measurement_precision_verified"] is False
    assert report["training_ready"] is False


def test_simulation_never_opens_formal_training_gate():
    report = compare_algorithms(recorded_frames(), {"stable_frames": 4})
    gate = training_gate(report, ALGORITHM_VERSION, operator_accepted=True)
    assert gate["ready"] is False
    assert any("真实无载" in reason for reason in gate["reasons"])
    assert any("仿真" in reason for reason in gate["reasons"])


def test_real_operator_acceptance_and_method_config_lock():
    report = compare_algorithms(recorded_frames(real=True), {"stable_frames": 4,
                               "confirmed_drift_nm": .5,
                               "drift_confirmation_evidence": "单元测试中模拟有证据的元数据，非现场验收"})
    assert report["evidence"]["real_comparable_press_frames"] > 2
    assert not training_gate(report, ALGORITHM_VERSION)["ready"]
    assert not training_gate(report, ALGORITHM_VERSION, "wrong", operator_accepted=True)["ready"]
    assert not training_gate(report, "automatic", operator_accepted=True)["ready"]
    gate = training_gate(report, ALGORITHM_VERSION, report["config_version"], operator_accepted=True)
    assert gate["ready"]
    assert gate["algorithm_definition_frozen"]
    assert gate["algorithm_version"] == ALGORITHM_VERSION
    assert gate["config_version"] == report["config_version"]


def test_no_silent_unloaded_confirmation_and_no_template_fallback():
    frames = recorded_frames()
    frames[0].pop("unloaded_confirmed")
    report = compare_algorithms(frames, {"stable_frames": 4})
    assert report["compared_frame_count"] == 0
    assert not report["reference_history"]
    assert not training_gate(report, TEMPLATE_ALGORITHM_VERSION, operator_accepted=True)["ready"]


def test_premature_press_during_initialization_cannot_create_reference():
    frames = [make_frame(i, phase="unloaded" if i < 2 or i >= 5 else "press") for i in range(12)]
    frames[0]["unloaded_confirmed"] = True
    report = compare_algorithms(frames, {"stable_frames": 4})
    assert report["reference_history"] == []
    assert report["compared_frame_count"] == 0
    assert [row["frame_id"] for row in report["phase_rejections"]] == [2, 3, 4]
    assert report["original_frame_count"] == len(frames)
    assert any("未完成" in row["action"] for row in report["phase_rejections"])


def test_premature_press_during_zero_preserves_old_reference_and_rejects_frames():
    frames = [make_frame(i, phase="press" if 9 <= i <= 15 else "unloaded") for i in range(20)]
    frames[0]["unloaded_confirmed"] = True
    frames[8]["unloaded_confirmed"] = True
    report = compare_algorithms(frames, {"stable_frames": 4})
    assert len(report["reference_history"]) == 1
    assert report["reference_history"][0]["version"] == 1
    assert [row["frame_id"] for row in report["phase_rejections"]] == list(range(9, 16))
    for algorithm in report["algorithms"].values():
        press_rows = [row for row in algorithm["frames"] if row["phase"] == "press"]
        assert len(press_rows) == 7
        assert all(not any(peak["valid"] for peak in row["peaks"]) for row in press_rows)
        assert all(row["reference_version"] == 1 for row in algorithm["frames"])
    assert all(row["template_reference_version"] == 1 for row in report["algorithms"][TEMPLATE_ALGORITHM_VERSION]["frames"])
    assert not report["template_press_versions"]


def test_comparison_exposes_fair_local_timing_and_separate_end_to_end_cost():
    report = compare_algorithms(recorded_frames(), {"stable_frames": 4})
    assert report["report_version"] == "1.1.0"
    scope = report["timing_scope"]["metric_id"]
    assert scope == "nine_peak_local_processing_v1"
    for name, algorithm in report["algorithms"].items():
        assert algorithm["summary"]["processing_timing_scope"] == scope
        assert all(row["processing_timing_scope"] == scope for row in algorithm["frames"])
        assert all(row["processing_ms"] >= 0 for row in algorithm["frames"])
        if name == ALGORITHM_VERSION:
            assert all(row["processing_ms"] <= row["tracker_end_to_end_ms"] for row in algorithm["frames"])
    assert report["independent_holdout"] is False
    assert report["template_build_frames_in_evaluation"] is True
