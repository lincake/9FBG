(function (global, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  global.NineFBGWavelengthChart = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const IDS = Array.from({ length: 9 }, (_, index) => "FBG" + (index + 1));
  const COLORS = ["#a86408", "#087f6b", "#246bc1", "#8a49b3", "#c04448", "#6b7a0d", "#087d98", "#b23d7d", "#5a59ac"];
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const idValid = value => typeof value === "string" && value !== "" || finite(value);
  const fmt = (value, digits) => finite(value) ? (Object.is(Number(value.toFixed(digits)), -0) ? 0 : value).toFixed(digits) : "—";
  function inspect(state) {
    const tracker = state?.tracker || {}, frame = state?.frame;
    const references = Array.isArray(tracker.reference_nm) && tracker.reference_nm.length === 9 && tracker.reference_nm.every(finite) ? tracker.reference_nm : null;
    const epoch = tracker.stream_epoch;
    const identity = JSON.stringify([epoch, tracker.frame_id]);
    const binding = JSON.stringify([epoch, tracker.reference_version, references, tracker.config_version, tracker.algorithm_version]);
    const sameFrame = frame && idValid(tracker.frame_id) && tracker.frame_id === frame.frame_id && tracker.host_rx_monotonic_ns === frame.host_rx_monotonic_ns && epoch === frame.stream_epoch;
    let reason = "";
    if (!state) reason = "本机服务状态中断，等待新帧";
    else if (!state.running) reason = "设备未连接，等待光谱数据";
    else if (!references || !tracker.reference_version) reason = "尚无九峰参考，请先确认无载并建立参考";
    else if (tracker.reference_pending) reason = "九峰参考建立中，等待参考就绪";
    else if (frame?.valid === false || tracker.valid === false) reason = "源帧无效，等待有效光谱数据";
    else if (tracker.display_stale || finite(tracker.display_age_ms) && tracker.display_age_ms > 1000) reason = "光谱数据已陈旧，等待新帧";
    else if (!sameFrame || !idValid(epoch) || !Number.isInteger(tracker.host_rx_monotonic_ns) || tracker.host_rx_monotonic_ns <= 0) reason = "等待同一源帧的有效九峰结果";
    const peaks = new Map();
    for (const peak of Array.isArray(tracker.peaks) ? tracker.peaks : []) if (IDS.includes(peak.id) && !peaks.has(peak.id)) peaks.set(peak.id, peak);
    const values = IDS.map(id => {
      const peak = peaks.get(id);
      return !reason && peak?.valid === true && peak.stale !== true && finite(peak.center_nm) && finite(peak.drift_nm) && peak.value_frame_id === tracker.frame_id ? { nm: peak.center_nm, pm: peak.drift_nm * 1000 } : null;
    });
    return { binding, identity, stamp: tracker.host_rx_monotonic_ns, values, reference: tracker.reference_version, reason, age: Math.max(0, finite(tracker.display_age_ms) ? tracker.display_age_ms : 0) };
  }
  class History {
    constructor() {
      this.points = []; this.binding = null; this.identity = null; this.stamp = null;
      this.current = IDS.map(() => null); this.breaks = IDS.map(() => true);
      this.acceptedAt = null; this.acceptedAge = 0; this.reason = "等待光谱数据"; this.reference = null; this.seen = new Set();
    }
    interrupt(reason = "光谱数据中断，等待新帧") {
      this.breaks.fill(true); this.current.fill(null); this.reason = reason;
    }
    ingest(state, now) {
      const sample = inspect(state);
      if (state && this.binding !== sample.binding) {
        this.points = []; this.identity = null; this.stamp = null; this.acceptedAt = null; this.seen.clear();
        this.interrupt(); this.binding = sample.binding;
      }
      this.reference = sample.reference;
      if (sample.reason) { this.interrupt(sample.reason); return false; }
      if (sample.identity === this.identity && sample.stamp === this.stamp) {
        // Repeated polls are not measurements and never refresh source age.
        sample.values.forEach((value, index) => { if (!value) { this.breaks[index] = true; this.current[index] = null; } });
        if (sample.values.some(value => !value)) this.reason = "部分光栅无效，曲线已断开";
        this.tick(now); return false;
      }
      if (this.seen.has(sample.identity) || this.stamp !== null && sample.stamp <= this.stamp) {
        this.interrupt("源帧重复或乱序，等待新的有效源帧"); return false;
      }
      const gap = this.stamp !== null && sample.stamp - this.stamp > 1e9;
      const starts = sample.values.map((value, index) => !value || gap || this.breaks[index]);
      this.points.push({ t: sample.stamp / 1e9, values: sample.values.map(value => value?.pm ?? null), starts });
      const cutoff = sample.stamp / 1e9 - 120;
      while (this.points.length && this.points[0].t < cutoff) this.points.shift();
      if (this.points.length > 2400) this.points.splice(0, this.points.length - 2400);
      this.current = sample.values; this.breaks = sample.values.map(value => !value);
      this.identity = sample.identity; this.stamp = sample.stamp; this.acceptedAt = now; this.acceptedAge = sample.age;
      this.seen.add(sample.identity); if (this.seen.size > 2400) this.seen.delete(this.seen.values().next().value);
      const count = this.current.filter(Boolean).length;
      this.reason = count === 9 ? "九路有效" : count ? "部分光栅无效，曲线已断开" : "九路均无效，等待有效拟合";
      return true;
    }
    tick(now) {
      if (this.acceptedAt !== null && now - this.acceptedAt + this.acceptedAge > 1000 && this.current.some(Boolean)) this.interrupt("光谱数据超过 1 秒未更新，等待新帧");
    }
    visible(seconds) {
      const end = this.points.at(-1)?.t ?? 0;
      return { end, points: this.points.filter(point => point.t >= end - seconds) };
    }
  }
  function draw(canvas, history, seconds, ratio = 1) {
    const bounds = canvas.getBoundingClientRect();
    if (!(bounds.width > 0 && bounds.height > 0)) return false;
    const width = bounds.width, height = bounds.height, dpr = Math.min(3, Math.max(1, ratio || 1));
    const pixels = [Math.round(width * dpr), Math.round(height * dpr)];
    if (canvas.width !== pixels[0]) canvas.width = pixels[0];
    if (canvas.height !== pixels[1]) canvas.height = pixels[1];
    const ctx = canvas.getContext("2d");
    if (!ctx) return false;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, width, height);
    const { end, points } = history.visible(seconds), area = { x: 57, y: 27, w: Math.max(1, width - 71), h: Math.max(1, height - 66) };
    let min = 0, max = 0;
    for (const point of points) for (const value of point.values) if (finite(value)) { min = Math.min(min, value); max = Math.max(max, value); }
    const pad = Math.max(.5, (max - min) * .12); min -= pad; max += pad;
    const x = time => area.x + (time - end + seconds) / seconds * area.w;
    const y = value => area.y + (max - value) / (max - min) * area.h;
    ctx.font = '10px Consolas,"Microsoft YaHei UI",sans-serif'; ctx.fillStyle = "#536779"; ctx.textAlign = "left"; ctx.fillText("波长变化 Δλ / pm", 10, 13);
    ctx.strokeStyle = "#d9e2e9"; ctx.lineWidth = 1;
    for (let index = 0; index <= 4; index++) {
      const value = min + (max - min) * index / 4, yy = y(value);
      ctx.beginPath(); ctx.moveTo(area.x, yy); ctx.lineTo(area.x + area.w, yy); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillText(fmt(value, Math.max(Math.abs(min), Math.abs(max)) < 10 ? 2 : 1), area.x - 6, yy + 3);
      const xx = area.x + area.w * index / 4;
      ctx.textAlign = "center"; ctx.fillText(String(-seconds + seconds * index / 4), xx, area.y + area.h + 16);
    }
    ctx.textAlign = "right"; ctx.fillText("相对接收时间 / s（最新＝0）", width - 12, height - 4);
    ctx.save(); ctx.beginPath(); ctx.rect(area.x, area.y, area.w, area.h); ctx.clip();
    IDS.forEach((_, index) => {
      ctx.strokeStyle = COLORS[index]; ctx.lineWidth = 1.5; ctx.beginPath(); let previous = false;
      for (const point of points) {
        const value = point.values[index];
        if (!finite(value)) { previous = false; continue; }
        if (!previous || point.starts[index]) ctx.moveTo(x(point.t), y(value)); else ctx.lineTo(x(point.t), y(value));
        previous = true;
      }
      ctx.stroke();
      // An isolated valid sample is visible without inventing a connecting line.
      for (const point of points) if (finite(point.values[index]) && point.starts[index]) { ctx.fillStyle = COLORS[index]; ctx.beginPath(); ctx.arc(x(point.t), y(point.values[index]), 1.7, 0, Math.PI * 2); ctx.fill(); }
    });
    ctx.restore();
    if (!points.some(point => point.values.some(finite))) { ctx.fillStyle = "#536779"; ctx.textAlign = "center"; ctx.fillText("等待有效九峰波长数据", area.x + area.w / 2, area.y + area.h / 2); }
    return true;
  }
  function mount(options) {
    const { canvas, status, legend, windowSelect, global, visible } = options;
    let history = new History(), frozen = false; let lastDraw = -Infinity, suspended = false, lastText = "", dirty = true, lastGeometry = "";
    let seconds = [30, 60, 120].includes(Number(windowSelect.value)) ? Number(windowSelect.value) : 60;
    windowSelect.value = String(seconds);
    legend.innerHTML = IDS.map((id, index) => '<div class="wavelength-legend-item"><span><i style="background:' + COLORS[index] + '"></i>' + id + '</span><strong id="expWavelengthValue' + (index + 1) + '">—</strong><small id="expWavelengthCenter' + (index + 1) + '">—</small></div>').join("");
    const values = IDS.map((_, index) => global.document.getElementById("expWavelengthValue" + (index + 1)));
    const centers = IDS.map((_, index) => global.document.getElementById("expWavelengthCenter" + (index + 1)));
    function paint(now) {
      if (frozen || !visible()) return;
      history.tick(now);
      const text = (history.reference ? "九峰参考 第 " + history.reference + " 版 · " : "") + history.reason + " · 最近 " + seconds + " s";
      if (text !== lastText) { status.textContent = text; lastText = text; dirty = true; }
      history.current.forEach((value, index) => {
        const pm = value ? fmt(value.pm, 2) + " pm" : "—", nm = value ? fmt(value.nm, 5) + " nm" : "无有效值";
        if (values[index].textContent !== pm) values[index].textContent = pm;
        if (centers[index].textContent !== nm) centers[index].textContent = nm;
      });
      const bounds = canvas.getBoundingClientRect(), geometry = [bounds.width, bounds.height, global.devicePixelRatio || 1].join(":");
      if (geometry !== lastGeometry) { dirty = true; lastGeometry = geometry; }
      if (dirty && now - lastDraw >= 100 && draw(canvas, history, seconds, global.devicePixelRatio)) { dirty = false; lastDraw = now; }
    }
    function tick(now = Date.now()) {
      if (frozen) return;
      if (!visible()) { if (!suspended) { history.interrupt("图表已暂停，返回后等待新的有效源帧"); suspended = true; dirty = true; } return; }
      if (suspended) { suspended = false; dirty = true; }
      paint(now);
    }
    windowSelect.addEventListener("change", () => { if (frozen) return; seconds = [30, 60, 120].includes(Number(windowSelect.value)) ? Number(windowSelect.value) : 60; windowSelect.value = String(seconds); dirty = true; tick(global.Date.now()); });
    if (typeof global.ResizeObserver === "function") new global.ResizeObserver(() => { if (frozen) return; dirty = true; tick(global.Date.now()); }).observe(canvas);
    else global.addEventListener("resize", () => { if (frozen) return; dirty = true; tick(global.Date.now()); });
    global.document.addEventListener?.("visibilitychange", () => tick(global.Date.now()));
    return {
      setFrozen(next) {
        next = next === true;
        if (next === frozen) return;
        frozen = next; windowSelect.disabled = frozen;
        if (frozen) {
          status.textContent = (history.reference ? "九峰参考 第 " + history.reference + " 版 · " : "") + (history.points.length ? "采集期间显示已冻结 · 曲线与数值为采集开始前的预览快照" : "采集期间显示已冻结 · 等待采集会话结束后显示新帧");
          return;
        }
        // This history is for display only: do not retain or bridge the
        // interval during which collection had priority over demonstration.
        history = new History(); suspended = false; dirty = true;
        lastDraw = -Infinity; lastText = ""; lastGeometry = "";
        windowSelect.value = String(seconds);
      },
      update(state, now = global.Date.now()) {
        if (frozen) return;
        if (!visible()) { tick(now); return; }
        if (suspended) { suspended = false; dirty = true; }
        if (history.ingest(state, now)) dirty = true;
        paint(now);
      },
      tick, get history() { return history; }, get frozen() { return frozen; }
    };
  }
  return { IDS, COLORS, History, inspect, draw, mount };
});
