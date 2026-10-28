"""Six-axis manual evidence checks, separate from the legacy 0.3 N rule."""
from __future__ import annotations
from copy import deepcopy
import math
import time

from .spectral_runtime import AXES, UNITS

EVIDENCE_SCHEMA = "spectral_validation_evidence_v1"


def is_spectral(value):
    return isinstance(value, dict) and (value.get("schema") == EVIDENCE_SCHEMA or
        value.get("schema") == "spectral_prediction_v1" or value.get("schema") == "spectral_manual_sample_v1")


def validated(evidence, check_clock=True):
    from .validation import _vector, _finite, _frame_id, _target_point, _checksum, _canonical
    value = deepcopy(evidence)
    _target_point(value.get("target_point_id"))
    if (value.get("source_kind") != "hardware" or value.get("valid") is not True or value.get("stale") is not False or
            value.get("experiment_running") is not False or any(value.get(key) is not True for key in
                ("test_reference_ready", "truth_zero_ready", "reference_frozen"))):
        raise ValueError("六维验证需要独立无载参考、冻结零偏和当前实机对照")
    for name, maximum in (("age_ms", 1000), ("force_age_ms", 250)):
        if not _finite(value.get(name)) or not 0 <= value[name] <= maximum:
            raise ValueError("六维验证源帧已过期")
    stamp = value.get("host_rx_monotonic_ns")
    if type(stamp) is not int or stamp <= 0 or not _frame_id(value.get("frame_id")):
        raise ValueError("六维验证缺少真实末帧身份")
    if check_clock and not 0 <= (time.perf_counter_ns() - stamp)/1e6 <= 1000:
        raise ValueError("六维验证末帧时间已过期")
    value["model_checksum"] = _checksum(value.get("model_checksum"))
    for key in ("optical_epoch", "force_epoch", "reference_id", "zero_id"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError("六维验证缺少时段、S0 或零偏版本")
    if value.get("axes") != AXES or value.get("units") != UNITS:
        raise ValueError("六维验证轴序或单位不一致")
    identity = {key: value[key] for key in ("frame_id", "optical_epoch", "force_epoch", "reference_id", "zero_id", "model_checksum")}
    for name in ("prediction_context", "truth_context"):
        if not isinstance(value.get(name), dict) or any(type(value[name].get(k)) is not type(v) or value[name].get(k) != v for k, v in identity.items()):
            raise ValueError("六维预测与对照的末帧、S0、零偏或模型身份不一致")
    prediction = value.get("prediction")
    if not isinstance(prediction, dict) or prediction.get("valid") is not True or prediction.get("comparison_ready") is not True:
        raise ValueError("当前全谱输出缺少有效六维对照")
    if prediction.get("axes") != AXES or prediction.get("units") != UNITS:
        raise ValueError("预测轴序或单位不一致")
    predicted = _vector(prediction.get("wrench_pred"), 6, "六维预测")
    truth = _vector(prediction.get("wrench_truth"), 6, "六维对照")
    errors = _vector(prediction.get("wrench_error"), 6, "六维误差")
    zero = _vector(value.get("force_zero"), 6, "六维零偏")
    sync = value.get("sync")
    if (not isinstance(sync, dict) or sync.get("valid") is not True or sync.get("matched") is not True or
            sync.get("spectrum_frame_id") != value["frame_id"] or sync.get("spectrum_stream_epoch") != value["optical_epoch"] or
            sync.get("force_stream_epoch") != value["force_epoch"]):
        raise ValueError("六维验证缺少可信的同帧同步")
    aligned = _vector(sync.get("force_values"), 6, "同步六轴")
    ids = sync.get("force_frame_ids")
    method = sync.get("method")
    required = 1 if method == "exact_receive_time" else 2 if method == "linear_receive_time" else 0
    if not required or not isinstance(ids, list) or len(ids) != required or len(set(ids)) != required:
        raise ValueError("最近力帧仅供诊断，不能作为六维标签")
    sources = value.get("force_source_frames")
    if not isinstance(sources, list) or len(sources) != required:
        raise ValueError("六维验证缺少原始同步引用帧")
    by_id = {}
    for frame in sources:
        if (not isinstance(frame, dict) or not _frame_id(frame.get("frame_id")) or
                frame.get("source_kind") != "hardware" or frame.get("stream_epoch") != value["force_epoch"] or
                frame.get("axes") != AXES or frame.get("units") != UNITS or frame.get("valid") is False or
                frame.get("stale") is True or frame.get("time_quality") not in {"host_rx_approx", "hardware_mapped"}):
            raise ValueError("六维原始引用帧无效或跨重连")
        _vector(frame.get("force_values"), 6, "原始六轴")
        t = frame.get("host_rx_monotonic_ns")
        if type(t) is not int or check_clock and not 0 <= (time.perf_counter_ns()-t)/1e6 <= 250:
            raise ValueError("六维原始引用时间无效或过期")
        by_id[frame["frame_id"]] = frame
    if set(by_id) != set(ids):
        raise ValueError("六维原始引用身份不一致")
    frames = [by_id[key] for key in ids]
    if required == 1:
        if frames[0]["host_rx_monotonic_ns"] != stamp:
            raise ValueError("精确六维同步时间不一致")
        expected = frames[0]["force_values"]
    else:
        left, right = [frame["host_rx_monotonic_ns"] for frame in frames]
        if not left < stamp < right or right-left > 60_000_000 or max(stamp-left, right-stamp) > 45_000_000:
            raise ValueError("六维同步超出 timed_wrench_sync_v1 端点或间隔范围")
        ratio = (stamp-left)/(right-left)
        expected = [a+(b-a)*ratio for a,b in zip(frames[0]["force_values"], frames[1]["force_values"])]
    close = lambda a,b: math.isclose(a,b,rel_tol=0,abs_tol=1e-9)
    if any(not close(a,b) for a,b in zip(aligned,expected)) or any(not close(a-z,t) for a,z,t in zip(aligned,zero,truth)):
        raise ValueError("六维对照与原始六轴或固定零偏不一致")
    if any(not close(p-t,e) for p,t,e in zip(predicted,truth,errors)):
        raise ValueError("六维误差与冻结预测和对照不一致")
    if math.sqrt(sum(x*x for x in truth[:3])) <= .05:
        raise ValueError("无载不能作为一次按压验证")
    reference = value.get("spectral_reference")
    if not isinstance(reference,dict) or reference.get("reference_id") != value["reference_id"] or reference.get("stream_epoch") != value["optical_epoch"]:
        raise ValueError("六维验证缺少对应的 S0 参考")
    drift = prediction.get("input_kind") == "delta_lambda_9"
    if drift:
        from .drift_features import validate_drift_reference, validate_drift_frame, drift_pm
        validate_drift_reference(reference)
        if reference.get("trigger") != "operator_unloaded_confirmation":
            raise ValueError("九路验证需要本次独立明确无载参考")
        times = reference["source_timestamps_ns"]
        if times[-1]-times[0] < 2_000_000_000 or any(not 0 < b-a <= 120_000_000 for a,b in zip(times,times[1:])):
            raise ValueError("九路验证无载参考需要 2 秒连续真实源帧")
    else:
        _vector(reference.get("S0"),512,"S0")
        _vector(reference.get("wavelength_nm"),512,"真实波长轴")
    window = value.get("window")
    if not isinstance(window,list) or len(window) != 16:
        raise ValueError("六维验证需要完整的真实 16 帧窗口")
    previous = None
    seen = set()
    maximum_gap_ns = 120_000_000
    if drift:
        rule = prediction.get("window_rule", {})
        maximum = rule.get("max_gap_ms", 120)
        if not _finite(maximum) or not 40 <= maximum <= 120:
            raise ValueError("九路验证缺少可信连续窗口门限")
        maximum_gap_ns = int(maximum*1e6)
    for frame in window:
        if drift:
            validate_drift_frame(frame)
            expected_drift = drift_pm(frame, reference)
            if frame.get("host_rx_monotonic_ns", 0) < reference["effective_ns"]:
                reference_sources = dict(zip(reference["source_frame_ids"], reference["source_timestamps_ns"]))
                if reference_sources.get(frame.get("frame_id")) != frame.get("host_rx_monotonic_ns"):
                    raise ValueError("参考生效前的九路窗口必须来自同次无载参考源帧")
            if frame.get("delta_lambda_pm") is not None and any(not close(a,b) for a,b in zip(
                    _vector(frame["delta_lambda_pm"],9,"九路漂移"), expected_drift)):
                raise ValueError("冻结九路漂移与真实峰心、固定无载参考不一致")
        else:
            _vector(frame.get("intensity"),512,"原始全谱")
        t, fid = frame.get("host_rx_monotonic_ns"), frame.get("frame_id")
        if (frame.get("stream_epoch") != value["optical_epoch"] or not _frame_id(fid) or fid in seen or
                type(t) is not int or (not drift and t < reference.get("effective_ns", 0)) or previous is not None and not 0 < t-previous <= maximum_gap_ns):
            raise ValueError("六维验证窗口跨断流、参考或包含重复身份")
        seen.add(fid)
        previous = t
    if window[-1]["frame_id"] != value["frame_id"] or previous != stamp:
        raise ValueError("六维验证窗口末帧与预测不同")
    if drift and stamp < reference["effective_ns"]:
        raise ValueError("九路验证末帧早于固定参考生效时间")
    tolerances = value.get("wrench_tolerances")
    if not isinstance(tolerances,list) or len(tolerances) != 6 or any(x is not None and (not _finite(x) or x <= 0) for x in tolerances):
        raise ValueError("六轴容差须按 N/N·m 分别填写有限正数或留空")
    _canonical(value)
    return value


def inference_valid(prediction):
    return bool(prediction.get("valid") and prediction.get("status") == "有效估计" and
                prediction.get("point_id") in {f"P{i:02d}" for i in range(1,26)})


def sample_fields(value):
    return {"schema": "spectral_manual_sample_v1", "absolute_error_N": None,
            "absolute_wrench_error": [abs(x) for x in value["prediction"]["wrench_error"]],
            "wrench_tolerances": deepcopy(value["wrench_tolerances"]), "axes": AXES, "units": UNITS,
            "force_tolerance_N": None}


def judgment_values(sample, position_correct, force_correct, actual_point_id):
    from .validation import _target_point
    if type(position_correct) is not bool or type(force_correct) is not bool:
        raise ValueError("位置与六轴力判断须明确选择真或假")
    _target_point(actual_point_id)
    target = sample.get("target_point_id")
    if target is not None:
        if actual_point_id is not None and actual_point_id != target:
            raise ValueError("冻结目标不能改为另一点位")
        actual_point_id = target
    if position_correct and (not sample["inference_valid"] or sample["prediction"].get("point_id") != actual_point_id):
        raise ValueError("冻结定位与实际点位不一致，不能标为位置正确")
    tolerances = sample["wrench_tolerances"]
    if force_correct and (any(x is None for x in tolerances) or any(
            error > tolerance and not math.isclose(error,tolerance,rel_tol=0,abs_tol=1e-12)
            for error,tolerance in zip(sample["absolute_wrench_error"],tolerances))):
        raise ValueError("六轴容差须全部配置且逐轴通过，才能标为六维力正确")
    return {"position_correct": position_correct,"force_correct": force_correct,
            "joint_correct": position_correct and force_correct,"actual_point_id": actual_point_id}
