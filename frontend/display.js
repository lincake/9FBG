(function (global, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  global.NineFBGDisplay = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // This module only transforms copies for display. It has no device or storage API.
  const AXES = Object.freeze(["Fx", "Fy", "Fz", "Mx", "My", "Mz"]);
  const UNITS = Object.freeze(["N", "N", "N", "N·m", "N·m", "N·m"]);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const freshLimitMs = 250;
  const sourceStamp = force => Number.isInteger(force?.host_rx_monotonic_ns) && force.host_rx_monotonic_ns > 0 ? force.host_rx_monotonic_ns : null;
  const sourceID = value => (typeof value === "string" && value.trim() !== "") || (finite(value) && Number.isInteger(value) && value >= 0) ? value : null;

  function normalizeForce(force, { running = true, connected = true } = {}) {
    const base = {
      valid: false, reason: null, values: null,
      epoch: typeof force?.stream_epoch === "string" && force.stream_epoch ? force.stream_epoch : null,
      frameId: sourceID(force?.frame_id), stamp: sourceStamp(force), stale: false
    };
    const reject = (reason, stale = false) => ({ ...base, reason, stale });
    if (!running || !connected) return reject("六轴参考已断开或采集已停止", true);
    if (!force || typeof force !== "object") return reject("尚未收到六轴参考帧");
    if (force.stale === true || (finite(force.age_ms) && force.age_ms > freshLimitMs)) return reject("六轴参考超过 250 ms 未更新", true);
    if (force.valid === false) return reject(force.reason || "六轴参考帧无效");
    if (force.error) return reject("六轴参考帧包含错误：" + String(force.error));
    if (force.age_ms != null && (!finite(force.age_ms) || force.age_ms < 0)) return reject("六轴参考帧时效信息无效");
    if (!Array.isArray(force.force_values) || force.force_values.length !== 6 || !force.force_values.every(finite)) return reject("六轴参考必须包含 6 个有限数值");
    const axes = force.axes == null ? AXES : force.axes;
    if (!Array.isArray(axes) || axes.length !== 6 || new Set(axes).size !== 6 || axes.some(axis => !AXES.includes(axis))) return reject("六轴参考轴标识缺失、重复或未知");
    const units = force.units == null ? axes.map(axis => UNITS[AXES.indexOf(axis)]) : force.units;
    if (!Array.isArray(units) || units.length !== 6 || units.some((unit, index) => unit !== UNITS[AXES.indexOf(axes[index])])) return reject("六轴参考单位错误：力应为 N，力矩应为 N·m");
    return { ...base, valid: true, values: AXES.map(axis => force.force_values[axes.indexOf(axis)]) };
  }

  function usableNormalized(force) {
    return force && force.valid === true && force.stale !== true && Array.isArray(force.values) && force.values.length === 6 && force.values.every(finite);
  }
  function identifiable(force) {
    return typeof force.epoch === "string" && force.epoch.length > 0 && sourceID(force.frameId) != null && Number.isInteger(force.stamp) && force.stamp > 0;
  }
  function median(values) {
    const sorted = values.slice().sort((left, right) => left - right);
    const middle = sorted.length / 2;
    return (sorted[middle - 1] + sorted[middle]) / 2;
  }

  class DisplayZero {
    constructor() {
      this.required = 12;
      this.version = 0;
      this.pending = false;
      this.offsets = null;
      this.epoch = null;
      this.reason = "尚未进行显示置零";
      this._samples = [];
      this._last = null;
      this._seen = new Set();
    }

    _clear(reason) {
      if (this.offsets) this.version += 1;
      this.pending = false;
      this.offsets = null;
      this.epoch = null;
      this.reason = reason;
      this._samples = [];
      this._last = null;
      this._seen.clear();
    }

    request(confirmed, force) {
      if (confirmed !== true) throw new Error("请先确认接触棒已移开，六轴参考当前无载");
      const normalized = force && Object.prototype.hasOwnProperty.call(force, "values") ? force : normalizeForce(force);
      if (!usableNormalized(normalized)) throw new Error(normalized?.reason || "显示置零需要新鲜有效的六轴参考帧");
      if (!identifiable(normalized)) throw new Error("显示置零需要原始采集时段、帧标识和接收时间");
      this._clear("正在收集无载六轴参考帧");
      this.pending = true;
      this.epoch = normalized.epoch;
      return this.update(normalized);
    }

    update(force) {
      if (!usableNormalized(force)) {
        this._clear(force?.reason || "六轴参考中断，显示零点已清除");
        return this.snapshot();
      }
      if (!identifiable(force)) {
        this._clear("六轴参考缺少原始采集身份，显示零点已清除");
        return this.snapshot();
      }
      const key = JSON.stringify([force.epoch, force.frameId]);
      const adopt = () => {
        this.epoch = force.epoch;
        this._last = { key, stamp: force.stamp, frameId: force.frameId };
        this._seen.add(key);
      };
      if (this.epoch != null && force.epoch !== this.epoch) {
        this._clear("六轴参考采集时段已改变，请重新确认无载并显示置零");
        adopt();
        return this.snapshot();
      }
      if (this._last) {
        const oldID = this._last.frameId;
        const idBackwards = typeof oldID === "number" && typeof force.frameId === "number" && force.frameId < oldID;
        if (force.stamp < this._last.stamp || idBackwards) {
          this._clear("六轴参考帧乱序或时间回退，显示零点已清除");
          adopt();
          return this.snapshot();
        }
        // Polling the same acquired frame never adds a zeroing sample.
        if (key === this._last.key) return this.snapshot();
        if (force.stamp === this._last.stamp || this._seen.has(key)) {
          this._clear("六轴参考帧身份重复或采集时间未递增，显示零点已清除");
          adopt();
          return this.snapshot();
        }
      }
      adopt();
      // Browser state polling may skip hundreds of live source frames. Only
      // the server's freshness/connection flags can establish an outage;
      // elapsed time between two visible frame IDs cannot establish one.
      // The bounded identity cache remains useful after zeroing has completed.
      if (this._seen.size > 2048) this._seen.delete(this._seen.values().next().value);
      if (!this.pending) return this.snapshot();
      this._samples.push({ stamp: force.stamp, values: force.values.slice() });
      const minimumSpan = 400e6;
      // Keep a moving time window, including its left bracket. At 50 Hz,
      // twelve consecutive packets span only 220 ms: keep collecting rather
      // than committing a shorter observation or waiting forever on 12 packets.
      while (this._samples.length > this.required && this._samples[1].stamp <= force.stamp - minimumSpan) this._samples.shift();
      const span = force.stamp - this._samples[0].stamp;
      if (this._samples.length < this.required || span < minimumSpan) {
        this.reason = this._samples.length >= this.required ? "已收集 12 帧，等待无载采样跨越 400 ms" : "正在收集无载六轴参考帧";
        return this.snapshot();
      }
      // Select exactly 12 distinct frames distributed over the full time window.
      const selected = Array.from({ length: this.required }, (_, index) => this._samples[Math.round(index * (this._samples.length - 1) / (this.required - 1))]);
      this.offsets = AXES.map((_, axis) => median(selected.map(sample => sample.values[axis])));
      this.pending = false;
      this.version += 1;
      this.reason = "显示已置零（12 个不同源帧的中位数，采样跨度至少 400 ms）";
      this._samples = selected;
      return this.snapshot();
    }

    reset() {
      this._clear("显示零点已重置");
      return this.snapshot();
    }

    snapshot() {
      return {
        pending: this.pending, count: Math.min(this.required, this._samples.length), required: this.required,
        offsets: this.offsets ? this.offsets.slice() : null, active: this.offsets != null,
        reason: this.reason, epoch: this.epoch, version: this.version
      };
    }

    correct(values) {
      if (!Array.isArray(values) || values.length !== 6 || !values.every(finite)) throw new Error("显示修正需要 6 个有限六轴数值");
      return values.map((value, axis) => value - (this.offsets ? this.offsets[axis] : 0));
    }
  }

  function validateRange(min, max, label = "坐标范围") {
    if (!finite(min) || !finite(max) || !(max > min) || !finite(max - min)) throw new Error(`${label}需要有限数值，且上限必须大于下限`);
    return [min, max];
  }

  function validateSpectrum(frame) {
    const fail = reason => ({ valid: false, reason, xRange: null, yRange: null });
    if (!frame || !Array.isArray(frame.wavelength_nm) || !Array.isArray(frame.intensity) || frame.wavelength_nm.length !== 512 || frame.intensity.length !== 512) return fail("光谱需要完整的 512 点实际波长轴与原始强度");
    if (!frame.wavelength_nm.every(finite) || !frame.intensity.every(finite)) return fail("光谱波长与强度必须为有限数值");
    if (frame.wavelength_nm.some((value, index, values) => index > 0 && value <= values[index - 1])) return fail("实际波长轴必须严格递增，不能重复或倒序");
    const xRange = [frame.wavelength_nm[0], frame.wavelength_nm[511]];
    let min = Math.min(...frame.intensity), max = Math.max(...frame.intensity);
    if (min === max) {
      const padding = Math.max(Math.abs(min) * 0.05, 1);
      min -= padding;
      max += padding;
    }
    if (!finite(xRange[1] - xRange[0]) || !finite(min) || !finite(max) || !finite(max - min)) return fail("光谱坐标范围超出可显示范围");
    return { valid: true, reason: null, xRange, yRange: [min, max] };
  }

  // axisTicks returns { values: number[], digits: number, format(number): string }.
  // Values are in-range multiples of a 1/2/2.5/5/10 step. Very small/large
  // labels use scientific notation; other labels share dynamic precision.
  function axisTicks(range, count = 5) {
    if (!Array.isArray(range) || range.length !== 2) throw new Error("刻度需要完整的坐标范围");
    const [min, max] = validateRange(range[0], range[1], "刻度范围");
    if (!Number.isInteger(count) || count < 2 || count > 20) throw new Error("刻度数量需要为 2～20 的整数");
    const target = (max - min) / (count - 1);
    const power = 10 ** Math.floor(Math.log10(target));
    const factor = [1, 2, 2.5, 5, 10].find(value => value * power >= target) || 10;
    const step = factor * power;
    const digits = Math.max(0, -Math.floor(Math.log10(step)) + (factor === 2.5 ? 1 : 0));
    const values = [];
    const first = Math.ceil(min / step - 1e-10);
    const last = Math.floor(max / step + 1e-10);
    for (let index = first; index <= last && values.length < 40; index += 1) {
      const value = index * step;
      values.push(Object.is(value, -0) ? 0 : value);
    }
    if (!values.length) values.push(min, max);
    const scientific = digits > 8 || Math.max(Math.abs(min), Math.abs(max)) >= 1e9;
    return {
      values, digits,
      format(value) {
        if (!finite(value)) return "—";
        if (Math.abs(value) < step * 1e-8) value = 0;
        return scientific ? value.toExponential(2) : value.toFixed(digits);
      }
    };
  }

  function forceRange(values, manualLimit = null) {
    if (manualLimit != null) {
      if (!finite(manualLimit) || manualLimit <= 0 || !finite(manualLimit * 2)) throw new Error("六轴显示量程需要有限的正数");
      return [-manualLimit, manualLimit];
    }
    const samples = Array.isArray(values) ? values.filter(finite) : [];
    const largest = samples.length ? Math.max(...samples.map(Math.abs)) : 0;
    const limit = largest > 0 ? Math.min(largest * 1.15, Number.MAX_VALUE / 2) : 1;
    return [-limit, limit];
  }

  return { AXES, UNITS, normalizeForce, DisplayZero, validateSpectrum, validateRange, axisTicks, forceRange };
});
