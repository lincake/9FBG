(function (global) {
  "use strict";
  const display = global.NineFBGDisplay || (typeof require === "function" ? require("./display.js") : null);
  const COLORS = ["#a86408", "#087f6b", "#246bc1", "#8a49b3", "#c04448", "#6b7a0d", "#087d98", "#b23d7d", "#5a59ac"];
  const IDS = Array.from({ length: 9 }, (_, index) => `FBG${index + 1}`);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const numeric = value => finite(value) ? value : null;
  const fmt = (value, digits = 2) => { if (!finite(value)) return "—"; const text = value.toFixed(digits); return Number(text) === 0 ? text.replace(/^-/, "") : text; };
  const integer = value => finite(value) ? Math.max(0, Math.round(value)).toLocaleString("zh-CN") : "0";
  const escapeHTML = value => String(value == null ? "" : value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const LABELS = {
    disconnected: "尚未连接", idle: "等待无载初始化", uninitialized: "等待无载初始化", waiting: "等待稳定帧", stabilizing: "正在检查无载稳定性", initializing: "正在识别 9 个主峰", tracking: "持续跟踪中", ready: "持续跟踪中", initialized: "持续跟踪中", pressing: "按压采集中", error: "需要处理异常", failed: "检查未通过", hardware: "实际设备", replay: "原始数据回放", unknown: "尚未核实", unavailable: "不可用", device: "设备采集时间", device_timestamp: "设备采集时间", host: "主机接收时间", host_receive: "主机接收时间", host_rx_monotonic: "主机接收单调时钟", host_monotonic: "主机单调时钟", estimated: "估计时间", good: "可靠", poor: "时间质量较低", valid: "有效", missing: "缺失", exact: "设备时间对齐", nearest: "最近时间对齐", unsynced: "未同步", synced: "已同步", matched: "已匹配", software: "主机时间", host_receive_only: "仅有主机接收时间", host_receive_monotonic: "主机接收单调时钟", recorded: "原始记录时间", recorded_host_receive: "原始主机接收时间", not_connected: "未连接", no_reference: "尚无参考", not_initialized: "尚未初始化", stable: "稳定", unstable: "光谱尚未稳定", collecting: "正在收集稳定帧", frozen: "按压期间冻结", active: "正在连续保存", closed: "已结束保存", stopped: "已停止", pending: "等待核验", raw: "原始数据", none: "无", insufficient_points: "局部原始采样点不足", low_prominence: "显著度不足", fit_failed: "局部拟合失败", saturated: "探测器饱和", overlap: "峰重叠", distorted: "峰形严重变形", abnormal_jump: "波长异常跳变", missing_peak: "跟踪窗口内漏峰", invalid_frame: "光谱帧无效", low_amplitude: "峰幅度不足", width_out_of_bounds: "峰宽超出约束", residual_too_large: "拟合残差过大", window_boundary: "峰接近跟踪窗口边界", out_of_window: "峰超出跟踪窗口", stale: "旧值", current: "当前帧", gaussian: "局部受约束高斯拟合", template_shift: "局部参考光谱位移拟合", raw_device_values: "设备原始采样值", not_attested: "尚未核实", duplicate: "重复帧", no_match: "未找到同步参考"
  };
  Object.assign(LABELS, { awaiting_confirmation: "等待无载确认", reference_waiting: "等待确认后的新帧", reference_pending: "正在检查无载稳定性", input_invalid: "原始帧不可用", initialization_failed: "初始化未通过", tracking_degraded: "部分光栅质量异常", host_rx_approx: "主机接收近似时间", hardware_mapped: "硬件时钟已映射", raw_sdk_uint16_counts: "SDK 原始 uint16 计数", exact_receive_time: "同一接收时刻匹配", linear_receive_time: "接收时间插值匹配", nearest_receive_time: "最近接收时刻匹配" });
  function label(value, fallback = "—") {
    if (value == null || value === "") return fallback;
    if (typeof value === "object") return label(value.label || value.status || value.quality || value.kind, fallback);
    return LABELS[value] || String(value);
  }
  function normalizeID(value) {
    const match = /^(?:FBG)?([1-9])$/i.exec(String(value == null ? "" : value));
    return match ? `FBG${match[1]}` : null;
  }
  function normalizePeaks(peaks) {
    const byID = new Map();
    for (const peak of Array.isArray(peaks) ? peaks : []) {
      const id = normalizeID(peak.id);
      if (id && !byID.has(id)) byID.set(id, { ...peak, id, valid: peak.valid === true, stale: peak.stale === true });
    }
    return IDS.map(id => byID.get(id) || { id, valid: false, stale: false, failure_reason: "尚未获得测量" });
  }
  function windowBounds(window) {
    if (!window || typeof window !== "object") return null;
    const range = Array.isArray(window) ? window : window.range_nm || window.bounds_nm || window.window_nm;
    const lo = range ? range[0] : window.left_nm ?? window.min_nm ?? window.low_nm ?? window.lower_nm ?? window.start_nm;
    const hi = range ? range[1] : window.right_nm ?? window.max_nm ?? window.high_nm ?? window.upper_nm ?? window.end_nm;
    return finite(lo) && finite(hi) && hi > lo ? [lo, hi] : null;
  }
  function parseReplay(text) {
    const source = String(text).replace(/^\uFEFF/, "").trim();
    if (!source) throw new Error("文件为空，请选择包含原始光谱帧的 JSON 或 JSONL 文件。");
    let data;
    try { data = JSON.parse(source); }
    catch (error) {
      try { data = source.split(/\r?\n/).filter(line => line.trim()).map((line, index) => {
        try { return JSON.parse(line); } catch (_) { throw new Error(`第 ${index + 1} 行不是有效 JSON。`); }
      }); } catch (lineError) { throw new Error(`无法读取光谱文件：${lineError.message}`); }
    }
    let frames = Array.isArray(data) ? data : Array.isArray(data.frames) ? data.frames : data.frame ? [data.frame] : [data];
    // Session JSONL may contain metadata/reference events. Never treat them as optical frames.
    frames = frames.filter(item => item && typeof item === "object" && (item.wavelength_nm || item.frame?.wavelength_nm));
    if (!frames.length) throw new Error("文件中没有含 wavelength_nm 与 intensity 的原始光谱帧。");
    frames = frames.map((item, index) => {
      const frame = item.frame && item.frame.wavelength_nm ? item.frame : item;
      if (!Array.isArray(frame.wavelength_nm) || !Array.isArray(frame.intensity) || frame.wavelength_nm.length !== 512 || frame.intensity.length !== 512) throw new Error(`第 ${index + 1} 个光谱帧需要完整的 512 点波长轴与强度。`);
      if (!frame.wavelength_nm.every(finite) || !frame.intensity.every(finite)) throw new Error(`第 ${index + 1} 个光谱帧包含非数值或非有限采样值。`);
      if (frame.frame_id == null) throw new Error(`第 ${index + 1} 个光谱帧缺少原始 frame_id，无法核查重复帧。`);
      if (!Number.isInteger(frame.host_rx_monotonic_ns) || frame.host_rx_monotonic_ns <= 0) throw new Error(`第 ${index + 1} 个光谱帧缺少原始 host_rx_monotonic_ns 采集接收时间，不能用回放时间替代。`);
      return frame;
    });
    return frames;
  }
  function quality(peak) {
    if (peak.stale) return { tone: "warn", text: "旧值", reason: `${finite(peak.age_ms) ? `距有效帧 ${fmt(peak.age_ms, 0)} ms；` : ""}${label(peak.failure_reason, "本帧无效")}` };
    if (peak.valid) return { tone: "good", text: "有效", reason: label(peak.quality_warning || peak.fit_availability, "") };
    return { tone: peak.failure_reason === "尚未获得测量" ? "neutral" : "bad", text: "无效", reason: label(peak.failure_reason, "本帧无有效测量") };
  }
  function gates(state, unloaded, hasFile, busy) {
    const tracker = state.tracker || {};
    const pressing = state.pressing === true || state.press_active === true || tracker.pressing === true || tracker.reference_frozen === true || tracker.press_active === true;
    const version = tracker.reference_version;
    const referenced = version != null && version !== 0 && version !== "0" && version !== "";
    return { connect: !state.running && !state.stopping && !busy && (state.requested_mode !== "replay" || hasFile), disconnect: !!state.running && !busy, reference: !!state.running && !!unloaded && !pressing && !busy, start: !!state.running && referenced && !pressing && !busy, end: !!state.running && pressing && !busy, compare: hasFile && !busy, pressing, referenced };
  }
  function comparisonRows(result) {
    const root = result.comparison || result.results || result;
    const rows = Array.isArray(root) ? root : Array.isArray(root.methods) ? root.methods : Object.entries(root.methods || root.algorithms || {}).map(([name, value]) => ({ method: name, ...value }));
    return rows.map(row => ({ ...row, ...(row.summary || {}), method: row.method || row.name || row.algorithm }));
  }
  function devicePresentation(state) {
    const devices = Array.isArray(state.devices) ? state.devices : [];
    const optical = devices.find(device => device.wavelength_axis || device.sdk_status_raw || device.device_identity?.spectrum_points) || {};
    const force = devices.find(device => device.port_identity) || {};
    const axis = optical.wavelength_axis || state.frame?.wavelength_axis_audit || {};
    const sdk = optical.sdk_status_raw || optical.device_identity || {};
    const preprocessing = state.frame?.preprocessing || {};
    const forcePreprocessing = state.force?.preprocessing || {};
    const exposure = sdk.integration ?? sdk.integration_us ?? preprocessing.integration_us_requested;
    const mode = sdk.sensor_mode ?? preprocessing.sensor_mode_requested;
    const interval = sdk.interval_ms;
    const trigger = sdk.trigger_frequency_hz ?? sdk.trigger_hz;
    const readback = sdk.timing_settings_readback_verified ?? preprocessing.settings_readback_verified;
    const stepMin = axis.step_min_nm ?? axis.minimum_step_nm;
    const stepMax = axis.step_max_nm ?? axis.maximum_step_nm;
    return {
      opticalConnected: optical.connected === true, forceConnected: force.connected === true,
      axisSource: axis.source || "等待设备标定轴来源；不能依据截图或均匀轴估算",
      axisIdentity: `${axis.axis_artifact_device_id ? `标定文件 ${axis.axis_artifact_device_id}；` : ""}${axis.physical_device_identity_verified === true ? "实际设备对应已验证" : "实际设备与标定文件对应尚未验证"}`,
      axisSpacing: finite(stepMin) && finite(stepMax) ? `${axis.uniform === true ? "均匀" : axis.uniform === false ? "非均匀" : "均匀性待核实"} · ${fmt(stepMin, 5)}～${fmt(stepMax, 5)} nm` : "等待 512 点实际标定轴",
      integration: finite(exposure) ? `${fmt(exposure, 1)} μs（请求值）` : "等待设备请求信息",
      sensorMode: mode === 1 ? "HDR 1（高动态范围请求）" : mode == null ? "等待设备请求信息" : `模式 ${mode}（请求值）`,
      readInterval: finite(interval) && interval > 0 ? `${fmt(interval, 1)} ms · ${fmt(1000 / interval, 2)} Hz 请求` : "等待设备请求信息",
      trigger: finite(trigger) ? `${fmt(trigger, 1)} Hz（设备触发请求）` : "等待设备请求信息",
      timingReadback: `${finite(sdk.effective_integration_us) ? `按寄存器换算 ${fmt(sdk.effective_integration_us, 3)} μs；` : ""}${readback === true ? "时序设置读回已验证" : "时序设置未由 SDK 实时读回验证"}`,
      forceIdentity: force.port_identity ? `${force.port_identity.port || "—"} · 固件 ${force.firmware || "待核实"}` : "等待六轴设备接口信息",
      forceValueDefinition: Array.isArray(state.force?.force_values) ? "设备原始六轴返回值 · N / N·m" : "等待有效原始返回值",
      tare: forcePreprocessing.software_tare_applied === true ? "已应用软件去皮" : forcePreprocessing.software_tare_applied === false ? "未应用软件去皮" : "等待有效帧核验",
      hardwareZero: forcePreprocessing.hardware_zero_command_sent === true ? "本次连接已发送" : forcePreprocessing.hardware_zero_command_sent === false ? "本次连接未发送" : "等待有效帧核验",
      clock: optical.hardware_timestamp_available === false && force.hardware_timestamp_available === false ? "未提供共同硬件时间戳" : "尚未验证共同硬件时钟",
      readStatus: [optical.last_sdk_warning, optical.last_error, force.last_error].filter(Boolean).join("；") || (state.running ? "按请求采集；实际接收率见实时指标" : "采集未启动")
    };
  }
  function captureDisplayActive(state) {
    return state?.experiment?.running === true || state?.recording === true;
  }
  const api = { COLORS, IDS, finite, fmt, escapeHTML, label, normalizeID, normalizePeaks, windowBounds, parseReplay, quality, gates, comparisonRows, devicePresentation, captureDisplayActive };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.NineFBG = api;
  if (typeof document === "undefined") return;

  const $ = id => document.getElementById(id);
  const UI_VERSION = $("version").dataset.uiVersion;
  let state = { running: false, tracker: {}, metrics: {} }, busy = false, polling = false, active = true;
  let lastError = "", noticeTimer = null, rawGeometry = null, stateReceivedAt = Date.now();
  let lastPeakRenderKey = null, lastSpectrumRenderKey = null;
  let displayFreeze = null, serviceUnavailable = false;
  const guiPriority = global.NineFBGGuiPriority?.shared();
  let guiNeedsFreshState = guiPriority?.isFrozen() === true;
  let guiStateEpoch = 0;
  const guiFrozen = () => guiPriority?.isFrozen() === true;
  const guiPaused = () => guiFrozen() || guiNeedsFreshState;
  const wavelengthChart = global.NineFBGWavelengthChart?.mount({ canvas: $("expWavelengthChart"), status: $("expWavelengthStatus"), legend: $("expWavelengthLegend"), windowSelect: $("expWavelengthWindow"), global,
    visible: () => !guiPaused() && global.document.hidden !== true && $("expWavelengthChart").getBoundingClientRect().width > 0 });
  const forceZero = new display.DisplayZero();
  let forceSample = null;
  let spectrumView = null, restoredSpectrumView = null;
  function currentForce() {
    const devices = Array.isArray(state.devices) ? state.devices : [];
    const device = devices.find(item => item.port_identity);
    const force = state.force && { ...state.force, age_ms: finite(state.force.age_ms) ? state.force.age_ms + Math.max(0, Date.now() - stateReceivedAt) : state.force.age_ms };
    return display.normalizeForce(force, { running: state.running === true,
      connected: state.force_connected ?? device?.connected ?? !!state.force });
  }
  function displayHeld() { return guiPaused() || displayFreeze?.painted === true; }
  function renderGuiPriority() {
    $("guiPriorityNotice").hidden = !guiPaused();
    put("guiPriorityMessage", guiFrozen() ? "脉搏曲线优先 · 本页显示与状态刷新已暂停。当前读数为暂停前快照；后台采集、保存与训练继续。" : "正在恢复本页显示 · 等待最新设备状态，当前读数仍为暂停前快照。");
    $("guiPriorityResume").disabled = !guiFrozen();
    if (guiPaused()) {
      $("spectrumTooltip").hidden = true;
      put("runningBadge", guiFrozen() ? "脉搏曲线优先 · 本页显示已暂停" : "正在获取最新状态");
      put("serverStatus", guiFrozen() ? "本页状态刷新已暂停" : "正在获取最新状态");
    }
  }
  function updateDisplayFreeze() {
    if (captureDisplayActive(state)) {
      if (!displayFreeze) {
        displayFreeze = { painted: false, frame: state.frame?.frame_id ?? "—", reference: gates(state, false, false, busy).referenced ? state.tracker.reference_version : "未建立", time: new Date().toLocaleTimeString("zh-CN", { hour12: false }) };
        $("spectrumTooltip").hidden = true;
      }
    } else if (displayFreeze) {
      displayFreeze = null;
      // A completed session may retain its last IDs; always resume from the
      // latest state, even if those IDs match the captured display snapshot.
      lastPeakRenderKey = lastSpectrumRenderKey = null;
      rawGeometry = null;
      $("spectrumTooltip").hidden = true;
    }
  }
  function renderDisplayFreezeStatus() {
    const frozen = !!displayFreeze;
    $("captureDisplayNotice").hidden = !frozen;
    $("peakDisplayStatus").hidden = !frozen;
    $("liveDisplayPanel").classList.toggle("display-frozen", frozen);
    put("frameLabel", frozen ? "冻结原始帧" : "当前原始帧");
    if (!frozen) return;
    put("captureDisplaySnapshot", `冻结于 ${displayFreeze.time} · 原始帧 ${displayFreeze.frame} · 九峰参考 ${displayFreeze.reference === "未建立" ? "未建立" : "第 " + displayFreeze.reference + " 版"}`);
    put("peakDisplayStatus", "显示已冻结 · 表中为采集开始时的快照");
    const elapsed = Math.max(0, Date.now() - stateReceivedAt);
    const error = state.error || state.experiment?.error || state.session?.error || state.session?.save_error;
    const invalidPeaks = normalizePeaks(state.tracker?.peaks).filter(peak => !peak.valid || peak.stale).length;
    const problem = serviceUnavailable ? "本机服务连接中断，当前采样与保存状态无法确认" : error ? label(error) : state.session?.overflow ? "写盘异常，请查看下方采集状态" : !state.running ? "光谱设备未连接" : elapsed + (state.tracker?.display_age_ms || 0) > 1000 || state.tracker?.display_stale ? "光谱数据已过期，请查看下方采集状态" : !forceSample?.valid ? "PX6D：" + (forceSample?.reason || "当前六轴不可用") : ["error", "failed"].includes(state.tracker?.state) ? label(state.tracker.message || state.tracker.state) : invalidPeaks ? `${invalidPeaks} 路光栅当前无有效测量，请查看下方采集状态` : "";
    put("captureDisplayHealth", problem ? "当前状态：" + problem : "当前状态：光谱与 PX6D 接收有效，下方采集数据继续更新");
    $("captureDisplayNotice").classList.toggle("display-freeze-alert", !!problem);
  }
  function inputNumber(id, name) {
    const value = $(id).value.trim();
    if (!value || !finite(Number(value))) throw new Error(name + "需要有限数值。");
    return Number(value);
  }
  function spectrumInputView() {
    return {
      x: display.validateRange(inputNumber("spectrumXMin", "波长下限"), inputNumber("spectrumXMax", "波长上限"), "波长显示范围"),
      y: display.validateRange(inputNumber("spectrumYMin", "强度下限"), inputNumber("spectrumYMax", "强度上限"), "强度显示范围")
    };
  }
  function checkSpectrumView(view, actual) {
    if (view.x[0] < actual.xRange[0] - 0.00001 || view.x[1] > actual.xRange[1] + 0.00001) throw new Error("波长显示范围必须位于当前实际标定轴内。");
    return view;
  }
  function saveSpectrumSettings() {
    const read = id => { const value = $(id).value.trim(); return value && finite(Number(value)) ? Number(value) : null; };
    global.NineFBGPreferences?.save("display", {
      spectrum_mode: $("spectrumRangeMode").value,
      spectrum_x_min: read("spectrumXMin"), spectrum_x_max: read("spectrumXMax"),
      spectrum_y_min: read("spectrumYMin"), spectrum_y_max: read("spectrumYMax")
    });
  }
  function restoreSpectrumSettings() {
    spectrumView = restoredSpectrumView = null;
    if ($("spectrumRangeMode").value === "manual") {
      try { restoredSpectrumView = spectrumInputView(); }
      catch (error) { notify("已保存的光谱范围无效，使用完整光谱显示。" + error.message, "error"); }
    }
    updateControls(); drawSpectrum();
  }
  const put = (id, value) => { const element = $(id); if (element && element.textContent !== String(value)) element.textContent = value; };
  function notify(message, tone = "info", transient = false) {
    clearTimeout(noticeTimer);
    const node = $("notice"); node.textContent = message; node.className = `notice ${tone}`; node.hidden = !message;
    if (transient) noticeTimer = setTimeout(() => { node.hidden = true; }, 9000);
  }
  async function request(path, body) {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let result;
    try { result = await response.json(); } catch (_) { throw new Error(`本机服务返回了无法读取的响应（${response.status}）。`); }
    if (!response.ok || result.ok === false) throw new Error(result.detail || result.error || result.message || `操作未完成（${response.status}）。`);
    return result;
  }
  async function action(callback) {
    if (busy) return;
    if (guiPaused()) { notify("脉搏曲线优先，本页操作已暂停；请点击“恢复本页显示”，收到最新状态后再操作。", "info"); return; }
    busy = true; updateControls();
    try { await callback(); await poll(); }
    catch (error) { notify(error instanceof TypeError ? "无法连接本机服务，请检查 9FBG 服务是否正在运行。" : error.message || "操作失败，请检查本机服务。", "error"); }
    finally { busy = false; updateControls(); }
  }
  function updateControls() {
    const allowed = gates({ ...state, requested_mode: "hardware" }, false, false, busy);
    $("connectBtn").disabled = !allowed.connect;
    $("disconnectBtn").disabled = !allowed.disconnect;
    $("sourceMode").disabled = !!state.running || busy;
    put("connectBtn", "↗ 连接光谱");
    put("sourceNote", "连接前设置曝光与采样请求；光谱、PX6D 分别连接。刷新页面不会重启设备。");
    const forceConnected = state.force_connected === true || (state.force_connected == null && (state.devices || []).some(item => item.port_identity && item.connected));
    $("px6dConnectBtn").disabled = busy || !state.running || state.mode !== "hardware" || forceConnected || state.force_connecting;
    $("px6dDisconnectBtn").disabled = busy || (!forceConnected && !state.force_connecting);
    for (const id of ["integrationUs", "sampleRateHz"]) $(id).disabled = !!state.running || busy || $("sourceMode").value !== "hardware";
    if (captureDisplayActive(state) || allowed.pressing || !forceSample?.valid) $("forceUnloadedCheck").checked = false;
    $("forceZeroBtn").disabled = busy || !forceSample?.valid || !$("forceUnloadedCheck").checked || allowed.pressing || forceZero.snapshot().pending || captureDisplayActive(state);
    $("forceZeroReset").disabled = captureDisplayActive(state) || (!forceZero.snapshot().active && !forceZero.snapshot().pending);
    $("forceUnloadedCheck").disabled = allowed.pressing || forceZero.snapshot().pending || captureDisplayActive(state);
    put("initialCalibrationNote", state.experiment?.running ? "点位会话正在进行：请在采集工作区使用本轮无载标定，首次标定已禁用。" : "先连接光谱与 PX6D，再移开接触棒确认无载并标定；开始点位会话后使用本轮无载标定。");
    $("spectrumRangeApply").disabled = !!displayFreeze || !display.validateSpectrum(state.frame).valid;
    $("spectrumRangeReset").disabled = !!displayFreeze;
    $("spectrumRangeMode").disabled = !!displayFreeze;
    $("showWindows").disabled = !!displayFreeze;
    const manualSpectrum = $("spectrumRangeMode").value === "manual";
    for (const id of ["spectrumXMin", "spectrumXMax", "spectrumYMin", "spectrumYMax"]) $(id).disabled = !!displayFreeze || !manualSpectrum;
    if (guiPaused()) for (const id of ["connectBtn", "disconnectBtn", "sourceMode", "px6dConnectBtn", "px6dDisconnectBtn", "integrationUs", "sampleRateHz", "forceZeroBtn", "forceZeroReset", "forceUnloadedCheck", "spectrumRangeApply", "spectrumRangeReset", "spectrumRangeMode", "showWindows", "spectrumXMin", "spectrumXMax", "spectrumYMin", "spectrumYMax"]) $(id).disabled = true;
  }
  function renderPeaks(peaks) {
    if (displayHeld()) return;
    const renderKey = JSON.stringify([state.frame?.stream_epoch, state.frame?.frame_id, state.frame?.host_rx_monotonic_ns,
      state.tracker?.reference_version, state.tracker?.config_version, state.tracker?.algorithm_version,
      state.tracker?.display_stale === true,
      peaks.map(peak => [peak.id, peak.valid, peak.stale, peak.center_nm, peak.drift_nm, peak.amplitude,
        peak.fwhm_nm, peak.prominence, peak.residual, quality(peak)])]);
    if (renderKey === lastPeakRenderKey) return;
    $("peakTable").innerHTML = peaks.map((peak, index) => {
      const q = quality(peak);
      // Unavailable current fits must not present cached numbers without a stale mark.
      const available = peak.valid || peak.stale;
      return `<tr class="${peak.stale ? "stale-row" : ""}"><td><span class="fbg-id"><i style="background:${COLORS[index]}"></i>${peak.id}</span></td><td>${available ? fmt(peak.center_nm, 5) : "—"}</td><td>${available && finite(peak.drift_nm) ? fmt(peak.drift_nm * 1000, 2) : "—"}</td><td>${available ? fmt(peak.amplitude, 2) : "—"}</td><td>${available ? fmt(peak.fwhm_nm, 4) : "—"}</td><td>${available ? fmt(peak.prominence, 2) : "—"}</td><td>${available ? fmt(peak.residual, 3) : "—"}</td><td><span class="quality-pill ${q.tone}">${q.text}</span><span class="quality-reason">${escapeHTML(q.reason)}</span></td></tr>`;
    }).join("");
    lastPeakRenderKey = renderKey;
  }
  function axisDescription(axis, frame) {
    if (!frame?.wavelength_nm?.length) return "波长轴与预处理状态将在实际帧到达后核验";
    const uniform = axis.uniform ?? axis.is_uniform ?? axis.uniformly_spaced;
    const spacing = axis.mean_spacing_nm ?? axis.spacing_nm ?? axis.median_step_nm;
    const preprocessing = axis.preprocessing ?? axis.preprocessing_status ?? frame.preprocessing;
    let prepText = preprocessing === false || preprocessing === "none" || preprocessing === "raw" ? "未预处理" : preprocessing === true ? "设备端已预处理" : label(preprocessing, "设备预处理状态尚未核实");
    if (preprocessing && typeof preprocessing === "object") {
      const p = preprocessing;
      if (p.state === "raw_sdk_uint16_counts") prepText = "SDK 原始 uint16 计数 · 软件未扣背景、未平滑 · DLL 内部处理未公开";
      else if (p.smoothing_enabled === true || p.smoothing_applied === true || p.raw_available === false) prepText = "输入已预处理 · 原始拟合不可用";
      else if (p.raw_available === true) prepText = "原始采样可用";
    }
    return `${uniform === true ? "均匀波长轴" : uniform === false ? "非均匀波长轴" : "波长轴均匀性待核实"}${finite(spacing) ? ` · 采样间隔 ${fmt(spacing, 5)} nm` : ""} · ${prepText}`;
  }
  function renderReference(tracker, allowed) {
    if (!displayHeld()) {
    put("referenceVersion", allowed.referenced ? `第 ${tracker.reference_version} 版` : "未建立");
    put("freezeStatus", allowed.pressing ? "按压期间冻结" : allowed.referenced ? "无载确认后可更新" : "等待无载确认");
    $("freezeDot").className = `status-dot ${allowed.pressing ? "warn" : allowed.referenced ? "good" : ""}`;
    }
    put("referenceState", allowed.pressing ? "已冻结 · 本次按压" : allowed.referenced ? "已建立 · 等待按压" : "尚未建立");
    put("algorithmVersion", tracker.algorithm_version || "—");
    put("configVersion", tracker.config_version || "—");
    $("configVersion").title = tracker.config_version || "";
    const records = tracker.reference_history || tracker.reference_updates || state.reference_history || [];
    $("referenceHistory").innerHTML = Array.isArray(records) && records.length ? records.slice(-5).reverse().map(record => `<div><span>第 ${escapeHTML(record.version ?? record.reference_version ?? "—")} 版 · ${escapeHTML(label(record.reason || record.action, "无载回零"))}</span><span>${escapeHTML(record.utc ? new Date(record.utc).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : record.timestamp || record.updated_at || record.frame_id || "")}</span></div>`).join("") : "<p>尚无参考更新记录</p>";
  }
  function renderForce(force, sync) {
    forceSample = currentForce();
    const zero = forceZero.update(forceSample);
    if (!displayHeld()) {
      const forceValues = forceSample.valid ? forceZero.correct(forceSample.values) : null;
      $("forceGrid").innerHTML = display.AXES.map((axis, index) => {
      const unit = display.UNITS[index], digits = index < 3 ? 3 : 5, raw = forceSample.values?.[index], offset = zero.offsets?.[index] ?? 0;
      return '<div class="force-axis"><span>' + axis + '</span><strong class="force-value">' + (forceValues ? fmt(forceValues[index], digits) : "—") + '</strong><small>' + unit + '</small><div class="force-raw">' + (forceSample.valid ? "原始 " : "原始旧值 ") + fmt(raw, digits) + ' ' + unit + '</div><div class="force-offset">显示零点 ' + fmt(offset, digits) + ' ' + unit + '</div></div>';
      }).join("");
    }
    const synced = forceSample.valid && !state.tracker?.display_stale && sync?.valid === true;
    put("syncState", displayFreeze ? "六轴显示已冻结" : !forceSample.valid ? (force ? "六轴无效 / 旧值" : "尚未连接 PX6D") : synced ? "六轴实时 · 近似配对" : "六轴实时 · 未配对光谱");
    $("syncState").className = "quality-pill " + (displayFreeze ? "warn" : forceSample.valid ? "good" : force ? "warn" : "neutral");
    put("syncDetail", displayFreeze ? "数值为采集开始时的快照；当前六轴与本轮力值请查看下方采集区。" : forceSample.valid ? "显示最新六轴接收值 · " + (finite(force?.age_ms) ? "距接收 " + fmt(force.age_ms, 0) + " ms · " : "") + (synced ? "光谱仅按主机接收时间近似配对" : "当前光谱配对无效；六轴显示独立更新") : forceSample.reason);
    put("forceDisplayZeroStatus", zero.pending ? "正在建立无载显示零点 " + zero.count + "/" + zero.required + " 帧" : zero.active ? "显示零点已生效 · 第 " + zero.version + " 版 · 原始值减显示零点" : !forceSample.valid ? forceSample.reason : "尚未标定 · 显示设备原始值" + (zero.version > 0 ? " · 上次显示零点已失效，请重新标定" : ""));
    $("forceDisplayZeroStatus").title = zero.reason || "";
  }
  function renderDevices() {
    const d = devicePresentation(state);
    for (const [id, value] of Object.entries({ axisSource:d.axisSource, axisIdentity:d.axisIdentity, axisSpacing:d.axisSpacing, integrationRequest:d.integration, sensorModeRequest:d.sensorMode, readIntervalRequest:d.readInterval, triggerRequest:d.trigger, timingReadback:d.timingReadback, forceDeviceIdentity:d.forceIdentity, forceValueDefinition:d.forceValueDefinition, forceTareStatus:d.tare, forceZeroStatus:d.hardwareZero, syncBasis:"原始帧主机接收时间 · 近似同步", sharedClockStatus:d.clock, deviceReadStatus:d.readStatus })) put(id, value);
    for (const [id, connected] of [["opticalDeviceStatus",d.opticalConnected],["forceDeviceStatus",d.forceConnected]]) {
      put(id, connected ? "已连接" : state.running ? "未连接 / 已停止" : "尚未连接");
      $(id).className = `quality-pill ${connected ? "good" : state.running ? "warn" : "neutral"}`;
    }
  }
  function renderState() {
    if (guiPaused()) { renderGuiPriority(); updateControls(); return; }
    updateDisplayFreeze();
    wavelengthChart?.setFrozen(!!displayFreeze);
    const tracker = state.tracker || {}, frame = state.frame, metrics = state.metrics || {};
    const peaks = normalizePeaks(tracker.peaks), allowed = gates(state, false, false, busy);
    put("version", UI_VERSION); put("footerVersion", UI_VERSION);
    const serviceVersion = state.version && state.version !== UI_VERSION ? "服务 " + state.version : "";
    put("serviceVersion", serviceVersion); $("serviceVersion").hidden = !serviceVersion;
    put("footerServiceVersion", serviceVersion ? " · " + serviceVersion : ""); $("footerServiceVersion").hidden = !serviceVersion;
    put("modeBadge", state.running ? label(state.mode) : "尚未连接");
    $("runningBadge").className = `live-badge ${state.running ? "active" : ""}`;
    $("runningBadge").innerHTML = `<i></i>${displayFreeze ? state.running ? "采集进行中 · 上方显示已冻结" : "设备未连接 · 上方显示已冻结" : state.running ? state.mode === "replay" ? "原始数据回放中" : "实时显示中" : "设备未连接"}`;
    if (!displayHeld()) {
    put("validCount", `${peaks.filter(peak => peak.valid && !peak.stale).length} / 9`);
    put("trackerSummary", allowed.referenced ? label(tracker.state) : (Array.isArray(tracker.candidates) ? "可靠候选 " + tracker.candidates.length + "/9 · 等待无载标定" : "等待连接与无载标定"));
    put("newFrameHz", finite(metrics.new_frame_hz) ? `${fmt(metrics.new_frame_hz, 1)} Hz` : "未知");
    put("newFrameReason", metrics.new_frame_rate_reason || "等待设备帧身份核验");
    $("newFrameReason").title = metrics.new_frame_rate_reason || "等待设备帧身份核验";
    put("receiveHz", fmt(metrics.receive_hz, 1));
    put("processingMs", `${fmt(metrics.processing_ms, 2)} ms`);
    put("queueDepth", integer(metrics.queue_depth));
    put("queueDelay", `队列等待 ${fmt(metrics.processing_queue_age_ms, 1)} ms`);
    put("frameCounts", `${integer(metrics.received)} / ${integer(metrics.processed)}`);
    put("duplicateCounts", `${integer(metrics.duplicates)} / ${integer(metrics.dropped)}` + (state.recording === false ? " · 显示跳过 " + integer(metrics.display_skipped) + " 帧" : ""));
    put("identicalPayloads", `${integer(metrics.identical_payloads)} 帧 · 不等同于已证重复帧`);
    put("queuePeak", `${integer(metrics.max_queue_depth)} 帧 / ${fmt(metrics.processing_p95_ms, 2)} ms`);
    put("frameId", frame?.frame_id ?? "—");
    put("frameTime", (displayFreeze ? "冻结快照 · " : "") + (frame?.frame_id != null ? `${tracker.display_stale ? "旧帧 · " : ""}${finite(tracker.display_age_ms) ? `距接收 ${fmt(tracker.display_age_ms, 0)} ms · ` : ""}${label(frame.time_quality, "时间质量尚未核实")}` : "等待数据"));
    $("frameTime").classList.toggle("session-warning", tracker.display_stale === true);
    put("timeQuality", label(frame?.time_quality, "等待原始帧"));
    put("timestampSource", label(frame?.timestamp_source));
    put("axisSummary", axisDescription(tracker.axis_diagnostics || {}, frame));
    put("rawPointCount", `${frame?.wavelength_nm?.length ?? "—"} 点`);
    renderPeaks(peaks);
    }
    const warnings = Array.isArray(tracker.warnings) ? [...tracker.warnings] : [];
    if (tracker.windows?.length && tracker.drift_range_verified !== true) warnings.push("跟踪窗口已固定；最大按压漂移范围尚待真实数据确认。");
    put("qualityWarnings", [...new Set(warnings)].join("　·　")); $("qualityWarnings").hidden = !warnings.length;
    const session = state.session || {};
    const saving = !!state.session && session.closed === false;
    const saveError = session.error || session.save_error;
    put("sessionState", state.experiment?.running ? "独立点位会话记录中 · " + integer(state.experiment.counts?.raw_spectra) + " 个原始光谱" : state.recording === false ? "仅实时显示 · 未记录数据" : saveError ? "保存失败，请处理" : saving ? `连续保存 · ${integer(session.counts?.raw_spectra ?? session.frames ?? session.frame_count ?? metrics.received)} 帧` : session.closed === true ? "已结束 · 原始帧已保存" : label(session.state, "未开始"));
    $("sessionState").classList.toggle("session-warning", !!saveError);
    const sourceQueue = (Array.isArray(state.devices) ? state.devices : []).reduce((sum, device) => sum + (numeric(device.queue_backlog) ?? 0), 0);
    put("pipelineQueues", `${integer(sourceQueue)} / ${integer(metrics.queue_depth)} / ${integer(state.experiment?.running ? state.experiment.writer_queue_depth : session.queue_depth)} 帧`);
    put("storageIntegrity", state.experiment?.error ? state.experiment.error : state.experiment?.running ? "正在独立记录原始两路；显示跳帧不会删原始记录" : state.recording === false ? "显示模式 · 不写采集文件" : saveError || session.overflow ? "写盘异常 · 数据不完整" : state.session ? session.closed ? "已结束 · 查看会话清单" : "正在连续记录原始帧" : "等待会话");
    $("storageIntegrity").classList.toggle("session-warning", !!saveError || !!session.overflow);
    put("sessionPath", state.experiment?.running ? "点位实验正在独立记录原始两路数据：" + state.experiment.path : state.recording === false ? "本页显示不自动记录；在采集工作区明确开始会话后记录" : session.path || session.directory || "原始帧 ID、采集时间戳与质量随测量结果保留");
    renderReference(tracker, allowed); renderForce(state.force, state.sync); renderDevices(); updateControls();
    drawSpectrum();
    wavelengthChart?.update(state, stateReceivedAt);
    if (displayFreeze) displayFreeze.painted = true;
    renderDisplayFreezeStatus();
    global.NineFBGExperiment?.render(state, true);
    const error = state.error || saveError;
    if (error && error !== lastError) { notify(label(error), "error"); lastError = error; }
    else if (!error) lastError = "";
    if (tracker.message && tracker.message !== renderState.lastMessage && !error) {
      renderState.lastMessage = tracker.message;
      notify(label(tracker.message), tracker.state === "error" || tracker.state === "failed" ? "error" : "info");
    }
  }
  async function poll() {
    if (polling || !active || guiFrozen()) return;
    polling = true;
    const requestEpoch = guiStateEpoch;
    const controller = typeof global.AbortController === "function" ? new global.AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), 1500) : null;
    try {
      const response = await fetch("/api/state", { cache: "no-store", ...(controller ? { signal: controller.signal } : {}) });
      if (!response.ok) throw new Error(`状态服务响应 ${response.status}`);
      const nextState = await response.json();
      // A request that began before pulse priority may finish afterwards.
      // Discard it without painting a fresh-looking paused snapshot.
      if (guiFrozen() || requestEpoch !== guiStateEpoch) return;
      state = nextState;
      serviceUnavailable = false;
      stateReceivedAt = Date.now();
      guiNeedsFreshState = false;
      renderGuiPriority();
      $("serverDot").className = "status-dot good"; put("serverStatus", "本机服务已连接");
      renderState();
    } catch (error) {
      if (guiFrozen() || requestEpoch !== guiStateEpoch) return;
      guiNeedsFreshState = false;
      renderGuiPriority();
      serviceUnavailable = true;
      $("serverDot").className = "status-dot bad"; put("serverStatus", "本机服务连接中断");
      wavelengthChart?.update(null, Date.now());
      wavelengthChart?.setFrozen(!!displayFreeze);
      global.NineFBGExperiment?.render(null, true);
      if (state.running) {
        put("runningBadge", "服务中断 · 状态未更新"); $("runningBadge").className = "live-badge";
        state.tracker = { ...(state.tracker || {}), display_stale: true, peaks: normalizePeaks(state.tracker?.peaks).map(peak => ({ ...peak, valid: false, stale: true, failure_reason: "本机服务连接中断，当前数据未更新" })) };
        state.force = state.force ? { ...state.force, stale: true, valid: false } : null;
        renderForce(state.force, null); updateControls();
        renderPeaks(state.tracker.peaks);
        if (!displayHeld()) { put("validCount", "0 / 9"); put("frameTime", "旧帧 · 本机服务连接中断"); $("frameTime").classList.add("session-warning"); }
        drawSpectrum();
        notify("本机服务连接中断。屏幕数据为上次状态，不能作为当前有效测量。", "error");
      }
      renderDisplayFreezeStatus();
      // Never reconnect devices automatically after a page or network interruption.
    } finally { if (timeout != null) clearTimeout(timeout); polling = false; }
  }
  function refreshStaleness() {
    if (guiPaused()) return;
    if (active) wavelengthChart?.tick(Date.now());
    if (!active || !state.running) return;
    const elapsed = Math.max(0, Date.now() - stateReceivedAt);
    if (forceSample?.valid && !currentForce().valid) { renderForce(state.force, state.sync); updateControls(); }
    if (elapsed + (state.tracker?.display_age_ms || 0) > 1000 && !state.tracker?.display_stale) {
      state.tracker = { ...(state.tracker || {}), display_stale: true, peaks: normalizePeaks(state.tracker?.peaks).map(peak => ({ ...peak, valid: false, stale: true, failure_reason: "状态超过 1 秒未更新" })) };
      renderPeaks(state.tracker.peaks);
      if (!displayHeld()) { put("validCount", "0 / 9"); put("frameTime", "旧帧 · 状态超过 1 秒未更新"); $("frameTime").classList.add("session-warning"); }
      drawSpectrum();
    }
    renderDisplayFreezeStatus();
  }
  function chart(canvas, title, unit, xRange, yRange, yDigits = 0) {
    const bounds = canvas.getBoundingClientRect(), ratio = Math.min(global.devicePixelRatio || 1, 2);
    const width = Math.max(200, bounds.width), height = Math.max(100, bounds.height);
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
    const ctx = canvas.getContext("2d"); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
    const area = { x: 63, y: 23, w: width - 85, h: height - 62 };
    const x = value => area.x + (value - xRange[0]) / (xRange[1] - xRange[0]) * area.w;
    const y = value => area.y + area.h - (value - yRange[0]) / (yRange[1] - yRange[0]) * area.h;
    ctx.font = '9px Consolas,"Microsoft YaHei UI",sans-serif'; ctx.fillStyle = "#536779";
    ctx.textAlign = "left"; ctx.fillText(title, 17, 12);
    ctx.strokeStyle = "#d9e2e9"; ctx.lineWidth = 1;
    const yTicks = display.axisTicks(yRange, 4), yPrecision = Math.min(8, Math.max(yDigits, yTicks.digits));
    const xTicks = display.axisTicks(xRange, 5);
    for (let i = 0; i <= 4; i++) {
      const py = area.y + area.h * i / 4;
      ctx.beginPath(); ctx.moveTo(area.x, py + .5); ctx.lineTo(area.x + area.w, py + .5); ctx.stroke();
      const yValue = yRange[1] - (yRange[1] - yRange[0]) * i / 4;
      ctx.textAlign = "right"; ctx.fillText(yTicks.digits > 8 ? yTicks.format(yValue) : fmt(yValue, yPrecision), area.x - 11, py + 3);
    }
    for (let i = 0; i <= 5; i++) {
      const px = area.x + area.w * i / 5;
      ctx.strokeStyle = "#e3e9ee"; ctx.beginPath(); ctx.moveTo(px + .5, area.y); ctx.lineTo(px + .5, area.y + area.h); ctx.stroke();
      const xValue = xRange[0] + (xRange[1] - xRange[0]) * i / 5;
      ctx.fillStyle = "#536779"; ctx.textAlign = "center"; ctx.fillText(unit === "nm" ? fmt(xValue, Math.min(8, Math.max(1, xTicks.digits))) : xTicks.format(xValue), px, height - 23);
    }
    ctx.textAlign = "right"; ctx.fillText(unit === "nm" ? "波长 / nm" : "采集时间 / s", width - 21, height - 5);
    return { ctx, area, x, y, width, height, xRange, yRange };
  }
  function clip(geometry, callback) { const { ctx, area } = geometry; ctx.save(); ctx.beginPath(); ctx.rect(area.x, area.y, area.w, area.h); ctx.clip(); callback(); ctx.restore(); }
  function line(geometry, xs, ys, color, width = 1.5, dash = []) {
    const { ctx, x, y } = geometry; ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash); ctx.beginPath(); let open = false;
    for (let i = 0; i < Math.min(xs.length, ys.length); i++) { if (!finite(xs[i]) || !finite(ys[i])) { open = false; continue; } const px = x(xs[i]), py = y(ys[i]); if (!open) ctx.moveTo(px, py); else ctx.lineTo(px, py); open = true; }
    ctx.stroke(); ctx.setLineDash([]);
  }
  function drawSpectrum() {
    if (displayHeld()) return;
    const frame = state.frame, tracker = state.tracker || {};
    const checked = display.validateSpectrum(frame), present = checked.valid;
    $("spectrumEmpty").hidden = present;
    if (present && restoredSpectrumView) {
      try { spectrumView = checkSpectrumView(restoredSpectrumView, checked); }
      catch (error) { spectrumView = null; notify("已保存的光谱范围与当前标定轴不符，使用完整光谱显示。" + error.message, "error"); }
      restoredSpectrumView = null;
    }
    const xs = present ? frame.wavelength_nm : [], ys = present ? frame.intensity : [];
    const xRange = spectrumView?.x || (present ? checked.xRange : [0, 1]);
    const yRange = spectrumView?.y || (present ? [Math.min(0, checked.yRange[0]), Math.max(1, checked.yRange[1] * 1.08)] : [0, 65535]);
    const stale = tracker.display_stale === true || !state.running;
    const peaks = normalizePeaks(tracker.peaks), canvas = $("spectrumChart"), bounds = canvas.getBoundingClientRect();
    const showWindows = $("showWindows").checked;
    const windows = Array.isArray(tracker.windows) ? tracker.windows : Object.entries(tracker.windows || {}).map(([id, ranges]) => ({ id, range_nm: ranges }));
    // Source identities stay immutable. Display and quality changes must still
    // repaint that same frame immediately, including independent staleness.
    const renderKey = JSON.stringify([frame?.stream_epoch, frame?.frame_id, frame?.host_rx_monotonic_ns,
      present, checked.reason, checked.xRange, checked.yRange, xRange, yRange, restoredSpectrumView, stale, tracker.reference_version, tracker.reference_pending,
      tracker.algorithm_version, tracker.config_version, $("spectrumRangeMode").value, showWindows,
      showWindows ? windows.map(window => [window.id, windowBounds(window)]) : null,
      peaks.map(peak => [peak.id, peak.valid, peak.stale, peak.center_nm, peak.amplitude, peak.background, peak.fit_x, peak.fit_y]),
      !tracker.reference_version && !stale ? tracker.candidates : null,
      bounds.width, bounds.height, Math.min(global.devicePixelRatio || 1, 2)]);
    if (renderKey === lastSpectrumRenderKey) return;
    const g = chart($("spectrumChart"), "强度 / 计数" + (present && stale ? " · 旧帧" : ""), "nm", xRange, yRange);
    rawGeometry = present ? g : null;
    if (!present) { put("spectrumDisplayStatus", restoredSpectrumView ? "已恢复手动范围设置，等待实际标定轴核验。" : frame ? checked.reason : "等待完整的 512 点实际标定光谱"); lastSpectrumRenderKey = renderKey; return; }
    if (!spectrumView && $("spectrumRangeMode").value !== "manual") {
      $("spectrumXMin").value = fmt(checked.xRange[0], 5); $("spectrumXMax").value = fmt(checked.xRange[1], 5);
      $("spectrumYMin").value = Math.floor(yRange[0]); $("spectrumYMax").value = Math.ceil(yRange[1]);
    }
    const hiddenPoints = xs.reduce((sum, value, index) => sum + (value < xRange[0] || value > xRange[1] || ys[index] < yRange[0] || ys[index] > yRange[1] ? 1 : 0), 0);
    put("spectrumDisplayStatus", (stale ? "旧帧 · " : "") + "实际标定轴 " + fmt(checked.xRange[0], 4) + "～" + fmt(checked.xRange[1], 4) + " nm · 强度为 SDK 计数 · " + (spectrumView ? "手动显示范围" : "完整波长轴 / 自动强度") + (hiddenPoints ? " · " + hiddenPoints + " 点在显示范围外" : ""));
    clip(g, () => {
      if (showWindows) {
        for (const [index, window] of windows.entries()) {
          const bounds = windowBounds(window); if (!bounds) continue;
          const id = normalizeID(window.id), color = COLORS[id ? IDS.indexOf(id) : index % 9];
          g.ctx.fillStyle = `${color}0c`; g.ctx.fillRect(g.x(bounds[0]), g.area.y, g.x(bounds[1]) - g.x(bounds[0]), g.area.h);
          g.ctx.strokeStyle = `${color}35`; g.ctx.lineWidth = 1; g.ctx.setLineDash([3, 5]);
          for (const bound of bounds) { g.ctx.beginPath(); g.ctx.moveTo(g.x(bound), g.area.y); g.ctx.lineTo(g.x(bound), g.area.y + g.area.h); g.ctx.stroke(); }
          g.ctx.setLineDash([]); g.ctx.fillStyle = color; g.ctx.textAlign = "center"; g.ctx.font = "9px Consolas,monospace";
        }
      }
      line(g, xs, ys, stale ? "#93632a" : "#596d80", 1.25, stale ? [4, 3] : []);
      if (!tracker.reference_version && !stale) {
        (tracker.candidates || []).forEach((candidate, index) => {
          if (!finite(candidate.index) || !finite(candidate.center_nm) || !finite(ys[candidate.index])) return;
          g.ctx.fillStyle = "#93632a"; g.ctx.beginPath(); g.ctx.arc(g.x(candidate.center_nm), g.y(ys[candidate.index]), 3, 0, Math.PI * 2); g.ctx.fill();
          g.ctx.textAlign = "center"; g.ctx.fillText("候选 " + (index + 1), g.x(candidate.center_nm), Math.max(g.area.y + 14, g.y(ys[candidate.index]) - 8));
        });
      }
      peaks.forEach((peak, index) => {
        if (!peak.valid || peak.stale) return;
        if (Array.isArray(peak.fit_x) && Array.isArray(peak.fit_y)) line(g, peak.fit_x, peak.fit_y, COLORS[index], 2);
        if (finite(peak.center_nm)) {
          const fitted = peak.fit_y?.filter(finite), top = fitted?.length ? Math.max(...fitted) : finite(peak.amplitude) ? peak.amplitude + (peak.background ?? 0) : null;
          if (finite(top)) { g.ctx.fillStyle = COLORS[index]; g.ctx.beginPath(); g.ctx.arc(g.x(peak.center_nm), g.y(top), 3, 0, Math.PI * 2); g.ctx.fill(); }
          if (finite(top)) { g.ctx.textAlign = "center"; g.ctx.fillText(peak.id, g.x(peak.center_nm), Math.max(g.area.y + 24, g.y(top) - 9)); }
          g.ctx.strokeStyle = `${COLORS[index]}80`; g.ctx.lineWidth = 1; g.ctx.setLineDash([2, 4]); g.ctx.beginPath(); g.ctx.moveTo(g.x(peak.center_nm), g.area.y + 18); g.ctx.lineTo(g.x(peak.center_nm), g.area.y + g.area.h); g.ctx.stroke(); g.ctx.setLineDash([]);
        }
      });
    });
    lastSpectrumRenderKey = renderKey;
  }
  $("sourceMode").addEventListener("change", updateControls);
  $("forceUnloadedCheck").addEventListener("change", updateControls);
  $("spectrumRangeMode").addEventListener("change", updateControls);
  $("spectrumRangeApply").addEventListener("click", () => {
    if (displayFreeze || guiPaused()) return;
    try {
      if ($("spectrumRangeMode").value === "auto") spectrumView = null;
      else {
        const actual = display.validateSpectrum(state.frame);
        if (!actual.valid) throw new Error(actual.reason);
        spectrumView = checkSpectrumView(spectrumInputView(), actual);
      }
      restoredSpectrumView = null;
      drawSpectrum(); saveSpectrumSettings(); notify("已应用光谱显示范围。", "success", true);
    } catch (error) { notify(error.message, "error"); }
  });
  $("spectrumRangeReset").addEventListener("click", () => { if (displayFreeze || guiPaused()) return; spectrumView = restoredSpectrumView = null; $("spectrumRangeMode").value = "auto"; updateControls(); drawSpectrum(); saveSpectrumSettings(); });
  $("px6dConnectBtn").addEventListener("click", () => action(async () => { await request("/api/force/connect", {}); notify("正在独立连接 PX6D，等待六轴原始值。", "info", true); }));
  $("px6dDisconnectBtn").addEventListener("click", () => action(async () => { await request("/api/force/disconnect", {}); forceZero.reset(); notify("PX6D 已断开，光谱继续显示。", "info", true); }));
  $("forceZeroBtn").addEventListener("click", () => action(async () => {
    if (!$("forceUnloadedCheck").checked) throw new Error("请先移开接触棒并确认当前无载荷。");
    await poll();
    if (guiPaused()) throw new Error("脉搏曲线优先，本页已暂停；恢复显示并重新确认无载后再标定。");
    if (captureDisplayActive(state)) throw new Error("采集会话正在进行，请使用采集工作区的本轮无载标定。");
    if (gates(state, false, false, busy).pressing) throw new Error("请结束按压、移开接触棒后重新确认无载。");
    const force = currentForce();
    if (!force.valid) throw new Error(force.reason);
    await request("/api/reference", { unloaded_confirmed: true });
    forceZero.request(true, force);
    $("forceUnloadedCheck").checked = false;
    notify("正在建立九峰无载参考与六轴显示零点，请保持无载。", "info");
  }));
  $("forceZeroReset").addEventListener("click", () => { if (guiPaused() || captureDisplayActive(state)) return; forceZero.reset(); renderForce(state.force, state.sync); updateControls(); notify("已恢复六轴原始值显示。", "info", true); });
  $("showWindows").addEventListener("change", drawSpectrum);
  $("connectBtn").addEventListener("click", () => action(async () => {
    const payload = { mode: "hardware", record: false, connect_force: false,
      integration_us: inputNumber("integrationUs", "曝光时间"), sample_rate_hz: inputNumber("sampleRateHz", "采样请求频率") };
    await request("/api/connect", payload);
    forceZero.reset();
    notify("光谱连接已发起。收到光谱后再连接 PX6D，确认无载并点击标定。", "success");
  }));
  $("disconnectBtn").addEventListener("click", () => action(async () => { await request("/api/disconnect", {}); forceZero.reset(); notify("设备已断开。", "info", true); }));
  $("spectrumChart").addEventListener("mousemove", event => {
    if (displayFreeze || guiPaused()) { $("spectrumTooltip").hidden = true; return; }
    if (!rawGeometry || !state.frame) return;
    const rect = event.currentTarget.getBoundingClientRect(), px = event.clientX - rect.left, py = event.clientY - rect.top;
    const g = rawGeometry, tooltip = $("spectrumTooltip");
    if (px < g.area.x || px > g.area.x + g.area.w || py < g.area.y || py > g.area.y + g.area.h) { tooltip.hidden = true; return; }
    const target = g.xRange[0] + (px - g.area.x) / g.area.w * (g.xRange[1] - g.xRange[0]);
    let closest = 0; const xs = state.frame.wavelength_nm;
    for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - target) < Math.abs(xs[closest] - target)) closest = i;
    tooltip.textContent = `${state.tracker?.display_stale ? "旧帧 · " : ""}采样 ${closest + 1} / ${xs.length} · ${fmt(xs[closest], 5)} nm · ${fmt(state.frame.intensity[closest], 0)} 计数`;
    tooltip.style.left = `${Math.max(12, Math.min(px + 12, rect.width - 295))}px`; tooltip.style.top = `${Math.max(6, py - 36)}px`; tooltip.hidden = false;
  });
  $("spectrumChart").addEventListener("mouseleave", () => { $("spectrumTooltip").hidden = true; });
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(drawSpectrum).observe(document.querySelector(".main-content"));
  else global.addEventListener("resize", drawSpectrum);
  global.addEventListener("pagehide", () => { active = false; });
  global.addEventListener("pageshow", () => { active = true; poll(); });
  $("guiPriorityResume").addEventListener("click", () => { guiPriority?.requestResume(); });
  guiPriority?.subscribe(() => {
    guiStateEpoch++;
    guiNeedsFreshState = true;
    if (guiFrozen()) {
      $("forceUnloadedCheck").checked = false;
      if (forceZero.snapshot().pending) forceZero.reset();
    }
    wavelengthChart?.setFrozen(true);
    renderGuiPriority(); updateControls();
    if (!guiFrozen()) poll();
  });
  if (guiPaused()) { wavelengthChart?.setFrozen(true); renderGuiPriority(); }
  renderState(); poll(); setInterval(poll, 100); setInterval(refreshStaleness, 100);
  global.NineFBGPreferences?.bind("connection", {
    source_mode: { id: "sourceMode", type: "string" }, integration_us: { id: "integrationUs", type: "number" }, sample_rate_hz: { id: "sampleRateHz", type: "number" }
  }, () => { $("sourceMode").value = "hardware"; updateControls(); });
  global.NineFBGPreferences?.bind("display", {
    show_windows: { id: "showWindows", type: "checkbox" },
    spectrum_mode: { id: "spectrumRangeMode", type: "string" },
    spectrum_x_min: { id: "spectrumXMin", type: "number" }, spectrum_x_max: { id: "spectrumXMax", type: "number" },
    spectrum_y_min: { id: "spectrumYMin", type: "number" }, spectrum_y_max: { id: "spectrumYMax", type: "number" }
  }, restoreSpectrumSettings);
})(typeof globalThis !== "undefined" ? globalThis : this);
