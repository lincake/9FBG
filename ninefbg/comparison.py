"""Offline comparison on the very same original frames; never an online fallback.

Frames must carry explicit phase annotations and unloaded confirmation to
reconstruct references. Declaring data simulated can never satisfy the real
measurement training gate. All output definitions and configuration hashes are
persistable and intentionally distinct.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import time

import numpy as np
from scipy.optimize import least_squares

from .tracking import (ALGORITHM_VERSION, TEMPLATE_ALGORITHM_VERSION, PeakTracker,
                       digest, noise_sigma, validated_config)

UNLOADED_PHASES = {"unloaded", "baseline", "no_load", "无载"}
PRESS_PHASES = {"press", "loading", "holding", "按压", "加载", "保持"}
TIMING_SCOPE = {
    "metric_id": "nine_peak_local_processing_v1",
    "summary_processing_ms": "每帧九峰局部处理耗时；两算法使用同一计时范围",
    "includes": ["逐峰局部数据选择", "本算法拟合", "逐峰质量检查", "局部诊断曲线生成", "上一有效参数或位移更新"],
    "excludes": ["设备读取", "全局波长轴与输入链审核", "全局粗定位", "参考稳定检查与提交", "结果存储和界面绘制"],
    "gaussian_source": "PeakTracker.peak_processing_ms",
    "template_source": "九峰局部模板处理循环的实际单调计时",
    "gaussian_end_to_end_field": "tracker_end_to_end_ms（另存，不用于两算法局部耗时比较）",
    "timing_note": "离线同批帧的本机实际运行耗时，不能当作设备采样周期或端到端实时性能；不据单次无载耗时宣称整体速度优劣。",
}


def _is_real(frame):
    return frame.get("is_real") is True or frame.get("source_kind") in {"hardware", "real_device", "actual"}


def _template_fit(axis, values, template, previous_shift, config, saturation=None):
    low, high = template["window"]["low_nm"], template["window"]["high_nm"]
    # Use the measured FBG width to choose local current samples; an edge FBG
    # must not accidentally fit half the instrument's spectrum. The frozen
    # template retains the full identity window for shifted interpolation.
    center_seed = template["reference_nm"] + previous_shift
    radius = config["fit_radius_sigma"] * template["sigma_nm"]
    fit_low, fit_high = max(low, center_seed - radius), min(high, center_seed + radius)
    mask = (axis >= fit_low) & (axis <= fit_high)
    x, y = axis[mask], values[mask]
    reference_x, reference_y = np.asarray(template["x"]), np.asarray(template["y"])
    if len(x) < 5 or len(reference_x) < 5:
        return {"valid": False, "failure_reason": "局部模板原始采样点不足 5 个", "stale": False}
    if saturation is not None and np.any(y >= saturation):
        return {"valid": False, "failure_reason": "局部光谱达到设备饱和阈值", "stale": False}
    step = float(np.median(np.diff(x)))
    scale = max(float(np.ptp(reference_y)), np.finfo(float).eps)
    background = float(np.min(reference_y))
    source = (reference_y - background) / scale
    observed = (y - background) / scale
    limit = min(template["reference_nm"] - low, high - template["reference_nm"])
    confirmed = template["window"].get("confirmed_drift_nm")
    if confirmed is not None:
        limit = min(limit, confirmed)
    # Optimize a continuous shift in units of the actual local sampling step.
    # Linear interpolation evaluates the reference; no grid spacing is offered
    # as a precision estimate.
    bounds = ([-2.0, 0.05, -limit / step], [3.0, 5.0, limit / step])

    def model(p):
        return p[0] + p[1] * np.interp(x - p[2] * step, reference_x, source,
                                      left=source[0], right=source[-1])

    try:
        initial = np.clip([0.0, 1.0, previous_shift / step], np.asarray(bounds[0]) + 1e-9, np.asarray(bounds[1]) - 1e-9)
        result = least_squares(lambda p: model(p) - observed, initial, bounds=bounds,
                               max_nfev=80, ftol=1e-8, xtol=1e-8, gtol=1e-8)
    except (ValueError, FloatingPointError, RuntimeError) as exc:
        return {"valid": False, "failure_reason": "局部参考光谱位移拟合失败：" + str(exc), "stale": False}
    shift = float(result.x[2] * step)
    residual = float(np.sqrt(np.mean((model(result.x) - observed) ** 2)) * scale)
    amplitude = float(result.x[1] * np.ptp(reference_y))
    coverage = float(np.mean((x - shift >= reference_x[0]) & (x - shift <= reference_x[-1])))
    reason = None
    if not result.success:
        reason = "局部参考光谱位移拟合未收敛"
    elif residual / max(amplitude, 1e-12) > config["maximum_relative_residual"]:
        reason = "局部参考光谱位移残差过大，当前峰形与冻结模板不相容"
    elif coverage < .8:
        reason = "移位后的原始模板覆盖不足"
    elif abs(shift - previous_shift) > config["maximum_jump_samples"] * step:
        reason = "局部参考光谱位移相对上一有效帧异常跳变"
    elif abs(shift) >= limit * .999:
        reason = "局部参考光谱位移到达固定范围边界"
    return {"center_nm": template["reference_nm"] + shift, "drift_nm": shift,
            "amplitude": amplitude, "residual": residual,
            "relative_residual": residual / max(amplitude, 1e-12),
            "valid": reason is None, "failure_reason": reason, "stale": False,
            "template_coverage": coverage, "template_reference_version": template["reference_version"],
            "fit_sample_count": len(x), "fit_x": x.tolist(),
            "fit_y": (model(result.x) * scale + background).tolist(),
            "fit_availability": "冻结原始局部模板的位移、幅度与背景三参数拟合",
            "interpolation_method": "原始设备波长点的线性插值", "measurement_precision_verified": False}


def _summary(records):
    values = [peak for row in records for peak in row["peaks"]]
    valid = [peak for peak in values if peak.get("valid") is True]
    by_peak = []
    for index in range(9):
        # Keep baseline segments apart. Pool within each reference version;
        # never call loading/holding variation measurement repeatability.
        groups = {}
        for row in records:
            if row["phase"] in UNLOADED_PHASES and len(row["peaks"]) == 9:
                peak = row["peaks"][index]
                if peak.get("valid") and peak.get("center_nm") is not None:
                    groups.setdefault(row["reference_version"], []).append(peak["center_nm"])
        repeatability = [{"reference_version": key, "frames": len(group),
                          "unloaded_center_std_nm": float(np.std(group, ddof=1)) if len(group) >= 2 else None}
                         for key, group in groups.items()]
        by_peak.append({"id": f"FBG{index + 1}", "unloaded_repeatability_segments": repeatability,
                        "repeatability_note": "无载同一参考段的时序标准差；需要重复按压试验才能给出加载重复性"})
    durations = [row["processing_ms"] for row in records]
    reasons = {}
    for peak in values:
        if peak.get("failure_reason"):
            reason = peak["failure_reason"]
            reasons[reason] = reasons.get(reason, 0) + 1
    return {"evaluated_peak_frames": len(values), "valid_peak_frames": len(valid),
            "valid_rate": len(valid) / len(values) if values else None,
            "complete_valid_frame_rate": sum(len(row["peaks"]) == 9 and all(p.get("valid") for p in row["peaks"]) for row in records) / len(records) if records else None,
            "processing_mean_ms": float(np.mean(durations)) if durations else None,
            "processing_p95_ms": float(np.percentile(durations, 95)) if durations else None,
            "processing_timing_scope": TIMING_SCOPE["metric_id"],
            "relative_residual_median": float(np.median([p["relative_residual"] for p in valid if p.get("relative_residual") is not None])) if any(p.get("relative_residual") is not None for p in valid) else None,
            "failure_reasons": reasons, "per_peak": by_peak,
            "shape_adaptability_note": "根据同一原始帧的残差、有效率和失败原因比较；缺少真实变形试验时不能宣称峰形适应性通过。"}


def compare_algorithms(frames, config=None):
    """Replay annotated raw frames and return both definitions and evidence gates.

    At the start of an unloaded segment set ``unloaded_confirmed=True`` on the
    confirmation frame. ``phase`` is unloaded/press/loading/holding/recovery.
    Real provenance uses ``source_kind='hardware'`` and preferably an original
    recording path/hash. Merely marking data real is operator evidence, not a
    device certification; acceptance remains explicit.
    """
    config = validated_config(config)
    tracker = PeakTracker(config)
    history = deque(maxlen=config["stable_frames"])
    templates = []
    shifts = [0.0] * 9
    gaussian_records, template_records = [], []
    raw_keys = []
    real_unloaded = real_press = real_comparable_unloaded = real_comparable_press = 0
    synthetic = unknown = 0
    errors = []
    phase_rejections = []
    template_reference_version = 0
    active_press = False
    press_template_versions = []
    template_build_history = []
    for original in frames:
        frame = deepcopy(original)
        phase = str(frame.get("phase", "unknown"))
        is_real = _is_real(frame)
        real_unloaded += int(is_real and phase in UNLOADED_PHASES)
        real_press += int(is_real and phase in PRESS_PHASES)
        synthetic += int(frame.get("source_kind") in {"simulation", "synthetic", "mock", "demo"} or frame.get("is_real") is False)
        unknown += int(not is_real and frame.get("source_kind") not in {"simulation", "synthetic", "mock", "demo"} and frame.get("is_real") is not False)
        if active_press and phase not in PRESS_PHASES:
            tracker.end_press()
            active_press = False
        if frame.get("unloaded_confirmed") is True:
            if phase not in UNLOADED_PHASES:
                errors.append({"frame_id": frame.get("frame_id"), "reason": "无载确认帧未标记为无载阶段"})
            else:
                tracker.request_reference(True)
                history.clear()
        if phase in PRESS_PHASES and not active_press:
            try:
                tracker.begin_press()
                active_press = True
                press_template_versions.append(template_reference_version)
            except ValueError as exc:
                errors.append({"frame_id": frame.get("frame_id"), "reason": str(exc)})
                phase_rejections.append({"frame_id": deepcopy(frame.get("frame_id")),
                                         "stream_epoch": deepcopy(frame.get("stream_epoch")),
                                         "reason": str(exc),
                                         "action": "拒绝该按压帧并取消未完成的无载参考申请"})
        measured = tracker.process(frame)
        raw_keys.append({"frame_id": deepcopy(frame.get("frame_id")), "stream_epoch": deepcopy(frame.get("stream_epoch")),
                         "host_rx_monotonic_ns": frame.get("host_rx_monotonic_ns"),
                         "raw_sha256": digest({"wavelength_nm": frame.get("wavelength_nm"), "intensity": frame.get("intensity")})})
        if phase in UNLOADED_PHASES and measured["state"] != "input_invalid":
            history.append(frame)
        # Only an explicit, completed reference check can replace the template.
        # During a press the reference and the spectrum template both freeze.
        if measured["reference_version"] > template_reference_version and measured["windows"] and not active_press:
            if len(history) == config["stable_frames"]:
                axis = np.asarray(frame["wavelength_nm"], dtype=float)
                intensity = np.median(np.asarray([item["intensity"] for item in history]), axis=0)
                templates = []
                for index, window in enumerate(measured["windows"]):
                    mask = (axis >= window["low_nm"]) & (axis <= window["high_nm"])
                    templates.append({"id": window["id"], "window": deepcopy(window),
                                      "x": axis[mask].tolist(), "y": intensity[mask].tolist(),
                                      "reference_nm": measured["reference_nm"][index],
                                      "sigma_nm": measured["peaks"][index]["sigma_nm"],
                                      "reference_version": measured["reference_version"],
                                      "source_frame_ids": [deepcopy(item.get("frame_id")) for item in history]})
                template_reference_version = measured["reference_version"]
                template_build_history.append({"reference_version": template_reference_version,
                                               "stream_epoch": deepcopy(frame.get("stream_epoch")),
                                               "source_frame_ids": [deepcopy(item.get("frame_id")) for item in history]})
                shifts = [0.0] * 9
        if not measured["windows"]:
            templates = []
        # Evaluate only frames on which both methods have a declared reference.
        if not templates:
            continue
        base = {"frame_id": deepcopy(frame.get("frame_id")), "stream_epoch": deepcopy(frame.get("stream_epoch")),
                "host_rx_monotonic_ns": frame.get("host_rx_monotonic_ns"),
                "timestamp_source": deepcopy(frame.get("timestamp_source")), "time_quality": deepcopy(frame.get("time_quality")),
                "phase": phase, "reference_version": measured["reference_version"]}
        gaussian_records.append({**deepcopy(base), "peaks": deepcopy(measured["peaks"]),
                                 "processing_ms": measured["peak_processing_ms"],
                                 "processing_timing_scope": TIMING_SCOPE["metric_id"],
                                 "tracker_end_to_end_ms": measured["processing_ms"]})
        rows = []
        saturation = config["saturation_level"]
        if saturation is None and isinstance(frame.get("preprocessing"), dict):
            saturation = frame["preprocessing"].get("saturation_level")
        # The Gaussian tracker converts/validates the full raw vectors before
        # its local timer. Do the template's same vector conversion outside its
        # local timer too, rather than charging nine repeated conversions.
        fit_axis = np.asarray(frame["wavelength_nm"], dtype=float)
        fit_values = np.asarray(frame["intensity"], dtype=float)
        started = time.perf_counter_ns()
        for index, template in enumerate(templates):
            if measured["state"] == "input_invalid":
                row = {"valid": False, "stale": False, "failure_reason": measured["message"]}
            else:
                row = _template_fit(fit_axis, fit_values, template, shifts[index], config, saturation)
            row["id"] = template["id"]
            if row.get("valid"):
                shifts[index] = row["drift_nm"]
            rows.append(row)
        template_processing_ms = (time.perf_counter_ns() - started) / 1e6
        template_records.append({**deepcopy(base), "peaks": rows, "processing_ms": template_processing_ms,
                                 "processing_timing_scope": TIMING_SCOPE["metric_id"],
                                 "template_reference_version": template_reference_version})
        both_valid = all(p.get("valid") for p in measured["peaks"]) and len(measured["peaks"]) == 9 and all(p.get("valid") for p in rows)
        real_comparable_unloaded += int(is_real and phase in UNLOADED_PHASES and both_valid)
        real_comparable_press += int(is_real and phase in PRESS_PHASES and both_valid)
    evaluated_template_build_frames = []
    for build in template_build_history:
        for row in template_records:
            if (row["template_reference_version"] == build["reference_version"]
                    and row["stream_epoch"] == build["stream_epoch"]
                    and row["frame_id"] in build["source_frame_ids"]):
                evaluated_template_build_frames.append({"frame_id": deepcopy(row["frame_id"]),
                                                        "stream_epoch": deepcopy(row["stream_epoch"]),
                                                        "template_reference_version": row["template_reference_version"]})
    report = {"report_version": "1.1.0", "config": deepcopy(config), "config_version": tracker.config_version,
              "original_frame_count": len(raw_keys), "compared_frame_count": len(gaussian_records),
              "same_original_frames": True, "source_manifest_sha256": digest(raw_keys), "source_frames": raw_keys,
              "algorithms": {ALGORITHM_VERSION: {"center_definition": "带常数背景的受约束高斯中心 μ（nm）",
                                                 "summary": _summary(gaussian_records), "frames": gaussian_records},
                             TEMPLATE_ALGORITHM_VERSION: {"center_definition": "冻结无载模板的参考波长加局部光谱拟合位移（nm）",
                                                          "summary": _summary(template_records), "frames": template_records}},
              "reference_history": tracker.export_state()["reference_history"],
              "template_press_versions": press_template_versions, "errors": errors,
              "phase_rejections": phase_rejections, "timing_scope": deepcopy(TIMING_SCOPE),
              "evidence": {"real_unloaded_frames": real_unloaded, "real_press_frames": real_press,
                           "real_comparable_unloaded_frames": real_comparable_unloaded,
                           "real_comparable_press_frames": real_comparable_press,
                           "synthetic_frames": synthetic, "unknown_provenance_frames": unknown,
                           "operator_acceptance_required": True},
              "method_selected": None, "training_ready": False,
              "measurement_precision_verified": False,
              "independent_holdout": False,
              "template_build_frames_in_evaluation": bool(evaluated_template_build_frames),
              "template_build_frames_evaluated": evaluated_template_build_frames,
              "template_build_history": template_build_history,
              "notes": ["算法比较不代表现场验收；真实数据来源与试验条件必须可追溯。",
                        "模板建立帧可能同时参与本报告评价；这是同批诊断比较，并非独立留出验证。",
                        "插值步长与显示位数不是波长测量精度。",
                        "更换算法或配置后须重新处理历史原始光谱并重新训练，部署不得静默切换中心定义。"]}
    return report


def training_gate(report, selected_algorithm, config_version=None, *, operator_accepted=False):
    """Return a persistable method lock only after actual comparison acceptance."""
    reasons = []
    evidence = report.get("evidence", {})
    if not report.get("same_original_frames") or not report.get("compared_frame_count"):
        reasons.append("需要同一批原始光谱完成两种算法比较。")
    if evidence.get("real_comparable_unloaded_frames", 0) < 2:
        reasons.append("缺少足够的真实无载同批有效比较帧。")
    if evidence.get("real_comparable_press_frames", 0) < 2:
        reasons.append("缺少足够的真实按压同批有效比较帧。")
    if evidence.get("synthetic_frames", 0) or evidence.get("unknown_provenance_frames", 0):
        reasons.append("正式训练比较批次不能混入仿真或来源不明的数据。")
    if report.get("phase_rejections"):
        reasons.append("比较批次存在未通过初始化或回零门槛的按压帧；请核实阶段记录并重新完成比较。")
    if not report.get("config", {}).get("confirmed_drift_nm") or not report.get("config", {}).get("drift_confirmation_evidence"):
        reasons.append("跟踪漂移范围尚未由真实按压数据确认并记录来源。")
    if selected_algorithm not in {ALGORITHM_VERSION, TEMPLATE_ALGORITHM_VERSION} or selected_algorithm not in report.get("algorithms", {}):
        reasons.append("必须明确选择本报告中已比较的算法定义。")
    if config_version is not None and config_version != report.get("config_version"):
        reasons.append("训练配置版本与算法比较报告不一致。")
    if operator_accepted is not True:
        reasons.append("需要操作员审阅真实数据的重复性、有效率、峰形适应性与耗时后确认。")
    ready = not reasons
    return {"ready": ready, "reasons": reasons, "algorithm_version": selected_algorithm if ready else None,
            "config_version": report.get("config_version") if ready else None,
            "source_manifest_sha256": report.get("source_manifest_sha256") if ready else None,
            "algorithm_definition_frozen": ready,
            "deployment_requirement": "部署必须核对完全相同的算法与配置版本；改变提峰算法须重处理原始历史数据并重新训练。"}
