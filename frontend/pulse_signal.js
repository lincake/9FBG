(function (global, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  global.NineFBGPulse = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Display-only processing. No hardware, reference, peak-fitting, or storage API.
  // Input order is the nine fixed FBG identities, NEVER inferred spatial order.
  // push accepts {seq,t_ns,frame_id,stream_epoch,source_kind,binding,values_pm:[9]}.
  // Each source frame is consumed once, independently of browser polling rate.
  const CHANNELS = 9;
  const BAND_HZ = Object.freeze([0.7, 3]);
  const FILTER_CONFIG_SCHEMA = "pulse-bandpass-config-v1";
  const FILTER_LIMITS_HZ = Object.freeze([0.1, 8]);
  const CANDIDATE_BAND_HZ = Object.freeze([0.7, 3]);
  const NOISE_SECONDS = 10;
  const CENTER_CHANNEL = 4; // The operator identified FBG5; no other spatial order is assumed.
  const CENTER_MIN_WEIGHT = 0.5;
  const NOISE_FLOOR_PM = 0.02;
  const CALIBRATION_METHOD = "unloaded-noise-fbg5-prior-v1";
  const STREAM_MODE = "continuous-noise-fixed-v1";
  const NOISE_POLICY = "effective-source-time-and-samples-v1";
  const WARMUP_SECONDS = 3;
  const MIN_RATE_HZ = 20;
  const MAX_GAP_SECONDS = 0.12;
  const PAUSE_GAP_SECONDS = 1;
  const MAX_NOISE_WALL_SECONDS = 60;
  const MAX_NOISE_FRAMES = 12000;
  const MIN_NOISE_RAW_SAMPLES = 181;
  const MIN_NOISE_FILTERED_SAMPLES = 126;
  const MIN_NOISE_FILTERED_SECONDS = NOISE_SECONDS - WARMUP_SECONDS;
  const finite = n => typeof n === "number" && Number.isFinite(n);
  const mean = a => a.reduce((s, x) => s + x, 0) / Math.max(1, a.length);
  const dot = (a, b) => a.reduce((s, x, i) => s + x * b[i], 0);
  const rms = a => Math.sqrt(mean(a.map(x => x * x)));
  const quantile = (a, p) => { const b = a.slice().sort((x, y) => x - y); return b[Math.min(b.length - 1, Math.floor(p * b.length))] || 0; };
  const centered = a => { const m = mean(a); return a.map(x => x - m); };
  const copyQuality = quality => ({ ...quality, channels: (quality.channels || []).slice() });
  const emptyQuality = label => ({ valid: false, label, correlation: 0, periodicity: 0, spectralFraction: 0, snr: 0, channels: [] });

  function validateFilterBandHz(band) {
    if (!Array.isArray(band) || band.length !== 2 || !band.every(finite)) throw new Error("带通上下限必须是有限数字，单位为 Hz");
    const [low, high] = band;
    if (low < FILTER_LIMITS_HZ[0] || high > FILTER_LIMITS_HZ[1] || low >= high) throw new Error("带通频率须满足 0.1 ≤ 下限 < 上限 ≤ 8 Hz");
    return band.slice();
  }

  // Bilinear-transform Butterworth sections recomputed from *source* dt.
  // Filtering at each original timestamp avoids UI-rate resampling/aliasing.
  // Delay depends on the configured band and the frequency of the signal.
  class Butterworth {
    constructor(cutoff, highpass) { this.cutoff = cutoff; this.highpass = highpass; this.ready = false; }
    next(x, dt) {
      if (!this.ready) {
        this.x1 = this.x2 = x;
        this.y1 = this.y2 = this.highpass ? 0 : x;
        this.ready = true;
      }
      const k = Math.tan(Math.PI * this.cutoff * dt);
      const norm = 1 / (1 + Math.SQRT2 * k + k * k);
      const b0 = this.highpass ? norm : k * k * norm;
      const b1 = this.highpass ? -2 * b0 : 2 * b0;
      const b2 = b0;
      const a1 = 2 * (k * k - 1) * norm;
      const a2 = (1 - Math.SQRT2 * k + k * k) * norm;
      const y = b0 * x + b1 * this.x1 + b2 * this.x2 - a1 * this.y1 - a2 * this.y2;
      this.x2 = this.x1; this.x1 = x; this.y2 = this.y1; this.y1 = y;
      return y;
    }
  }
  const makeFilters = (band = BAND_HZ) => Array.from({ length: CHANNELS }, () => ({ hp: new Butterworth(band[0], true), lp: new Butterworth(band[1], false) }));

  function spectralPeak(points, band = CANDIDATE_BAND_HZ) {
    const y = centered(points.map(p => p.value));
    const power = mean(y.map(x => x * x));
    if (!(power > 1e-12)) return { frequency: null, fraction: 0 };
    const t0 = points[0].t;
    // Time-weighted least-squares sinusoid at original, irregular timestamps.
    // A sinusoid explains a large fraction of variance; band-limited random
    // noise must additionally pass autocorrelation, half-window and SNR gates.
    let best = { frequency: null, fraction: 0 };
    // Advance each sample's phase along the existing 0.01 Hz frequency grid.
    // This is the same fit at every original timestamp, with O(samples) trig
    // calls instead of O(samples * frequencies); no frames are downsampled.
    const phases = points.map((point, i) => {
      const offset = 2 * Math.PI * (point.t - t0);
      return {
        s: Math.sin(band[0] * offset), c: Math.cos(band[0] * offset),
        ds: Math.sin(0.01 * offset), dc: Math.cos(0.01 * offset),
        dt: i ? point.t - points[i - 1].t : points[1].t - t0
      };
    });
    for (let f = band[0]; f <= band[1] + 1e-9; f += 0.01) {
      let ss = 0, cc = 0, sc = 0, ys = 0, yc = 0;
      for (let i = 0; i < points.length; i++) {
        const phase = phases[i], { s, c, dt } = phase;
        ss += s * s * dt; cc += c * c * dt; sc += s * c * dt;
        ys += y[i] * s * dt; yc += y[i] * c * dt;
        phase.s = s * phase.dc + c * phase.ds;
        phase.c = c * phase.dc - s * phase.ds;
      }
      const determinant = ss * cc - sc * sc;
      if (determinant <= 1e-12) continue;
      const a = (ys * cc - yc * sc) / determinant;
      const b = (yc * ss - ys * sc) / determinant;
      const duration = points.at(-1).t - t0 + (points[1].t - t0);
      const fraction = Math.max(0, Math.min(1, (a * ys + b * yc) / duration / power));
      if (fraction > best.fraction) best = { frequency: f, fraction };
    }
    return best;
  }

  function periodicity(points, band = CANDIDATE_BAND_HZ) {
    if (!band) return { valid: false, label: "当前滤波频带未覆盖 0.7～3 Hz 候选周期范围，仅显示曲线", bpm: null, periodicity: 0, spectralFraction: 0 };
    if (points.length < 100 || points.at(-1).t - points[0].t < 5) return { valid: false, label: "等待足够的连续周期", bpm: null, periodicity: 0, spectralFraction: 0 };
    const peak = spectralPeak(points, band);
    if (!peak.frequency) return { valid: false, label: "没有稳定的周期信号", bpm: null, periodicity: 0, spectralFraction: 0 };
    const lag = 1 / peak.frequency;
    const left = [], right = [];
    let j = 0;
    for (const p of points) {
      const target = p.t - lag;
      if (target < points[0].t) continue;
      while (j + 1 < points.length && points[j + 1].t < target) j++;
      if (j + 1 >= points.length) break;
      const a = points[j], b = points[j + 1];
      const fraction = (target - a.t) / (b.t - a.t);
      left.push(p.value); right.push(a.value + fraction * (b.value - a.value));
    }
    const lc = centered(left), rc = centered(right);
    const correlation = dot(lc, rc) / Math.max(1e-12, Math.sqrt(dot(lc, lc) * dot(rc, rc)));
    const middle = Math.floor(points.length / 2);
    const first = spectralPeak(points.slice(0, middle), band);
    const second = spectralPeak(points.slice(middle), band);
    const stable = first.frequency != null && second.frequency != null && Math.abs(first.frequency - second.frequency) <= 0.18;
    const enough = (points.at(-1).t - points[0].t) * peak.frequency >= 4;
    const valid = peak.fraction >= 0.35 && correlation >= 0.65 && stable && enough;
    return { valid, label: valid ? "发现稳定候选周期" : "周期不稳定或随机噪声占优", bpm: valid ? peak.frequency * 60 : null, periodicity: correlation, spectralFraction: peak.fraction };
  }

  function fusedNoise(weights, covariance) {
    return Math.sqrt(Math.max(0, dot(weights, covariance.map(row => dot(row, weights)))));
  }

  function noiseWeights(noise) {
    // Unloaded noise says nothing about response sign or mechanical sensitivity.
    // The only spatial prior is the operator's identification of FBG5 as centre.
    // Its explicit half-share prevents a quiet distant channel taking over.
    const precision = noise.map(sigma => 1 / Math.max(sigma, NOISE_FLOOR_PM) ** 2);
    const total = precision.reduce((sum, value) => sum + value, 0);
    return precision.map((value, j) => (1 - CENTER_MIN_WEIGHT) * value / total + (j === CENTER_CHANNEL ? CENTER_MIN_WEIGHT : 0));
  }

  /** Session states: idle -> noise -> monitor (automatically).
   * beginNoise({confirmed:true}) explicitly confirms 10 s unloaded acquisition.
   * Completed band-limited noise fixes positive, sum-one weights once, using
   * inverse variance and an explicit >=50% FBG5 centre prior. No press, response
   * sensitivity, response sign, or spatial order of other FBGs is inferred.
   * The curve keeps running with the same weights whether a hand is present.
   * Motion and source gaps split the displayed history, clear periodic estimates
   * and rewarm filters without erasing earlier real samples. Warmup points are
   * visible but cannot contribute to a candidate. stopMonitor retains weights.
   * Source/identity/reference/missing-channel faults invalidate calibration.
   * Empty batches create no synthetic frames. pause retains completed weights
   * and resumes automatically with valid same-binding frames; interrupt discards.
   * Candidate periodic trends are never a medical pulse diagnosis.
   */
  class PulseSession {
    constructor({ filterBandHz = BAND_HZ } = {}) {
      this.revision = 0; this._configureFilterBand(validateFilterBandHz(filterBandHz)); this.reset();
    }
    _configureFilterBand(band) {
      this.filterBandHz = Object.freeze(band.slice());
      const low = Math.max(band[0], CANDIDATE_BAND_HZ[0]), high = Math.min(band[1], CANDIDATE_BAND_HZ[1]);
      this.candidateBandHz = low < high ? Object.freeze([low, high]) : null;
      this.warmupSeconds = band[0] >= BAND_HZ[0] ? WARMUP_SECONDS : 2.1 / band[0];
      // Every coefficient is computed strictly below the source Nyquist limit.
      // A wider display band can therefore require shorter source intervals.
      this.maxGapSeconds = Math.min(MAX_GAP_SECONDS, 0.45 / band[1]);
    }
    setFilterBand(band) {
      const validated = validateFilterBandHz(band);
      if (validated.every((value, j) => value === this.filterBandHz[j])) return this.snapshot();
      this._configureFilterBand(validated);
      return this.reset("滤波频带已修改，请确认无载并重新采集噪声以生成固定权重");
    }
    reset(reason = "请先确认无载并采集噪声") {
      this.revision++;
      this.phase = "idle"; this.reason = reason; this.progress = 0;
      this.weights = null; this.noise = null; this.noiseCovariance = null; this.noiseBinding = null; this.baselinePm = null;
      this.rawNoise = null; this.rawCovariance = null; this.rawNoiseRows = [];
      this.calibration = null; this.noisePoints = []; this.curveKind = null;
      this.motionLimits = null; this.fusedMotionLimit = null;
      this.binding = null; this.epoch = null; this.sourceKind = null;
      this.current = null; this.valuePm = null; this.points = []; this.rows = [];
      this.suspended = false; this.pendingBreak = false; this.pointVersion = 0;
      this.candidateBpm = null; this.quality = emptyQuality(reason);
      this.processedCount = 0; this.rejectedCount = 0;
      this.gapCount = 0; this.maxGapMs = 0;
      this.filterIntervalLimitCount = 0;
      this.effectiveNoiseSeconds = 0; this.noiseFilteredSeconds = 0;
      this.noiseRawSamples = 0; this.noiseFilteredSamples = 0;
      this._clearStream();
      return this.snapshot();
    }
    _clearStream(keepWatermark = false) {
      this.filters = makeFilters(this.filterBandHz); this.last = null; this.startT = null; this.segmentStartT = null;
      this.times = []; this.rateHz = null;
      if (!keepWatermark) { this.frameIds = new Set(); this.watermark = null; }
      this.lastQualityT = null; this.monitorStartT = null;
    }
    _fail(reason) {
      this.phase = "failed"; this.reason = reason; this.progress = 0;
      this.weights = null; this.noise = null; this.noiseCovariance = null; this.noiseBinding = null; this.baselinePm = null;
      this.rawNoise = null; this.rawCovariance = null; this.rawNoiseRows = [];
      this.calibration = null; this.noisePoints = []; this.curveKind = null;
      this.motionLimits = null; this.fusedMotionLimit = null;
      this.valuePm = null; this.points = []; this.rows = [];
      this.suspended = false; this.pendingBreak = false; this.pointVersion++;
      this.candidateBpm = null; this.quality = emptyQuality(reason);
      this.revision++; this._clearStream();
    }
    interrupt(reason = "光谱数据已中断，请重新采集无载噪声") {
      if (!["idle", "failed"].includes(this.phase)) this._fail(reason);
      return this.snapshot();
    }
    pause(reason = "光谱数据暂时中断，请确认恢复后继续") {
      if (["idle", "failed"].includes(this.phase)) return this.snapshot();
      if (!this.noise) {
        this._fail(`${reason}；本次无载采集未完成，请重新采集无载噪声`);
        return this.snapshot();
      }
      this.phase = "monitor"; this.suspended = true;
      this.reason = `${reason}；保留曲线和固定权重，等待同一采集源自动恢复`;
      this.progress = 1; this.rows = []; this.valuePm = null;
      this.curveKind = "monitor";
      this.pendingBreak = true;
      this.candidateBpm = null; this.quality = emptyQuality(this.reason);
      this.revision++; this._clearStream(true);
      return this.snapshot();
    }
    beginNoise({ confirmed = false } = {}) {
      if (confirmed !== true) throw new Error("请明确确认传感器无载后再采集噪声");
      this.reset(); this.phase = "noise"; this.reason = `无载噪声采集中，前 ${this.warmupSeconds.toFixed(1).replace(/\.0$/, "")} 秒预热；断段后自动补足有效采样`;
      return this.snapshot();
    }
    beginMonitor() {
      if (!this.weights || !["calibrated_ready", "monitor"].includes(this.phase)) throw new Error("请先完成 10 秒无载噪声采集以固定权重");
      if (this.phase === "monitor") return this.snapshot();
      this.phase = "monitor"; this.progress = 1;
      this.reason = "固定权重曲线持续显示，可随时放手观察；预热中暂不判定周期";
      this.rows = []; this.valuePm = null; this.candidateBpm = null;
      this.suspended = false; this.pendingBreak = true;
      this.curveKind = "monitor"; this.quality = emptyQuality(this.reason);
      this.revision++; this._clearStream(true);
      return this.snapshot();
    }
    stopMonitor() {
      if (this.phase === "calibrated_ready" && this.weights) { this._clearStream(true); return this.snapshot(); }
      if (this.phase !== "monitor" || !this.weights) return this.snapshot();
      this.phase = "calibrated_ready"; this.progress = 1;
      this.reason = "曲线已停止，保留本次固定权重；可恢复持续观察";
      this.rows = []; this.suspended = false;
      this.curveKind = "monitor"; this.valuePm = this.points.at(-1)?.value ?? null;
      this.candidateBpm = null; this.quality = emptyQuality(this.reason);
      this.revision++; this._clearStream(true);
      return this.snapshot();
    }
    push(samples, snapshotOptions) {
      const list = Array.isArray(samples) ? samples : [samples];
      for (const sample of list) this._pushOne(sample);
      return this.snapshot(snapshotOptions);
    }
    _pushOne(sample) {
      const identity = x => typeof x === "string" ? x.length > 0 : Number.isInteger(x) && x >= 0;
      if (!sample || !Number.isInteger(sample.t_ns) || sample.t_ns <= 0 || !Number.isInteger(sample.seq) || sample.seq < 0 || !identity(sample.frame_id) || !identity(sample.stream_epoch) || typeof sample.binding !== "string" || !sample.binding || typeof sample.source_kind !== "string" || !sample.source_kind) {
        this.rejectedCount++;
        if (!["idle", "failed"].includes(this.phase)) this._fail("源帧时间、身份或参考绑定缺失，请重新采集");
        return;
      }
      if (/simulat|synthetic|demo|replay|模拟|回放/i.test(sample.source_kind)) {
        this.rejectedCount++;
        if (!["idle", "failed"].includes(this.phase)) this._fail("脉搏测量需要实际设备的连续源帧");
        return;
      }
      const active = !["idle", "failed"].includes(this.phase);
      if (active && this.binding != null && (sample.binding !== this.binding || sample.stream_epoch !== this.epoch || sample.source_kind !== this.sourceKind)) {
        this.rejectedCount++; this._fail("参考绑定或采集时段已改变，请重新采集无载噪声"); return;
      }
      const t = sample.t_ns / 1e9;
      const frameKey = String(sample.frame_id);
      const sameWatermarkSource = this.watermark && sample.binding === this.watermark.binding && sample.stream_epoch === this.watermark.epoch && sample.source_kind === this.watermark.sourceKind;
      if (sameWatermarkSource && (sample.t_ns <= this.watermark.t_ns || sample.seq <= this.watermark.seq || this.frameIds.has(frameKey))) {
        this.rejectedCount++; return;
      }
      if (this.watermark && !sameWatermarkSource) { this._clearStream(); }
      const values = sample.values_pm;
      const needed = this.weights ? this.weights.map((w, j) => w !== 0 ? j : null).filter(j => j != null) : Array.from({ length: CHANNELS }, (_, j) => j);
      if (!Array.isArray(values) || values.length !== CHANNELS || needed.some(j => !finite(values[j]))) {
        this.rejectedCount++;
        if (active) this._fail("参与合成的光栅漂移缺失或无效，请重新采集无载噪声");
        return;
      }
      this.binding = sample.binding; this.epoch = sample.stream_epoch; this.sourceKind = sample.source_kind;
      const sourceDt = this.last ? t - this.last.t : null;
      const filterIntervalLimited = sourceDt != null && sourceDt > this.maxGapSeconds + 1e-9 && sourceDt <= MAX_GAP_SECONDS + 1e-9;
      const split = ["noise", "monitor"].includes(this.phase) && sourceDt > this.maxGapSeconds + 1e-9;
      if (split) {
        this.gapCount++; this.maxGapMs = Math.max(this.maxGapMs, sourceDt * 1000);
        if (filterIntervalLimited) this.filterIntervalLimitCount++;
        if (sourceDt >= PAUSE_GAP_SECONDS - 1e-9 && this.phase === "noise") {
          // An unfinished unloaded capture cannot survive a long interruption.
          this.watermark = { t_ns: sample.t_ns, seq: sample.seq, binding: sample.binding, epoch: sample.stream_epoch, sourceKind: sample.source_kind };
          this.frameIds.add(frameKey);
          this.rejectedCount++;
          this.pause("连续源帧中断达到 1 秒，无载采集已暂停");
          return;
        }
        // No filter step, motion comparison or periodic estimate crosses a gap.
        this.filters = makeFilters(this.filterBandHz); this.segmentStartT = t;
        this.times = []; this.rateHz = null; this.lastQualityT = null;
        if (this.phase === "monitor") {
          this.rows = []; this.valuePm = null; this.pendingBreak = true;
          this.candidateBpm = null; this.monitorStartT = t;
          this.quality = emptyQuality(filterIntervalLimited ? "源采样间隔不足以支持所选滤波上限，曲线已断段，等待更快采样后重新预热" : "源帧缺口已断开显示，保留固定权重，正在重新预热");
          this.reason = this.quality.label;
        }
      }
      const dt = sourceDt != null && !split ? sourceDt : 0.04;
      const previous = split ? null : this.last;
      this.last = { t, t_ns: sample.t_ns, seq: sample.seq, raw: values.slice() };
      this.watermark = { t_ns: sample.t_ns, seq: sample.seq, binding: sample.binding, epoch: sample.stream_epoch, sourceKind: sample.source_kind };
      this.current = values.slice(); this.processedCount++;
      if (this.suspended) {
        this.suspended = false; this.pendingBreak = true;
        this.reason = "同一采集源已恢复，固定权重曲线继续显示，预热中暂不判定周期";
      }
      this.frameIds.add(frameKey);
      if (this.frameIds.size > 4000) this.frameIds.delete(this.frameIds.values().next().value);
      this.times.push(t); while (this.times.length > 1 && t - this.times[0] > 5) this.times.shift();
      this.rateHz = this.times.length > 1 ? (this.times.length - 1) / (t - this.times[0]) : null;
      // A deliberately paused curve still validates the source binding and
      // tracks its real rate, but must not evaluate filter coefficients while
      // source intervals can exceed the selected band's safety boundary.
      if (!active || this.phase === "calibrated_ready") return;
      if (this.startT == null) this.startT = t;
      if (this.segmentStartT == null) this.segmentStartT = t;
      if (this.phase === "monitor" && this.monitorStartT == null) this.monitorStartT = t;
      const elapsed = t - this.startT;
      const segmentElapsed = t - this.segmentStartT;
      let filtered = values.map((value, j) => finite(value) ? this.filters[j].lp.next(this.filters[j].hp.next(value - (this.baselinePm?.[j] || 0), dt), dt) : 0);
      const lowRate = segmentElapsed >= 2 && this.rateHz < MIN_RATE_HZ - 0.1;
      if (this.phase === "noise") {
        // Twenty hertz qualifies a periodic candidate, not an unloaded noise
        // estimate. A slower real source continues until both actual time and
        // sample-count requirements are met; no nominal-rate samples are made.
        if (previous) this.effectiveNoiseSeconds += sourceDt;
        this.rawNoiseRows.push({ t, raw: values.slice() });
        if (segmentElapsed + 1e-9 >= this.warmupSeconds) {
          this.rows.push({ t, filtered, raw: values.slice() });
          if (previous) this.noiseFilteredSeconds += Math.max(0, t - Math.max(previous.t, this.segmentStartT + this.warmupSeconds));
        }
        this.noiseRawSamples = this.rawNoiseRows.length; this.noiseFilteredSamples = this.rows.length;
        this.progress = Math.min(1, this.effectiveNoiseSeconds / NOISE_SECONDS, this.noiseFilteredSeconds / MIN_NOISE_FILTERED_SECONDS, this.noiseRawSamples / MIN_NOISE_RAW_SAMPLES, this.noiseFilteredSamples / MIN_NOISE_FILTERED_SAMPLES);
        this.reason = this.filterIntervalLimitCount ? "源采样间隔不足以支持所选滤波上限，无载噪声分段采集；请降低滤波上限或提高采样率" : this.gapCount ? "无载噪声采集中，缺口时间不计入有效 10 秒；各段滤波预热后自动补足" : segmentElapsed < this.warmupSeconds ? `无载噪声采集中，前 ${this.warmupSeconds.toFixed(1).replace(/\.0$/, "")} 秒预热` : lowRate ? `当前有效提峰率 ${this.rateHz.toFixed(1)} Hz，无载噪声继续采集；自动补足有效时间和真实样本` : "无载噪声采集中，请保持完全无载";
        const complete = this.effectiveNoiseSeconds + 1e-9 >= NOISE_SECONDS && this.noiseFilteredSeconds + 1e-9 >= MIN_NOISE_FILTERED_SECONDS && this.noiseFilteredSamples >= MIN_NOISE_FILTERED_SAMPLES && this.noiseRawSamples >= MIN_NOISE_RAW_SAMPLES;
        if (!complete && (elapsed >= MAX_NOISE_WALL_SECONDS || this.rawNoiseRows.length >= MAX_NOISE_FRAMES)) {
          this._fail(this.filterIntervalLimitCount ? "源采样间隔不足以支持所选滤波上限，无载有效采样不足；请降低滤波上限或提高采样率后重录" : "无载有效采样不足或缺帧过于频繁，请改善采集性能后重新录制无载"); return;
        }
        if (complete) {
          const columns = Array.from({ length: CHANNELS }, (_, j) => centered(this.rows.map(row => row.filtered[j])));
          this.noise = columns.map(column => rms(column));
          this.noiseCovariance = columns.map((column, j) => columns.map((other, k) => dot(column, other) / this.rows.length + (j === k ? NOISE_FLOOR_PM ** 2 : 0)));
          // An explicit display-only unloaded baseline; the device reference
          // and raw drift samples remain untouched. Median resists one outlier.
          this.baselinePm = Array.from({ length: CHANNELS }, (_, j) => quantile(this.rawNoiseRows.map(row => row.raw[j]), 0.5));
          const rawColumns = Array.from({ length: CHANNELS }, (_, j) => centered(this.rawNoiseRows.map(row => row.raw[j])));
          this.rawNoise = rawColumns.map(column => Math.max(rms(column), NOISE_FLOOR_PM));
          this.rawCovariance = rawColumns.map((column, j) => rawColumns.map((other, k) => dot(column, other) / this.rawNoiseRows.length + (j === k ? NOISE_FLOOR_PM ** 2 : 0)));
          if (![...this.noise, ...this.rawNoise, ...this.noiseCovariance.flat(), ...this.rawCovariance.flat()].every(finite)) {
            this._fail("无载噪声统计无效，请检查光栅数据后重新采集"); return;
          }
          this.rawNoiseRows = [];
          this.noiseBinding = this.binding;
          this.weights = noiseWeights(this.noise);
          this.motionLimits = this.rawNoise.map(sigma => Math.max(8, 30 * sigma));
          this.fusedMotionLimit = Math.max(8, 30 * fusedNoise(this.weights, this.rawCovariance));
          this.calibration = {
            method: CALIBRATION_METHOD, noisePolicy: NOISE_POLICY, centerChannel: "FBG5", centerIndex: CENTER_CHANNEL,
            centerMinWeight: CENTER_MIN_WEIGHT, noiseFloorPm: NOISE_FLOOR_PM,
            weightBasis: "band-noise-inverse-variance-with-center-prior",
            responseDirection: "not-inferred", responseSensitivity: "not-measured",
            channels: Array.from({ length: CHANNELS }, (_, j) => j),
            durationSeconds: elapsed, effectiveNoiseSeconds: this.effectiveNoiseSeconds,
            filteredSeconds: this.noiseFilteredSeconds, filterBandHz: this.filterBandHz.slice(),
            filterConfigSchema: FILTER_CONFIG_SCHEMA, candidateBandHz: this.candidateBandHz?.slice() || null,
            warmupSeconds: this.warmupSeconds, maxGapSeconds: this.maxGapSeconds,
            rawSampleCount: this.noiseRawSamples, filteredSampleCount: this.noiseFilteredSamples,
            // Overall source throughput includes missing intervals. It is not
            // the rate of only the retained contiguous/filter-warmed segments.
            sourceSpanRateHz: (this.noiseRawSamples - 1) / elapsed,
            binding: this.binding, streamEpoch: this.epoch, sourceKind: this.sourceKind
          };
          // This is actual unloaded filtered data, never a press or pulse preview.
          this.noisePoints = this.rows.map(row => ({ t: row.t, value: dot(row.filtered, this.weights) }));
          this.points = this.noisePoints.map(point => ({ ...point }));
          this.valuePm = this.points.at(-1)?.value ?? null;
          this.curveKind = "monitor"; this.phase = "monitor";
          this.progress = 1; this.rows = [];
          this.reason = "10 秒无载已完成，噪声权重已固定；曲线持续显示，可随时把手放到 FBG5 中心附近观察";
          this.quality = emptyQuality(this.reason);
          this.pendingBreak = true; this.pointVersion++;
          this.revision++; this._clearStream(true);
        }
      } else if (this.phase === "monitor") {
        const rawDelta = previous ? values.map((value, j) => value - previous.raw[j]) : null;
        // A low-weight auxiliary's isolated small jump must not erase the window.
        // Large centre jumps or a large actual fused jump still restart filtering.
        const movement = rawDelta && (Math.abs(rawDelta[CENTER_CHANNEL]) > this.motionLimits[CENTER_CHANNEL] || Math.abs(dot(rawDelta, this.weights)) > this.fusedMotionLimit);
        if (movement) {
          this.filters = makeFilters(this.filterBandHz); this.rows = []; this.pendingBreak = true;
          this.candidateBpm = null; this.monitorStartT = t;
          filtered = values.map((value, j) => finite(value) ? this.filters[j].lp.next(this.filters[j].hp.next(value - this.baselinePm[j], 0.04), 0.04) : 0);
          this.quality = emptyQuality("检测到明显移动，曲线继续显示，预热中暂不判定周期"); this.reason = this.quality.label;
          this.lastQualityT = null;
        }
        const warmup = t - this.monitorStartT < this.warmupSeconds;
        this.valuePm = dot(filtered, this.weights);
        this.points.push({ t, value: this.valuePm, warmup, breakBefore: this.pendingBreak });
        this.pendingBreak = false; this.pointVersion++;
        while (this.points.length && (t - this.points[0].t > 60 || this.points.length > 12000)) this.points.shift();
        if (warmup || lowRate) {
          this.candidateBpm = null;
          if (lowRate) this.rows = [];
          if (lowRate || (!movement && !split)) this.quality = emptyQuality(lowRate ? "实际有效提峰率不足 20 Hz，曲线继续显示，暂不判定周期" : "实时滤波预热中，曲线继续显示，暂不判定周期");
          this.reason = this.quality.label; return;
        }
        this.rows.push({ t, filtered, raw: values.slice() });
        // Match the bounded display history at high source rates. The actual
        // retained timestamps, rather than a nominal 12 s, still determine
        // whether periodicity has enough elapsed time and complete cycles.
        while (this.rows.length && (t - this.rows[0].t > 12 || this.rows.length > 12000)) this.rows.shift();
        if (this.lastQualityT == null || t - this.lastQualityT >= 1) {
          this.lastQualityT = t;
          const qualityPoints = this.points.slice(-this.rows.length);
          const periodic = periodicity(qualityPoints, this.candidateBandHz);
          const output = centered(qualityPoints.map(p => p.value));
          const baseline = fusedNoise(this.weights, this.noiseCovariance);
          const snr = rms(output) / Math.max(baseline, 0.005);
          const center = centered(this.rows.map(row => row.filtered[CENTER_CHANNEL]));
          const centerSnr = rms(center) / Math.max(this.noise[CENTER_CHANNEL], NOISE_FLOOR_PM);
          const centerCorrelation = Math.abs(dot(center, output) / Math.max(1e-12, Math.sqrt(dot(center, center) * dot(output, output))));
          // Distant gratings need not share the centre pulse. They do not get
          // to certify a periodic artifact when the physical centre is quiet.
          const valid = periodic.valid && snr >= 3 && centerSnr >= 3 && centerCorrelation >= 0.65;
          const label = !this.candidateBandHz ? periodic.label : snr < 3 ? "合成信号未明显高于无载噪声" : centerSnr < 3 ? "FBG5 中心信号未明显高于无载噪声" : centerCorrelation < 0.65 ? "合成曲线与 FBG5 中心信号一致性不足，等待接触恢复" : periodic.label;
          this.quality = { valid, label, correlation: centerCorrelation, centerCorrelation, centerSnr, periodicity: periodic.periodicity, spectralFraction: periodic.spectralFraction, snr, channels: needed.slice() };
          this.candidateBpm = valid ? periodic.bpm : null; this.reason = label;
        }
      }
    }
    // A synchronous drawing consumer may inspect this view without copying.
    // Treat the array and its points as read-only; retain snapshot() for copies.
    readPoints() { return this.points; }
    snapshot({ includePoints = true } = {}) {
      return {
        phase: this.phase, reason: this.reason, progress: this.progress,
        weights: this.weights?.slice() || null, noise: this.noise?.slice() || null,
        rawNoise: this.rawNoise?.slice() || null,
        baselinePm: this.baselinePm?.slice() || null,
        calibration: this.calibration ? { ...this.calibration, channels: this.calibration.channels.slice(), filterBandHz: this.calibration.filterBandHz.slice(), candidateBandHz: this.calibration.candidateBandHz?.slice() || null } : null,
        curveKind: this.curveKind,
        streamMode: STREAM_MODE,
        noisePolicy: NOISE_POLICY,
        rateHz: this.rateHz, current: this.current?.slice() || null,
        valuePm: this.valuePm,
        ...(includePoints ? { points: this.points.map(p => ({ ...p })) } : {}),
        pointCount: this.points.length, pointVersion: this.pointVersion, suspended: this.suspended,
        candidateBpm: this.candidateBpm, quality: copyQuality(this.quality),
        binding: this.binding, streamEpoch: this.epoch, sourceKind: this.sourceKind,
        revision: this.revision, processedCount: this.processedCount, rejectedCount: this.rejectedCount,
        gapCount: this.gapCount, maxGapMs: this.maxGapMs,
        filterIntervalLimitCount: this.filterIntervalLimitCount,
        effectiveNoiseSeconds: this.effectiveNoiseSeconds, noiseFilteredSeconds: this.noiseFilteredSeconds,
        noiseRawSamples: this.noiseRawSamples, noiseFilteredSamples: this.noiseFilteredSamples,
        filterConfigSchema: FILTER_CONFIG_SCHEMA, filterBandHz: this.filterBandHz.slice(), candidateBandHz: this.candidateBandHz?.slice() || null, filterDelayMs: null,
        filterDelayDescription: this.filterBandHz.every((value, j) => value === BAND_HZ[j]) ? "实时因果滤波，延迟随频率变化（约 90～410 ms）；预热曲线可见，前 3 秒不判定周期" : `实时因果滤波，延迟随所选频带与频率变化；预热曲线可见，前 ${this.warmupSeconds.toFixed(1).replace(/\.0$/, "")} 秒不判定周期`,
        warmupSeconds: this.warmupSeconds, maxGapSeconds: this.maxGapSeconds, lastSampleNs: this.last?.t_ns || null
      };
    }
  }
  return Object.freeze({ PulseSession, BAND_HZ, FILTER_CONFIG_SCHEMA, FILTER_LIMITS_HZ, CANDIDATE_BAND_HZ, validateFilterBandHz, NOISE_SECONDS, CENTER_CHANNEL, CENTER_MIN_WEIGHT, NOISE_FLOOR_PM, CALIBRATION_METHOD, STREAM_MODE, NOISE_POLICY, WARMUP_SECONDS, MIN_RATE_HZ, MAX_GAP_SECONDS, PAUSE_GAP_SECONDS });
});
