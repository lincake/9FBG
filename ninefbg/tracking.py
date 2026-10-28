"""Nine fixed-identity FBGs, measured-axis fits, and explicit reference lifecycle.

Only the coarse locator may smooth. Every Gaussian residual is computed from
the supplied, unsmoothed samples. No hardware, files, or deployment models are
opened here. Source timestamps are copied unchanged; fit completion is timing
diagnostic information and is never the sample timestamp.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import time

import numpy as np
from scipy.optimize import least_squares
from scipy.signal import find_peaks, savgol_filter

ALGORITHM_VERSION = "gaussian_background_fixed_identity_1.0.0"
TEMPLATE_ALGORITHM_VERSION = "local_reference_spectrum_shift_1.0.0"
AUTO_REFERENCE_SOURCE = "optical_connect_auto_v1"
MANUAL_REFERENCE_SOURCE = "operator_unloaded_confirmation_v1"
DEFAULT_CONFIG = {
    "sample_count": 512, "expected_peaks": 9,
    "stable_frames": 8, "prominence_noise_sigma": 6.0,
    "minimum_prominence": 0.0, "relative_prominence": 0.05,
    "minimum_width_samples": 1.2, "minimum_distance_samples": 3,
    "distance_width_fraction": 1.0, "smooth_locator": True,
    "smoothing_width_fraction": 0.5, "maximum_smoothing_window": 7,
    "stable_center_samples": 0.30, "stable_amplitude_fraction": 0.20,
    "minimum_sigma_samples": 0.35, "fit_radius_sigma": 3.5,
    "maximum_relative_residual": 0.15, "maximum_jump_samples": 2.5,
    "maximum_width_ratio": 2.5, "window_margin_samples": 0.5,
    "uniform_axis_relative_tolerance": 0.0001,
    "confirmed_drift_nm": None, "drift_confirmation_evidence": None,
    "saturation_level": None,
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def validated_config(config=None):
    supplied = deepcopy(config or {})
    unknown = sorted(set(supplied) - set(DEFAULT_CONFIG))
    if unknown:
        raise ValueError("未知提峰配置：" + "、".join(unknown))
    result = {**deepcopy(DEFAULT_CONFIG), **supplied}
    if result["sample_count"] != 512 or result["expected_peaks"] != 9:
        raise ValueError("9FBG 要求 512 个原始采样点及 9 个光栅。")
    for name in ("stable_frames", "minimum_distance_samples", "maximum_smoothing_window"):
        if type(result[name]) is not int or result[name] < (3 if name == "stable_frames" else 1):
            raise ValueError("配置必须是合适的正整数：" + name)
    if result["maximum_smoothing_window"] % 2 != 1:
        raise ValueError("粗定位平滑窗口必须是奇数。")
    for name, value in result.items():
        if name in {"confirmed_drift_nm", "drift_confirmation_evidence", "saturation_level"}:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value) or value < 0:
                raise ValueError("配置必须有限且非负：" + name)
    for name in ("prominence_noise_sigma", "minimum_width_samples", "minimum_sigma_samples",
                 "fit_radius_sigma", "maximum_relative_residual", "maximum_jump_samples",
                 "stable_center_samples", "distance_width_fraction", "smoothing_width_fraction"):
        if result[name] <= 0:
            raise ValueError("配置必须大于零：" + name)
    if result["maximum_width_ratio"] <= 1 or not 0 <= result["relative_prominence"] < 1:
        raise ValueError("峰宽比例或相对显著度配置不正确。")
    drift = result["confirmed_drift_nm"]
    if drift is not None:
        if isinstance(drift, (int, float)) and not isinstance(drift, bool):
            drift = [float(drift)] * 9
        if not isinstance(drift, (list, tuple)) or len(drift) != 9 or any(
                not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in drift):
            raise ValueError("已确认漂移范围应为正数或 9 个正数，单位 nm。")
        if not result["drift_confirmation_evidence"]:
            raise ValueError("声明实测漂移范围时必须提供真实数据的确认记录。")
        result["confirmed_drift_nm"] = list(map(float, drift))
    saturation = result["saturation_level"]
    if saturation is not None and (not isinstance(saturation, (int, float)) or isinstance(saturation, bool)
                                    or not math.isfinite(saturation)):
        raise ValueError("饱和阈值必须是设备提供的有限数值。")
    if type(result["smooth_locator"]) is not bool:
        raise ValueError("粗定位平滑开关必须为布尔值。")
    return result


def noise_sigma(y):
    differences = np.diff(y)
    return float(np.median(np.abs(differences - np.median(differences))) / (0.6744897501960817 * math.sqrt(2)))


def analyze_axis(axis):
    if axis.shape != (512,) or not np.isfinite(axis).all():
        raise ValueError("波长轴必须含有 512 个有限的实际设备校准值。")
    spacing = np.diff(axis)
    if not np.all(spacing > 0):
        raise ValueError("设备波长轴必须严格递增，不能按显示图像估算或排序后冒充原轴。")
    median = float(np.median(spacing))
    deviation = float(np.max(np.abs(spacing - median)) / median)
    return {"points": 512, "minimum_nm": float(axis[0]), "maximum_nm": float(axis[-1]),
            "median_step_nm": median, "minimum_step_nm": float(np.min(spacing)),
            "maximum_step_nm": float(np.max(spacing)), "relative_step_deviation": deviation,
            "axis_sha256": digest(axis.tolist()), "fit_coordinate": "实际波长轴（nm）"}


def locate_candidates(axis, values, config):
    """Noise, prominence, width and data-derived separation; never a top-nine cut."""
    noise = noise_sigma(values)
    scale = max(1.0, float(np.max(np.abs(values))))
    # A single saturated pixel must not raise the relative threshold so far
    # that the other eight healthy FBGs disappear from their own windows.
    relative_range = float(np.percentile(values, 99) - np.percentile(values, 10))
    threshold = max(config["minimum_prominence"], config["prominence_noise_sigma"] * noise,
                    config["relative_prominence"] * relative_range, np.finfo(float).eps * scale * 32)
    indices, properties = find_peaks(values, prominence=threshold,
                                     width=(config["minimum_width_samples"], None), plateau_size=(None, None))
    median_width = float(np.median(properties["widths"])) if len(indices) else None
    smoothing = 1
    if config["smooth_locator"] and median_width is not None:
        smoothing = min(config["maximum_smoothing_window"], int(median_width * config["smoothing_width_fraction"]))
        smoothing = max(1, smoothing if smoothing % 2 else smoothing - 1)
    coarse = savgol_filter(values, smoothing, min(2, smoothing - 1), mode="interp") if smoothing >= 3 else values
    distance = max(config["minimum_distance_samples"],
                   int(math.ceil(median_width * config["distance_width_fraction"])) if median_width else 1)
    coarse_indices, _ = find_peaks(coarse, prominence=threshold * 0.8)
    candidates = []
    rejected = []
    for k, index in enumerate(indices):
        row = {"index": int(index), "center_nm": float(axis[index]),
               "prominence": float(properties["prominences"][k]),
               "width_samples": float(properties["widths"][k]),
               "fwhm_nm": float(np.interp(properties["right_ips"][k], np.arange(512), axis)
                                  - np.interp(properties["left_ips"][k], np.arange(512), axis)),
               "plateau_samples": int(properties["plateau_sizes"][k])}
        if not len(coarse_indices) or np.min(np.abs(coarse_indices - index)) > max(1, smoothing // 2):
            rejected.append({**row, "reason": "粗定位与原始采样峰位置不一致"})
        else:
            candidates.append(row)
    kept = []
    for row in sorted(candidates, key=lambda item: -item["prominence"]):
        if all(abs(row["index"] - other["index"]) >= distance for other in kept):
            kept.append(row)
        else:
            rejected.append({**row, "reason": "峰间距小于基于实测峰宽的距离门限，可能重叠"})
    kept.sort(key=lambda item: item["index"])
    return kept, {"noise_sigma": noise, "prominence_threshold": threshold,
                  "relative_signal_range": relative_range,
                  "median_width_samples": median_width, "minimum_distance_samples": distance,
                  "smoothing_window": smoothing, "smoothing_applies_to_fit": False,
                  "candidate_count": len(kept), "rejected": rejected,
                  "count_forced": False}


class PeakTracker:
    def __init__(self, config: dict | None = None):
        self.config = validated_config(config)
        self.config_version = digest(self.config)
        self.algorithm_version = ALGORITHM_VERSION
        self._windows = []
        self._anchors = []
        self._reference = []
        self._reference_version = 0
        self._reference_history = []
        self._events = []
        self._last_valid = {}
        self._stable = deque(maxlen=self.config["stable_frames"])
        self._pending_reference = False
        self._pending_reference_source = None
        self._pending_unloaded_confirmed = False
        self._press = False
        self._binding = None
        self._last_identity = None
        self._last_time = None
        self._latest = None
        self._processed = self._duplicates = self._invalid = self._sequence_gaps = 0
        self._durations = deque(maxlen=300)
        self._intervals = deque(maxlen=300)
        self._last_device_sequence = None

    def _event(self, action, **fields):
        self._events.append({"utc": datetime.now(timezone.utc).isoformat(), "action": action, **deepcopy(fields)})

    def request_reference(self, unloaded_confirmed: bool) -> None:
        if unloaded_confirmed is not True:
            raise ValueError("请移开接触棒并明确确认无载荷后，再申请初始化或回零。")
        if self._press:
            raise ValueError("一次按压期间参考冻结，请先结束按压并移开接触棒。")
        self._pending_reference = True
        self._pending_reference_source = MANUAL_REFERENCE_SOURCE
        self._pending_unloaded_confirmed = True
        self._stable.clear()
        self._event("无载确认", reference_version=self._reference_version,
                    reference_source=MANUAL_REFERENCE_SOURCE, unloaded_confirmed=True,
                    purpose="回零" if self._anchors else "首次初始化")

    def request_connection_reference(self) -> None:
        """Queue one initial optical reference, without asserting no load.

        AcquisitionService calls this once on a fresh hardware connection. A
        changed binding or a completed request cannot silently request another
        zero; the operator's explicit reference path remains independent.
        """
        if self._processed or self._binding is not None or self._pending_reference or self._reference_version:
            raise ValueError("连接自动参考只能在本次光谱连接开始、尚未处理源帧时申请。")
        self._pending_reference = True
        self._pending_reference_source = AUTO_REFERENCE_SOURCE
        self._pending_unloaded_confirmed = False
        self._stable.clear()
        self._event("光谱连接自动参考申请", reference_version=self._reference_version,
                    reference_source=AUTO_REFERENCE_SOURCE, unloaded_confirmed=False)

    def begin_press(self) -> None:
        if not self._anchors or not self._reference:
            raise ValueError("请先确认无载荷并完成 9 峰稳定初始化。")
        if self._pending_reference:
            raise ValueError("正在检查无载稳定条件，请完成回零后再按压。")
        if not self._latest or not all(p.get("valid") for p in self._latest.get("peaks", [])):
            raise ValueError("当前 9 峰并非全部有效，不能开始记录按压。")
        self._press = True
        self._event("开始按压", reference_version=self._reference_version)

    def end_press(self) -> None:
        self._press = False
        self._event("结束按压", reference_version=self._reference_version)

    def _clear_binding(self, reason):
        self._event("采集条件变化", reason=reason, previous_reference_version=self._reference_version)
        self._anchors, self._windows, self._reference = [], [], []
        self._last_valid.clear()
        self._stable.clear()
        self._pending_reference = False
        self._pending_reference_source = None
        self._pending_unloaded_confirmed = False
        self._press = False
        self._last_identity = self._last_time = self._last_device_sequence = None

    def _stale(self, index, reason, stamp):
        previous = self._last_valid.get(index)
        row = deepcopy(previous["row"]) if previous else {
            "id": f"FBG{index + 1}", "center_nm": None, "drift_nm": None, "amplitude": None,
            "fwhm_nm": None, "prominence": None, "residual": None, "fit_x": [], "fit_y": []}
        age = max(0.0, (stamp - previous["timestamp"]) / 1e6) if previous and stamp is not None else None
        row.update(valid=False, failure_reason=reason, stale=bool(previous), age_ms=age,
                   value_frame_id=previous["frame_id"] if previous else None,
                   quality=[reason], fit_availability="上一有效帧旧值" if previous else "不可用")
        return row

    def _fit(self, axis, values, candidate, window, previous, noise, saturation):
        config = self.config
        coarse_mu = candidate["center_nm"]
        index = candidate["index"]
        local_step = float(np.median(np.diff(axis[max(0, index - 2):min(512, index + 3)])))
        observed_sigma = max(candidate["fwhm_nm"] / 2.354820045, config["minimum_sigma_samples"] * local_step)
        radius = config["fit_radius_sigma"] * max(observed_sigma, previous.get("sigma_nm", 0) if previous else 0)
        low, high = max(window["low_nm"], coarse_mu - radius), min(window["high_nm"], coarse_mu + radius)
        mask = (axis >= low) & (axis <= high)
        local_x, raw_y = axis[mask], values[mask]
        n = len(local_x)
        if n < 3:
            return None, "局部原始采样点少于 3 个，无法拟合峰心"
        if saturation is not None and np.any(raw_y >= saturation):
            return None, "局部光谱达到设备饱和阈值"
        if candidate["plateau_samples"] >= 3:
            return None, "峰顶连续平台，疑似饱和或严重变形"
        origin = coarse_mu
        x = (local_x - origin) / local_step
        offset = float(np.min(raw_y))
        scale = max(float(np.ptp(raw_y)), candidate["prominence"], np.finfo(float).eps)
        y = (raw_y - offset) / scale
        mu_min = (window["low_nm"] - origin) / local_step + config["window_margin_samples"]
        mu_max = (window["high_nm"] - origin) / local_step - config["window_margin_samples"]
        if mu_max <= mu_min:
            return None, "固定跟踪范围不足以容纳峰心安全边界"
        sigma_max = max(config["minimum_sigma_samples"] * 1.01, (window["high_nm"] - window["low_nm"]) / local_step / 2)
        if previous:
            mu0 = (previous["center_nm"] - origin) / local_step
            sigma0 = previous["sigma_nm"] / local_step
            amplitude0 = previous["amplitude"] / scale
            background0 = (previous["background"] - offset) / scale
        else:
            mu0, sigma0, amplitude0, background0 = 0.0, observed_sigma / local_step, 1.0, 0.0
        sigma0 = float(np.clip(sigma0, config["minimum_sigma_samples"] * 1.01, sigma_max * .99))
        # Three samples: fixed local baseline and coarse/previous width, two
        # unknowns. Four to six: fixed width, three unknowns. Report this openly.
        mode = "四参数背景高斯" if n >= 7 else "固定峰宽的三参数拟合" if n >= 4 else "固定背景与峰宽的双参数拟合"
        initial = [background0, amplitude0, mu0, sigma0]
        lower = [-2.0, max(noise / scale, 1e-9), mu_min, config["minimum_sigma_samples"]]
        upper = [3.0, 5.0, mu_max, sigma_max]
        free = [0, 1, 2, 3] if n >= 7 else [0, 1, 2] if n >= 4 else [1, 2]
        fixed = np.clip(np.asarray(initial, dtype=float), np.asarray(lower) + 1e-9, np.asarray(upper) - 1e-9)
        if n == 3:
            fixed[0] = 0.0

        def model_and_jac(p):
            q = fixed.copy()
            q[free] = p
            b, a, mu, sigma = q
            d = x - mu
            e = np.exp(-0.5 * (d / sigma) ** 2)
            fitted = b + a * e
            jac = np.column_stack((np.ones(n), e, a * e * d / sigma ** 2,
                                   a * e * d ** 2 / sigma ** 3))[:, free]
            return fitted, jac, q

        try:
            fit = least_squares(lambda p: model_and_jac(p)[0] - y, fixed[free],
                                jac=lambda p: model_and_jac(p)[1], bounds=(np.asarray(lower)[free], np.asarray(upper)[free]),
                                max_nfev=80, ftol=1e-8, xtol=1e-8, gtol=1e-8)
            fitted, _, q = model_and_jac(fit.x)
        except (ValueError, FloatingPointError, RuntimeError) as exc:
            return None, "受约束高斯拟合失败：" + str(exc)
        b, a, mu, sigma = q
        center = float(origin + mu * local_step)
        sigma_nm = float(sigma * local_step)
        rmse = float(np.sqrt(np.mean((fitted - y) ** 2)) * scale)
        amplitude = float(a * scale)
        relative = rmse / max(amplitude, np.finfo(float).eps)
        reason = None
        if not fit.success or not np.isfinite(q).all():
            reason = "高斯拟合未收敛"
        elif relative > config["maximum_relative_residual"]:
            reason = "拟合残差过大，峰形严重偏离背景高斯"
        elif center - window["low_nm"] <= config["window_margin_samples"] * local_step * 1.01 or window["high_nm"] - center <= config["window_margin_samples"] * local_step * 1.01:
            reason = "峰心到达固定跟踪范围边界，可能漂移越界或重叠"
        elif previous and abs(center - previous["center_nm"]) > config["maximum_jump_samples"] * local_step:
            reason = "峰心相对上一有效帧异常跳变"
        elif previous and max(sigma_nm / previous["sigma_nm"], previous["sigma_nm"] / sigma_nm) > config["maximum_width_ratio"]:
            reason = "峰宽异常变化，可能重叠或严重变形"
        elif sigma >= sigma_max * .999 or sigma <= config["minimum_sigma_samples"] * 1.001:
            reason = "拟合峰宽到达约束边界，原始采样不足或峰形异常"
        curve_x = np.linspace(float(local_x[0]), float(local_x[-1]), max(24, n * 2))
        background = float(offset + b * scale)
        row = {"center_nm": center, "amplitude": amplitude, "background": background,
               "sigma_nm": sigma_nm, "fwhm_nm": float(sigma_nm * 2.354820045),
               "prominence": candidate["prominence"], "residual": rmse,
               "relative_residual": relative, "fit_x": curve_x.tolist(),
               "fit_y": (background + amplitude * np.exp(-0.5 * ((curve_x - center) / sigma_nm) ** 2)).tolist(),
               "fit_availability": mode, "fit_sample_count": n,
               "fit_parameter_count": len(free), "fit_degrees_of_freedom": n - len(free),
               "fit_uses_smoothed_data": False, "seed_source": "上一有效帧" if previous else "当前原始粗定位",
               "valid": reason is None, "failure_reason": reason, "stale": False, "age_ms": 0.0,
               "quality": [] if reason is None else [reason]}
        return row, reason

    def _make_windows(self, axis, rows):
        centers = [row["center_nm"] for row in rows]
        boundaries = [float(axis[0])] + [(a + b) / 2 for a, b in zip(centers[:-1], centers[1:])] + [float(axis[-1])]
        confirmed = self.config["confirmed_drift_nm"]
        windows = []
        for index, row in enumerate(rows):
            low, high = boundaries[index], boundaries[index + 1]
            if confirmed:
                reserve = confirmed[index] + self.config["fit_radius_sigma"] * row["sigma_nm"]
                low, high = max(low, centers[index] - reserve), min(high, centers[index] + reserve)
            windows.append({"id": f"FBG{index + 1}", "low_nm": low, "high_nm": high,
                            "identity_anchor_nm": centers[index],
                            "confirmed_drift_nm": confirmed[index] if confirmed else None,
                            "drift_range_verified": bool(confirmed),
                            "range_note": "实测漂移范围及峰宽预留" if confirmed else "相邻峰中点限制；允许漂移范围尚未实测确认"})
        return windows

    def _stable_frame(self, rows, frame):
        label = "自动九峰参考" if self._pending_reference_source == AUTO_REFERENCE_SOURCE else "无载"
        if not all(row.get("valid") for row in rows) or len(rows) != 9:
            self._stable.clear()
            return False, "9 个峰必须连续有效，稳定计数已重置"
        centers = np.asarray([row["center_nm"] for row in rows])
        amplitudes = np.asarray([row["amplitude"] for row in rows])
        if self._stable:
            prior_centers = np.asarray([item["centers"] for item in self._stable])
            prior_amplitudes = np.asarray([item["amplitudes"] for item in self._stable])
            tolerance = self.config["stable_center_samples"] * self._axis_diag["median_step_nm"]
            if np.max(np.abs(centers - np.median(prior_centers, axis=0))) > tolerance:
                self._stable.clear()
                return False, f"{label}峰心仍在变化，稳定计数已重置"
            if np.max(np.abs(amplitudes - np.median(prior_amplitudes, axis=0)) / np.maximum(np.median(prior_amplitudes, axis=0), 1e-12)) > self.config["stable_amplitude_fraction"]:
                self._stable.clear()
                return False, f"{label}峰幅仍在变化，稳定计数已重置"
        self._stable.append({"centers": centers.tolist(), "amplitudes": amplitudes.tolist(),
                             "frame_id": deepcopy(frame.get("frame_id")),
                             "host_rx_monotonic_ns": frame.get("host_rx_monotonic_ns")})
        return len(self._stable) == self.config["stable_frames"], f"{label}稳定帧 {len(self._stable)}/{self.config['stable_frames']}"

    def _commit_reference(self, frame, initialization):
        self._reference = np.median(np.asarray([item["centers"] for item in self._stable]), axis=0).tolist()
        self._reference_version += 1
        entry = {"version": self._reference_version, "utc": datetime.now(timezone.utc).isoformat(),
                 "action": ("连接自动九峰参考" if self._pending_reference_source == AUTO_REFERENCE_SOURCE
                            else "首次初始化" if initialization else "无载回零"),
                 "reference_source": self._pending_reference_source,
                 "unloaded_confirmed": self._pending_unloaded_confirmed,
                 "reference_nm": deepcopy(self._reference), "identity_anchor_nm": deepcopy(self._anchors),
                 "frame_ids": [deepcopy(item["frame_id"]) for item in self._stable],
                 "stable_frame_count": len(self._stable), "stream_epoch": deepcopy(frame.get("stream_epoch")),
                 "algorithm_version": self.algorithm_version, "config_version": self.config_version,
                 "axis_sha256": self._axis_diag["axis_sha256"]}
        self._reference_history.append(entry)
        self._event("参考更新", reference=entry)
        self._stable.clear()
        self._pending_reference = False
        self._pending_reference_source = None
        self._pending_unloaded_confirmed = False

    def process(self, frame: dict) -> dict:
        started = time.perf_counter_ns()
        peak_processing_ns = 0
        stamp = frame.get("host_rx_monotonic_ns")
        result = {key: deepcopy(frame.get(key)) for key in (
            "frame_id", "host_rx_monotonic_ns", "timestamp_source", "time_quality", "stream_epoch",
            "device_sequence", "device_timestamp_raw", "read_start_ns", "read_end_ns", "local_receive_id")}
        result.update(algorithm_version=self.algorithm_version, config_version=self.config_version,
                      state="awaiting_confirmation", message="请移开接触棒，确认无载荷后初始化。", peaks=[],
                      candidates=[], windows=deepcopy(self._windows), reference_version=self._reference_version,
                      press_active=self._press, reference_frozen=self._press,
                      peak_processing_ms=0.0,
                      peak_processing_scope="九峰局部数据选择、拟合、逐峰质量检查及上一有效参数更新；不含全局粗定位、参考稳定检查与存储显示",
                      axis_diagnostics={}, locator_diagnostics={}, warnings=[],
                      raw_spectrum={"wavelength_nm": deepcopy(frame.get("wavelength_nm")),
                                    "intensity": deepcopy(frame.get("intensity")),
                                    "preprocessing": deepcopy(frame.get("preprocessing"))})
        self._processed += 1
        try:
            if digest(self.config) != self.config_version:
                raise ValueError("提峰配置已改变；请显式新建跟踪会话及配置版本，不能静默沿用旧参考或模型。")
            if str(frame.get("phase", "")) in {"press", "loading", "holding", "按压", "加载", "保持"} and not self._press:
                if self._pending_reference:
                    self._pending_reference = False
                    self._pending_reference_source = None
                    self._pending_unloaded_confirmed = False
                    self._stable.clear()
                    self._event("无载参考取消", reason="初始化或回零未完成就进入按压阶段，必须重新确认无载")
                raise ValueError("按压阶段未通过开始条件；本帧拒绝用于提峰或建立参考，请结束按压并重新确认无载。")
            axis = np.asarray(frame.get("wavelength_nm"), dtype=float)
            values = np.asarray(frame.get("intensity"), dtype=float)
            diag = analyze_axis(axis)
            if values.shape != (512,) or not np.isfinite(values).all():
                raise ValueError("光谱强度必须含有 512 个有限的原始设备采样值。")
            if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0:
                raise ValueError("缺少有效的源帧主机单调接收时间，不能以拟合完成时间替代。")
            if frame.get("valid") is False:
                raise ValueError("采集接口将原始光谱帧标记为无效。")
            preprocessing = frame.get("preprocessing")
            metadata = preprocessing if isinstance(preprocessing, dict) else {}
            smoothing = metadata.get("smoothing_enabled", metadata.get("smoothing", metadata.get("smoothing_applied")))
            if any(metadata.get(key) is True for key in ("smoothing_enabled", "smoothing", "smoothing_applied")) or metadata.get("raw_available") is False:
                raise ValueError("当前输入已平滑或不含原始采样，原始高斯拟合被阻断；请从设备接口提供未平滑采样。")
            diag.update(uniform=diag["relative_step_deviation"] <= self.config["uniform_axis_relative_tolerance"],
                        preprocessing=deepcopy(preprocessing),
                        preprocessing_verified=metadata.get("raw_available") is True and smoothing is False,
                        raw_fit_input="接口提供的未另行平滑采样",
                        precision_claim="采样间距、插值步长与显示小数位数均不代表测量精度")
            if not diag["preprocessing_verified"]:
                result["warnings"].append("设备预处理链尚未充分核实；当前输入不代表已验证的原始计数。")
            if not diag["uniform"]:
                result["warnings"].append("波长轴非均匀；高斯拟合直接使用设备实际波长坐标。")
            self._axis_diag = diag
            result["axis_diagnostics"] = deepcopy(diag)
            binding_metadata = {key: metadata.get(key) for key in (
                "raw_available", "smoothing", "smoothing_enabled", "pipeline_id", "algorithm",
                "correction_algorithm", "calibration_sha256", "factory_calibration_sha256",
                "axis_source", "effective_integration_us", "exposure_us", "sensor_mode",
                "integration_us_requested", "sensor_mode_requested", "average_number", "state",
                "background_applied", "baseline_correction_applied", "factory_intensity_correction_applied")}
            binding = (deepcopy(frame.get("stream_epoch")), diag["axis_sha256"], digest(binding_metadata))
            if self._binding is not None and binding != self._binding:
                self._clear_binding("采集段、波长校准轴或预处理链改变，需要重新确认无载并初始化")
                result["message"] = "采集条件已改变；原身份与参考已停用，请重新确认无载初始化。"
            self._binding = binding
            identity = frame.get("device_sequence") if frame.get("device_sequence") is not None else frame.get("frame_id")
            if identity is not None and self._last_identity is not None and identity == self._last_identity:
                self._duplicates += 1
                raise ValueError("重复源帧身份；该帧不计入新帧率或无载稳定帧。")
            if self._last_time is not None and stamp <= self._last_time:
                raise ValueError("源帧单调时间未递增，不能跨乱序或回退帧跟踪。")
            sequence = frame.get("device_sequence")
            if isinstance(sequence, int) and self._last_device_sequence is not None:
                if sequence < self._last_device_sequence:
                    self._clear_binding("设备序号回退但采集段未更新，需要重新确认无载")
                    result["warnings"].append("设备序号回退；需要新的采集段及无载确认。")
                elif sequence > self._last_device_sequence + 1:
                    self._sequence_gaps += 1
                    result["warnings"].append("设备帧序号发生跳跃，缺失帧不能补作独立测量。")
                    self._stable.clear()
            if self._last_time is not None:
                self._intervals.append((stamp - self._last_time) / 1e9)
            self._last_identity, self._last_time = deepcopy(identity), stamp
            self._last_device_sequence = sequence if isinstance(sequence, int) else None
            candidates, locator = locate_candidates(axis, values, self.config)
            result["candidates"], result["locator_diagnostics"] = deepcopy(candidates), locator
            saturation = self.config["saturation_level"]
            if saturation is None:
                saturation = metadata.get("saturation_level")
                if saturation is not None and (not isinstance(saturation, (int, float)) or not math.isfinite(saturation)):
                    raise ValueError("设备饱和阈值不是有限数值。")
            if saturation is None:
                result["warnings"].append("设备饱和阈值未知；仅可检测峰顶平台疑点，不能宣称已排除饱和。")
            if not self._anchors:
                rows = []
                if len(candidates) == 9:
                    centers = [row["center_nm"] for row in candidates]
                    boundaries = [float(axis[0])] + [(a + b) / 2 for a, b in zip(centers[:-1], centers[1:])] + [float(axis[-1])]
                    peak_started = time.perf_counter_ns()
                    for index, candidate in enumerate(candidates):
                        row, reason = self._fit(axis, values, candidate,
                                                {"low_nm": boundaries[index], "high_nm": boundaries[index + 1]},
                                                None, locator["noise_sigma"], saturation)
                        rows.append({**(row or {}), "id": None, "candidate_index": index + 1,
                                     "valid": reason is None, "failure_reason": reason})
                    peak_processing_ns += time.perf_counter_ns() - peak_started
                if not self._pending_reference:
                    result["peaks"] = rows
                elif len(candidates) != 9:
                    self._stable.clear()
                    result.update(state="initializing", message=f"可靠候选为 {len(candidates)} 个，要求恰好 9 个；请检查噪声、峰宽、峰间距、饱和及光纤连接，不补足或截取峰数。", peaks=rows)
                else:
                    ready, message = self._stable_frame(rows, frame)
                    result.update(state="initializing", message=message, peaks=rows)
                    if ready:
                        automatic = self._pending_reference_source == AUTO_REFERENCE_SOURCE
                        self._anchors = [row["center_nm"] for row in rows]
                        self._windows = self._make_windows(axis, rows)
                        self._commit_reference(frame, True)
                        for index, row in enumerate(rows):
                            row.update(id=f"FBG{index + 1}", drift_nm=row["center_nm"] - self._reference[index], value_frame_id=deepcopy(frame.get("frame_id")))
                            self._last_valid[index] = {"row": deepcopy(row), "timestamp": stamp, "frame_id": deepcopy(frame.get("frame_id"))}
                        message = ("9 个主峰已稳定建立连接自动参考，FBG1～FBG9 身份固定；未作无载确认。"
                                   if automatic else "9 个主峰已稳定初始化，FBG1～FBG9 身份固定。")
                        result.update(state="tracking", message=message, peaks=rows)
            else:
                rows = []
                peak_started = time.perf_counter_ns()
                for index, window in enumerate(self._windows):
                    if saturation is not None and np.any(values[(axis >= window["low_nm"]) & (axis <= window["high_nm"])] >= saturation):
                        rows.append(self._stale(index, "局部光谱达到设备饱和阈值", stamp))
                        continue
                    local = [row for row in candidates if window["low_nm"] <= row["center_nm"] <= window["high_nm"]]
                    if len(local) != 1:
                        reason = "固定窗口内未找到可靠主峰" if not local else "固定窗口内存在多个可靠峰，可能重叠或峰形分裂"
                        rows.append(self._stale(index, reason, stamp))
                        continue
                    previous = self._last_valid.get(index, {}).get("row")
                    row, reason = self._fit(axis, values, local[0], window, previous, locator["noise_sigma"], saturation)
                    if reason:
                        rows.append(self._stale(index, reason, stamp))
                        continue
                    drift_limit = window["confirmed_drift_nm"]
                    if drift_limit is not None and abs(row["center_nm"] - window["identity_anchor_nm"]) > drift_limit:
                        rows.append(self._stale(index, "峰心超出已有真实数据确认的漂移范围", stamp))
                        continue
                    row.update(id=f"FBG{index + 1}", drift_nm=row["center_nm"] - self._reference[index], value_frame_id=deepcopy(frame.get("frame_id")))
                    rows.append(row)
                    self._last_valid[index] = {"row": deepcopy(row), "timestamp": stamp, "frame_id": deepcopy(frame.get("frame_id"))}
                peak_processing_ns += time.perf_counter_ns() - peak_started
                count = sum(row["valid"] for row in rows)
                result.update(peaks=rows, state="tracking", message=f"当前有效峰 {count}/9；参考版本 {self._reference_version}。")
                if self._pending_reference:
                    ready, message = self._stable_frame(rows, frame)
                    result.update(state="reference_pending", message=message)
                    if ready:
                        self._commit_reference(frame, False)
                        for index, row in enumerate(rows):
                            row["drift_nm"] = row["center_nm"] - self._reference[index]
                            self._last_valid[index]["row"]["drift_nm"] = row["drift_nm"]
                        result.update(state="tracking", message="无载稳定检查通过；已更新参考波长，光栅身份与跟踪窗口保持固定。")
        except (ValueError, TypeError, OverflowError) as exc:
            self._invalid += 1
            self._stable.clear()
            peak_started = time.perf_counter_ns()
            result.update(state="input_invalid", message=str(exc),
                          peaks=[self._stale(index, str(exc), stamp if isinstance(stamp, int) else None) for index in range(9)] if self._anchors else [])
            peak_processing_ns += time.perf_counter_ns() - peak_started
        result.update(windows=deepcopy(self._windows), reference_version=self._reference_version,
                      reference_nm=deepcopy(self._reference), press_active=self._press, reference_frozen=self._press,
                      stable_frame_count=len(self._stable), stable_frame_required=self.config["stable_frames"],
                      drift_range_verified=self.config["confirmed_drift_nm"] is not None,
                      reference_pending=self._pending_reference,
                      pending_reference_source=self._pending_reference_source,
                      reference_source=self._reference_history[-1].get("reference_source") if self._reference and self._reference_history else None,
                      reference_unloaded_confirmed=self._reference_history[-1].get("unloaded_confirmed", False) if self._reference and self._reference_history else False)
        result["processing_ms"] = (time.perf_counter_ns() - started) / 1e6
        result["peak_processing_ms"] = peak_processing_ns / 1e6
        self._durations.append(result["processing_ms"])
        self._latest = deepcopy(result)
        return result

    def status(self) -> dict:
        metrics = {"processed_frames": self._processed, "duplicate_frames": self._duplicates,
                   "invalid_frames": self._invalid, "sequence_gap_events": self._sequence_gaps,
                   "host_receive_rate_hz": len(self._intervals) / sum(self._intervals) if self._intervals else None,
                   "new_frame_rate_hz": len(self._intervals) / sum(self._intervals) if self._intervals and self._last_device_sequence is not None else None,
                   "rate_note": "设备序号用于辨别新帧" if self._last_device_sequence is not None else "缺少设备帧身份，仅报告主机接收率；不能证明新光谱率",
                   "processing_mean_ms": float(np.mean(self._durations)) if self._durations else None,
                   "processing_p95_ms": float(np.percentile(self._durations, 95)) if self._durations else None}
        waiting_auto = self._pending_reference_source == AUTO_REFERENCE_SOURCE
        return {"initialized": bool(self._anchors), "state": self._latest.get("state") if self._latest else "initializing" if waiting_auto else "awaiting_confirmation",
                "message": self._latest.get("message") if self._latest else "等待本次连接的稳定光谱，自动建立九峰参考。" if waiting_auto else "等待实际光谱及无载确认。",
                "reference_pending": self._pending_reference, "press_active": self._press,
                "pending_reference_source": self._pending_reference_source,
                "reference_source": self._reference_history[-1].get("reference_source") if self._reference and self._reference_history else None,
                "reference_unloaded_confirmed": self._reference_history[-1].get("unloaded_confirmed", False) if self._reference and self._reference_history else False,
                "reference_frozen": self._press, "reference_version": self._reference_version,
                "algorithm_version": self.algorithm_version, "config_version": self.config_version,
                "windows": deepcopy(self._windows), "reference_nm": deepcopy(self._reference),
                "drift_range_verified": self.config["confirmed_drift_nm"] is not None,
                "stable_frame_count": len(self._stable), "metrics": metrics}

    def export_state(self) -> dict:
        return {**self.status(), "config": deepcopy(self.config), "reference_history": deepcopy(self._reference_history),
                "events": deepcopy(self._events), "identity_anchor_nm": deepcopy(self._anchors),
                "binding": deepcopy(self._binding), "latest": deepcopy(self._latest),
                "measurement_precision_verified": False,
                "training_requirement": "正式训练前必须完成真实无载及按压数据的同批原始光谱算法比较，冻结算法与配置版本。"}
