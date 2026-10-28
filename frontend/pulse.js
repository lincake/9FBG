(function (global) {
  "use strict";
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const format = (value, digits = 2) => finite(value) ? value.toFixed(digits) : "—";
  const EXPECTED_CALIBRATION_METHOD = "unloaded-noise-fbg5-prior-v1";
  const EXPECTED_STREAM_MODE = "continuous-noise-fixed-v1";
  const EXPECTED_NOISE_POLICY = "effective-source-time-and-samples-v1";
  const EXPECTED_FILTER_CONFIG_SCHEMA = "pulse-bandpass-config-v1";
  const PHASES = { idle: "准备录制噪声", noise: "正在记录 10 s 无负载", calibrated_ready: "曲线已暂停 · 固定权重已保留", monitor: "固定权重曲线持续显示 · 可随时上手", failed: "噪声参考已失效 · 请重新录制无负载" };
  const geometry = new WeakMap();
  function invalidateCanvas(canvas) { geometry.delete(canvas); }
  // Display reduction preserves each pixel's actual extrema, ordering and breaks.
  // The processing session always retains every original source sample.
  function displayPoints(points, start, end, width) {
    const output = []; let bucket = [], column = null;
    function flush() {
      if (!bucket.length) return;
      let lo = 0, hi = 0;
      for (let i = 1; i < bucket.length; i++) { if (bucket[i].value < bucket[lo].value) lo = i; if (bucket[i].value > bucket[hi].value) hi = i; }
      for (const index of [...new Set([0, lo, hi, bucket.length - 1])].sort((a, b) => a - b)) output.push(bucket[index]);
      bucket = [];
    }
    let previous = null;
    for (let point of points) {
      if (point.t < start) continue;
      const next = Math.floor((point.t - start) / Math.max(1e-9, end - start) * width);
      const gap = previous && point.t - previous.t > .12 + 1e-9;
      if (!finite(point.value) || point.breakBefore || previous && (gap || !!point.warmup !== !!previous.warmup)) { flush(); column = null; }
      if (gap && !point.breakBefore) point = { ...point, breakBefore: true };
      if (!finite(point.value)) { output.push(point); previous = null; continue; }
      if (column !== next) { flush(); column = next; }
      bucket.push(point); previous = point;
    }
    flush(); return output;
  }
  function draw(canvas, points, seconds, ratio = 1) {
    ratio = Math.max(1, Math.min(3, ratio));
    let box = geometry.get(canvas);
    if (!box || box.ratio !== ratio) {
      const rect = canvas.getBoundingClientRect(); box = { width: rect.width, height: rect.height, ratio }; geometry.set(canvas, box);
    }
    const { width, height } = box; if (!width || !height) return;
    const pixelWidth = Math.round(width * ratio), pixelHeight = Math.round(height * ratio);
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    const ctx = canvas.getContext("2d"); if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
    const area = { x: 65, y: 28, w: Math.max(1, width - 85), h: Math.max(1, height - 73) };
    const end = points.at(-1)?.t ?? 0, start = end - seconds;
    let maximum = .05, count = 0;
    for (const point of points) if (point.t >= start && finite(point.value)) { maximum = Math.max(maximum, Math.abs(point.value)); count++; }
    const max = maximum * 1.15;
    const x = t => area.x + (t - start) / seconds * area.w;
    const y = v => area.y + (1 - v / max) * area.h / 2;
    ctx.font = '11px Consolas,"Microsoft YaHei UI",sans-serif'; ctx.fillStyle = "#536477";
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    ctx.fillText("等效漂移 / pm", 14, 17); ctx.strokeStyle = "#d9e2e9"; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const yy = area.y + i * area.h / 4;
      ctx.beginPath(); ctx.moveTo(area.x, yy); ctx.lineTo(area.x + area.w, yy); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillText(format(max * (1 - i / 2)), area.x - 9, yy + 4);
      ctx.textAlign = "center"; ctx.fillText(String(-seconds + i * seconds / 4), area.x + i * area.w / 4, area.y + area.h + 20);
    }
    ctx.textAlign = "right"; ctx.fillText("相对源接收时间 / s（最新＝0）", width - 20, height - 8);
    ctx.save(); ctx.beginPath(); ctx.rect(area.x, area.y, area.w, area.h); ctx.clip();
    let previous = null, warmup = null;
    ctx.lineWidth = 1.8; ctx.beginPath();
    for (const point of displayPoints(points, start, end, area.w)) {
      if (!finite(point.value)) { previous = null; continue; }
      if (!!point.warmup !== warmup) { ctx.stroke(); ctx.beginPath(); ctx.strokeStyle = point.warmup ? "#8ebbb2" : "#087f6b"; warmup = !!point.warmup; }
      if (!previous || point.breakBefore) ctx.moveTo(x(point.t), y(point.value));
      else ctx.lineTo(x(point.t), y(point.value));
      previous = point;
    }
    ctx.stroke(); ctx.restore();
    if (!count) { ctx.textAlign = "center"; ctx.fillText("完成 10 s 无负载后，自动持续显示合成曲线", area.x + area.w / 2, area.y + area.h / 2); }
  }
  const api = { draw, displayPoints, invalidateCanvas, PHASES };
  if (typeof module === "object" && module.exports) module.exports = api;
  if (!global.document) return;
  const $ = id => document.getElementById(id);
  const core = global.NineFBGPulse;
  if (core?.CALIBRATION_METHOD !== EXPECTED_CALIBRATION_METHOD || core?.STREAM_MODE !== EXPECTED_STREAM_MODE || core?.NOISE_POLICY !== EXPECTED_NOISE_POLICY || core?.FILTER_CONFIG_SCHEMA !== EXPECTED_FILTER_CONFIG_SCHEMA || !["pulseFreezeOthers", "pulseMonitorBtn", "pulseWeightSummary", "pulseBandLow", "pulseBandHigh", "pulseFilterApply", "pulseFilterForm", "pulseFilterStatus", "pulseFilterCurrent", "pulseFilterHelp", "pulseBandMetric"].every(id => $(id))) {
    if ($("pulseServiceNotice")) $("pulseServiceNotice").textContent = "脉搏页面与处理模块版本不一致，请从新版工作台重新打开“脉搏测量”，或按 Ctrl+F5 重新加载。本版 10 s 无负载后自动持续显示曲线。";
    if ($("pulsePhase")) $("pulsePhase").textContent = "页面版本不一致 · 请重新打开";
    for (const id of ["pulseReferenceBtn", "pulseNoiseBtn", "pulseLearnBtn", "pulseMonitorBtn", "pulseStopBtn", "pulseResetBtn", "pulseUnloaded", "pulseReady", "pulseContact", "pulseFreezeOthers", "pulseBandLow", "pulseBandHigh", "pulseFilterApply"]) if ($(id)) $(id).disabled = true;
    return;
  }
  const session = new core.PulseSession(), priority = global.NineFBGGuiPriority?.shared();
  const meta = () => session.snapshot({ includePoints: false });
  let cursor = 0, initialized = false, connected = false, available = false, polling = false;
  let canInitialize = false, referenceBusy = false, serviceVersion = null;
  let pending = null, generation = 0, active = true, lastFreshAt = null, lastSourceStamp = null;
  let renderQueued = false, chartKey = null, weightsKey = null;
  let filterError = "", filterMessage = "";
  const bandText = band => band.join("～") + " Hz";
  const displayedRateLimit = rate => Math.floor(Math.min(8, rate * .45) * 100 + 1e-9) / 100;
  let serviceMessage = "请先在工作台手动连接光谱；首次使用可在本页确认无载并初始化九峰参考。无需连接 PX6D。";
  const text = (id, value) => { const node = $(id); if (node.textContent !== value) node.textContent = value; };
  const html = (id, value) => { const node = $(id); if (node.innerHTML !== value) node.innerHTML = value; };
  function updatePriority(state) {
    priority?.setActive(active && !document.hidden && available && $("pulseFreezeOthers").checked && (pending === "noise" || ["noise", "monitor"].includes(state.phase)));
    text("pulsePriorityStatus", priority?.isActive() ? "合成曲线优先 · 其他新版 9FBG 界面已冻结，设备采集继续。" : "其他界面可正常刷新；设备采集继续。");
  }
  function render() {
    renderQueued = false;
    const state = meta(), learning = state.phase === "noise";
    updatePriority(state);
    const band = state.filterBandHz || core.BAND_HZ;
    const filterLocked = learning || pending !== null || referenceBusy;
    for (const id of ["pulseBandLow", "pulseBandHigh", "pulseFilterApply"]) $(id).disabled = filterLocked;
    text("pulseFilterCurrent", "当前频带 " + bandText(band));
    text("pulseBandMetric", "固定权重 · " + bandText(band) + " 带通");
    const dirtyBand = [$("pulseBandLow").value, $("pulseBandHigh").value].some((value, i) => String(value).trim() === "" || Number(value) !== band[i]);
    text("pulseFilterStatus", filterError || (dirtyBand ? "编辑尚未应用，曲线仍使用 " + bandText(band) + "；应用更改会清除当前噪声权重，需重新录制无负载。" : filterMessage || "输入后点击应用；更改频带需重新录制无负载噪声，匹配这次频带的权重。"));
    $("pulseFilterStatus").className = "pulse-filter-status" + (filterError ? " error" : filterMessage && !dirtyBand ? " success" : "");
    const rateLimit = finite(state.rateHz) && state.rateHz > 0 ? "当前有效提峰率下，上限须 ≤ " + format(displayedRateLimit(state.rateHz), 2) + " Hz。" : "上限还受实际有效提峰率限制。";
    const candidateBand = state.candidateBandHz;
    text("pulseFilterHelp", "范围 0.1～8 Hz，下限须小于上限。" + rateLimit + " 当前预热约 " + format(state.warmupSeconds || 3, 1) + " s，仍需补足预热后 7 s 噪声。" + (candidateBand ? "周期判断范围 " + bandText(candidateBand) + "。" : "当前频带未覆盖 0.7～3 Hz，曲线可显示，暂不判断周期。"));
    text("pulsePhase", pending ? "等待操作后的新源帧" : state.suspended ? "等待源帧恢复 · 曲线与权重保留" : PHASES[state.phase] || state.phase);
    text("pulseReason", state.reason || "尚未采集");
    $("pulseProgress").value = state.progress || 0;
    html("pulseBpm", format(state.candidateBpm, 0) + " <small>次/分</small>");
    html("pulseRate", format(state.rateHz, 1) + " <small>Hz</small>");
    html("pulseValue", format(state.valuePm, 3) + " <small>pm</small>");
    text("pulseQuality", state.phase === "calibrated_ready" ? "已暂停" : state.suspended ? "等待源帧" : state.phase === "monitor" && state.quality?.valid ? "周期稳定" : state.phase === "failed" ? "需重采噪声" : state.phase === "monitor" ? "观察中" : learning ? "采集中" : "等待");
    $("pulseQuality").title = state.quality?.label || state.reason;
    text("pulseWeightVersion", state.weights ? "无负载噪声固定权重 · FBG5 中心优先" : "尚无固定权重");
    text("pulseWeightSummary", state.weights ? "权重已固定 · FBG5 " + format(state.weights[4] * 100, 1) + "%" : learning ? "正在采集噪声 · 完成后自动计算" : "等待无负载噪声");
    $("pulseReferenceBtn").hidden = !canInitialize && !referenceBusy;
    $("pulseReferenceBtn").disabled = !canInitialize || referenceBusy || !$("pulseUnloaded").checked;
    $("pulseNoiseBtn").disabled = !available || referenceBusy || pending !== null || learning || !!state.weights || !$("pulseUnloaded").checked;
    $("pulseUnloaded").disabled = learning || pending !== null || referenceBusy || !!state.weights;
    $("pulseMonitorBtn").disabled = referenceBusy || pending !== null || !state.weights || state.phase === "calibrated_ready" && !available;
    text("pulseMonitorBtn", state.phase === "calibrated_ready" ? "继续显示曲线" : "暂停曲线 · 保留权重");
    $("pulseResetBtn").disabled = referenceBusy;
    text("pulseServiceNotice", state.phase === "failed" ? state.reason || serviceMessage : serviceMessage);
    $("pulseServiceNotice").className = "notice" + (available && state.phase !== "failed" ? " success" : "");
    text("pulseServerStatus", connected ? "本机服务已连接" : "本机服务未连接");
    $("pulseServerDot").className = "status-dot " + (connected ? "good" : "bad");
    text("pulseServiceVersion", serviceVersion ? "界面 0.18.0 / 采集服务 " + serviceVersion + (serviceVersion !== "0.18.0" ? "（接口兼容）" : "") : "等待采集服务");
    const noiseProgress = learning ? "有效无载 " + format(state.effectiveNoiseSeconds, 1) + "/10 s · 预热后 " + format(state.noiseFilteredSeconds, 1) + "/7 s · 原始有效样本 " + (state.noiseRawSamples || 0) + "/181 · 带内样本 " + (state.noiseFilteredSamples || 0) + "/126" : "";
    const gaps = state.gapCount > 0 ? "已分段处理 " + state.gapCount + " 次采样间隔 · 最大间隔 " + format(state.maxGapMs, 0) + " ms" : "";
    text("pulseTiming", [noiseProgress, gaps].filter(Boolean).join(" · "));
    $("pulseTiming").hidden = !learning && !gaps;
    const signature = JSON.stringify([state.weights, state.noise]);
    if (weightsKey !== signature) {
      weightsKey = signature;
      $("pulseWeights").innerHTML = Array.from({ length: 9 }, (_, i) => {
        const weight = state.weights?.[i], noise = state.noise?.[i];
        return '<tr' + (i === 4 ? ' class="pulse-center-row"' : '') + '><td>FBG' + (i + 1) + '</td><td>' + (finite(weight) ? weight.toFixed(4) + '<span class="pulse-weight-track"><i style="width:' + Math.min(100, weight * 100) + '%"></i></span>' : "—") + '</td><td>' + format(noise, 4) + '</td><td>' + (i === 4 ? "中心主路 · 操作员确认位置" : finite(weight) ? weight === 0 ? "未参与" : "噪声加权辅助" : "辅助通道 · 待计算") + '</td></tr>';
      }).join("");
    }
    text("pulseCurveKind", "持续合成曲线"); text("pulseChartTitle", "FBG5 中心附近 · 实时合成趋势");
    text("pulseChartNote", "随时放上或移开手，曲线持续显示，权重保持不变。" + bandText(band) + " 带通有相位延迟；运动与采样间隔过长处分段，浅色为预热段，暂不判定周期。源帧恢复后自动继续；更改频带、参考或有效通道改变需重采噪声。");
    const key = [state.pointVersion, $("pulseWindow").value, global.devicePixelRatio || 1].join(":");
    if (key !== chartKey) { chartKey = key; draw($("pulseChart"), session.readPoints(), Number($("pulseWindow").value), global.devicePixelRatio || 1); }
  }
  function requestRender() {
    if (renderQueued) return;
    renderQueued = true;
    if (typeof global.requestAnimationFrame === "function") global.requestAnimationFrame(render); else render();
  }
  priority?.subscribe(({ reason }) => {
    if (reason === "resume") $("pulseFreezeOthers").checked = false;
    requestRender();
  });
  function reanchor() { initialized = false; lastFreshAt = null; lastSourceStamp = null; }
  function suspend(reason, { transient = false } = {}) {
    const state = meta();
    if (!["idle", "failed"].includes(state.phase)) {
      if (transient) { if (state.phase !== "calibrated_ready") session.pause(reason); }
      else session.interrupt(reason);
    }
    generation++; reanchor(); pending = null; available = false; canInitialize = false;
  }
  async function poll() {
    if (!active || document.hidden || polling || referenceBusy) return;
    polling = true; const token = generation, anchor = !initialized, controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
      // Future cursor requests only the current watermark, avoiding megabytes of
      // pre-action history. reset_required is expected ONLY for this anchor read.
      const response = await fetch("/api/pulse/samples?after=" + (anchor ? Number.MAX_SAFE_INTEGER : cursor), { cache: "no-store", signal: controller.signal });
      if (token !== generation) return;
      if (response.status === 404) throw new Error("当前服务未加载脉搏接口。请先停止并保存会话，受控关闭服务后通过 9FBG 快捷方式重新打开；页面刷新无法升级后台。");
      const batch = await response.json();
      if (token !== generation || !active || document.hidden) return;
      if (!response.ok) throw new Error(batch.error || "脉搏数据读取失败");
      if (batch.schema !== "nine_fbg_pulse_samples_v1" || !Number.isSafeInteger(batch.cursor) || batch.cursor < 0 || !Array.isArray(batch.samples)) throw new Error("脉搏数据格式不匹配，需要支持连续九峰数据的 9FBG 服务。");
      connected = true; serviceVersion = typeof batch.version === "string" ? batch.version : "版本未知";
      canInitialize = batch.can_initialize === true && !referenceBusy;
      const hardwareRunning = batch.running === true && batch.stopping !== true && batch.mode === "hardware";
      const sourceTimeout = hardwareRunning && finite(batch.age_ms) && batch.age_ms > 1000 && (batch.blocked !== true || batch.reason === "光谱超过 1 秒未更新或缺少源帧接收时间");
      available = batch.blocked !== true && hardwareRunning && !sourceTimeout;
      serviceMessage = available ? "光谱已连接 · 10 s 无负载后自动持续显示合成曲线，可随时上手观察。FBG5 为中心主路，权重只计算一次。" : batch.running === false && batch.stopping !== true ? "请先返回工作台手动连接光谱；本页可独立初始化九峰参考，无需 PX6D。" : batch.reason || "等待有效光谱和九峰参考";
      const samples = batch.samples, sourceStamp = samples.at(-1)?.t_ns;
      if (finite(sourceStamp) && sourceStamp !== lastSourceStamp && finite(batch.age_ms) && batch.age_ms < 1000) {
        lastSourceStamp = sourceStamp; lastFreshAt = Date.now() - Math.max(0, batch.age_ms);
      }
      cursor = batch.cursor;
      const binding = meta().binding;
      const bindingChanged = binding != null && typeof batch.latest_binding === "string" && batch.latest_binding !== binding;
      const allowedReference = canInitialize;
      if (bindingChanged) { serviceMessage = "参考、配置、算法或采集时段已变化，请重新录制无负载噪声"; suspend(serviceMessage); }
      else if (!anchor && (batch.truncated || batch.reset_required)) {
        const transient = !batch.reset_required && hardwareRunning && (batch.blocked !== true || sourceTimeout);
        serviceMessage = transient ? "脉搏读取缓冲缺帧，曲线和权重保留，自动等待新源帧" : "源数据缓冲已重置或服务已重启，请重新录制无负载";
        suspend(serviceMessage, { transient });
      } else if (!available) {
        if (sourceTimeout) serviceMessage = "光谱源帧超过 1 秒未更新，保留曲线和权重，等待同一源恢复";
        suspend(serviceMessage, { transient: sourceTimeout }); canInitialize = allowedReference;
      } else if (anchor) {
        initialized = true;
        if (pending === "noise") session.beginNoise({ confirmed: true });
        if (pending === "monitor") session.beginMonitor();
        pending = null;
      } else session.push(samples, { includePoints: false });
    } catch (error) {
      if (token !== generation) return;
      connected = false; serviceMessage = error.name === "AbortError" ? "读取超时，曲线与权重保留，等待本机服务恢复" : error.message;
      suspend(serviceMessage, { transient: true });
    } finally { clearTimeout(timeout); polling = false; requestRender(); }
  }
  function arm(action) {
    generation++; reanchor(); pending = action;
    if (action === "noise") { session.reset(); $("pulseUnloaded").checked = false; }
    render(); poll();
  }
  function applyFilterBand() {
    if ($("pulseFilterApply").disabled || referenceBusy || pending !== null || meta().phase === "noise") return;
    const state = meta(); let next;
    try {
      const values = [$("pulseBandLow").value, $("pulseBandHigh").value];
      if (values.some(value => String(value).trim() === "")) throw new Error("请输入带通滤波下限和上限，单位 Hz。");
      next = core.validateFilterBandHz(values.map(Number));
      const unchanged = next.every((value, i) => value === state.filterBandHz[i]);
      if (unchanged) { filterError = ""; filterMessage = "频带未改变，现有曲线和权重继续保留。"; render(); return; }
      if (finite(state.rateHz) && state.rateHz > 0 && next[1] > state.rateHz * .45 + 1e-9) throw new Error("上限过高：当前有效提峰率约 " + format(state.rateHz, 1) + " Hz，请将上限设为 ≤ " + format(displayedRateLimit(state.rateHz), 2) + " Hz。");
      session.setFilterBand(next);
    } catch (error) { filterError = error.message; filterMessage = ""; render(); return; }
    generation++; pending = null; reanchor(); chartKey = null;
    $("pulseUnloaded").checked = false;
    $("pulseBandLow").value = String(next[0]); $("pulseBandHigh").value = String(next[1]);
    filterError = ""; filterMessage = "已应用 " + bandText(next) + "，请移开接触物，重新确认无负载并录制噪声。";
    render(); poll();
  }
  $("pulseFilterApply").addEventListener("click", applyFilterBand);
  $("pulseFilterForm").addEventListener("submit", event => { event.preventDefault(); applyFilterBand(); });
  for (const id of ["pulseBandLow", "pulseBandHigh"]) for (const event of ["input", "change"]) $(id).addEventListener(event, () => { filterError = ""; filterMessage = ""; render(); });
  $("pulseNoiseBtn").addEventListener("click", () => { if (!$("pulseNoiseBtn").disabled && $("pulseUnloaded").checked) arm("noise"); });
  $("pulseReferenceBtn").addEventListener("click", async () => {
    if ($("pulseReferenceBtn").disabled || !$("pulseUnloaded").checked) return;
    referenceBusy = true; generation++; pending = null;
    const token = generation; session.reset("正在初始化九峰参考，请保持无载");
    $("pulseUnloaded").checked = false; render();
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/api/pulse/reference", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ unloaded_confirmed: true }), signal: controller.signal });
      const result = await response.json();
      if (token !== generation || !active || document.hidden) return;
      if (!response.ok) throw new Error(result.error || "九峰参考初始化未通过");
      serviceMessage = "已明确确认无载，等待九峰稳定初始化；完成后请再次确认无载并采集噪声。";
    } catch (error) { if (token === generation) serviceMessage = error.name === "AbortError" ? "初始化响应超时，请查看参考状态，勿重复点击" : error.message; }
    finally { clearTimeout(timeout); referenceBusy = false; canInitialize = false; reanchor(); render(); poll(); }
  });
  $("pulseMonitorBtn").addEventListener("click", () => {
    if ($("pulseMonitorBtn").disabled) return;
    if (meta().phase === "calibrated_ready") arm("monitor");
    else { generation++; pending = null; reanchor(); session.stopMonitor(); render(); }
  });
  $("pulseResetBtn").addEventListener("click", () => {
    if (referenceBusy) return;
    generation++; pending = null; reanchor(); session.reset("固定权重已清除，请重新确认无负载并记录 10 s");
    $("pulseUnloaded").checked = false; render();
  });
  for (const id of ["pulseUnloaded", "pulseFreezeOthers", "pulseWindow"]) $(id).addEventListener("change", render);
  function resize() { invalidateCanvas($("pulseChart")); chartKey = null; requestRender(); }
  global.addEventListener("resize", resize);
  if (typeof global.ResizeObserver === "function") new global.ResizeObserver(resize).observe($("pulseChart"));
  function leave() {
    generation++; pending = null; reanchor(); available = false;
    const state = meta();
    if (state.weights && state.phase === "monitor") session.pause("页面暂时隐藏，返回后自动继续观察");
    else if (!state.weights) session.interrupt("页面暂时隐藏，本次无载采集未完成，请重新录制");
    updatePriority(meta());
  }
  global.addEventListener("pagehide", () => { active = false; leave(); });
  global.addEventListener("pageshow", () => { if (!active) { active = true; generation++; reanchor(); chartKey = null; render(); poll(); } });
  document.addEventListener("visibilitychange", () => { if (document.hidden) { leave(); render(); } else { reanchor(); resize(); poll(); } });
  setInterval(() => {
    if (active && !document.hidden && lastFreshAt !== null && Date.now() - lastFreshAt > 1000) {
      suspend("光谱源帧超过 1 秒未更新，保留曲线和权重，等待同一源恢复", { transient: true }); requestRender();
    }
    if (!document.hidden) poll();
  }, 100);
  render(); poll();
})(typeof globalThis !== "undefined" ? globalThis : this);
