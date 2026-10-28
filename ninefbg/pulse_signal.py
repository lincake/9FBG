"""Native display-only port of frontend/pulse_signal.js.

The source-time Butterworth sections, fixed FBG5 prior, effective unloaded
coverage, source binding and candidate quality gates intentionally match the
existing pulse-bandpass-config-v1 rules. This module does not acquire hardware,
fit peaks, change references, store data or claim physical pulse accuracy.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import math
import re

CHANNELS = 9
BAND_HZ = (0.7, 3.0)
FILTER_CONFIG_SCHEMA = "pulse-bandpass-config-v1"
FILTER_LIMITS_HZ = (0.1, 8.0)
CANDIDATE_BAND_HZ = (0.7, 3.0)
NOISE_SECONDS = 10
CENTER_CHANNEL = 4
CENTER_MIN_WEIGHT = 0.5
NOISE_FLOOR_PM = 0.02
CALIBRATION_METHOD = "unloaded-noise-fbg5-prior-v1"
STREAM_MODE = "continuous-noise-fixed-v1"
NOISE_POLICY = "effective-source-time-and-samples-v1"
WARMUP_SECONDS = 3
MIN_RATE_HZ = 20
MAX_GAP_SECONDS = 0.12
PAUSE_GAP_SECONDS = 1
MAX_NOISE_WALL_SECONDS = 60
MAX_NOISE_FRAMES = 12000
MIN_NOISE_RAW_SAMPLES = 181
MIN_NOISE_FILTERED_SAMPLES = 126
MIN_NOISE_FILTERED_SECONDS = NOISE_SECONDS - WARMUP_SECONDS


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def integer(value):
    # JavaScript has one numeric type: 1 and 1.0 both satisfy Number.isInteger.
    return finite(value) and (type(value) is int or value.is_integer())


def same_identity(left, right):
    return ((type(left) in (int, float) and type(right) in (int, float)) or
            type(left) is type(right)) and left == right


def mean(values):
    return sum(values) / max(1, len(values))


def dot(left, right):
    return sum(x * y for x, y in zip(left, right))


def rms(values):
    return math.sqrt(mean([value * value for value in values]))


def centered(values):
    average = mean(values)
    return [value - average for value in values]


def quantile(values, probability):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.floor(probability * len(ordered)))] if ordered else 0


def empty_quality(label):
    return dict(valid=False, label=label, correlation=0, periodicity=0,
                spectralFraction=0, snr=0, channels=[])


def validate_filter_band_hz(band):
    if not isinstance(band, (list, tuple)) or len(band) != 2 or not all(finite(value) for value in band):
        raise ValueError("带通上下限必须是有限数字，单位为 Hz")
    low, high = band
    if low < FILTER_LIMITS_HZ[0] or high > FILTER_LIMITS_HZ[1] or low >= high:
        raise ValueError("带通频率须满足 0.1 ≤ 下限 < 上限 ≤ 8 Hz")
    return list(band)


class Butterworth:
    def __init__(self, cutoff, highpass):
        self.cutoff, self.highpass, self.ready = cutoff, highpass, False

    def next(self, value, dt):
        if not self.ready:
            self.x1 = self.x2 = value
            self.y1 = self.y2 = 0 if self.highpass else value
            self.ready = True
        k = math.tan(math.pi * self.cutoff * dt)
        norm = 1 / (1 + math.sqrt(2) * k + k * k)
        b0 = norm if self.highpass else k * k * norm
        b1, b2 = (-2 * b0 if self.highpass else 2 * b0), b0
        a1 = 2 * (k * k - 1) * norm
        a2 = (1 - math.sqrt(2) * k + k * k) * norm
        result = b0 * value + b1 * self.x1 + b2 * self.x2 - a1 * self.y1 - a2 * self.y2
        self.x2, self.x1, self.y2, self.y1 = self.x1, value, self.y1, result
        return result


def make_filters(band=BAND_HZ):
    return [dict(hp=Butterworth(band[0], True), lp=Butterworth(band[1], False)) for _ in range(CHANNELS)]


def spectral_peak(points, band=CANDIDATE_BAND_HZ):
    """Same weighted sinusoid fit and 0.01 Hz recurrence as the former GUI."""
    values = centered([point["value"] for point in points])
    power = mean([value * value for value in values])
    if not power > 1e-12:
        return dict(frequency=None, fraction=0)
    t0 = points[0]["t"]
    best = dict(frequency=None, fraction=0)
    phases = []
    for index, point in enumerate(points):
        offset = 2 * math.pi * (point["t"] - t0)
        phases.append([math.sin(band[0] * offset), math.cos(band[0] * offset),
                       math.sin(0.01 * offset), math.cos(0.01 * offset),
                       point["t"] - points[index - 1]["t"] if index else points[1]["t"] - t0])
    frequency = band[0]
    while frequency <= band[1] + 1e-9:
        ss = cc = sc = ys = yc = 0.0
        for value, phase in zip(values, phases):
            s, c, ds, dc, dt = phase
            ss += s * s * dt
            cc += c * c * dt
            sc += s * c * dt
            ys += value * s * dt
            yc += value * c * dt
            phase[0], phase[1] = s * dc + c * ds, c * dc - s * ds
        determinant = ss * cc - sc * sc
        if determinant > 1e-12:
            a, b = (ys * cc - yc * sc) / determinant, (yc * ss - ys * sc) / determinant
            duration = points[-1]["t"] - t0 + (points[1]["t"] - t0)
            fraction = max(0, min(1, (a * ys + b * yc) / duration / power))
            if fraction > best["fraction"]:
                best = dict(frequency=frequency, fraction=fraction)
        frequency += 0.01
    return best


def periodicity(points, band=CANDIDATE_BAND_HZ):
    def unavailable(label):
        return dict(valid=False, label=label, bpm=None, periodicity=0, spectralFraction=0)
    if not band:
        return unavailable("当前滤波频带未覆盖 0.7～3 Hz 候选周期范围，仅显示曲线")
    if len(points) < 100 or points[-1]["t"] - points[0]["t"] < 5:
        return unavailable("等待足够的连续周期")
    peak = spectral_peak(points, band)
    if not peak["frequency"]:
        return unavailable("没有稳定的周期信号")
    lag = 1 / peak["frequency"]
    left, right, index = [], [], 0
    for point in points:
        target = point["t"] - lag
        if target < points[0]["t"]:
            continue
        while index + 1 < len(points) and points[index + 1]["t"] < target:
            index += 1
        if index + 1 >= len(points):
            break
        a, b = points[index], points[index + 1]
        fraction = (target - a["t"]) / (b["t"] - a["t"])
        left.append(point["value"])
        right.append(a["value"] + fraction * (b["value"] - a["value"]))
    lc, rc = centered(left), centered(right)
    correlation = dot(lc, rc) / max(1e-12, math.sqrt(dot(lc, lc) * dot(rc, rc)))
    middle = len(points) // 2
    first, second = spectral_peak(points[:middle], band), spectral_peak(points[middle:], band)
    stable = first["frequency"] is not None and second["frequency"] is not None and abs(first["frequency"] - second["frequency"]) <= 0.18
    enough = (points[-1]["t"] - points[0]["t"]) * peak["frequency"] >= 4
    valid = peak["fraction"] >= 0.35 and correlation >= 0.65 and stable and enough
    return dict(valid=valid, label="发现稳定候选周期" if valid else "周期不稳定或随机噪声占优",
                bpm=peak["frequency"] * 60 if valid else None, periodicity=correlation, spectralFraction=peak["fraction"])


def fused_noise(weights, covariance):
    return math.sqrt(max(0, dot(weights, [dot(row, weights) for row in covariance])))


def noise_weights(noise):
    precision = [1 / max(sigma, NOISE_FLOOR_PM) ** 2 for sigma in noise]
    total = sum(precision)
    return [(1 - CENTER_MIN_WEIGHT) * value / total + (CENTER_MIN_WEIGHT if index == CENTER_CHANNEL else 0)
            for index, value in enumerate(precision)]


class PulseSession:
    def __init__(self, filter_band_hz=BAND_HZ, **options):
        self.revision = 0
        self._configure_filter_band(validate_filter_band_hz(options.get("filterBandHz", filter_band_hz)))
        self.reset()

    def _configure_filter_band(self, band):
        self.filterBandHz = tuple(band)
        low, high = max(band[0], CANDIDATE_BAND_HZ[0]), min(band[1], CANDIDATE_BAND_HZ[1])
        self.candidateBandHz = (low, high) if low < high else None
        self.warmupSeconds = WARMUP_SECONDS if band[0] >= BAND_HZ[0] else 2.1 / band[0]
        self.maxGapSeconds = min(MAX_GAP_SECONDS, 0.45 / band[1])

    def set_filter_band(self, band):
        validated = validate_filter_band_hz(band)
        if tuple(validated) == self.filterBandHz:
            return self.snapshot()
        self._configure_filter_band(validated)
        return self.reset("滤波频带已修改，请确认无载并重新采集噪声以生成固定权重")

    def reset(self, reason="请先确认无载并采集噪声"):
        self.revision += 1
        self.phase, self.reason, self.progress = "idle", reason, 0
        self.weights = self.noise = self.noiseCovariance = self.noiseBinding = self.baselinePm = None
        self.rawNoise = self.rawCovariance = None
        self.rawNoiseRows, self.noisePoints = [], []
        self.calibration = self.curveKind = self.motionLimits = self.fusedMotionLimit = None
        self.binding = self.epoch = self.sourceKind = self.current = self.valuePm = None
        self.points, self.rows = [], []
        self.suspended = self.pendingBreak = False
        self.pointVersion = 0
        self.candidateBpm, self.quality = None, empty_quality(reason)
        self.processedCount = self.rejectedCount = self.gapCount = self.maxGapMs = 0
        self.filterIntervalLimitCount = 0
        self.effectiveNoiseSeconds = self.noiseFilteredSeconds = 0
        self.noiseRawSamples = self.noiseFilteredSamples = 0
        self._clear_stream()
        return self.snapshot()

    def _clear_stream(self, keep_watermark=False):
        self.filters = make_filters(self.filterBandHz)
        self.last = self.startT = self.segmentStartT = None
        self.times, self.rateHz = [], None
        if not keep_watermark:
            self.frameIds, self.watermark = OrderedDict(), None
        self.lastQualityT = self.monitorStartT = None

    def _fail(self, reason):
        self.phase, self.reason, self.progress = "failed", reason, 0
        self.weights = self.noise = self.noiseCovariance = self.noiseBinding = self.baselinePm = None
        self.rawNoise = self.rawCovariance = None
        self.rawNoiseRows, self.noisePoints = [], []
        self.calibration = self.curveKind = self.motionLimits = self.fusedMotionLimit = self.valuePm = None
        self.points, self.rows = [], []
        self.suspended = self.pendingBreak = False
        self.pointVersion += 1
        self.candidateBpm, self.quality = None, empty_quality(reason)
        self.revision += 1
        self._clear_stream()

    def interrupt(self, reason="光谱数据已中断，请重新采集无载噪声"):
        if self.phase not in ("idle", "failed"):
            self._fail(reason)
        return self.snapshot()

    def pause(self, reason="光谱数据暂时中断，请确认恢复后继续"):
        if self.phase in ("idle", "failed"):
            return self.snapshot()
        if self.noise is None:
            self._fail(reason + "；本次无载采集未完成，请重新采集无载噪声")
            return self.snapshot()
        self.phase, self.suspended, self.progress = "monitor", True, 1
        self.reason = reason + "；保留曲线和固定权重，等待同一采集源自动恢复"
        self.rows, self.valuePm, self.curveKind = [], None, "monitor"
        self.pendingBreak, self.candidateBpm, self.quality = True, None, empty_quality(self.reason)
        self.revision += 1
        self._clear_stream(True)
        return self.snapshot()

    def begin_noise(self, confirmed=False):
        if confirmed is not True:
            raise ValueError("请明确确认传感器无载后再采集噪声")
        self.reset()
        self.phase, self.reason = "noise", f"无载噪声采集中，前 {self.warmupSeconds:.1f}".removesuffix(".0") + " 秒预热；断段后自动补足有效采样"
        return self.snapshot()

    def begin_monitor(self):
        if self.weights is None or self.phase not in ("calibrated_ready", "monitor"):
            raise ValueError("请先完成 10 秒无载噪声采集以固定权重")
        if self.phase == "monitor":
            return self.snapshot()
        self.phase, self.progress = "monitor", 1
        self.reason = "固定权重曲线持续显示，可随时放手观察；预热中暂不判定周期"
        self.rows, self.valuePm, self.candidateBpm = [], None, None
        self.suspended, self.pendingBreak, self.curveKind = False, True, "monitor"
        self.quality = empty_quality(self.reason)
        self.revision += 1
        self._clear_stream(True)
        return self.snapshot()

    def stop_monitor(self):
        if self.phase == "calibrated_ready" and self.weights is not None:
            self._clear_stream(True)
            return self.snapshot()
        if self.phase != "monitor" or self.weights is None:
            return self.snapshot()
        self.phase, self.progress = "calibrated_ready", 1
        self.reason = "曲线已停止，保留本次固定权重；可恢复持续观察"
        self.rows, self.suspended, self.curveKind = [], False, "monitor"
        self.valuePm = self.points[-1]["value"] if self.points else None
        self.candidateBpm, self.quality = None, empty_quality(self.reason)
        self.revision += 1
        self._clear_stream(True)
        return self.snapshot()

    def push(self, samples, include_points=True):
        for sample in samples if isinstance(samples, list) else [samples]:
            self._push_one(sample)
        return self.snapshot(include_points=include_points)

    def _push_one(self, sample):
        def identity(value):
            return (isinstance(value, str) and bool(value)) or (integer(value) and value >= 0)
        if not isinstance(sample, dict) or not integer(sample.get("t_ns")) or sample["t_ns"] <= 0 or not integer(sample.get("seq")) or sample["seq"] < 0 or not identity(sample.get("frame_id")) or not identity(sample.get("stream_epoch")) or not isinstance(sample.get("binding"), str) or not sample["binding"] or not isinstance(sample.get("source_kind"), str) or not sample["source_kind"]:
            self.rejectedCount += 1
            if self.phase not in ("idle", "failed"):
                self._fail("源帧时间、身份或参考绑定缺失，请重新采集")
            return
        if re.search(r"simulat|synthetic|demo|replay|模拟|回放", sample["source_kind"], re.I):
            self.rejectedCount += 1
            if self.phase not in ("idle", "failed"):
                self._fail("脉搏测量需要实际设备的连续源帧")
            return
        active = self.phase not in ("idle", "failed")
        if active and self.binding is not None and (sample["binding"] != self.binding or not same_identity(sample["stream_epoch"], self.epoch) or sample["source_kind"] != self.sourceKind):
            self.rejectedCount += 1
            self._fail("参考绑定或采集时段已改变，请重新采集无载噪声")
            return
        t = sample["t_ns"] / 1e9
        frame_key = sample["frame_id"] if isinstance(sample["frame_id"], str) else str(int(sample["frame_id"]))
        same_source = self.watermark and sample["binding"] == self.watermark["binding"] and same_identity(sample["stream_epoch"], self.watermark["epoch"]) and sample["source_kind"] == self.watermark["sourceKind"]
        if same_source and (sample["t_ns"] <= self.watermark["t_ns"] or sample["seq"] <= self.watermark["seq"] or frame_key in self.frameIds):
            self.rejectedCount += 1
            return
        if self.watermark and not same_source:
            self._clear_stream()
        values = sample.get("values_pm")
        needed = [j for j, weight in enumerate(self.weights) if weight != 0] if self.weights is not None else list(range(CHANNELS))
        if not isinstance(values, list) or len(values) != CHANNELS or any(not finite(values[j]) for j in needed):
            self.rejectedCount += 1
            if active:
                self._fail("参与合成的光栅漂移缺失或无效，请重新采集无载噪声")
            return
        self.binding, self.epoch, self.sourceKind = sample["binding"], sample["stream_epoch"], sample["source_kind"]
        source_dt = t - self.last["t"] if self.last else None
        interval_limited = source_dt is not None and source_dt > self.maxGapSeconds + 1e-9 and source_dt <= MAX_GAP_SECONDS + 1e-9
        split = self.phase in ("noise", "monitor") and source_dt is not None and source_dt > self.maxGapSeconds + 1e-9
        watermark = dict(t_ns=sample["t_ns"], seq=sample["seq"], binding=sample["binding"], epoch=sample["stream_epoch"], sourceKind=sample["source_kind"])
        if split:
            self.gapCount += 1
            self.maxGapMs = max(self.maxGapMs, source_dt * 1000)
            if interval_limited:
                self.filterIntervalLimitCount += 1
            if source_dt >= PAUSE_GAP_SECONDS - 1e-9 and self.phase == "noise":
                self.watermark, self.frameIds[frame_key] = watermark, None
                self.rejectedCount += 1
                self.pause("连续源帧中断达到 1 秒，无载采集已暂停")
                return
            self.filters, self.segmentStartT = make_filters(self.filterBandHz), t
            self.times, self.rateHz, self.lastQualityT = [], None, None
            if self.phase == "monitor":
                self.rows, self.valuePm, self.pendingBreak = [], None, True
                self.candidateBpm, self.monitorStartT = None, t
                self.quality = empty_quality("源采样间隔不足以支持所选滤波上限，曲线已断段，等待更快采样后重新预热" if interval_limited else "源帧缺口已断开显示，保留固定权重，正在重新预热")
                self.reason = self.quality["label"]
        dt, previous = (source_dt if source_dt is not None and not split else 0.04), (None if split else self.last)
        self.last = dict(t=t, t_ns=sample["t_ns"], seq=sample["seq"], raw=values[:])
        self.watermark, self.current = watermark, values[:]
        self.processedCount += 1
        if self.suspended:
            self.suspended, self.pendingBreak = False, True
            self.reason = "同一采集源已恢复，固定权重曲线继续显示，预热中暂不判定周期"
        self.frameIds[frame_key] = None
        if len(self.frameIds) > 4000:
            self.frameIds.popitem(last=False)
        self.times.append(t)
        while len(self.times) > 1 and t - self.times[0] > 5:
            self.times.pop(0)
        self.rateHz = (len(self.times) - 1) / (t - self.times[0]) if len(self.times) > 1 else None
        if not active or self.phase == "calibrated_ready":
            return
        if self.startT is None:
            self.startT = t
        if self.segmentStartT is None:
            self.segmentStartT = t
        if self.phase == "monitor" and self.monitorStartT is None:
            self.monitorStartT = t
        elapsed, segment_elapsed = t - self.startT, t - self.segmentStartT
        filtered = [self.filters[j]["lp"].next(self.filters[j]["hp"].next(value - (self.baselinePm[j] if self.baselinePm else 0), dt), dt) if finite(value) else 0 for j, value in enumerate(values)]
        low_rate = segment_elapsed >= 2 and (self.rateHz or 0) < MIN_RATE_HZ - 0.1
        if self.phase == "noise":
            if previous:
                self.effectiveNoiseSeconds += source_dt
            self.rawNoiseRows.append(dict(t=t, raw=values[:]))
            if segment_elapsed + 1e-9 >= self.warmupSeconds:
                self.rows.append(dict(t=t, filtered=filtered, raw=values[:]))
                if previous:
                    self.noiseFilteredSeconds += max(0, t - max(previous["t"], self.segmentStartT + self.warmupSeconds))
            self.noiseRawSamples, self.noiseFilteredSamples = len(self.rawNoiseRows), len(self.rows)
            self.progress = min(1, self.effectiveNoiseSeconds / NOISE_SECONDS, self.noiseFilteredSeconds / MIN_NOISE_FILTERED_SECONDS, self.noiseRawSamples / MIN_NOISE_RAW_SAMPLES, self.noiseFilteredSamples / MIN_NOISE_FILTERED_SAMPLES)
            self.reason = ("源采样间隔不足以支持所选滤波上限，无载噪声分段采集；请降低滤波上限或提高采样率" if self.filterIntervalLimitCount else "无载噪声采集中，缺口时间不计入有效 10 秒；各段滤波预热后自动补足" if self.gapCount else f"无载噪声采集中，前 {self.warmupSeconds:.1f}".removesuffix(".0") + " 秒预热" if segment_elapsed < self.warmupSeconds else f"当前有效提峰率 {self.rateHz:.1f} Hz，无载噪声继续采集；自动补足有效时间和真实样本" if low_rate else "无载噪声采集中，请保持完全无载")
            complete = self.effectiveNoiseSeconds + 1e-9 >= NOISE_SECONDS and self.noiseFilteredSeconds + 1e-9 >= MIN_NOISE_FILTERED_SECONDS and self.noiseFilteredSamples >= MIN_NOISE_FILTERED_SAMPLES and self.noiseRawSamples >= MIN_NOISE_RAW_SAMPLES
            if not complete and (elapsed >= MAX_NOISE_WALL_SECONDS or len(self.rawNoiseRows) >= MAX_NOISE_FRAMES):
                self._fail("源采样间隔不足以支持所选滤波上限，无载有效采样不足；请降低滤波上限或提高采样率后重录" if self.filterIntervalLimitCount else "无载有效采样不足或缺帧过于频繁，请改善采集性能后重新录制无载")
                return
            if complete:
                columns = [centered([row["filtered"][j] for row in self.rows]) for j in range(CHANNELS)]
                self.noise = [rms(column) for column in columns]
                self.noiseCovariance = [[dot(column, other) / len(self.rows) + (NOISE_FLOOR_PM ** 2 if j == k else 0) for k, other in enumerate(columns)] for j, column in enumerate(columns)]
                self.baselinePm = [quantile([row["raw"][j] for row in self.rawNoiseRows], 0.5) for j in range(CHANNELS)]
                raw_columns = [centered([row["raw"][j] for row in self.rawNoiseRows]) for j in range(CHANNELS)]
                self.rawNoise = [max(rms(column), NOISE_FLOOR_PM) for column in raw_columns]
                self.rawCovariance = [[dot(column, other) / len(self.rawNoiseRows) + (NOISE_FLOOR_PM ** 2 if j == k else 0) for k, other in enumerate(raw_columns)] for j, column in enumerate(raw_columns)]
                if not all(finite(value) for value in self.noise + self.rawNoise + sum(self.noiseCovariance, []) + sum(self.rawCovariance, [])):
                    self._fail("无载噪声统计无效，请检查光栅数据后重新采集")
                    return
                self.rawNoiseRows, self.noiseBinding = [], self.binding
                self.weights = noise_weights(self.noise)
                self.motionLimits = [max(8, 30 * sigma) for sigma in self.rawNoise]
                self.fusedMotionLimit = max(8, 30 * fused_noise(self.weights, self.rawCovariance))
                self.calibration = dict(method=CALIBRATION_METHOD, noisePolicy=NOISE_POLICY, centerChannel="FBG5", centerIndex=CENTER_CHANNEL, centerMinWeight=CENTER_MIN_WEIGHT, noiseFloorPm=NOISE_FLOOR_PM, weightBasis="band-noise-inverse-variance-with-center-prior", responseDirection="not-inferred", responseSensitivity="not-measured", channels=list(range(CHANNELS)), durationSeconds=elapsed, effectiveNoiseSeconds=self.effectiveNoiseSeconds, filteredSeconds=self.noiseFilteredSeconds, filterBandHz=list(self.filterBandHz), filterConfigSchema=FILTER_CONFIG_SCHEMA, candidateBandHz=list(self.candidateBandHz) if self.candidateBandHz else None, warmupSeconds=self.warmupSeconds, maxGapSeconds=self.maxGapSeconds, rawSampleCount=self.noiseRawSamples, filteredSampleCount=self.noiseFilteredSamples, sourceSpanRateHz=(self.noiseRawSamples - 1) / elapsed, binding=self.binding, streamEpoch=self.epoch, sourceKind=self.sourceKind)
                self.noisePoints = [dict(t=row["t"], value=dot(row["filtered"], self.weights)) for row in self.rows]
                self.points = deepcopy(self.noisePoints)
                self.valuePm = self.points[-1]["value"] if self.points else None
                self.curveKind, self.phase, self.progress, self.rows = "monitor", "monitor", 1, []
                self.reason = "10 秒无载已完成，噪声权重已固定；曲线持续显示，可随时把手放到 FBG5 中心附近观察"
                self.quality, self.pendingBreak = empty_quality(self.reason), True
                self.pointVersion += 1
                self.revision += 1
                self._clear_stream(True)
        elif self.phase == "monitor":
            raw_delta = [value - previous["raw"][j] for j, value in enumerate(values)] if previous else None
            movement = raw_delta is not None and (abs(raw_delta[CENTER_CHANNEL]) > self.motionLimits[CENTER_CHANNEL] or abs(dot(raw_delta, self.weights)) > self.fusedMotionLimit)
            if movement:
                self.filters, self.rows, self.pendingBreak = make_filters(self.filterBandHz), [], True
                self.candidateBpm, self.monitorStartT = None, t
                filtered = [self.filters[j]["lp"].next(self.filters[j]["hp"].next(value - self.baselinePm[j], 0.04), 0.04) if finite(value) else 0 for j, value in enumerate(values)]
                self.quality = empty_quality("检测到明显移动，曲线继续显示，预热中暂不判定周期")
                self.reason, self.lastQualityT = self.quality["label"], None
            warmup = t - self.monitorStartT < self.warmupSeconds
            self.valuePm = dot(filtered, self.weights)
            self.points.append(dict(t=t, value=self.valuePm, warmup=warmup, breakBefore=self.pendingBreak))
            self.pendingBreak = False
            self.pointVersion += 1
            while self.points and (t - self.points[0]["t"] > 60 or len(self.points) > 12000):
                self.points.pop(0)
            if warmup or low_rate:
                self.candidateBpm = None
                if low_rate:
                    self.rows = []
                if low_rate or (not movement and not split):
                    self.quality = empty_quality("实际有效提峰率不足 20 Hz，曲线继续显示，暂不判定周期" if low_rate else "实时滤波预热中，曲线继续显示，暂不判定周期")
                self.reason = self.quality["label"]
                return
            self.rows.append(dict(t=t, filtered=filtered, raw=values[:]))
            while self.rows and (t - self.rows[0]["t"] > 12 or len(self.rows) > 12000):
                self.rows.pop(0)
            if self.lastQualityT is None or t - self.lastQualityT >= 1:
                self.lastQualityT = t
                quality_points = self.points[-len(self.rows):]
                periodic = periodicity(quality_points, self.candidateBandHz)
                output = centered([point["value"] for point in quality_points])
                baseline = fused_noise(self.weights, self.noiseCovariance)
                snr = rms(output) / max(baseline, 0.005)
                center = centered([row["filtered"][CENTER_CHANNEL] for row in self.rows])
                center_snr = rms(center) / max(self.noise[CENTER_CHANNEL], NOISE_FLOOR_PM)
                center_correlation = abs(dot(center, output) / max(1e-12, math.sqrt(dot(center, center) * dot(output, output))))
                valid = periodic["valid"] and snr >= 3 and center_snr >= 3 and center_correlation >= 0.65
                label = periodic["label"] if not self.candidateBandHz else "合成信号未明显高于无载噪声" if snr < 3 else "FBG5 中心信号未明显高于无载噪声" if center_snr < 3 else "合成曲线与 FBG5 中心信号一致性不足，等待接触恢复" if center_correlation < 0.65 else periodic["label"]
                self.quality = dict(valid=valid, label=label, correlation=center_correlation, centerCorrelation=center_correlation, centerSnr=center_snr, periodicity=periodic["periodicity"], spectralFraction=periodic["spectralFraction"], snr=snr, channels=needed[:])
                self.candidateBpm, self.reason = (periodic["bpm"] if valid else None), label

    def read_points(self):
        return self.points

    def snapshot(self, include_points=True):
        result = dict(phase=self.phase, reason=self.reason, progress=self.progress,
                      weights=deepcopy(self.weights), noise=deepcopy(self.noise), rawNoise=deepcopy(self.rawNoise),
                      baselinePm=deepcopy(self.baselinePm), calibration=deepcopy(self.calibration), curveKind=self.curveKind,
                      streamMode=STREAM_MODE, noisePolicy=NOISE_POLICY, rateHz=self.rateHz, current=deepcopy(self.current), valuePm=self.valuePm,
                      pointCount=len(self.points), pointVersion=self.pointVersion, suspended=self.suspended, candidateBpm=self.candidateBpm,
                      quality=deepcopy(self.quality), binding=self.binding, streamEpoch=self.epoch, sourceKind=self.sourceKind,
                      revision=self.revision, processedCount=self.processedCount, rejectedCount=self.rejectedCount,
                      gapCount=self.gapCount, maxGapMs=self.maxGapMs, filterIntervalLimitCount=self.filterIntervalLimitCount,
                      effectiveNoiseSeconds=self.effectiveNoiseSeconds, noiseFilteredSeconds=self.noiseFilteredSeconds,
                      noiseRawSamples=self.noiseRawSamples, noiseFilteredSamples=self.noiseFilteredSamples,
                      filterConfigSchema=FILTER_CONFIG_SCHEMA, filterBandHz=list(self.filterBandHz), candidateBandHz=list(self.candidateBandHz) if self.candidateBandHz else None,
                      filterDelayMs=None, filterDelayDescription="实时因果滤波，延迟随频率变化（约 90～410 ms）；预热曲线可见，前 3 秒不判定周期" if self.filterBandHz == BAND_HZ else f"实时因果滤波，延迟随所选频带与频率变化；预热曲线可见，前 {self.warmupSeconds:.1f}".removesuffix(".0") + " 秒不判定周期",
                      warmupSeconds=self.warmupSeconds, maxGapSeconds=self.maxGapSeconds, lastSampleNs=self.last["t_ns"] if self.last else None)
        if include_points:
            result["points"] = deepcopy(self.points)
        return result

    # The original names remain convenient when comparing parity fixtures.
    setFilterBand = set_filter_band
    beginNoise = begin_noise
    beginMonitor = begin_monitor
    stopMonitor = stop_monitor
    readPoints = read_points


validateFilterBandHz = validate_filter_band_hz
