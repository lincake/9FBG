(function (global, factory) {
  "use strict";
  const api = factory(global);
  if (typeof module === "object" && module.exports) module.exports = api;
  global.NineFBGExperiment = api;
  if (typeof document !== "undefined") api.mount();
})(typeof globalThis !== "undefined" ? globalThis : this, function (global) {
  "use strict";
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const escapeHTML = value => String(value == null ? "" : value).replace(/[&<>"']/g, value => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[value]));
  const number = (value, digits = 3) => finite(value) ? value.toFixed(digits) : "—";
  const PHASES = ["waiting_training", "waiting_contact", "preload", "no_contact", "loading", "release", "hold", "removed", "recovery", "saving"];
  const PHASE_LABELS = { waiting_training: "等待训练暂停确认", waiting_contact: "连续记录 · 等待目标点接触", release: "已达目标 · 请卸载，仍在记录", saving: "等待本轮保存确认", idle: "等待试次", unloaded: "无载基线", preload: "无接触预采样", loading: "加载", hold: "保持", removed: "已移开", recovery: "恢复观察", finish: "已完成", aborted: "已中止", stopped: "会话已停止" };
  const COORDINATE_SCHEMA = "bottom_left_xy_v1";
  const REQUIRED_SERVICE_VERSION = "0.14.0";
  // These delivered versions share the verified experiment API. Keep an
  // explicit allowlist: an unknown future version is not automatically safe.
  const COMPATIBLE_SERVICE_VERSIONS = ["0.13.5", "0.13.6", REQUIRED_SERVICE_VERSION, "0.15.0", "0.15.1", "0.16.0", "0.16.1", "0.16.2", "0.17.0", "0.17.1", "0.18.0"];
  const NO_CONTACT = "NO_CONTACT";
  const selectionLabel = value => value === NO_CONTACT ? "无接触" : value;
  const noContactSelection = experiment => experiment?.point_id === NO_CONTACT;
  const INFERENCE_STOPPED_MESSAGE = "推理已结束；重新载入模型并建立无接触参考后可再次推理";
  const WRENCH_AXES = ["Fx", "Fy", "Fz", "Mx", "My", "Mz"];
  const WRENCH_UNITS = ["N", "N", "N", "N·m", "N·m", "N·m"];
  const wrenchVector = value => Array.isArray(value) && value.length === 6 && value.every(finite) ? value.slice() : null;
  const sequenceMethod = method => ["spectral_gru", "drift_gru"].includes(method);
  const driftModel = value => value?.method === "drift_gru" || value?.input_kind === "delta_lambda_9" || value?.metadata?.input_kind === "delta_lambda_9" || value?.model_definition?.input_kind === "delta_lambda_9" || value?.prediction?.input_kind === "delta_lambda_9";
  const modelTrainingMethod = model => driftModel(model) ? "drift_gru" : model?.method || (model?.model_kind === "spectral_gru" ? "spectral_gru" : null);
  const spectralModel = analysis => analysis?.model_kind === "spectral_gru" || analysis?.prediction?.schema === "spectral_prediction_v1";
  const WRENCH_PROTOCOLS = ["timed_wrench_v1", "force_target_wrench_v1"];
  const experimentProtocol = experiment => experiment?.settings?.acquisition_protocol || experiment?.settings?.protocol || experiment?.guided?.protocol;
  const wrenchProtocol = experiment => WRENCH_PROTOCOLS.includes(experimentProtocol(experiment));
  const captureDrift = experiment => experiment?.settings?.capture_mode === "peak_drift" || driftModel(experiment?.settings);
  const captureMode = experiment => experiment?.running === true ? captureDrift(experiment) ? "peak_drift" : "full_spectrum" : (mounted ? $("expCaptureMode")?.value : null) || "full_spectrum";
  const captureUsesDriftMode = experiment => captureMode(experiment) === "peak_drift";
  const legacyDriftCapture = experiment => experiment?.running === true && captureDrift(experiment) && experiment.settings?.capture_mode !== "peak_drift";
  const captureReference = experiment => captureUsesDriftMode(experiment) ? "寻峰漂移参考" : "全谱参考";
  const spectralTraining = analysis => sequenceMethod(analysis?.training?.method) && trainingView(analysis.training).busy;
  function actualSampleRate(state) {
    const device = (Array.isArray(state?.devices) ? state.devices : []).find(item => item.wavelength_axis || item.sdk_status_raw || item.device_identity?.spectrum_points);
    const interval = device?.sdk_status_raw?.interval_ms ?? device?.device_identity?.interval_ms;
    return finite(interval) && interval > 0 ? 1000 / interval : state?.timing?.sample_rate_hz ?? state?.config?.sample_rate_hz ?? state?.sample_rate_hz ?? null;
  }
  function backendCompatible(state) { return COMPATIBLE_SERVICE_VERSIONS.includes(state?.version); }
  function compatibilityMessage(state) {
    return state?.version ? "采集服务版本未通过兼容性核验，请先停止并保存会话，再关闭程序重新打开。（当前服务 " + state.version + "，界面兼容基准 " + REQUIRED_SERVICE_VERSION + "）" + (legacyRawRecording(state) && !state.experiment?.running ? " 当前原始采集仍在写入；点击“停止并保存会话”会停止设备、排空队列并保存原始记录。" : "") : "正在核验服务版本；确认采集服务属于已验证的兼容版本后才能进行引导采集。";
  }
  function legacyRawRecording(state) {
    return !backendCompatible(state) && state?.running === true && (state.recording === true || state.session?.closed === false);
  }
  function pointGrid() {
    return Array.from({ length: 25 }, (_, index) => ({ id: "P" + String(index + 1).padStart(2, "0"), row: Math.floor(index / 5), col: index % 5, u: (index % 5) / 4, v: Math.floor(index / 5) / 4 }));
  }
  function visualPointGrid(points = pointGrid()) {
    return points.slice().sort((left, right) => right.row - left.row || left.col - right.col);
  }
  function pointLayout(settings, hasSession = false) {
    const definition = settings?.coordinate_definition;
    const stored = Array.isArray(settings?.point_grid) ? settings.point_grid : settings?.point_grid?.points;
    const current = definition?.schema === COORDINATE_SCHEMA;
    const validStored = Array.isArray(stored) && stored.length === 25 && new Set(stored.map(point => point.id)).size === 25 && stored.every(point => /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(point.id || "") && Number.isInteger(point.row) && Number.isInteger(point.col) && finite(point.u ?? point.x_norm) && finite(point.v ?? point.y_norm));
    if (validStored) {
      const points = stored.map(point => ({ ...point, u: point.u ?? point.x_norm, v: point.v ?? point.y_norm }));
      return { points, visual: current ? visualPointGrid(points) : points.slice(), current, known: true, recorded: true };
    }
    if (hasSession && (!current || stored != null)) {
      // Unknown historical geometry keeps its original point IDs without
      // assigning today's corrected row/column or axis directions to it.
      const points = pointGrid().map(point => ({ id: point.id, row: null, col: null, u: null, v: null }));
      return { points, visual: points.slice(), current: false, known: false, recorded: true };
    }
    const points = pointGrid();
    return { points, visual: visualPointGrid(points), current: true, known: true, recorded: false };
  }
  function phaseLabel(phase, experiment) {
    if (phase === "no_contact") return "连续记录 · 无接触";
    if (experimentProtocol(experiment) === "force_target_wrench_v1") {
      if (experiment?.guided?.step === "zeroing") return "直接建立下一轮软件零点与" + captureReference(experiment);
      if (phase === "preload") return "连续记录 · 2 秒无载基线";
    }
    return PHASE_LABELS[phase] || phase || "等待会话";
  }
  function experimentGates(state, busy = false, pendingPoint = null) {
    const experiment = state.experiment || {}, phase = experiment.phase || "idle";
    const running = experiment.running === true;
    const guided = experiment.guided || {}, enabled = guided.enabled === true;
    const active = running && (!!experiment.trial_id || PHASES.includes(phase) || ["waiting_training", "waiting_contact", "preload", "no_contact", "loading", "release", "unloading", "checking", "zeroing", "saving"].includes(guided.step));
    const baselineReady = experiment.baseline?.ready === true || experiment.baseline?.status === "ready";
    const confirmed = (wrenchProtocol(experiment) || experiment.settings?.force_direction_confirmed === true) && (experiment.settings?.coordinates_confirmed === true || experiment.settings?.point_grid_confirmed === true);
    const selectionMatches = pendingPoint === null || pendingPoint === experiment.point_id;
    const selectionSupported = (pendingPoint !== NO_CONTACT && !noContactSelection(experiment)) || experimentProtocol(experiment) === "force_target_wrench_v1";
    const compatible = backendCompatible(state);
    return {
      start: compatible && !busy && ((!state.analysis?.busy && !trainingView(state.analysis?.training).busy) || spectralTraining(state.analysis)) && !running && state.running === true && state.mode === "hardware" && state.force_connected === true,
      stop: !busy && (running || legacyRawRecording(state)), point: compatible && !busy && !active,
      pointConfirm: compatible && !busy && running && enabled && !active && (pendingPoint !== NO_CONTACT || experimentProtocol(experiment) === "force_target_wrench_v1"),
      baseline: compatible && !busy && running && enabled && !active && selectionMatches && selectionSupported && ["needs_baseline", "retry", "round_complete"].includes(guided.step) && !!experiment.point_id,
      trialStart: compatible && !busy && running && enabled && !active && selectionMatches && selectionSupported && (guided.step === "ready" || experimentProtocol(experiment) === "force_target_wrench_v1" && ["round_complete", "retry"].includes(guided.step)) && baselineReady && confirmed && !!experiment.point_id,
      loading: false, hold: false, removed: false, recovery: false, finish: compatible && !busy && running && enabled && !!experiment.trial_id && experimentProtocol(experiment) === "force_target_wrench_v1" && (noContactSelection(experiment) ? phase === "no_contact" && guided.step === "no_contact" : phase === "release" && guided.step === "release") && guided.can_finish === true,
      abort: compatible && !busy && active
    };
  }
  function inferenceStopAllowed(state, busy = false, pending = false) {
    return backendCompatible(state) && !busy && state?.experiment?.running !== true && !!state?.analysis?.model_id && state.analysis.inference_active === true && !pending && !state?.validation?.pending_sample;
  }
  function repetitionsValue(value) {
    const count = Number(value);
    if (!String(value).trim() || !Number.isInteger(count) || count < 1 || count > 999) throw new Error("每点重复次数必须为 1～999 的整数。");
    return count;
  }
  function guidedView(experiment) {
    const source = experiment?.guided || {}, enabled = !!experiment?.session_id && source.enabled === true;
    const count = value => enabled && Number.isInteger(value) && value >= 0 ? value : 0;
    const independentCount = (...values) => count(values.find(value => Number.isInteger(value) && value >= 0));
    const percent = value => enabled && finite(value) ? Math.max(0, Math.min(100, value)) : 0;
    return { enabled, step: enabled ? source.step : "waiting", message: enabled ? source.message || "等待服务端更新本轮状态。" : "待开始：建立会话后，选择并确认实际采集点位。",
      repetitions: enabled ? count(source.repetitions_per_point) : 0, round: count(source.current_round),
      pointAccepted: count(source.point_accepted), pointAttempts: count(source.point_attempts), totalAccepted: count(source.total_accepted), totalRequired: count(source.total_required),
      noContactAccepted: independentCount(source.no_contact_accepted, source.accepted_by_point?.[NO_CONTACT], noContactSelection(experiment) ? source.point_accepted : null),
      noContactAttempts: independentCount(source.no_contact_attempts, source.attempts_by_point?.[NO_CONTACT], noContactSelection(experiment) ? source.point_attempts : null),
      totalPercent: percent(source.progress_percent), pointPercent: percent(source.point_progress_percent), forcePercent: percent(source.force_progress_percent),
      acceptedByPoint: enabled ? source.accepted_by_point || {} : {}, lastQuality: enabled ? source.last_quality || null : null, complete: enabled && source.complete === true };
  }
  function captureInstruction(experiment) {
    const guided = guidedView(experiment), source = experiment?.guided || {};
    const targetMode = experimentProtocol(experiment) === "force_target_wrench_v1";
    const tone = guided.step === "retry" ? "retry" : guided.complete && !noContactSelection(experiment) ? "success" : "";
    if (!targetMode) return { text: guided.message, detail: "", tone };
    if (experiment.error) return { text: "采集异常：" + experiment.error, detail: guided.message, tone: "retry", urgent: true };
    if (!guided.enabled) return { text: "待开始：确认无载并建立参考，再确认点位、开始连续记录。", detail: "开始后先保持无接触记录 2 秒基线，提示后再找点按压；达到目标后移开压力棒，点击“结束本轮”直接软件回零。", tone: "" };
    const limit = experiment.settings?.max_force_norm_N, peak = source.observed_peak_force_norm_N, live = experiment.live_force;
    const liveOverload = live?.valid === true && finite(live.age_ms) && live.age_ms <= 250 && finite(limit) && finite(live.force_norm_N) && live.force_norm_N > limit;
    if (experiment.trial_id && (liveOverload || finite(limit) && finite(peak) && peak > limit))
      return { text: "本轮合力已超过设定上限，请立即卸载并中止本轮；原始数据保留。", detail: source.finish_block_reason || guided.message, tone: "retry", urgent: true };
    const fault = source.finish_block_reason || "";
    if (experiment.trial_id && /故障|异常|标记无效|锁定|断流/.test(fault))
      return { text: fault, detail: guided.message, tone: "retry", urgent: true };
    if (noContactSelection(experiment)) {
      const messages = {
        needs_baseline: "已确认采集“无接触”。请保持无接触，确认无载后点击“标定本轮”。",
        calibrating: "正在建立真实" + captureReference(experiment) + "与六轴软件零偏，请保持无接触。",
        ready: "无接触参考已就绪，点击“开始本轮采集”；本轮全过程保持无接触。",
        waiting_training: "正在等待后台训练暂停确认，请保持无接触。",
        preload: "正在记录 2 秒无载基线；本轮继续保持无接触。",
        no_contact: source.can_finish === true ? "无接触数据检查已满足结束条件；保持无接触，点击“结束本轮”保存。" : "正在连续记录无接触数据，请保持无接触，等待服务端数据检查。",
        checking: "正在检查本轮无接触数据，请继续保持无接触。",
        zeroing: "正在建立下一轮软件零点与" + captureReference(experiment) + "，请保持无接触。",
        saving: "正在保存本轮无接触数据，请保持无接触。",
        round_complete: "本轮无接触数据已保存，次数已独立累计；可以选择并确认其他点位，或继续采集无接触。",
        point_complete: "本轮无接触数据已保存；可以选择并确认其他点位，或继续采集无接触。",
        retry: experiment.baseline?.ready === true ? "本轮无接触检查未通过，失败记录已保留；可保持无接触重新采集。" : "本轮无接触检查未通过，失败记录已保留；请保持无接触并重新标定。"
      };
      return { text: messages[guided.step] || guided.message, detail: guided.step === "calibrating" && experiment.baseline?.reason ? experiment.baseline.reason : source.finish_block_reason || guided.message, tone };
    }
    const target = source.target_force_norm_N ?? experiment.settings?.target_force_norm_N ?? 4;
    const messages = {
      select_point: "请选中点位，并点击“确认采集点位”。",
      needs_baseline: "请移开压力棒，确认无载后点击“标定本轮”。",
      calibrating: "正在建立真实" + captureReference(experiment) + "与六轴软件零偏，请保持无接触。",
      ready: "本轮无载参考已就绪，确认点位后点击“开始本轮采集”；先保持无接触记录 2 秒基线。",
      waiting_training: "正在等待后台训练暂停确认，请保持无接触。",
      preload: "先保持无接触，正在记录 2 秒基线；提示后再按压。",
      waiting_contact: source.press_prompt_ready === false ? "先保持无接触，正在记录 2 秒基线；提示后再按压。" : "基线已记录 2 秒，请开始按压；可移动找点，在所选目标点缓慢加载。",
      loading: "正在连续记录，请在目标点缓慢加载至合力 " + number(target, 2) + " N。",
      release: "已达到目标，请先移开压力棒；系统允许后点击“结束本轮”，即确认已卸载并直接软件回零。",
      checking: "正在检查本轮数据并直接软件回零，请保持压力棒移开。",
      zeroing: "正在直接建立下一轮软件零点与" + captureReference(experiment) + "，请保持无接触。",
      saving: "正在保存本轮数据，请保持无载；保存确认后准备下一轮参考。",
      round_complete: "本轮已保存，下一轮无载参考已就绪，可以继续采集。",
      point_complete: guided.lastQuality?.accepted === false ? "当前点位规定合格轮次已保存；最近一轮未通过，失败记录保留。请确认下一点位。" : "当前点位规定轮次已保存，请选择并确认下一点位。",
      session_complete: "全部点位规定轮次已完成，请停止并保存会话。",
      retry: experiment.baseline?.ready === true ? "本轮未通过，失败记录已保留；下一轮参考已就绪，可重新采集。" : "本轮未通过，失败记录已保留；请保持无载并重新标定。"
    };
    return { text: messages[guided.step] || guided.message, detail: guided.step === "calibrating" && experiment.baseline?.reason ? experiment.baseline.reason : guided.message, tone };
  }
  function preloadView(experiment) {
    const guided = guidedView(experiment), source = guided.enabled ? experiment.guided.preload : null;
    const available = source?.schema === "preload_diagnostics_v1";
    const duration = value => available && finite(value) && value >= 0 ? value : null;
    const elapsed = duration(source?.elapsed_ms), continuous = duration(source?.valid_continuous_ms);
    const target = duration(source?.target_ms), timeout = duration(source?.timeout_ms);
    const timedOut = available && source.timed_out === true;
    return { visible: guided.enabled && (guided.step === "preload" || timedOut), available, elapsed, continuous,
      target: target > 0 ? target : 500, timeout, timedOut,
      resetCount: available && Number.isInteger(source.reset_count) && source.reset_count >= 0 ? source.reset_count : null,
      percent: continuous !== null ? Math.max(0, Math.min(100, continuous / (target > 0 ? target : 500) * 100)) : 0,
      reason: available && typeof source.last_block_reason === "string" ? source.last_block_reason : "等待服务端预采样诊断。" };
  }
  function roundQualityText(quality) {
    if (!quality || typeof quality.accepted !== "boolean") return "尚无本轮判定；合格次数只由服务端质量检查计入。";
    const reasons = Array.isArray(quality.reasons) ? quality.reasons.filter(value => typeof value === "string") : [];
    return (quality.accepted ? "最近一轮采集检查通过，已计入次数" : "最近一轮采集检查未通过，不计入次数；请卸载后手动标定重采") + (reasons.length ? " · " + reasons.join("；") : "");
  }
  function predictionView(prediction) {
    const p = prediction || {};
    if (p.schema === "spectral_prediction_v1" || p.model_kind === "spectral_gru") {
      const predicted = p.valid === true ? wrenchVector(p.wrench_pred) : null, truth = wrenchVector(p.wrench_truth);
      const error = predicted && truth ? predicted.map((value, index) => value - truth[index]) : null;
      const contact = p.contact === true;
      return { spectral: true, status: p.status || (predicted ? contact ? "有效估计" : "无接触" : (driftModel(p) ? "寻峰漂移推理预热 / 无效" : "全谱推理预热 / 无效")),
        point: predicted && contact && /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(p.point_id || "") ? p.point_id : null,
        force: null, truth: null, error: null, wrenchPred: predicted, wrenchTruth: truth, wrenchError: error,
        forceNorm: predicted && finite(p.force_norm_N) ? p.force_norm_N : predicted ? Math.hypot(...predicted.slice(0, 3)) : null,
        axes: WRENCH_AXES, units: WRENCH_UNITS, contact, inferenceValid: !!predicted,
        contactProbability: predicted && finite(p.contact_probability) && p.contact_probability >= 0 && p.contact_probability <= 1 ? p.contact_probability : null,
        contactMethod: p.contact_method === "binary_contact_head_v1" ? "binary_contact_head_v1" : p.contact_method === "legacy_predicted_force_hysteresis_v1" ? "legacy_predicted_force_hysteresis_v1" : null,
        confidence: predicted && finite(p.confidence) ? p.confidence : null,
        tolerances: Array.isArray(p.wrench_tolerances) ? p.wrench_tolerances : null,
        reason: p.reason || (driftModel(p) ? "九路波长漂移采用本次 2 秒无载峰心参考，单位 pm；六轴保持 PX6D 坐标系与参考原点。" : "全谱输入采用当前固定无载参考；六轴保持 PX6D 坐标系与参考原点。") };
    }
    const valid = p.status === "有效估计" && /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(p.point_id || "") && finite(p.force_N);
    const truth = finite(p.truth_force_N) ? p.truth_force_N : null;
    return {
      status: p.status || "待采集 / 未加载模型", point: valid ? p.point_id : null,
      force: valid ? p.force_N : null, truth,
      error: valid && truth !== null ? p.force_N - truth : null,
      confidence: valid && finite(p.confidence) ? p.confidence : null,
      reason: p.reason || "载入真实模型并完成本次无接触参考后，才会显示有效估计。"
    };
  }
  function trainedPointIDs(analysis) {
    const source = analysis?.trained_point_ids;
    if (Array.isArray(source)) return [...new Set(source.filter(id => /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(id)))];
    return analysis?.model_kind === "full" ? pointGrid().map(point => point.id) : [];
  }
  function validationLiveView(analysis, stale = false) {
    const view = predictionView(spectralModel(analysis) ? { ...analysis?.prediction, model_kind: "spectral_gru", ...(driftModel(analysis) ? { input_kind: "delta_lambda_9" } : {}) } : analysis?.prediction), trained = trainedPointIDs(analysis);
    const stopped = analysis?.inference_active === false && !!analysis?.model_id;
    if (stopped) { view.status = "推理已结束"; view.reason = INFERENCE_STOPPED_MESSAGE; view.confidence = null; }
    const ready = !stale && analysis?.inference_active === true && !!analysis?.model_id && analysis?.test_reference?.ready === true && analysis.test_reference.pending !== true && analysis?.prediction?.stale !== true;
    const valid = ready && trained.includes(view.point) && analysis?.prediction?.valid !== false;
    const comparisonReady = ready && (typeof analysis?.prediction?.comparison_ready === "boolean" ? analysis.prediction.comparison_ready : view.spectral ? !!view.wrenchTruth : valid && view.truth !== null);
    return { ...view, point: valid ? view.point : null, force: valid ? view.force : null, error: valid ? view.error : null, truth: stale || stopped ? null : view.truth,
      ...(view.spectral ? { wrenchPred: ready ? view.wrenchPred : null, wrenchTruth: ready ? view.wrenchTruth : null, wrenchError: ready ? view.wrenchError : null, forceNorm: ready ? view.forceNorm : null, contactProbability: ready ? view.contactProbability : null } : {}), valid, trained, comparisonReady };
  }
  function frozenSampleView(sample) {
    if (!sample || typeof sample.sample_id !== "string" || !sample.sample_id) return null;
    const prediction = predictionView(sample.prediction);
    if (prediction.spectral) {
      const inferenceValid = sample.inference_valid !== false && prediction.inferenceValid;
      return { ...prediction, id: sample.sample_id, model: sample.model_id || "未记录", valid: sample.valid === true, inferenceValid,
        point: inferenceValid ? prediction.point : null, tolerances: sample.wrench_tolerances || prediction.tolerances,
        created: sample.created_utc || null, reason: sample.reason || prediction.reason, saved: sample.judged === true || sample.saved === true };
    }
    const inferenceValid = sample.inference_valid !== false && !!prediction.point && prediction.force !== null && prediction.truth !== null;
    return { id: sample.sample_id, model: sample.model_id || "未记录", valid: sample.valid === true, inferenceValid,
      point: inferenceValid ? prediction.point : null, force: inferenceValid ? prediction.force : null, truth: prediction.truth, error: inferenceValid ? prediction.error : null,
      created: sample.created_utc || null, reason: sample.reason || prediction.reason, saved: sample.judged === true || sample.saved === true };
  }
  function forceWithinTolerance(error) { return finite(error) && Math.abs(error) <= .3 + 1e-12; }
  function wrenchWithinTolerance(errors, tolerances) {
    return !!wrenchVector(errors) && Array.isArray(tolerances) && tolerances.length === 6 && tolerances.every((limit, index) => finite(limit) && limit > 0 && Math.abs(errors[index]) <= limit + 1e-12);
  }
  const selectedForceCorrect = view => view?.spectral ? wrenchWithinTolerance(view.wrenchError, view.tolerances) : forceWithinTolerance(view?.error);
  function targetComparison(target, live) {
    if (!target) return "请先点击要识别的目标点位。";
    if (live?.valid && live.point) return "目标 " + target + " · 识别 " + live.point + " · " + (target === live.point ? "位置一致" : "位置不一致");
    return "目标 " + target + (live?.comparisonReady ? " · 当前未输出有效识别位置。" : " · 待按压或数据尚未就绪。");
  }
  function targetPredictionView(live, target) {
    const matches = /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(target || "") && live.valid === true && live.point === target;
    const comparison = matches && live.comparisonReady === true;
    return { ...live, force: matches ? live.force : null, truth: comparison ? live.truth : null, error: comparison ? live.error : null,
      wrenchPred: matches ? live.wrenchPred : null, wrenchTruth: comparison ? live.wrenchTruth : null,
      wrenchError: comparison ? live.wrenchError : null, forceNorm: matches ? live.forceNorm : null,
      selectionNote: !target ? "请点击目标点位；仅识别位置与所选点位一致时显示预测力和 PX6D 对照。" : matches ? comparison ? "当前显示 " + target + " 的预测力与 PX6D 对照。" : "当前显示 " + target + " 的预测力；PX6D 有效同步对照尚未就绪。" : live.point ? "已选 " + target + "，当前识别 " + live.point + "；预测力与 PX6D 对照留空。" : "已选 " + target + "；等待该点位的有效接触识别，预测力与 PX6D 对照留空。" };
  }
  function validationStatistics(source) {
    const count = value => Number.isInteger(value) && value >= 0 ? value : 0;
    const total = count(source?.judged_total), position = count(source?.position_correct), force = count(source?.force_correct);
    return { total, position: Math.min(position, total), force: Math.min(force, total), pending: count(source?.pending_count), invalid: count(source?.invalid_count),
      positionRate: total ? Math.min(position, total) / total : null, forceRate: total ? Math.min(force, total) / total : null };
  }
  function modelOptionLabel(model) {
    const ids = Array.isArray(model?.point_ids) ? model.point_ids : [];
    const kind = model?.model_kind === "spectral_gru" ? (driftModel(model) ? "寻峰漂移六维力研究模型" : "全谱六维力研究模型") + ((model.trained_point_ids || ids).length ? " · " + (model.trained_point_ids || ids).length + " 点" : "") : model?.model_kind === "pilot" ? "少量点研究模型" + (ids.length ? " · " + ids.length + " 点" : "") : model?.model_kind === "full" ? "完整 25 点模型" : "模型范围待核对";
    const dataID = model?.data_id || (Array.isArray(model?.data_ids) ? model.data_ids.filter(Boolean).join(" / ") : "") || "未编号";
    const acquired = model?.data_started_utc || model?.data_started_minute;
    const acquiredMinute = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(acquired || "") ? acquired : sessionTimeText(acquired);
    return (model?.model_id || "编号未记录") + " · " + kind + " · 数据 " + dataID + (acquired ? " · 采集 " + acquiredMinute : "") + (model?.method ? " · " + (model.method_label || trainingMethodLabel(model.method)) : "") + (model?.created_local_minute ? " · 训练 " + model.created_local_minute : model?.created_utc ? " · 训练 " + sessionTimeText(model.created_utc) : "");
  }
  function liveForceView(force, elapsed = 0, timed = false) {
    const age = finite(force?.age_ms) ? force.age_ms + Math.max(0, elapsed) : null;
    if (timed) {
      const norm = finite(force?.force_norm_N) ? force.force_norm_N : null;
      const valid = force?.valid === true && norm !== null && age !== null && age <= 250;
      return { valid, force: valid ? norm : null, peak: finite(force?.observed_peak_force_norm_N) ? force.observed_peak_force_norm_N : null,
        age, limit: finite(force?.max_force_norm_N) ? force.max_force_norm_N : 5, outside: valid && (force.over_force_norm_limit === true || norm > (force.max_force_norm_N ?? 5)),
        reason: valid ? "" : age !== null && age > 250 ? "最新六轴超过 250 ms 未更新，当前合力已失效。" : force?.reason || "等待有效六轴源帧与本轮软件零偏。" };
    }
    const valid = force?.valid === true && finite(force.force_N) && age !== null && age <= 250;
    return {
      valid, force: valid ? force.force_N : null,
      raw: valid && finite(force.raw_Fz_N) ? force.raw_Fz_N : null,
      zero: valid && finite(force.zero_Fz_N) ? force.zero_Fz_N : null,
      age, outside: valid && (force.over_provisional_range === true || force.force_N > 3),
      reason: valid ? "" : age !== null && age > 250 ? "最新六轴超过 250 ms 未更新，当前按压力已失效。" : force?.reason || "本试次基线与有效 PX6D 原始帧就绪后显示。"
    };
  }
  function trialCurves(report, pointID, axis = 0) {
    const point = report?.point_curves?.[pointID];
    if (!Number.isInteger(axis) || axis < 0 || axis > 8) return [];
    return (Array.isArray(point?.trials) ? point.trials : []).map(trial => ({
      id: trial.trial_id, session: trial.session_id, split: trial.split,
      originalCount: trial.original_count, displayCount: trial.display_count,
      samples: (Array.isArray(trial.force_N) ? trial.force_N : []).flatMap((force, index) => {
        const x = trial.x_pm?.[index]?.[axis];
        return finite(force) && finite(x) ? [{ force, x }] : [];
      })
    })).filter(trial => trial.samples.length);
  }
  const methodLabel = method => ({ linear: "线性标定", piecewise: "分段标定", mlp: "小型 MLP 对照" }[method] || method);
  const splitLabel = split => ({ A: "A · 拟合内诊断", B: "B · 阈值选择", C: "C · 最终留出", overall: "全部 · 混合诊断" }[split] || split);
  function splitMetrics(result, split) {
    if (!result || typeof result !== "object") return null;
    // A/B/C never fall back to overall: that would mix training and test results.
    return split === "overall" ? result.overall || null : result.by_split?.[split] || null;
  }
  function metricCount(metrics) {
    return metrics?.n ?? metrics?.joint_force?.n ?? metrics?.known_point_force?.n ?? 0;
  }
  function hasSplitData(report, split) {
    return Object.values(report?.methods || {}).some(result => metricCount(splitMetrics(result, split)) > 0);
  }
  function defaultReportSplit(report) {
    return ["C", "B", "A"].find(split => hasSplitData(report, split)) || "C";
  }
  function modelSessionIDs(available, selected) {
    const ids = Array.from(selected || []);
    const chosen = ids.map(id => (available || []).find(session => session.session_id === id));
    const batch = session => session?.settings?.batch || session?.batch;
    if (chosen.some(session => batch(session) === "C")) throw new Error("建模只能选择 A / B 会话。请取消 C 批的勾选，把 C 批留给“评估所选独立会话”。");
    if (!chosen.length || chosen.some(session => !["A", "B"].includes(batch(session)))) throw new Error("请仅选择已核实的 A / B 会话进行建模；批次不明的会话不能用于建模。");
    if (!chosen.some(session => batch(session) === "A") || !chosen.some(session => batch(session) === "B")) throw new Error("建立模型需要同时选择 A 批和 B 批会话：A 用于拟合，B 用于选择拒识阈值。");
    return ids;
  }
  function sessionHasTrainingData(session) {
    return session?.complete === true && session.has_training_data === true && ((Number.isInteger(session.accepted_trial_count) && session.accepted_trial_count > 0) || (Number.isInteger(session.accepted_no_contact_trial_count) && session.accepted_no_contact_trial_count > 0));
  }
  function visibleSessions(available, showUnqualified = false) {
    return (Array.isArray(available) ? available : []).filter(session => showUnqualified || sessionHasTrainingData(session));
  }
  function latestEffectiveSession(available) {
    return visibleSessions(available).slice().sort((left, right) => {
      const stamp = session => { const value = Date.parse(session.ended_utc || session.started_utc || ""); return Number.isFinite(value) ? value : 0; };
      return stamp(right) - stamp(left) || String(right.session_id).localeCompare(String(left.session_id));
    })[0] || null;
  }
  function sessionTimeText(value) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime())) return "时间未记录";
    const parts = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(date);
    const part = name => parts.find(item => item.type === name)?.value || "";
    return part("year") + "-" + part("month") + "-" + part("day") + " " + part("hour") + ":" + part("minute");
  }
  function sessionPointSummary(session) {
    return (Array.isArray(session?.point_summary) ? session.point_summary : []).filter(point => /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(point.point_id || "") && Number.isInteger(point.attempts) && point.attempts > 0).map(point => ({
      id: point.point_id, accepted: Number.isInteger(point.accepted) && point.accepted >= 0 ? point.accepted : null,
      attempts: point.attempts, location: Number.isInteger(point.row) && Number.isInteger(point.col) ? "行 " + point.row + " / 列 " + point.col : "历史方向未记录"
    }));
  }
  function pilotSessionIDs(available, selected) {
    const ids = Array.from(selected || []), chosen = ids.map(id => (available || []).find(session => session.session_id === id));
    if (!ids.length || chosen.some(session => !sessionHasTrainingData(session))) throw new Error("少量点分轮验证需要已保存且有合格按压的真实会话；请取消诊断、空会话或未完整保存会话的勾选。");
    if (chosen.some(session => (session.settings?.batch || session.batch) === "C")) throw new Error("请取消 C 批的勾选。训练使用 A / B 数据，C 批是保留数据，不参与训练。");
    if (ids.length !== 1 || chosen.some(session => !["A", "B"].includes(session.settings?.batch || session.batch))) throw new Error("训练请仅选择一个 A 批或 B 批的有效会话；可使用“选择最近有效会话”。");
    return ids;
  }
  function historyTrainingStatus(session, method = null) {
    if (!session) return { allowed: false, reason: "记录已变化，请刷新历史列表。" };
    if (session.deleted === true) return { allowed: false, reason: "记录在回收区，请先恢复。" };
    const batch = session.settings?.batch || session.batch;
    if (batch === "C") return { allowed: false, reason: "C 批是保留数据，不参与训练。" };
    if (!["A", "B"].includes(batch)) return { allowed: false, reason: "批次不明，不能训练。" };
    if (session.diagnostic === true) return { allowed: false, reason: "诊断记录不能训练。" };
    if (session.trainable === false) return { allowed: false, reason: session.history_error || "该记录未通过训练数据核验。" };
    if (!sessionHasTrainingData(session)) return { allowed: false, reason: session.history_error || (session.complete !== true ? "记录尚未完整保存。" : "尚无合格采集数据。") };
    if (method && !sequenceMethod(method) && session.accepted_no_contact_trial_count > 0 && !(session.accepted_trial_count > session.accepted_no_contact_trial_count)) return { allowed: false, reason: "无接触训练标签须使用寻峰漂移或全谱 MLP＋GRU。" };
    if (method === "spectral_gru" && (session.spectral_available === false || (session.capture_mode || session.settings?.capture_mode) === "peak_drift")) return { allowed: false, reason: session.spectral_unavailable_reason || "寻峰漂移会话只保存九路峰心与漂移，没有 512 点原始全谱；请选择寻峰漂移 MLP＋GRU。" };
    if (method === "drift_gru" && session.drift_available === false) return { allowed: false, reason: session.drift_unavailable_reason || session.drift_error || "该会话缺少可信九路峰心或 2 秒无载基线，不能用于寻峰漂移训练。" };
    if (method && !sequenceMethod(method) && (session.settings && WRENCH_PROTOCOLS.includes(session.settings.acquisition_protocol) || WRENCH_PROTOCOLS.includes(session.acquisition_protocol))) return { allowed: false, reason: "六维协议须选择寻峰漂移或全谱 MLP＋GRU；原有方法不能继承新协议标签。" };
    return { allowed: true, reason: "可训练" };
  }
  function historyTrainingIDs(available, selected, method = null) {
    const ids = Array.from(selected || []);
    if (!ids.length) throw new Error("请勾选一条或多条采集数据。");
    const invalid = ids.map(id => ({ id, status: historyTrainingStatus((available || []).find(session => session.session_id === id), method) })).filter(item => !item.status.allowed);
    if (invalid.length) throw new Error("所选记录不能全部用于训练：" + invalid.map(item => item.id + "：" + item.status.reason).join("；") + " 请取消这些记录，程序不会静默跳过。");
    return ids;
  }
  function cumulativeSpectralSourceIDs(available, method = null) {
    return (Array.isArray(available) ? available : []).filter(session => !session.deleted && session.diagnostic !== true && ["A", "B"].includes(session.settings?.batch || session.batch) && sessionHasTrainingData(session) && (method !== "drift_gru" || historyTrainingStatus(session, method).allowed) && (method !== "spectral_gru" || session.spectral_available !== false && (session.capture_mode || session.settings?.capture_mode) !== "peak_drift")).map(session => session.session_id);
  }
  function sessionCaptureLabel(session) {
    if ((session?.capture_mode || session?.settings?.capture_mode) === "peak_drift") return "寻峰漂移 · 仅九路时序，不保存 512 点原谱";
    if (session?.settings?.input_kind === "delta_lambda_9") return "旧九路漂移 · 含原谱备份";
    return "原始全谱 · 512 点";
  }
  function historyRecordLabel(session) {
    return sessionTimeText(session?.started_utc) + " · 数据编号 " + (session?.data_id || session?.settings?.data_id || "未编号") + " · " + (session?.session_id || "编号未记录");
  }
  function selectedRoundSummary(available, selected) {
    const ids = [...new Set(selected || [])], count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
    const total = { sessions: ids.length, attempts: 0, accepted: 0, press: 0, noContact: 0, rejected: 0, pending: 0 };
    const fields = ["attempts", "accepted", "press", "noContact", "rejected", "pending"];
    for (const id of ids) {
      const row = (available || []).find(item => item.session_id === id);
      const attempts = count(row?.trial_count), accepted = count(row?.accepted_trial_count), rejected = count(row?.rejected_trial_count);
      // Old spatial-only summaries have no no-contact fields. Explicit missing
      // counts in newer summaries remain unknown rather than becoming zero.
      const noContact = row && !Object.hasOwn(row, "no_contact_trial_count") && !Object.hasOwn(row, "accepted_no_contact_trial_count") ? 0 : count(row?.accepted_no_contact_trial_count);
      const press = accepted !== null && noContact !== null && noContact <= accepted ? accepted - noContact : null;
      const pending = row && Object.hasOwn(row, "pending_trial_count") ? count(row.pending_trial_count) : attempts !== null && accepted !== null && rejected !== null && attempts >= accepted + rejected ? attempts - accepted - rejected : null;
      const values = { attempts, accepted, press, noContact, rejected, pending };
      for (const field of fields) total[field] = total[field] === null || values[field] === null || !Number.isSafeInteger(total[field] + values[field]) ? null : total[field] + values[field];
    }
    return total;
  }
  function pilotReportView(data) {
    const scopeName = "within_session_whole_trial_cross_validation";
    const pilot = data?.schema === "9fbg_pilot_cv_v1" || data?.kind === "pilot" || data?.scope === scopeName, summary = pilot ? data.summary || {} : {};
    const metrics = source => {
      const populated = Number.isInteger(source?.n) && source.n > 0;
      const value = key => populated && finite(source[key]) ? source[key] : null;
      return { n: populated ? source.n : 0, accuracy: value("classification_accuracy"), acceptedAccuracy: value("accepted_classification_accuracy"), mae: value("force_mae_N"), rmse: value("force_rmse_N"), p95: value("force_p95_abs_error_N"), position: value("position_error_norm_mean"), rejection: value("rejection_rate") };
    };
    const interpretation = typeof data?.interpretation === "string" && /[\u4e00-\u9fff]/.test(data.interpretation) ? data.interpretation : "仅检验实际采集点在同一会话中的整轮重复性，不证明完整 25 点、跨会话或跨天泛化，也不构成正式测量精度验收。";
    const scope = typeof data?.scope === "string" && /[\u4e00-\u9fff]/.test(data.scope) ? data.scope : "同一会话内的整轮留出交叉验证";
    return { pilot, summary: metrics(summary), testTrials: Number.isInteger(summary.test_trials) && summary.test_trials >= 0 ? summary.test_trials : null,
      points: pilot ? Object.entries(data.by_point || {}).filter(([id]) => /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(id)).map(([id, source]) => ({ id, ...metrics(source) })) : [],
      folds: pilot && Array.isArray(data.folds) ? data.folds : [], scope: scope + " · " + interpretation };
  }
  function analysisProgressText(progress) {
    const source = progress || {};
    const stage = typeof source.stage === "string" && /[\u4e00-\u9fff]/.test(source.stage) ? source.stage : "正在离线补算";
    const session = source.session_id ? " · 当前会话 " + source.session_id : "";
    const counts = finite(source.processed) && finite(source.total) && source.processed >= 0 && source.total >= 0 ? " · 当前会话已补算 " + Math.floor(source.processed) + "/" + Math.floor(source.total) + " 条光谱" : "";
    return stage + session + (counts || (!source.stage ? " · 等待当前会话进度" : ""));
  }
  function modelDefinitionText(definition) {
    if (!definition) return "待加载";
    if (definition.model_kind === "spectral_gru" || sequenceMethod(definition.method)) return (definition.finger_id || "模型样件未记录") + (driftModel(definition) ? " · 九路波长漂移 / pm / PX6D 六轴原坐标系" : " · 全谱差值 / PX6D 六轴原坐标系");
    const force = definition.force_sign === -1 ? "−" : definition.force_sign === 1 ? "+" : null;
    return (definition.finger_id || "模型样件未记录") + " · " + (force ? "F = " + force + "(Fz − Fz₀)" : "力方向尚未核实");
  }
  function testReferenceText(reference, experimentRunning = false) {
    const source = reference || {};
    const scope = experimentRunning ? "本会话基线参考" : "本次模型推理参考";
    const status = source.pending === true ? "正在建立" : source.ready === true ? "已就绪" : "尚未就绪";
    const version = source.reference_version != null ? " · 第 " + source.reference_version + " 版" : "";
    const reason = source.reason ? " · " + source.reason : source.ready || source.pending ? "" : " · 请使用现有无接触确认和“建立本次无接触参考”按钮";
    return scope + "：" + status + version + reason;
  }
  function summaryRows(report, split = "C") {
    return Object.entries(report?.methods || {}).map(([method, result]) => {
      const metrics = splitMetrics(result, split);
      const count = metricCount(metrics), known = metrics?.known_point_force, joint = metrics?.joint_force;
      const populatedKnown = known && known.n > 0 ? known : known ? { n: known.n } : null;
      const populatedJoint = joint && joint.n > 0 ? joint : joint ? { n: joint.n } : null;
      return { key: method, method: methodLabel(method), metrics,
        known: populatedKnown, joint: populatedJoint,
        mae: populatedJoint?.mae_N, rmse: populatedJoint?.rmse_N,
        p95: populatedJoint?.p95_abs_error_N, accuracy: count > 0 ? metrics?.classification_accuracy : null,
        acceptedAccuracy: count > 0 ? metrics?.accepted_classification_accuracy : null, rejection: count > 0 ? metrics?.rejection_rate : null,
        n: count };
    });
  }

  let mounted = false, latest = {}, busy = false, sessions = [], deletedSessions = [], models = [], deletedModels = [], tab = "session";
  const guiPriority = global.NineFBGGuiPriority?.shared();
  let guiNeedsFreshState = guiPriority?.isFrozen() === true;
  const guiPaused = () => guiPriority?.isFrozen() === true || guiNeedsFreshState;
  const heldControls = new Map();
  function holdControls(held) {
    if (held) {
      for (const control of $("experimentWorkspace").querySelectorAll("button, input, select, textarea")) {
        if (!heldControls.has(control)) heldControls.set(control, control.disabled);
        control.disabled = true;
      }
    } else {
      for (const [control, disabled] of heldControls) control.disabled = disabled;
      heldControls.clear();
    }
  }
  let activePoint = "P01", lastModelKey = "", receivedAt = 0, expired = false, sessionsLoaded = false, modelsLoaded = false, pointLayoutKey = "", previousPreloadTimedOut = false;
  let observedSessionID = null, observedPointID = null, pendingPointTouched = false, protocolTouched = false;
  let capturePromptKey = "", capturePromptSession = null, captureDetailAt = null, captureDetail = "";
  const capturePromptHistory = [], CAPTURE_DETAIL_INTERVAL_MS = 2000;
  let frozenSample = null, frozenSaved = false, frozenDiscarded = false, validationMapKey = "", validationModelKey = "";
  let targetPoint = null, judgementTarget = null;
  let completedTrainingKey = "", preferredModelID = null, validationResult = null, historyPendingAction = null, modelPendingAction = null, lastModelManagementKey = "";
  let cumulativeSpectralSelection = true, captureOptionsKey = "", inferenceStoppedLocally = false;
  const selectedSessions = new Set(), selectedModels = new Set(), removedModelIDs = new Set();
  let previousTrainingMode = "scratch", scratchCumulativeSelection = true, lastBaseModelsKey = "";
  const pendingReads = new Map();
  const LIST_REQUEST_TIMEOUT_MS = 8000, COMMAND_REQUEST_TIMEOUT_MS = 5000;
  const COMMAND_TIMEOUTS = {
    "/api/analysis/load": 30000, "/api/training/start": 30000,
    "/api/history/delete": 30000, "/api/history/restore": 30000,
    "/api/analysis/models/delete": 30000, "/api/analysis/models/restore": 30000,
    "/api/experiment/stop": 30000, "/api/disconnect": 15000,
    "/api/validation/finish": 15000
  };
  const modeSessionSelections = { scratch: null, finetune: null }, modeLearningRates = { scratch: null, finetune: null };
  const $ = id => global.document.getElementById(id);
  const put = (id, value) => { const element = $(id); if (element) element.textContent = value; };
  const CAPTURE_PROTOCOL_OPTIONS = '<option value="force_target_wrench_v1">全谱六维：合力目标＋手动结束</option><option value="guided_round_v1">原有引导：加载到 3 N</option>';
  function migrateTimedSelection() {
    const limit = Number($("expMaxForceNorm").value) || 5;
    const target = Math.min(Number($("expTargetForceNorm").value) || 4, limit);
    $("expAcquisitionProtocol").value = "force_target_wrench_v1";
    $("expTargetForceNorm").value = String(target);
    $("expProtocolUpgradeNotice").hidden = false;
    put("expProtocolUpgradeNotice", "固定 3 s 已从新建会话选项移除，待选偏好已改为“合力目标＋手动结束”。沿用当前合力上限 " + limit + " N，目标 " + target + " N；请核对。历史会话保持原协议，历史数据仍可读取与训练。");
    global.NineFBGPreferences?.save?.("experiment", { acquisition_protocol: "force_target_wrench_v1", target_force_norm_N: target });
  }
  function renderProtocolSelection(experiment) {
    const select = $("expAcquisitionProtocol"), previous = select.value;
    const runningProtocol = experiment?.running ? experimentProtocol(experiment) || "guided_round_v1" : null;
    const showLegacyTimed = runningProtocol === "timed_wrench_v1";
    if (experiment?.running) $("expCaptureMode").value = captureDrift(experiment) ? "peak_drift" : "full_spectrum";
    const drift = captureUsesDriftMode(experiment), key = JSON.stringify([showLegacyTimed, drift]);
    if (key !== captureOptionsKey) {
      select.innerHTML = (drift ? '<option value="force_target_wrench_v1">寻峰漂移六维：合力目标＋手动结束</option>' : CAPTURE_PROTOCOL_OPTIONS) + (showLegacyTimed ? '<option value="timed_wrench_v1" disabled>历史会话：固定按压 3 s（只读）</option>' : '');
      captureOptionsKey = key;
      select.value = showLegacyTimed ? "timed_wrench_v1" : previous === "guided_round_v1" ? previous : "force_target_wrench_v1";
      if (drift && select.value !== "force_target_wrench_v1") select.value = "force_target_wrench_v1";
      if (!showLegacyTimed && previous === "timed_wrench_v1") migrateTimedSelection();
    }
    if (runningProtocol) select.value = runningProtocol;
    put("expCaptureContentNote", experiment?.running && captureDrift(experiment) && !experiment.settings?.capture_mode ? "旧九路漂移会话：含 512 点原谱备份，保存方式保持原会话定义。" : drift ? "寻峰漂移：仅保存 FBG1～FBG9 的波长漂移时序、峰心、时间戳、参考、点位和六轴标签；不保存 512 点原谱。设备仍读取光谱进行寻峰。" : "原始全谱：保留 512 点光谱采集、全谱 MLP＋GRU 训练与推理；采集流程沿用原方法。");
  }
  const captureProtocol = experiment => experiment?.running === true ? experimentProtocol(experiment) : $("expAcquisitionProtocol").value;
  const captureUsesWrenchProtocol = experiment => WRENCH_PROTOCOLS.includes(captureProtocol(experiment));
  const captureUsesTargetProtocol = experiment => captureProtocol(experiment) === "force_target_wrench_v1";
  const trainingMethodLabel = method => ({ linear: "加权最小二乘", mlp: "小型 MLP", spectral_gru: "全谱 MLP＋GRU（六维力）", drift_gru: "寻峰漂移 MLP＋GRU（六维力）" }[method] || "方法待核实");
  const TRAINING_PARAMETER_FIELDS = { epochs: "expTrainingEpochs", batch_size: "expTrainingBatchSize", learning_rate: "expTrainingLR", patience: "expTrainingPatience", seed: "expTrainingSeed", cpu_threads: "expTrainingThreads", position_loss_weight: "expTrainingPositionWeight", wrench_loss_weight: "expTrainingWrenchWeight" };
  const fineTuneSelected = () => sequenceMethod($("expTrainingMethod").value) && $("expTrainingMode").value === "finetune";
  const baseModelCandidates = () => models.filter(model => model.model_kind === "spectral_gru" && modelTrainingMethod(model) === $("expTrainingMethod").value && model.loadable !== false && model.deleted !== true);
  const selectedBaseModel = () => baseModelCandidates().find(model => model.model_id === $("expBaseModelSelect").value);
  function changeTrainingMode(next) {
    if (!["scratch", "finetune"].includes(next)) next = "scratch";
    const previous = previousTrainingMode;
    if (previous !== next) {
      modeSessionSelections[previous] = new Set(selectedSessions);
      modeLearningRates[previous] = $("expTrainingLR").value;
      if (previous === "scratch") scratchCumulativeSelection = cumulativeSpectralSelection;
      const saved = modeSessionSelections[next];
      const restored = next === "finetune" ? (cumulativeSpectralSelection ? saved || new Set() : new Set(selectedSessions)) : saved || new Set(selectedSessions);
      selectedSessions.clear(); for (const id of restored) selectedSessions.add(id);
      $("expTrainingLR").value = modeLearningRates[next] ?? (next === "finetune" ? "0.0001" : "0.001");
      previousTrainingMode = next;
      global.NineFBGPreferences?.save?.("analysis", { learning_rate: Number($("expTrainingLR").value) });
    } else if (next === "scratch") scratchCumulativeSelection = cumulativeSpectralSelection;
    $("expTrainingMode").value = next;
    cumulativeSpectralSelection = next === "finetune" ? false : scratchCumulativeSelection;
    applyCumulativeSpectralSelection(); renderSessions();
  }
  function renderBaseModels() {
    const candidates = baseModelCandidates(), selected = $("expBaseModelSelect").value;
    const missing = !!selected && !candidates.some(model => model.model_id === selected);
    const key = JSON.stringify([candidates, selected, missing]);
    if (key !== lastBaseModelsKey) {
      $("expBaseModelSelect").innerHTML = '<option value="">请选择同输入基础模型</option>' + (missing ? '<option value="' + escapeHTML(selected) + '" disabled>原基础模型已不可用：' + escapeHTML(selected) + '</option>' : '') + candidates.map(model => '<option value="' + escapeHTML(model.model_id) + '">' + escapeHTML(modelOptionLabel(model)) + '</option>').join('');
      $("expBaseModelSelect").value = selected; lastBaseModelsKey = key;
    }
    put("expBaseModelState", missing ? "原基础模型已删除、损坏或不可载入；不会自动改选其他模型，请明确重新选择。" : selected ? "基础模型 " + selected + " 仅作为训练起点；不替换当前推理模型。来源与新增数据兼容性由服务端核验。" : "请选择已保存、可载入且输入相同的模型；无需载入到推理，也无需无载确认。");
  }
  function spectralTrainingParameters() {
    const params = Object.fromEntries(Object.entries(TRAINING_PARAMETER_FIELDS).map(([key, id]) => [key, numericInput(id, $(id).parentElement?.textContent || "训练参数")]));
    for (const [key, low, high] of [["epochs", 1, 1000], ["batch_size", 1, 1024], ["patience", 1, 1000], ["seed", 0, 2147483647], ["cpu_threads", 1, 8]]) if (!Number.isInteger(params[key]) || params[key] < low || params[key] > high) throw new Error("训练整数参数超出允许范围：" + key);
    if (params.learning_rate < 1e-6 || params.learning_rate > .1 || params.position_loss_weight < 0 || params.position_loss_weight > 100 || params.wrench_loss_weight < 0 || params.wrench_loss_weight > 100 || params.position_loss_weight + params.wrench_loss_weight <= 0) throw new Error("学习率与损失权重无效；至少一项损失权重须大于零。");
    const mode = $("expTrainingMode").value;
    if (!["scratch", "finetune"].includes(mode)) throw new Error("请选择从头训练或继承模型继续训练。");
    params.training_mode = mode;
    if (mode === "scratch") params.adapt_legacy_unloaded = $("expTrainingMethod").value === "drift_gru" ? false : $("expAdaptLegacyUnloaded").checked;
    if (mode === "finetune") {
      const base = selectedBaseModel();
      if (!base) throw new Error("请明确选择当前可用的同输入基础模型；已删除或损坏模型不能继续训练。");
      params.base_model_id = base.model_id;
    }
    return params;
  }
  function readWrenchTolerances(strict = true) {
    return WRENCH_AXES.map((axis, index) => {
      const text = $("expWrenchTolerance" + index).value.trim(), value = Number(text);
      if (!text) return null;
      if (!finite(value) || value <= 0) { if (strict) throw new Error(axis + " 容差须为正的有限数值。"); return null; }
      return value;
    });
  }
  function wrenchTable(predicted, truth, errors, titles = ["模型", "PX6D 对照", "模型 − 对照"]) {
    const columns = titles.map((title, index) => ({ title, values: [predicted, truth, errors][index] })).filter(column => column.title);
    return '<table class="experiment-table wrench-table"><thead><tr><th>轴 / 单位</th>' + columns.map(column => '<th>' + escapeHTML(column.title) + '</th>').join('') + '</tr></thead><tbody>' + WRENCH_AXES.map((axis, index) => '<tr><th>' + axis + ' / ' + WRENCH_UNITS[index] + '</th>' + columns.map(column => '<td>' + number(column.values?.[index], index < 3 ? 3 : 5) + '</td>').join('') + '</tr>').join('') + '</tbody></table>';
  }
  function trainingView(source) {
    const status = ["idle", "running", "pause_requested", "paused", "cancelling", "cancelled", "completed", "failed"].includes(source?.status) ? source.status : "idle";
    const progress = finite(source?.progress_percent) ? Math.min(100, Math.max(0, source.progress_percent)) : null;
    const complete = status === "completed" && progress === 100 && typeof source?.model_id === "string" && !!source.model_id;
    const iteration = Number.isInteger(source?.current_iteration) ? source.current_iteration : Number.isInteger(source?.epoch) ? source.epoch : null;
    const iterations = Number.isInteger(source?.max_iterations) ? source.max_iterations : Number.isInteger(source?.epochs) ? source.epochs : null;
    return { status, busy: source?.busy === true || ["running", "pause_requested", "paused", "cancelling"].includes(status), progress, complete,
      stage: status === "paused" ? "训练进程已确认暂停，保留优化器与进度；只有全部暂停原因解除后才继续。" : source?.message || ({ idle: "请选择真实会话和模型方法，明确点击开始训练。", starting: "正在启动 CPU 训练进程", scanning: "正在核验原始文件与来源", preparing: source?.method === "drift_gru" ? "正在构建寻峰漂移数据集与固定划分" : "正在构建全谱数据集与固定划分", prepare: source?.method === "drift_gru" ? "正在核验九路峰心、无载基线与来源" : "正在核验原始全谱与来源", resuming: "正在继续 CPU 训练", training: "正在训练因果时序模型", saving: "正在校验并原子保存模型", completed: "模型已完整保存", stopped: "训练进程已退出", cancelled: "训练已取消", failed: "训练失败" }[source?.stage]) || source?.stage || (status === "idle" ? "请选择真实会话和模型方法，明确点击开始训练。" : "等待服务端阶段更新。"),
      error: source?.error || null, modelID: complete ? source.model_id : null,
      method: source?.method, methodLabel: source?.method_label || trainingMethodLabel(source?.method),
      mode: source?.training_mode === "finetune" ? "finetune" : "scratch", baseModelID: source?.base_model_id || null,
      newSessionIDs: Array.isArray(source?.new_session_ids) ? source.new_session_ids : [], mergedSessionIDs: Array.isArray(source?.merged_session_ids) ? source.merged_session_ids : [],
      created: source?.created_local_minute || (source?.created_utc ? sessionTimeText(source.created_utc) : null),
      points: Array.isArray(source?.trained_point_ids || source?.point_ids) ? (source.trained_point_ids || source.point_ids).filter(id => /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(id)) : [],
      pauseReasons: Array.isArray(source?.pause_reasons) ? source.pause_reasons : [],
      iteration, iterations, fold: Number.isInteger(source?.fold) ? source.fold : null, folds: Number.isInteger(source?.fold_count) ? source.fold_count : null };
  }
  function validationResultView(validation) {
    const raw = validation?.session_stats || validation?.current_stats || validation?.stats || {}, stats = validationStatistics(raw);
    const errors = Number.isInteger(raw.force_error_count) ? raw.force_error_count : null;
    return { ...stats, mae: stats.total > 0 && errors !== 0 && finite(raw.mae_N ?? raw.MAE_N) ? raw.mae_N ?? raw.MAE_N : null,
      rmse: stats.total > 0 && errors !== 0 && finite(raw.rmse_N ?? raw.RMSE_N) ? raw.rmse_N ?? raw.RMSE_N : null,
      rejected: Number.isInteger(raw.inference_rejected) && raw.inference_rejected >= 0 ? raw.inference_rejected : 0,
      errors, sessionID: validation?.validation_session_id || validation?.session_id || null };
  }
  function trainingHTML() {
    return [
      '<section id="expPanelTraining" class="panel experiment-section" role="tabpanel" aria-labelledby="expTabTraining" hidden><div class="training-layout"><div><h3>采集数据与历史记录</h3><label class="confirmation session-filter"><input id="expShowDeleted" type="checkbox"><span>查看已删除</span></label><p id="expSessionFilterState" class="display-note">显示全部采集记录，只有完整保存的合格会话可供训练。</p><label id="expCumulativeSourcesField" class="confirmation session-filter" hidden><input id="expCumulativeSources" type="checkbox" checked><span>时序训练默认累计已保存 A / B 来源；手动勾选可改用子集</span></label><p id="expHistorySelectionState" class="display-note">已选择 0 条记录</p><p id="expTrainingRoundSummary" class="training-selection-summary" role="status">已勾选 0 个会话 · 合格共 0 轮（按压 0 轮 / 无接触 0 轮） · 总试次 0 轮</p><div id="expSessionList" class="experiment-session-list"><p class="experiment-empty">尚未读取会话列表</p></div><div class="display-action-row"><button id="expRefreshSessions" class="button subtle">刷新会话列表</button><button id="expSelectTrainable" class="button secondary" disabled>全选可训练数据</button><button id="expClearSessions" class="button subtle" disabled>清空选择</button><button id="expDeleteSessions" class="button history-delete" disabled>删除所选</button><button id="expRestoreSessions" class="button secondary" hidden disabled>恢复所选</button></div></div>',
      '<div class="training-controls"><h3>训练模型</h3><p id="expTrainingModelsRefreshState" class="experiment-statusline" role="status" hidden></p><label class="display-field" for="expTrainingMethod"><span>模型方法</span><select id="expTrainingMethod"><option value="spectral_gru" selected>全谱 MLP＋GRU（六维力）</option><option value="drift_gru">寻峰漂移 MLP＋GRU（六维力）</option><option value="linear">加权最小二乘</option><option value="mlp">小型 MLP</option></select></label><p id="expTrainingMethodNote" class="display-note">旧方法合并兼容 A / B 的合格整轮，每点至少 3 轮；C 批保留独立测试。</p>',
      '<details id="expSpectralParameters" class="spectral-parameters" hidden><summary>CPU 训练参数与训练方式</summary><label class="display-field"><span>训练方式</span><select id="expTrainingMode"><option value="scratch">从头训练</option><option value="finetune">继承模型继续训练</option></select></label><div id="expFineTuneSettings" class="finetune-settings" hidden><div class="model-select-row"><label class="display-field"><span>同输入基础模型（仅作训练起点）</span><select id="expBaseModelSelect"><option value="">请选择同输入基础模型</option></select></label><button id="expRefreshTrainingModels" class="button subtle">刷新基础模型</button></div><p id="expBaseModelState" class="display-note">请选择同输入基础模型；无需载入到推理，也无需无载确认。</p><p class="display-note">仅勾选新增数据；程序自动带入基础模型历史来源并去重，旧验证数据继续保留，新模型另存，学习率默认 0.0001。继续训练不保证指标提高，完成后比较同一验证集结果并手动载入候选模型。</p></div><div class="experiment-form-grid">',
      '<label class="display-field"><span>迭代上限</span><input id="expTrainingEpochs" type="number" value="100" min="1" max="1000" step="1"></label><label class="display-field"><span>批大小</span><input id="expTrainingBatchSize" type="number" value="32" min="1" max="1024" step="1"></label><label class="display-field"><span>学习率</span><input id="expTrainingLR" type="number" value="0.001" min="0.000001" max="0.1" step="any"></label>',
      '<label class="display-field"><span>早停耐心</span><input id="expTrainingPatience" type="number" value="10" min="1" max="1000" step="1"></label><label class="display-field"><span>随机种子</span><input id="expTrainingSeed" type="number" value="1701" min="0" max="2147483647" step="1"></label><label class="display-field"><span>CPU 计算线程</span><input id="expTrainingThreads" type="number" value="2" min="1" max="8" step="1"></label>',
      '<label class="display-field"><span>位置损失权重</span><input id="expTrainingPositionWeight" type="number" value="1" min="0" max="100" step="any"></label><label class="display-field"><span>六维力损失权重</span><input id="expTrainingWrenchWeight" type="number" value="1" min="0" max="100" step="any"></label></div><label class="confirmation"><input id="expAdaptLegacyUnloaded" type="checkbox"><span id="expAdaptLegacyLabel">显式适配有可信原始无载谱的旧会话（记录派生 S0 来源）</span></label><p class="display-note">仅使用已结束会话。A / B 是候选来源，按完整试次或会话固定划分训练与验证；C 不参与拟合或早停。可另建采集会话，按压前须等待采集保护暂停确认。</p></details>',
      '<button id="expTrainingStart" class="button primary" disabled>批量训练所选数据</button><section class="training-progress"><div class="guided-progress-label"><span id="expTrainingState">尚未开始训练</span><strong id="expTrainingPercent">—</strong></div><progress id="expTrainingProgress" max="100" value="0" aria-label="服务端真实训练进度"></progress><p id="expTrainingStage" role="status" aria-live="polite">等待训练任务</p><p id="expTrainingLineage" class="display-note"></p><p id="expTrainingIteration" class="display-note"></p><p id="expTrainingPauseReasons" class="display-note"></p><div id="expTrainingActions" class="display-action-row" hidden><button id="expTrainingPause" class="button subtle" disabled>暂停训练</button><button id="expTrainingResume" class="button secondary" disabled>继续训练</button><button id="expTrainingCancel" class="button history-delete" disabled>取消训练</button></div><p id="expTrainingError" class="experiment-statusline error" hidden></p><p class="display-note">训练进度来自服务端；只有完整模型包校验并保存后显示 100%。暂停仍占用训练任务名额。</p></section>',
      '<section id="expTrainingResult" class="training-result" hidden><h3>模型已保存</h3><dl class="experiment-dl"><div><dt>模型编号</dt><dd id="expTrainingModelID">—</dd></div><div><dt>模型方法</dt><dd id="expTrainingModelMethod">—</dd></div><div><dt>保存时间（北京时间）</dt><dd id="expTrainingModelTime">—</dd></div><div><dt>训练点位</dt><dd id="expTrainingModelPoints">—</dd></div></dl><div id="expSpectralEvaluation" class="display-note" hidden></div><div id="expFineTuneComparison" class="finetune-comparison" hidden></div><button id="expGoValidation" class="button secondary" disabled>去模型推理</button><p class="display-note">手动载入已保存模型并明确建立本次无接触参考；未训练点不输出有效定位。</p></section></div></div></section>'
    ].join("");
  }
  function historyDialogHTML() {
    return '<div id="expHistoryDialog" class="history-dialog" role="dialog" aria-modal="true" aria-labelledby="expHistoryDialogTitle" hidden><div class="history-dialog-card"><h3 id="expHistoryDialogTitle">确认记录操作</h3><p id="expHistoryDialogSummary"></p><ul id="expHistoryDialogItems"></ul><p id="expHistoryDialogError" class="experiment-statusline error" hidden></p><div class="display-action-row"><button id="expHistoryCancel" class="button subtle">取消</button><button id="expHistoryConfirm" class="button secondary">确认操作</button></div></div></div>';
  }
  function modelDialogHTML() {
    return '<div id="expModelDialog" class="history-dialog" role="dialog" aria-modal="true" aria-labelledby="expModelDialogTitle" hidden><div class="history-dialog-card"><h3 id="expModelDialogTitle">确认模型操作</h3><p id="expModelDialogSummary"></p><ul id="expModelDialogItems"></ul><p id="expModelDialogError" class="experiment-statusline error" hidden></p><div class="display-action-row"><button id="expModelCancel" class="button subtle">取消</button><button id="expModelConfirm" class="button secondary">确认操作</button></div></div></div>';
  }
  function testHTML() {
    return [
      '<section id="expPanelTest" class="panel experiment-section" role="tabpanel" aria-labelledby="expTabTest" hidden><div class="validation-layout"><div class="validation-preparation"><h3>① 准备模型推理</h3><p class="display-note">载入模型 → 明确建立本次无接触参考 → 点击要识别的目标点 → 在手指对应位置真实按压。橙色圈表示目标，绿色灯只表示模型识别。</p>',
      '<div class="model-select-row"><label class="display-field" for="expModelSelect"><span>已保存模型编号</span><select id="expModelSelect"><option value="">尚无可用模型</option></select></label><button id="expLoadModelBtn" class="button secondary" disabled>载入模型</button><button id="expRefreshModels" class="button subtle">刷新模型</button><button id="expInferenceStop" class="button secondary inference-stop" disabled>结束推理</button></div><p id="expModelsRefreshState" class="experiment-statusline" role="status" hidden></p><div id="expInferenceState" class="experiment-statusline" role="status">尚未载入模型；载入后建立本次无接触参考。</div>',
      '<details id="expModelManagement" class="model-management"><summary>管理已保存模型</summary><p class="display-note">勾选模型后可移入可恢复回收区；训练数据和已保存人工判断报告保留。正在推理的当前模型请先“结束推理”。</p><label class="confirmation"><input id="expShowDeletedModels" type="checkbox"><span>查看已删除</span></label><p id="expModelSelectionState" class="display-note">已选择 0 个模型</p><div id="expModelList" class="model-list"><p class="experiment-empty">尚未读取模型列表</p></div><div class="display-action-row"><button id="expSelectModels" class="button secondary" disabled>全选可操作模型</button><button id="expClearModels" class="button subtle" disabled>清空选择</button><button id="expDeleteModels" class="button history-delete" disabled>删除所选</button><button id="expRestoreModels" class="button secondary" hidden disabled>恢复所选</button></div><p id="expModelManagementState" class="display-note"></p></details>',
      '<dl class="experiment-dl"><div><dt>当前模型</dt><dd id="expActiveModel">未加载</dd></div><div><dt>模型样件 / 力定义</dt><dd id="expModelDefinition">待加载</dd></div><div><dt>已训练点位</dt><dd id="expTrainedPoints">待加载</dd></div><div><dt>候选区分度</dt><dd id="expPredictionConfidence">—</dd></div></dl>',
      '<label class="confirmation"><input id="expTestUnloaded" type="checkbox"><span>确认当前无接触，建立本次模型推理光学参考</span></label><div class="display-action-row"><button id="expTestReferenceBtn" class="button secondary" disabled>建立本次无接触参考</button></div><div id="expTestReferenceState" class="experiment-statusline" role="status">本次模型推理参考尚未就绪</div><div id="expTruthZeroState" class="experiment-statusline">PX6D 真值仅用于对照；模型输入只有九路 FBG 特征。</div><p class="display-note">目标点只作人工对照，不输入模型。未训练点为灰色；无接触、不确定、超范围或陈旧结果不亮识别灯。</p>',
      '</div><div class="validation-live"><h3>② 选择目标点，实际按压观察</h3><div id="expTargetComparison" class="experiment-statusline">请先点击要识别的目标点位。</div><div id="expPredictionState" class="prediction-state">待加载模型与本次无接触参考</div><p id="expValidationAxes" class="validation-axis">待载入模型并核对其点阵定义；当前编号排列不声明空间方向。</p><div id="expValidationMap" class="validation-point-map" role="group" aria-label="5乘5实机识别矩阵，点击选目标，绿色灯仅为有效模型识别"></div><p id="expValidationMapState" class="display-note">橙色圈：选定目标；绿色灯：当前模型识别。两者独立。</p>',
      '<p id="expPredictionSelectionState" class="display-note" role="status">请点击目标点位；仅识别位置与所选点位一致时显示预测力和 PX6D 对照。</p><div id="expScalarPrediction" class="prediction-grid"><div class="prediction-card"><span>模型识别位置</span><strong id="expPredictionPoint">—</strong><small>模型输出，与目标选择分开</small></div><div class="prediction-card"><span>模型按压力</span><strong id="expPredictionForce">—</strong><small>N</small></div><div class="prediction-card"><span>PX6D 按压力 · 仅对照</span><strong id="expPredictionTruth">—</strong><small>N</small></div><div class="prediction-card"><span>模型 − PX6D</span><strong id="expPredictionError">—</strong><small>N</small></div></div><div id="expWrenchPrediction" class="wrench-comparison" hidden></div><p id="expPredictionReason" class="display-note">可以只选目标并观察识别灯与力差；需要计入统计时再勾选或保存判断。</p></div></div>',
      '<section class="validation-judgement-panel"><div id="expSpectralValidationSettings" class="spectral-validation-settings" hidden><label class="display-field"><span>六维验证模式</span><select id="expSpectralValidationMode"><option value="observe">实时观察</option><option value="judge">人工六轴验证</option></select></label><details><summary>逐轴容差（留空表示未配置）</summary><div class="wrench-tolerance-grid"><label class="display-field"><span>Fx / N</span><input id="expWrenchTolerance0" type="number" min="0" step="any" placeholder="未配置"></label><label class="display-field"><span>Fy / N</span><input id="expWrenchTolerance1" type="number" min="0" step="any" placeholder="未配置"></label><label class="display-field"><span>Fz / N</span><input id="expWrenchTolerance2" type="number" min="0" step="any" placeholder="未配置"></label><label class="display-field"><span>Mx / N·m</span><input id="expWrenchTolerance3" type="number" min="0" step="any" placeholder="未配置"></label><label class="display-field"><span>My / N·m</span><input id="expWrenchTolerance4" type="number" min="0" step="any" placeholder="未配置"></label><label class="display-field"><span>Mz / N·m</span><input id="expWrenchTolerance5" type="number" min="0" step="any" placeholder="未配置"></label></div></details><p class="display-note">六轴均配置且逐轴误差通过后才可勾选六维力正确。没有 PX6D 只显示光谱推理，不生成真值核验。</p></div><h3>本次判断（可选保存统计）</h3><p id="expFrozenStatus" class="display-note">尚未开始本次判断；选择目标并实际按压即可观察。</p><div id="expFrozenScalarSummary" class="validation-judgement-summary"><span>目标 <strong id="expJudgementTarget">—</strong></span><span>识别 <strong id="expFrozenPoint">—</strong></span><span>模型力 <strong id="expFrozenForce">—</strong> N</span><span>PX6D <strong id="expFrozenTruth">—</strong> N</span><span>差值 <strong id="expFrozenError">—</strong> N</span></div>',
      '<div id="expWrenchFrozen" class="wrench-comparison" hidden></div><div class="validation-judgement"><label class="confirmation"><input id="expPositionCorrect" type="checkbox" disabled><span>位置识别正确</span></label><label class="confirmation"><input id="expForceCorrect" type="checkbox" disabled><span id="expForceCorrectLabel">力正确：绝对差值 ≤ 0.3 N</span></label><button id="expValidationSave" class="button primary" disabled>保存本次判断</button><button id="expValidationDiscard" class="button subtle" disabled>重新判断</button></div><p id="expJudgementNote" class="display-note">勾选后本次判断读数保持不变。保存时未勾选就表示不正确；两项都不勾也可保存失败按压。重新判断会放弃当前未保存判断，不计入分母。</p></section>',
      '<section class="validation-statistics"><div class="validation-result-heading"><h3>本轮真实按压判断统计</h3><button id="expValidationFinish" class="button primary" disabled>保存本轮判断报告</button></div><p id="expValidationReportState" class="display-note">保存本轮真实按压判断报告；未完成判断请先保存或重新判断。</p><div id="expValidationStatsState" class="experiment-statusline">尚无已保存判断</div><div id="expValidationStats" class="experiment-report-cards"></div><p class="display-note">位置与力分别统计正确次数 / 已判定有效按压次数。识别失败但采集证据有效的真实按压也可保存为失败；未判定或采集无效不进入分母。正确率只来自你实际按压并保存的人工判断。</p></section></section>'
    ].join("");
  }
  function trialForceHTML() {
    return '<div id="expLiveForceCard" class="experiment-live-force"><span id="expLiveForceLabel">PX6D 当前按压力 / N</span><strong id="expLiveForceValue">—</strong><div id="expLiveForceRaw">原始 Fz：— N · 本试次零点：— N</div><div id="expLiveForceAge">最新六轴 · 非光谱配对</div><p id="expLiveForceReason">本试次基线与有效 PX6D 原始帧就绪后显示。</p></div>';
  }
  function captureHTML() {
    return [
      '<section id="expPanelSession" class="panel experiment-section" role="tabpanel" aria-labelledby="expTabSession"><div class="capture-session-card"><div class="capture-session-toolbar"><h3><span class="capture-number">1</span>采集会话</h3><p id="expDataNumberState" class="display-note" role="status">数据编号由服务端自动分配，成功保存会话后递增。</p><div class="display-action-row"><button id="expStartBtn" class="button primary" disabled>建立采集会话</button><button id="expStopBtn" class="button subtle" disabled>停止并保存会话</button></div></div>',
      '<details id="expSessionSetup" class="capture-session-setup" open><summary>会话设置与安装确认<span id="expProtocolSummary">目标 3 N · 每点 1～999 轮</span></summary><div class="experiment-form-grid">',
      '<label class="display-field" for="expFingerID"><span>手指 / 样件标识</span><input id="expFingerID" type="text" placeholder="输入实际样件标识" maxlength="120"></label>',
      '<label class="display-field" for="expCaptureMode"><span>采集模式</span><select id="expCaptureMode"><option value="full_spectrum" selected>原始全谱（512 点）</option><option value="peak_drift">寻峰漂移（FBG1～FBG9）</option></select></label><p id="expCaptureContentNote" class="display-note experiment-wide"></p>',
      '<label class="display-field" for="expAcquisitionProtocol"><span>采集协议</span><select id="expAcquisitionProtocol">' + CAPTURE_PROTOCOL_OPTIONS + '</select></label><label id="expTargetForceField" class="display-field" for="expTargetForceNorm"><span>三维合力目标 / N</span><input id="expTargetForceNorm" type="number" value="4" min="0.01" max="100" step="any"></label><label id="expForceNormField" class="display-field" for="expMaxForceNorm" hidden><span>观测三维合力上限 / N</span><input id="expMaxForceNorm" type="number" value="5" min="0.01" max="100" step="any"></label><p id="expProtocolUpgradeNotice" class="display-note experiment-wide" hidden></p><p id="expSampleRateNotice" class="display-note experiment-wide" hidden>新协议应使用实际 25 Hz 光谱请求；请在设备未连接时明确修改顶部采样设置。</p><label class="display-field" for="expRepetitions"><span>每点合格重复次数</span><input id="expRepetitions" type="number" min="1" max="999" step="1" value="3"></label>',
      '<label id="expForceSignField" class="display-field" for="expForceSign"><span>法向按压力方向</span><select id="expForceSign"><option value="-1">−Fz 为按压力</option><option value="1">+Fz 为按压力</option></select></label>',
      '<label class="display-field" for="expTemperature"><span>环境温度 / °C</span><input id="expTemperature" type="number" step="any" placeholder="可留空"></label></div>',
      '<label id="expDirectionConfirmationField" class="confirmation"><input id="expDirectionConfirmed" type="checkbox"><span id="expDirectionDescription">确认当前安装：向下按压时 Fz 更负，F = −(Fz − Fz₀)；指腹朝上对应 −PX6D Z</span></label>',
      '</details></div><div class="capture-layout">',
      '<div class="capture-point-column"><aside class="capture-point-panel"><h3><span class="capture-number">2</span>选择并确认采集点位</h3><div class="point-map-caption"><span id="expXAxisDirection">+x / +u → 右</span><span id="expYAxisDirection">+y / +v ↑ 指尖</span></div><div id="expPointMap" class="point-map" role="group" aria-label="25 点归一化接触位置"></div>',
      '<button id="expNoContact" type="button" class="point-cell no-contact-option" aria-pressed="false" disabled>无接触<small id="expNoContactCount">独立采集类别 · 不属于 25 点坐标</small></button><p id="expNoContactNote" class="display-note">无接触次数独立累计，不受每点重复次数限制，不计入 25 点完成进度；本轮保存后可继续无接触，或选择并确认其他点位。</p>',
      '<div id="expPointMapDefinition" class="point-map-side">原点：左下 P01（第 0 行、第 0 列）；+x 向右、+y 向上至指尖，指腹朝上对应 −PX6D Z。</div>',
      '<div class="experiment-statusline capture-selected-point"><strong id="expSelectedPoint">P01 · 行 0 / 列 0</strong><span id="expSelectedCoordinates">x/u = 0.00，y/v = 0.00</span><div id="expConfirmedPoint">尚未确认采集点位</div></div>',
      '<button id="expConfirmPointBtn" class="button secondary" disabled>确认采集点位</button><p class="display-note">网格和无接触选项只预选，须点击确认；活动轮次不能切换。</p>',
      '<details class="capture-session-details"><summary>会话编号与保存位置</summary><dl class="experiment-dl"><div><dt>会话编号</dt><dd id="expSessionID">尚未建立</dd></div><div><dt>当前批次</dt><dd id="expCurrentBatch">—</dd></div><div><dt>运行状态</dt><dd id="expSessionState">等待设备</dd></div></dl><p id="expSessionPath" class="experiment-path">会话路径将在记录开始后显示</p></details></aside>',
      '</div>',
      '<div class="capture-step-card capture-round-panel"><h3><span class="capture-number">3</span>本轮标定与采集</h3><div id="expGuidedMessage" class="guided-instruction" role="status" aria-live="polite">待开始：先建立会话并确认采集点位。</div>',
      '<div id="expGuidedDiagnostic" class="capture-diagnostic" hidden><span>最近检查详情 · 同阶段每 2 秒刷新，阶段变化与异常立即更新</span><p id="expGuidedDiagnosticText"></p></div><details class="capture-prompt-history"><summary>最近阶段提示（本页，最多 8 条）</summary><ol id="expGuidedHistory"><li>尚无阶段记录</li></ol></details>',
      '<div id="expPreloadStatus" class="preload-status" hidden><div class="guided-progress-label"><span>连续有效无载同步</span><strong id="expPreloadProgressText">等待服务端诊断</strong></div><progress id="expPreloadProgress" max="500" value="0" aria-label="连续有效无载同步时长"></progress><div class="preload-meta"><span id="expPreloadElapsed">已等待：—</span><span id="expPreloadResets">计时重置：—</span></div><p id="expPreloadBlockReason" role="status" aria-live="polite">等待服务端预采样诊断。</p><strong id="expPreloadTimeout" hidden>预采样超时，本轮失败且不计数。请保持无接触，重新明确确认无载并手动标定本轮。</strong><small id="expPreloadCriteriaNote">连续时长仅由服务端根据真实九峰与六轴同步结果累计；界面计时不会放行加载。</small></div>',
      '<div class="guided-round-meta"><strong id="expRoundLabel">当前轮次：待开始</strong><span id="expAttemptLabel">当前点尝试 0 次</span></div>',
      '<label class="confirmation"><input id="expBaselineConfirmed" type="checkbox"><span id="expBaselineConfirmationLabel">已移开接触棒，确认当前无接触；本轮重新建立六轴零点与九峰参考</span></label>',
      '<div class="display-action-row capture-round-actions"><button id="expBaselineBtn" class="button secondary" disabled>标定本轮</button><button id="expTrialStart" class="button primary" disabled>开始本轮采集</button><button id="expTrialFinish" class="button primary" hidden disabled>结束本轮</button><button id="expTrialAbort" class="button subtle" disabled>中止本轮</button></div>',
      '<div id="expBaselineState" class="experiment-statusline">本轮无载基线尚未就绪</div>',
      '<p class="display-note">每轮自动更新采集软件零偏；顶部六轴显示使用独立显示零点，原始读数保留。</p>',
      '<div class="guided-progress-group"><div class="guided-progress-label"><span>25 点总采集进度</span><strong id="expTotalProgressText">0 / 0 · 0%</strong></div><progress id="expTotalProgress" max="100" value="0" aria-label="25点总采集进度"></progress>',
      '<div class="guided-progress-label"><span id="expPointProgressLabel">当前采集点进度</span><strong id="expPointProgressText">0 / 0 · 0%</strong></div><progress id="expPointProgress" max="100" value="0" aria-label="当前点合格轮次进度"></progress>',
      '<div class="guided-progress-label"><span id="expTrialProgressLabel">本轮加载到 3 N</span><strong id="expForceProgressText">0%</strong></div><progress id="expForceProgress" max="100" value="0" aria-label="服务端当前加载进度"></progress></div>',
      trialForceHTML(), '<div id="expTargetForceState" class="experiment-statusline" role="status" hidden></div><div id="expRoundQuality" class="experiment-statusline">尚无本轮判定；合格次数只由服务端质量检查计入。</div>',
      '<details class="capture-record-details"><summary>操作步骤、原始记录与同步质量</summary><ol id="expGuidedFlow" class="guided-flow"><li>确认无载并手动标定本轮</li><li>点击开始，保持无接触等待提示加载</li><li>缓慢加载至 3 N，按提示卸载</li><li>移开接触棒，等待判定；每轮重新标定</li></ol><dl class="experiment-dl"><div><dt>采集点</dt><dd id="expTrialPoint">—</dd></div><div><dt>本轮试次编号</dt><dd id="expTrialID">尚未开始</dd></div><div><dt>当前阶段</dt><dd id="expTrialPhase">等待本轮</dd></div><div><dt>记录状态</dt><dd id="expWriterStatus">等待会话</dd></div></dl>',
      '<div class="experiment-counters"><div class="experiment-counter"><span id="expOpticalCountLabel">原始光谱</span><strong id="expSpectraCount">0</strong></div><div class="experiment-counter"><span>原始六轴</span><strong id="expForceCount">0</strong></div><div class="experiment-counter"><span>有效 / 无效同步</span><strong id="expSyncCount">0 / 0</strong></div><div class="experiment-counter"><span>完成 / 中止试次</span><strong id="expTrialCount">0 / 0</strong></div></div><p class="display-note">检查通过才计数，失败或中止不计数；本模式数据连续保留。</p></details></div></div></section>'
    ].join("");
  }
  function html() {
    return [
      '<div class="experiment-heading"><div><h2>采集、训练、模型推理</h2><p>采集真实按压 → 选择方法训练 → 点选目标并实际按压验证</p></div><span id="expRecordBadge" class="quality-pill neutral">等待会话</span></div>',
      '<div id="expCompatibilityNotice" class="experiment-statusline error" role="alert" hidden></div>',
      '<div class="experiment-tabs" role="tablist" aria-label="实验工作区"><button class="experiment-tab" id="expTabSession" role="tab" aria-selected="true" aria-controls="expPanelSession">① 采集</button><button class="experiment-tab" id="expTabTraining" role="tab" aria-selected="false" aria-controls="expPanelTraining">② 训练</button><button class="experiment-tab" id="expTabTest" role="tab" aria-selected="false" aria-controls="expPanelTest">③ 模型推理</button></div>',
      '<div id="expMessage" class="experiment-statusline" role="status" aria-live="polite">先连接光谱与 PX6D，核对无载参考后建立采集会话。</div>',
      captureHTML(), trainingHTML(), testHTML(), historyDialogHTML(), modelDialogHTML()
    ].join("");
  }
  function message(text, tone = "") {
    put("expMessage", text); $("expMessage").className = "experiment-statusline" + (tone ? " " + tone : "");
  }
  async function performRequest(path, body, timeoutMs) {
    if (body !== undefined && !backendCompatible(latest) && !["/api/experiment/stop", "/api/disconnect"].includes(path)) throw new Error(compatibilityMessage(latest));
    const controller = typeof global.AbortController === "function" ? new global.AbortController() : null;
    const timer = controller ? global.setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await global.fetch(path, body === undefined ? { cache: "no-store", ...(controller ? { signal: controller.signal } : {}) } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), ...(controller ? { signal: controller.signal } : {}) });
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(result.detail || result.error || result.message || "操作未完成（" + response.status + "）");
      return result;
    } catch (error) {
      if (error.name === "AbortError") {
        if (body !== undefined) {
          expired = true;
          invalidateLiveValidation("操作响应超时，等待最新服务状态后再核对操作结果。");
          updateControls();
        }
        throw new Error(body === undefined ? "读取列表超时，请稍后手动刷新列表。" : "操作响应超时，结果尚未确认。请先检查当前状态，勿重复点击同一操作。");
      }
      throw error;
    } finally { if (timer !== null) global.clearTimeout(timer); }
  }
  async function request(path, body, timeoutMs = body === undefined ? LIST_REQUEST_TIMEOUT_MS : COMMAND_TIMEOUTS[path] || COMMAND_REQUEST_TIMEOUT_MS) {
    // Coalesce read-only list refreshes. Commands are sent once and never retried.
    if (body !== undefined) return performRequest(path, body, timeoutMs);
    if (pendingReads.has(path)) return pendingReads.get(path);
    const pending = performRequest(path, body, timeoutMs);
    pendingReads.set(path, pending);
    try { return await pending; }
    finally { if (pendingReads.get(path) === pending) pendingReads.delete(path); }
  }
  async function run(callback) {
    if (busy) return;
    if (guiPaused()) { message("脉搏曲线优先，本页显示与操作已暂停；请先恢复本页显示并核对最新状态。", "info"); return; }
    busy = true; updateControls();
    try { await callback(); }
    catch (error) { message(error instanceof TypeError ? "无法连接本机服务，请检查 9FBG 是否正在运行。" : error.message, "error"); }
    finally { busy = false; updateControls(); }
  }
  function switchTab(next) {
    tab = next;
    for (const name of ["session", "training", "test"]) {
      const suffix = name[0].toUpperCase() + name.slice(1);
      $("expPanel" + suffix).hidden = name !== next;
      $("expTab" + suffix).setAttribute("aria-selected", String(name === next));
      $("expTab" + suffix).tabIndex = name === next ? 0 : -1;
    }
    if (next === "training" && (!sessionsLoaded || !modelsLoaded)) run(async () => { if (!sessionsLoaded) await refreshSessions(); if (!modelsLoaded) await refreshModels(); });
    if (next === "test" && !modelsLoaded) run(refreshModels);
  }
  function numericInput(id, label, optional = false) {
    const text = $(id).value.trim();
    if (!text && optional) return null;
    const value = Number(text);
    if (!text || !finite(value)) throw new Error(label + "需要有限数值。");
    return value;
  }
  function historyRows() { return $("expShowDeleted").checked ? deletedSessions : sessions; }
  function checkedSessionIDs() { return Array.from(selectedSessions); }
  function historyMutationBlocked() { return !backendCompatible(latest) || expired || latest.experiment?.running === true || latest.analysis?.busy === true || trainingView(latest.analysis?.training).busy; }
  function applyHistoryList(result) {
    sessions = Array.isArray(result.sessions) ? result.sessions.map(row => ({ ...row, deleted: false })) : [];
    deletedSessions = Array.isArray(result.deleted_sessions) ? result.deleted_sessions.map(row => ({ ...row, deleted: true })) : [];
    sessionsLoaded = true;
    const available = new Set(historyRows().map(session => session.session_id));
    if (!sequenceMethod($("expTrainingMethod").value)) for (const id of selectedSessions) if (!available.has(id)) selectedSessions.delete(id);
    applyCumulativeSpectralSelection();
    renderSessions();
  }
  function applyCumulativeSpectralSelection() {
    const method = $("expTrainingMethod").value;
    if (!sequenceMethod(method) || fineTuneSelected() || !cumulativeSpectralSelection || $("expShowDeleted").checked) return;
    if (sequenceMethod(method)) for (const id of selectedSessions) {
      const session = sessions.find(row => row.session_id === id);
      // Only the automatic cumulative set changes with the input method.
      // Manual selections stay visible for an explicit incompatibility error.
      if (session && (method === "drift_gru" && !historyTrainingStatus(session, method).allowed || method === "spectral_gru" && (session.spectral_available === false || (session.capture_mode || session.settings?.capture_mode) === "peak_drift"))) selectedSessions.delete(id);
    }
    for (const id of cumulativeSpectralSourceIDs(sessions, method)) selectedSessions.add(id);
  }
  function manualTrainingSelection() { cumulativeSpectralSelection = false; $("expCumulativeSources").checked = false; }
  function renderSessions() {
    const visible = historyRows(), deleted = $("expShowDeleted").checked, count = value => Number.isInteger(value) && value >= 0 ? value : "—";
    $("expSessionList").innerHTML = visible.length ? visible.map(session => {
      const batch = session.settings?.batch || session.batch || "—", status = historyTrainingStatus(session, $("expTrainingMethod").value);
      const checked = selectedSessions.has(session.session_id), points = sessionPointSummary(session);
      const pointMarkup = points.length ? '<span class="session-point-counts">' + points.map(point => '<span>' + escapeHTML(point.id) + ' · ' + escapeHTML(point.location) + ' · 合格 ' + (point.accepted ?? "—") + ' / 尝试 ' + point.attempts + ' 轮</span>').join("") + '</span>' : '<small>尚无逐点合格按压记录</small>';
      const noContactMarkup = Number.isInteger(session.no_contact_trial_count) && session.no_contact_trial_count > 0 ? '<small class="session-no-contact-count">无接触 · 合格 ' + count(session.accepted_no_contact_trial_count) + ' / 尝试 ' + count(session.no_contact_trial_count) + ' 轮 · 独立类别，无空间坐标</small>' : '';
      const pressCount = Number.isInteger(session.accepted_trial_count) ? Math.max(0, session.accepted_trial_count - (Number.isInteger(session.accepted_no_contact_trial_count) ? session.accepted_no_contact_trial_count : 0)) : null;
      const eligibility = deleted ? (session.restorable === true ? "已删除 · 可恢复" : "已删除 · 恢复受限") : status.allowed ? "可训练" : "不可训练：" + status.reason;
      return '<div class="experiment-session-row history-record-row' + (status.allowed ? " qualified" : " unqualified") + '"><input type="checkbox" aria-label="选择 ' + escapeHTML(historyRecordLabel(session)) + '" data-session-id="' + escapeHTML(session.session_id) + '"' + (checked ? " checked" : "") + '><div class="history-record-info"><strong class="session-title">样件 ' + escapeHTML(session.settings?.finger_id || "未记录") + ' · ' + escapeHTML(batch) + ' 批 · 数据编号 ' + escapeHTML(session.data_id || session.settings?.data_id || '未编号') + ' · 合格按压 ' + count(pressCount) + ' 轮</strong><small>开始 ' + escapeHTML(sessionTimeText(session.started_utc)) + ' · 结束 ' + escapeHTML(sessionTimeText(session.ended_utc)) + '</small><small>' + (session.complete ? "文件已完整保存" : "文件完整性待确认") + ' · 总尝试 ' + count(session.trial_count) + ' 轮 · 未通过 ' + count(session.rejected_trial_count) + ' 轮' + (Number.isInteger(session.repetitions_per_point) ? ' · 目标每点 ' + session.repetitions_per_point + ' 轮' : '') + (session.acquisition_complete === true ? ' · 25 点采集已达次数' : ' · 25 点采集尚未完成') + '</small><small>' + escapeHTML(sessionCaptureLabel(session)) + '</small>' + pointMarkup + noContactMarkup + '<small class="history-eligibility">' + escapeHTML(eligibility) + '</small>' + (session.history_error ? '<small class="history-record-error">' + escapeHTML(session.history_error) + '</small>' : '') + '<small class="session-id">' + escapeHTML(session.session_id) + '</small><small class="history-record-path">' + escapeHTML(session.path || "保存位置未记录") + '</small></div><button type="button" class="button subtle history-open" data-open-session="' + escapeHTML(session.session_id) + '" data-deleted="' + deleted + '">打开保存位置</button></div>';
    }).join("") : '<p class="experiment-empty">' + (deleted ? "回收区暂无已删除记录。" : "尚无采集记录。先进行真实采集，再刷新列表。") + '</p>';
    put("expSessionFilterState", (deleted ? "回收区 " + deletedSessions.length : "全部采集记录 " + sessions.length) + " 条；时间为原始采集北京时间，精确到分钟。删除只移入可恢复回收区，不永久删除文件。");
    updateControls();
  }
  async function refreshSessions() { applyHistoryList(await request("/api/history/sessions")); }
  function closeHistoryDialog() {
    historyPendingAction = null; $("expHistoryDialog").hidden = true;
    $("expHistoryDialogError").hidden = true; updateControls();
  }
  function openHistoryDialog(action) {
    if (busy || historyMutationBlocked() || modelPendingAction) { message("采集或训练进行中，或服务状态尚未就绪，不能删除或恢复记录。", "error"); return; }
    const deleted = $("expShowDeleted").checked, ids = checkedSessionIDs(), rows = ids.map(id => historyRows().find(row => row.session_id === id));
    if (!ids.length || rows.some(row => !row)) { message("请先选择列表中的记录。", "error"); return; }
    if ((action === "restore") !== deleted || rows.some(row => action === "delete" ? row.deletable !== true : row.restorable !== true)) { message("所选记录包含受保护或当前不能" + (action === "delete" ? "删除" : "恢复") + "的项，请核对各条状态后重新选择。", "error"); return; }
    historyPendingAction = { action, ids: ids.slice(), deleted };
    put("expHistoryDialogTitle", "确认" + (action === "delete" ? "删除" : "恢复") + "所选 " + ids.length + " 条记录");
    put("expHistoryDialogSummary", action === "delete" ? "以下记录将整体移入可恢复回收区，不永久删除原始文件。请核对采集时间和数据编号后确认。" : "以下记录将从回收区恢复至原保存位置。请核对采集时间和数据编号后确认。");
    $("expHistoryDialogItems").innerHTML = rows.map(row => '<li><strong>' + escapeHTML(historyRecordLabel(row)) + '</strong><small>' + escapeHTML(row.path || "") + '</small></li>').join("");
    $("expHistoryDialogError").hidden = true;
    put("expHistoryConfirm", action === "delete" ? "确认移入回收区" : "确认恢复");
    $("expHistoryDialog").hidden = false; updateControls(); $("expHistoryCancel").focus?.();
  }
  function modelRows() { return $("expShowDeletedModels").checked ? deletedModels : models; }
  function modelMutationBlocked() {
    return !backendCompatible(latest) || expired || !receivedAt || Date.now() - receivedAt > 1500 || latest.experiment?.running === true || latest.stopping === true || latest.recording === true || latest.analysis?.busy === true || trainingView(latest.analysis?.training).busy || hasPendingJudgement() || !!latest.validation?.pending_sample;
  }
  function modelRowBlockReason(model, restoring = $("expShowDeletedModels").checked) {
    if (!model) return "模型已不在当前列表，请刷新后重选。";
    if (!restoring && model.model_id === latest.analysis?.model_id && latest.analysis?.inference_active === true) return "当前正在推理，请先结束推理。";
    if (restoring ? model.restorable !== true : model.deletable !== true) return model.model_error || model.history_error || model.reason || (restoring ? "当前不能恢复，请核对模型状态。" : "当前不能删除，请核对模型状态。");
    return "";
  }
  function applyModelList(result) {
    models = Array.isArray(result.models) ? result.models : [];
    deletedModels = Array.isArray(result.deleted_models) ? result.deleted_models : [];
    modelsLoaded = true;
    const available = new Set(modelRows().map(row => row.model_id));
    for (const id of selectedModels) if (!available.has(id)) selectedModels.delete(id);
    renderModels();
  }
  function renderModelManagement() {
    const deleted = $("expShowDeletedModels").checked, rows = modelRows();
    const key = JSON.stringify([deleted, rows, Array.from(selectedModels), latest.analysis?.model_id, latest.analysis?.inference_active]);
    if (key === lastModelManagementKey) return;
    lastModelManagementKey = key;
    $("expModelList").innerHTML = rows.length ? rows.map(model => {
      const points = Array.isArray(model.trained_point_ids || model.point_ids) ? model.trained_point_ids || model.point_ids : [], reason = modelRowBlockReason(model);
      const created = model.created_local_minute || sessionTimeText(model.created_utc);
      return '<label class="model-record-row' + (reason ? ' protected' : '') + '"><input type="checkbox" data-model-id="' + escapeHTML(model.model_id) + '" aria-label="选择模型 ' + escapeHTML(model.model_id) + '"' + (selectedModels.has(model.model_id) ? ' checked' : '') + (reason ? ' disabled' : '') + '><span><strong>' + escapeHTML(model.model_id) + '</strong><small>' + escapeHTML(model.method_label || trainingMethodLabel(model.method || model.model_kind)) + ' · 保存 ' + escapeHTML(created) + '</small><small>已训练点位：' + escapeHTML(points.length ? points.join('、') : '未记录') + '</small><small class="model-record-status">' + escapeHTML(reason || (deleted ? '已删除 · 可恢复' + (model.deleted_local_minute || model.deleted_utc ? ' · 删除 ' + (model.deleted_local_minute || sessionTimeText(model.deleted_utc)) : '') : '可移入回收区')) + '</small>' + (model.loadable === false && model.history_error ? '<small class="model-record-error">不可载入：' + escapeHTML(model.history_error) + '</small>' : '') + '</span></label>';
    }).join('') : '<p class="experiment-empty">' + (deleted ? '回收区暂无已删除模型。' : '尚无已保存模型。') + '</p>';
  }
  function closeModelDialog() {
    modelPendingAction = null; $("expModelDialog").hidden = true; $("expModelDialogError").hidden = true; updateControls();
  }
  function openModelDialog(action) {
    if (busy || modelMutationBlocked() || historyPendingAction) { message("采集、训练或保存进行中，或仍有待保存判断，当前不能删除或恢复模型。", "error"); return; }
    const deleted = $("expShowDeletedModels").checked, ids = Array.from(selectedModels), rows = ids.map(id => modelRows().find(model => model.model_id === id));
    if (!ids.length || (action === "restore") !== deleted || rows.some(row => modelRowBlockReason(row, deleted))) { message("请勾选当前列表中可" + (action === "delete" ? "删除" : "恢复") + "的模型；正在推理的模型须先结束推理。", "error"); return; }
    modelPendingAction = { action, ids: ids.slice(), deleted };
    put("expModelDialogTitle", "确认" + (action === "delete" ? "删除" : "恢复") + "所选 " + ids.length + " 个模型");
    put("expModelDialogSummary", action === "delete" ? "以下模型将整体移入可恢复回收区。训练数据和已保存人工判断报告保留；此操作不会自动载入其他模型。" : "以下模型将恢复至已保存模型列表；恢复不会自动载入或开启推理。");
    $("expModelDialogItems").innerHTML = rows.map(row => '<li><strong>' + escapeHTML(modelOptionLabel(row)) + '</strong></li>').join('');
    $("expModelDialogError").hidden = true; put("expModelConfirm", action === "delete" ? "确认移入回收区" : "确认恢复");
    $("expModelDialog").hidden = false; updateControls(); $("expModelCancel").focus?.();
  }
  function renderModels() {
    const loadable = models.filter(model => model.loadable !== false);
    const newest = loadable.slice().sort((a, b) => String(b.created_utc || b.created_local_minute || b.model_id).localeCompare(String(a.created_utc || a.created_local_minute || a.model_id)))[0]?.model_id || "";
    const selected = preferredModelID || $("expModelSelect").value || newest;
    const nextKey = JSON.stringify(loadable);
    if (nextKey !== lastModelKey) {
      $("expModelSelect").innerHTML = '<option value="">选择已保存模型</option>' + loadable.map(model => '<option value="' + escapeHTML(model.model_id) + '">' + escapeHTML(modelOptionLabel(model)) + '</option>').join("");
      lastModelKey = nextKey;
    }
    $("expModelSelect").value = loadable.some(model => model.model_id === selected) ? selected : newest;
    if (preferredModelID) preferredModelID = null;
    renderBaseModels(); renderModelManagement(); updateControls();
  }
  async function refreshModels(preferred = null) {
    if (preferred) preferredModelID = preferred;
    try {
      applyModelList(await request("/api/analysis/models"));
      modelRefreshNotice("");
    } catch (error) {
      modelRefreshNotice("模型列表尚未更新：" + (error instanceof TypeError ? "暂时无法连接本机服务，请稍后手动刷新模型。" : error.message));
      throw error;
    }
  }
  function modelRefreshNotice(text) {
    for (const id of ["expTrainingModelsRefreshState", "expModelsRefreshState"]) {
      put(id, text); $(id).hidden = !text;
    }
  }
  function renderFineTuneComparison(evaluation) {
    const comparison = evaluation?.finetuning_comparison;
    $("expFineTuneComparison").hidden = !comparison;
    if (!comparison) { $("expFineTuneComparison").innerHTML = ""; return; }
    if (comparison.available === false) { $("expFineTuneComparison").innerHTML = '<h4>基础模型与新模型验证对照不可用</h4><p>' + escapeHTML(comparison.reason || '没有可供父子比较的固定独立验证数据。') + '</p>'; return; }
    const parent = comparison.parent || {}, child = comparison.child || {};
    const accuracy = metrics => finite(metrics.position_accuracy) ? number(metrics.position_accuracy * 100, 1) + "%" : "数据不足 / 未报告";
    const parentMAE = wrenchVector(parent.wrench?.mae), childMAE = wrenchVector(child.wrench?.mae);
    const parentRMSE = wrenchVector(parent.wrench?.rmse), childRMSE = wrenchVector(child.wrench?.rmse);
    const trials = Array.isArray(comparison.validation_trial_ids) ? comparison.validation_trial_ids.length : null;
    $("expFineTuneComparison").innerHTML = '<h4>基础模型与新模型的同集验证对照</h4><p>固定验证试次 ' + (trials ?? '未报告') + ' 个 · 窗口 ' + (Number.isInteger(comparison.window_count) ? comparison.window_count : '未报告') + ' 个；指标可能升高或降低，不代表实机精度验收。</p><table class="experiment-table"><thead><tr><th>指标</th><th>基础模型</th><th>新候选模型</th></tr></thead><tbody><tr><th>接触位置准确率</th><td>' + accuracy(parent) + '</td><td>' + accuracy(child) + '</td></tr></tbody></table>' + (parentMAE || childMAE ? '<p>六轴 MAE</p>' + wrenchTable(parentMAE, childMAE, null, ['基础模型', '新候选模型', '']) : '') + (parentRMSE || childRMSE ? '<p>六轴 RMSE</p>' + wrenchTable(parentRMSE, childRMSE, null, ['基础模型', '新候选模型', '']) : '') + '<p>新模型另存，当前推理模型保持；需人工比较后手动载入。</p>';
  }
  function renderTraining(analysis) {
    const view = trainingView(analysis?.training);
    put("expTrainingState", ({ idle: "尚未开始训练", running: "正在训练", pause_requested: "已请求暂停 · 等待当前批次确认", paused: "训练已暂停", cancelling: "正在取消训练", cancelled: "训练已取消", completed: view.complete ? "训练完成 · 模型已保存" : "等待确认模型保存完成", failed: "训练失败" })[view.status]);
    put("expTrainingPercent", view.progress === null ? "—" : number(view.progress, 0) + "%");
    $("expTrainingProgress").hidden = view.progress === null;
    $("expTrainingProgress").value = view.progress ?? 0;
    put("expTrainingStage", view.stage);
    put("expTrainingLineage", sequenceMethod(view.method) ? (view.mode === "finetune" ? "继承模型继续训练 · 基础模型 " + (view.baseModelID || "等待服务端确认") : "从头训练") + (view.newSessionIDs.length ? " · 新增 " + view.newSessionIDs.length + " 个会话" : "") + (view.mergedSessionIDs.length ? " · 合并来源 " + view.mergedSessionIDs.length + " 个会话" : "") : "");
    put("expTrainingPauseReasons", view.pauseReasons.length ? "暂停原因：" + view.pauseReasons.map(reason => ({ user: "用户主动暂停", acquisition: "采集保护暂停", trial: "采集保护暂停" }[reason] || reason)).join("；") : "");
    $("expTrainingActions").hidden = !sequenceMethod(view.method) || !view.busy;
    put("expTrainingIteration", (view.iteration !== null ? "实际迭代 " + view.iteration + (view.iterations !== null ? " / " + view.iterations : "") : "") + (view.fold !== null ? " · 整轮验证折 " + view.fold + (view.folds !== null ? " / " + view.folds : "") : ""));
    $("expTrainingError").hidden = !view.error; put("expTrainingError", view.error || "");
    $("expTrainingResult").hidden = !view.complete;
    put("expTrainingModelID", view.modelID || "—"); put("expTrainingModelMethod", view.methodLabel);
    put("expTrainingModelTime", view.created || "等待服务端保存时间"); put("expTrainingModelPoints", view.points.length ? view.points.join(" / ") : "等待模型点位定义");
    const evaluation = analysis?.training?.evaluation || analysis?.training?.report || analysis?.training?.metrics;
    $("expSpectralEvaluation").hidden = !sequenceMethod(view.method) || !evaluation || !view.complete;
    renderFineTuneComparison(view.complete && sequenceMethod(view.method) ? evaluation : null);
    if (sequenceMethod(view.method) && evaluation) {
      const measured = evaluation.validation || {}, axisMAE = wrenchVector(measured.wrench?.mae || evaluation.wrench_mae), axisRMSE = wrenchVector(measured.wrench?.rmse || evaluation.wrench_rmse);
      $("expSpectralEvaluation").innerHTML = '<p>' + escapeHTML(evaluation.validation_note || (evaluation.independent_validation === true ? '固定完整分组验证' : '数据不足，无独立验证')) + ' · 接触位置准确率 ' + (finite(measured.position_accuracy) ? number(measured.position_accuracy * 100, 1) + '%' : '数据不足 / 未报告') + '</p>' + (axisMAE || axisRMSE ? wrenchTable(axisMAE, axisRMSE, null, ["MAE", "RMSE", ""]) : '') + '<p>训练指标不代表硬件同步或物理精度验收；完整混淆矩阵、逐点召回与阶段误差保存在模型元数据。</p>';
      const contact = measured.contact;
      if (contact && Number.isInteger(contact.count) && contact.count > 0) {
        const pct = value => finite(value) ? number(value * 100, 1) + '%' : '数据不足 / 未报告';
        $("expSpectralEvaluation").innerHTML += '<p>接触判断准确率 ' + pct(contact.accuracy) + ' · 接触召回率 ' + pct(contact.contact_recall) + ' · 无接触误报率 ' + pct(contact.no_contact_false_positive_rate) + ' · 无接触保留率 ' + pct(contact.no_contact_retention_rate) + '</p><p>验证窗口：接触 ' + escapeHTML(contact.contact_count ?? '—') + ' / 无接触 ' + escapeHTML(contact.no_contact_count ?? '—') + '；重叠窗口数量不等于独立采集轮数。</p>';
      }
      const forceBins = measured.position_by_force_norm_N;
      if (forceBins && typeof forceBins === "object") $("expSpectralEvaluation").innerHTML += '<p>分合力定位：' + Object.entries(forceBins).map(([range, row]) => escapeHTML(range) + ' N · ' + (Number.isInteger(row?.count) && row.count > 0 && finite(row.accuracy) ? number(row.accuracy * 100, 1) + '%（' + row.count + ' 窗）' : '数据不足')).join('；') + '</p>';
      const sampling = evaluation.training_sampling;
      if (sampling?.schema === "spectral_train_sampling_v2" && ["explicit_no_contact_selected_count", "explicit_no_contact_available_train_count", "explicit_no_contact_heldout_count"].every(key => Number.isInteger(sampling[key]) && sampling[key] >= 0)) {
        $("expSpectralEvaluation").innerHTML += '<p>本次优化使用显式无接触 ' + sampling.explicit_no_contact_selected_count + ' 个窗口；验证/保留 ' + sampling.explicit_no_contact_heldout_count + ' 个窗口。训练分组可用 ' + sampling.explicit_no_contact_available_train_count + ' 个窗口。</p>';
        const note = sampling.explicit_no_contact_note || (sampling.explicit_no_contact_selected_count === 0 ? "显式无接触标签未参与本次优化；请核对固定分组与训练来源。" : "");
        if (note) $("expSpectralEvaluation").innerHTML += '<p' + (sampling.explicit_no_contact_selected_count === 0 ? ' class="history-record-error"' : '') + '>' + escapeHTML(note) + '</p>';
      }
    }
    if (view.complete && completedTrainingKey !== (analysis.training.job_id || view.modelID)) {
      completedTrainingKey = analysis.training.job_id || view.modelID;
      preferredModelID = view.modelID;
      // A completed job is observed once per page. List failure stays local;
      // it must not replace live acquisition instructions or retry each poll.
      refreshModels(view.modelID).catch(() => {});
      message("训练完成，模型已保存。可前往模型推理，手动载入后建立本次无接触参考。", "success");
    }
  }
  function updateControls() {
    if (!mounted) return;
    if (guiPaused()) { holdControls(true); return; }
    holdControls(false);
    const compatible = backendCompatible(latest);
    const gates = experimentGates(latest, busy || expired, activePoint), analysisBusy = busy || latest.analysis?.busy === true || trainingView(latest.analysis?.training).busy || !compatible;
    const buttonGates = { expStartBtn: gates.start, expStopBtn: gates.stop, expConfirmPointBtn: gates.pointConfirm, expBaselineBtn: gates.baseline && $("expBaselineConfirmed").checked, expTrialStart: gates.trialStart, expTrialFinish: gates.finish, expTrialAbort: gates.abort };
    for (const [id, enabled] of Object.entries(buttonGates)) $(id).disabled = !enabled;
    for (const button of $("expPointMap").querySelectorAll("button")) button.disabled = !gates.point;
    $("expNoContact").disabled = !gates.point || !captureUsesTargetProtocol(latest.experiment);
    const running = latest.experiment?.running === true, training = trainingView(latest.analysis?.training);
    const protocol = captureUsesWrenchProtocol(latest.experiment), targetProtocol = captureUsesTargetProtocol(latest.experiment);
    for (const id of ["expFingerID", "expForceSign", "expRepetitions", "expTemperature", "expDirectionConfirmed", "expCaptureMode", "expAcquisitionProtocol", "expMaxForceNorm", "expTargetForceNorm"]) $(id).disabled = busy || running;
    $("expDirectionConfirmed").disabled = busy || running || protocol;
    $("expForceSign").disabled = busy || running || protocol;
    $("expForceNormField").hidden = !protocol;
    $("expTargetForceField").hidden = !targetProtocol;
    $("expTargetForceState").hidden = !targetProtocol;
    $("expTrialFinish").hidden = !targetProtocol;
    $("expTrialFinish").title = noContactSelection(latest.experiment) ? gates.finish ? "保持无接触，点击保存本轮并建立下一轮参考。" : latest.experiment?.guided?.finish_block_reason || "真实无接触数据满足服务端检查后，结束本轮才会点亮。" : gates.finish ? "请先移开压力棒；点击即确认已卸载，直接建立下一轮软件零点与" + captureReference(latest.experiment) + "并保存。" : latest.experiment?.guided?.finish_block_reason || "达到配置目标并通过服务端按压数据检查后，结束本轮才会点亮。";
    $("expForceSignField").hidden = protocol;
    $("expDirectionConfirmationField").hidden = protocol;
    $("expSampleRateNotice").hidden = !protocol;
    const requestedRate = actualSampleRate(latest);
    put("expSampleRateNotice", (captureUsesDriftMode(latest.experiment) ? legacyDriftCapture(latest.experiment) ? "旧九路漂移会话使用光谱请求 25 Hz（含原谱备份）；当前服务设置 " : "寻峰漂移六维协议使用光谱请求 25 Hz（原谱只用于实时寻峰，不保存）；当前服务设置 " : "全谱六维协议使用光谱请求 25 Hz；当前服务设置 ") + (finite(requestedRate) ? requestedRate + " Hz" : "待核对") + "。请先在顶部明确设置并按设备状态重新连接；界面不会自动改设备。");
    put("expProtocolSummary", targetProtocol ? "开始先记录 2 s 无载基线 · 目标默认 4 N · 结束直接软件回零 · 上限默认 5 N" : protocol ? "主按压 3 s · 合力默认上限 5 N" : "目标 3 N · 每点 1～999 轮");
    put("expBaselineConfirmationLabel", protocol ? captureUsesDriftMode(latest.experiment) ? "已移开接触棒，确认当前无接触；建立寻峰漂移参考和六轴软件零偏，" + (legacyDriftCapture(latest.experiment) ? "旧会话保留原谱备份" : "仅保存九路时序与标签") : "已移开接触棒，确认当前无接触；建立真实 512 点无载参考和六轴软件零偏" : "已移开接触棒，确认当前无接触；本轮重新建立六轴零点与九峰参考");
    $("expBaselineConfirmed").disabled = !gates.baseline;
    const selected = checkedSessionIDs();
    const spectral = sequenceMethod($("expTrainingMethod").value), finetune = fineTuneSelected();
    $("expCumulativeSourcesField").hidden = !spectral;
    $("expCumulativeSources").checked = cumulativeSpectralSelection;
    $("expCumulativeSources").disabled = analysisBusy || finetune || !!historyPendingAction || $("expShowDeleted").checked;
    $("expTrainingStart").disabled = analysisBusy || expired || running && !spectral || !!modelPendingAction || !!historyPendingAction || $("expShowDeleted").checked || !selected.length || selected.some(id => !historyTrainingStatus(sessions.find(row => row.session_id === id), $("expTrainingMethod").value).allowed) || spectral && (!["scratch", "finetune"].includes($("expTrainingMode").value) || finetune && !selectedBaseModel());
    $("expTrainingMethod").disabled = analysisBusy || !!historyPendingAction;
    $("expSpectralParameters").hidden = !spectral;
    $("expFineTuneSettings").hidden = !finetune;
    for (const id of ["expTrainingMode", "expBaseModelSelect", "expRefreshTrainingModels"]) $(id).disabled = !spectral || analysisBusy || expired || !!historyPendingAction || !!modelPendingAction;
    for (const id of [...Object.values(TRAINING_PARAMETER_FIELDS), "expAdaptLegacyUnloaded"]) $(id).disabled = analysisBusy || !!historyPendingAction;
    $("expAdaptLegacyUnloaded").disabled = analysisBusy || !!historyPendingAction || finetune || $("expTrainingMethod").value === "drift_gru";
    put("expAdaptLegacyLabel", finetune ? "旧会话无载适配规则继承基础模型；继续训练时不单独更改。" : $("expTrainingMethod").value === "drift_gru" ? "寻峰漂移须有可信九峰源数据及 2 秒无载基线；不以旧无载全谱替代" : "显式适配有可信原始无载谱的旧会话（记录派生 S0 来源）");
    put("expTrainingMethodNote", $("expTrainingMethod").value === "drift_gru" ? "9 路波长漂移（pm）→ 16 帧因果窗口 → 位置与有符号六维力。每轮用开始后的 2 秒无载九峰中位数作为固定参考；失峰、时间间断或缺少可信基线的记录不能训练。寻峰漂移模式仅保存九路时序与标签；历史含原谱备份的数据保持原样。" : spectral ? "512 点真实原始全谱 → 16 帧因果窗口 → 位置与有符号六维力。允许少量已采点；缺少可信无载 S0 的会话不能假装兼容。新模型需要单独安装 CPU ML 依赖。" : "加权最小二乘与小型 MLP 均支持少量已采点，合并兼容 A / B 的合格整轮，每个训练点至少 3 轮。C 批保留独立测试，不兼容记录明确报错。");
    $("expTrainingPause").disabled = !compatible || busy || expired || !sequenceMethod(training.method) || !["running", "pause_requested", "paused"].includes(training.status) || training.pauseReasons.includes("user");
    $("expTrainingResume").disabled = !compatible || busy || expired || !sequenceMethod(training.method) || !["paused", "pause_requested"].includes(training.status) || !training.pauseReasons.includes("user");
    $("expTrainingCancel").disabled = !compatible || busy || expired || !sequenceMethod(training.method) || !training.busy || training.status === "cancelling";
    $("expGoValidation").disabled = busy || !compatible || !trainingView(latest.analysis?.training).complete;
    const selectedSpectral = models.find(model => model.model_id === $("expModelSelect").value)?.model_kind === "spectral_gru" || /^S[A-Za-z0-9_-]+$/.test($("expModelSelect").value);
    const timedGap = running && wrenchProtocol(latest.experiment) && !latest.experiment.trial_id && !["waiting_training", "waiting_contact", "preload", "no_contact", "loading", "release", "unloading", "checking", "zeroing", "saving"].includes(latest.experiment.guided?.step);
    $("expLoadModelBtn").disabled = analysisBusy || running && !(timedGap && selectedSpectral) || hasPendingJudgement() || !$("expModelSelect").value || selectedSpectral && !$("expTestUnloaded").checked;
    $("expTestReferenceBtn").disabled = !compatible || busy || expired || latest.running !== true || running || !latest.analysis?.model_id || latest.analysis?.inference_active !== true || !$("expTestUnloaded").checked;
    $("expTestReferenceBtn").title = latest.analysis?.inference_active === false && latest.analysis?.model_id ? INFERENCE_STOPPED_MESSAGE : "载入模型后，明确确认当前无接触以建立本次参考。";
    $("expInferenceStop").disabled = !inferenceStopAllowed(latest, busy || expired || !receivedAt || Date.now() - receivedAt > 1500, hasPendingJudgement());
    $("expInferenceStop").title = hasPendingJudgement() || latest.validation?.pending_sample ? "请先保存本次判断或点击“重新判断”，再结束推理。" : "结束当前模型推理；设备显示与后台训练继续，恢复须重新载入模型并建立参考。";
    $("expTestUnloaded").disabled = !compatible || busy || expired || running && !(timedGap && selectedSpectral) || latest.running !== true;
    const staleValidation = expired || !receivedAt || Date.now() - receivedAt > 1500;
    const live = validationLiveView(latest.analysis, staleValidation || !compatible || latest.running !== true || running);
    const frozen = frozenSampleView(frozenSample), pending = !!frozen && !frozenSaved && !frozenDiscarded;
    const validationMode = !spectralModel(latest.analysis) || $("expSpectralValidationMode").value === "judge";
    const judge = validationMode && compatible && !busy && !running && pending && frozen.valid && frozen.model === latest.analysis?.model_id && !!judgementTarget;
    const begin = validationMode && compatible && !busy && !running && !pending && !latest.validation?.pending_sample && !!targetPoint && live.comparisonReady;
    $("expValidationSave").disabled = !(judge || begin);
    $("expValidationDiscard").disabled = !compatible || busy || !frozen || frozen.model !== latest.analysis?.model_id;
    const selectedView = pending ? frozen : live, selectedTarget = pending ? judgementTarget : targetPoint;
    $("expPositionCorrect").disabled = !(judge || begin) || !(pending ? frozen.inferenceValid : live.valid) || selectedView.point !== selectedTarget;
    $("expForceCorrect").disabled = !(judge || begin) || !(pending ? frozen.inferenceValid : live.valid) || !selectedForceCorrect(pending ? frozen : { ...selectedView, tolerances: readWrenchTolerances(false) });
    $("expSpectralValidationMode").disabled = busy || pending;
    for (let axis = 0; axis < 6; axis++) $("expWrenchTolerance" + axis).disabled = busy || pending;
    $("expSpectralValidationSettings").hidden = !spectralModel(latest.analysis);
    put("expForceCorrectLabel", spectralModel(latest.analysis) ? "六维力正确：六轴均在各自容差内" : "力正确：绝对差值 ≤ 0.3 N");
    for (const button of $("expValidationMap").querySelectorAll("button")) button.disabled = !compatible || busy || pending;
    $("expValidationFinish").disabled = !compatible || busy || running || pending || !latest.analysis?.model_id || validationResultView(latest.validation).total === 0;
    $("expCompatibilityNotice").hidden = compatible;
    put("expCompatibilityNotice", compatible ? "" : compatibilityMessage(latest));
    if (!compatible) {
      $("expBaselineConfirmed").checked = false;
    }
    const serviceNotice = captureServiceNotice();
    if (serviceNotice) renderCapturePrompt(serviceNotice);
    $("expRefreshSessions").disabled = $("expRefreshModels").disabled = busy;
    const deletedView = $("expShowDeleted").checked, chosen = selected.map(id => historyRows().find(row => row.session_id === id));
    const historyBlocked = busy || historyMutationBlocked() || !!historyPendingAction || !!modelPendingAction;
    $("expSelectTrainable").disabled = busy || !!historyPendingAction || deletedView || !sessions.some(row => historyTrainingStatus(row, $("expTrainingMethod").value).allowed);
    $("expClearSessions").disabled = busy || !!historyPendingAction || !selected.length;
    $("expDeleteSessions").hidden = deletedView; $("expRestoreSessions").hidden = !deletedView;
    $("expDeleteSessions").disabled = historyBlocked || deletedView || !selected.length || chosen.some(row => row?.deletable !== true);
    $("expRestoreSessions").disabled = historyBlocked || !deletedView || !selected.length || chosen.some(row => row?.restorable !== true);
    $("expShowDeleted").disabled = busy || !!historyPendingAction;
    $("expRefreshSessions").disabled = busy || !!historyPendingAction;
    const invalidTraining = chosen.filter(row => !historyTrainingStatus(row, $("expTrainingMethod").value).allowed);
    const roundSummary = selectedRoundSummary(historyRows(), selected), rounds = value => value === null ? "待核实" : value + " 轮";
    put("expTrainingRoundSummary", "勾选数据：" + roundSummary.sessions + " 个会话 · 合格共 " + rounds(roundSummary.accepted) + "（按压 " + rounds(roundSummary.press) + " / 无接触 " + rounds(roundSummary.noContact) + "） · 总试次 " + rounds(roundSummary.attempts) + " · 未通过 " + rounds(roundSummary.rejected) + " · 未完成 " + rounds(roundSummary.pending) + (finetune ? " · 仅统计勾选来源，基础模型历史来源不重复累加。" : ""));
    put("expHistorySelectionState", "已选择 " + selected.length + " 条记录" + (deletedView ? " · 回收区记录不能训练，恢复后再选择。" : invalidTraining.length ? " · 包含 " + invalidTraining.length + " 条不可训练或已缺失来源；训练已停用，请取消这些项或清空后重选。" + (sequenceMethod($("expTrainingMethod").value) ? " 原因：" + invalidTraining.map(row => (row?.session_id || "已缺失来源") + "：" + historyTrainingStatus(row, $("expTrainingMethod").value).reason).join("；") : "") : finetune ? " · 仅勾选新增会话；基础模型历史来源由服务端自动带入并去重。" : spectral ? cumulativeSpectralSelection ? $("expTrainingMethod").value === "drift_gru" ? " · 默认累计可用于寻峰漂移训练的已保存 A / B 来源；缺少 2 秒无载基线的旧记录不加入。跨样件等不兼容组合由服务端明确拒绝。" : " · 默认累计已保存 A / B 来源；刷新加入新数据，跨样件等不兼容组合由服务端明确拒绝。" : " · 使用手动选择的子集；刷新保留当前选择，重新勾选默认累计可加入全部来源。" : " · 可多选兼容的 A / B 数据一并训练。"));
    for (const input of $("expSessionList").querySelectorAll("input[data-session-id]")) input.disabled = busy || !!historyPendingAction;
    $("expHistoryConfirm").disabled = busy || historyMutationBlocked() || !historyPendingAction;
    $("expHistoryCancel").disabled = busy;
    const modelDeleted = $("expShowDeletedModels").checked, chosenModels = Array.from(selectedModels).map(id => modelRows().find(row => row.model_id === id));
    const modelBlocked = busy || modelMutationBlocked() || !!modelPendingAction || !!historyPendingAction;
    $("expSelectModels").disabled = modelBlocked || !modelRows().some(row => !modelRowBlockReason(row));
    $("expClearModels").disabled = busy || !!modelPendingAction || !selectedModels.size;
    $("expDeleteModels").hidden = modelDeleted; $("expRestoreModels").hidden = !modelDeleted;
    $("expDeleteModels").disabled = modelBlocked || modelDeleted || !selectedModels.size || chosenModels.some(row => modelRowBlockReason(row));
    $("expRestoreModels").disabled = modelBlocked || !modelDeleted || !selectedModels.size || chosenModels.some(row => modelRowBlockReason(row));
    $("expShowDeletedModels").disabled = busy || !!modelPendingAction;
    $("expRefreshModels").disabled = busy || !!modelPendingAction;
    for (const input of $("expModelList").querySelectorAll("input[data-model-id]")) input.disabled = modelBlocked || !!modelRowBlockReason(modelRows().find(row => row.model_id === input.dataset.modelId));
    put("expModelSelectionState", (modelDeleted ? "回收区 " + deletedModels.length : "已保存 " + models.length) + " 个模型 · 已选择 " + selectedModels.size + " 个");
    put("expModelManagementState", modelMutationBlocked() ? "服务状态未就绪、采集/训练/保存进行中或有待保存判断；暂不能删除或恢复模型。" : "删除可恢复；恢复仅加入模型候选列表，不会启动推理。");
    $("expModelConfirm").disabled = busy || modelMutationBlocked() || !modelPendingAction || modelPendingAction.ids.some(id => modelRowBlockReason(modelRows().find(row => row.model_id === id), modelPendingAction.deleted));
    $("expModelCancel").disabled = busy;
    if (modelPendingAction) $("expLoadModelBtn").disabled = $("expInferenceStop").disabled = $("expTestReferenceBtn").disabled = true;
    if (expired) {
      // Stop remains an explicit operator action while stale stage changes are blocked.
      $("expStopBtn").disabled = busy || (!running && !legacyRawRecording(latest));
    }
  }
  function renderLiveForce(force, elapsed = 0) {
    const timed = captureUsesWrenchProtocol(latest.experiment), targetMode = captureUsesTargetProtocol(latest.experiment), guided = latest.experiment?.guided || {};
    const target = latest.experiment?.running ? guided.target_force_norm_N ?? latest.experiment.settings?.target_force_norm_N ?? 4 : Number($("expTargetForceNorm").value) || 4;
    const limit = latest.experiment?.running ? latest.experiment.settings?.max_force_norm_N : Number($("expMaxForceNorm").value) || 5;
    const view = liveForceView(timed ? { ...force, force_norm_N: force?.force_norm_N ?? guided.force_norm_N, max_force_norm_N: limit, observed_peak_force_norm_N: guided.observed_peak_force_norm_N } : force, elapsed, timed);
    $("expLiveForceCard").className = "experiment-live-force" + (view.outside ? " over-range" : "");
    put("expLiveForceValue", number(view.force));
    put("expLiveForceLabel", timed ? "PX6D 当前三维合力 / N" : "PX6D 当前按压力 / N");
    put("expLiveForceRaw", timed ? "本轮观测峰值：" + number(view.peak) + " N" + (targetMode ? " · 目标：" + number(target) + " N" : "") + " · 上限：" + number(view.limit) + " N" : "原始 Fz：" + number(view.raw) + " N · 本试次零点：" + number(view.zero) + " N");
    put("expLiveForceAge", (view.age !== null ? "距接收 " + number(view.age, 0) + " ms · " : "") + "最新六轴 · 非光谱配对");
    put("expLiveForceReason", timed ? view.outside ? "观测合力超限，原始六轴与标签保留真实值，本轮不计合格次数。" : targetMode && guided.target_reached ? experimentGates(latest, expired).finish ? "系统已确认可结束本轮，请卸载后点击“结束本轮”；仍须检查真实无载与保存。" : "已达到合力目标，仍在连续记录；等待服务端按压数据检查通过后，结束按钮才会点亮。" : view.valid ? "sqrt(Fx²＋Fy²＋Fz²)，扣除有效软件零偏；力矩不计入范数。采样间隙物理峰值未知。" : view.reason : view.outside ? "已超过 3 N 暂定范围，保留真实数值；请结束加载并记录该情况。" : view.valid ? "来自本次服务端无载基线，与上方浏览器显示置零独立；不是模型预测。" : view.reason);
  }
  function renderPointMap(layout) {
    const guided = guidedView(latest.experiment), key = JSON.stringify([layout, guided.acceptedByPoint, guided.repetitions]);
    if (key !== pointLayoutKey) {
      $("expPointMap").innerHTML = layout.visual.map(point => {
        const accepted = Number.isInteger(guided.acceptedByPoint[point.id]) ? guided.acceptedByPoint[point.id] : 0;
        const completed = guided.enabled && accepted >= guided.repetitions && guided.repetitions > 0;
        return '<button class="point-cell' + (layout.current && point.id === "P01" ? " point-origin" : "") + (completed ? " point-complete" : "") + '" data-point-id="' + escapeHTML(point.id) + '" aria-pressed="' + (point.id === activePoint) + '" disabled>' + escapeHTML(point.id) + '<small>' + (layout.known ? '行 ' + point.row + ' · 列 ' + point.col : '历史方向未核实') + '</small>' + (guided.enabled ? '<small class="point-count">' + accepted + '/' + guided.repetitions + (completed ? ' · 完成' : '') + '</small>' : '') + '</button>';
      }).join("");
      pointLayoutKey = key;
    }
    put("expXAxisDirection", layout.current ? "+x / +u → 右" : "历史坐标定义 · 原始编号");
    put("expYAxisDirection", layout.current ? "+y / +v ↑ 指尖" : layout.known ? "按已存网格显示" : "方向未记录");
    put("expPointMapDefinition", layout.current ? "原点左下 P01（第 0 行、第 0 列），顶部为行 4" + (captureUsesWrenchProtocol(latest.experiment) ? "；六轴方向与原点保持 PX6D 定义。" : "；指腹朝上对应 −PX6D Z。") : layout.known ? "历史会话按其已存点阵保留行列与顺序，不套用新原点。" : "历史点阵方向未核实；只保留原始编号，不推定坐标。");
  }
  function renderPointSelection(layout) {
    const point = activePoint === NO_CONTACT ? { id: NO_CONTACT } : layout.points.find(point => point.id === activePoint) || layout.points[0];
    activePoint = point.id;
    for (const button of $("expPointMap").querySelectorAll("button")) button.setAttribute("aria-pressed", String(button.dataset.pointId === activePoint));
    $("expNoContact").setAttribute("aria-pressed", String(activePoint === NO_CONTACT));
    put("expSelectedPoint", activePoint === NO_CONTACT ? "无接触" : point.id + (layout.known ? " · " + (layout.current ? "" : "历史 ") + "行 " + point.row + " / 列 " + point.col : " · 历史坐标方向未核实"));
    put("expSelectedCoordinates", activePoint === NO_CONTACT ? "独立采集类别，无空间坐标；本轮全过程保持无接触。" : layout.known ? (layout.current ? "x/u" : "已存 u") + " = " + number(point.u, 2) + "，" + (layout.current ? "y/v" : "已存 v") + " = " + number(point.v, 2) : "保留原始点编号，坐标待核实。");
    put("expConfirmPointBtn", activePoint === NO_CONTACT ? "确认采集‘无接触’" : "确认采集点位");
    const supported = captureUsesTargetProtocol(latest.experiment), guided = guidedView(latest.experiment);
    put("expNoContactCount", guided.enabled ? "合格 " + guided.noContactAccepted + " 次 · 尝试 " + guided.noContactAttempts + " 次 · 独立累计" : "独立采集类别 · 不属于 25 点坐标");
    put("expNoContactNote", supported ? "无接触次数独立累计，不受每点重复次数限制，不计入 25 点完成进度；本轮保存后可继续无接触，或选择并确认其他点位。" : "原有引导协议不支持无接触标签；请选择目标合力连续采集协议。");
    const confirmed = latest.experiment?.point_id;
    put("expConfirmedPoint", confirmed ? (confirmed === NO_CONTACT ? "服务端已确认采集类别：" : "服务端已确认采集点：") + selectionLabel(confirmed) + (confirmed !== activePoint ? "；当前预选尚未确认" : "") : "当前仅预选；尚未确认采集点位");
  }
  function renderValidationMap(analysis, stale = false) {
    const view = validationLiveView(analysis, stale), trained = new Set(view.trained);
    const layout = pointLayout(analysis?.model_definition, true);
    const key = JSON.stringify([view.trained, view.point, targetPoint, layout.current, layout.known, layout.visual.map(point => [point.id, point.row, point.col])]);
    if (key !== validationMapKey) {
      $("expValidationMap").innerHTML = layout.visual.map(point => '<button type="button" class="validation-point ' + (trained.has(point.id) ? 'trained' : 'untrained') + (view.point === point.id ? ' is-predicted' : '') + (targetPoint === point.id ? ' is-target' : '') + '" data-prediction-point="' + point.id + '" data-target-point="' + point.id + '" aria-pressed="' + (targetPoint === point.id) + '" aria-label="' + point.id + (layout.known ? '，' + (layout.current ? '' : '已存') + '行 ' + point.row + ' 列 ' + point.col : '，历史几何未核实') + '，' + (targetPoint === point.id ? '选定目标，' : '') + (view.point === point.id ? '当前模型有效识别位置' : trained.has(point.id) ? '已训练' : '未训练') + '"><span>' + point.id + '</span><small>' + (targetPoint === point.id ? '目标 · ' : '') + (view.point === point.id ? '有效识别' : trained.has(point.id) ? '已训练' : '未训练') + '</small></button>').join("");
      validationMapKey = key;
    }
    put("expValidationAxes", !analysis?.model_id ? "待载入模型并核对其点阵定义；当前编号排列不声明空间方向。" : layout.current ? "y / v ↑ 指尖 · 原点左下 P01 · x / u → 右 · 指腹朝上 = −PX6D Z" : layout.known ? "历史模型：编号和行列按模型保存顺序显示；空间轴方向未核实，不套用当前左下原点或安装方向。" : "模型点阵几何和空间轴方向未核实；仅按原始 P01～P25 编号选择目标，网格排列不表示实际空间位置。");
    put("expTrainedPoints", view.trained.length ? view.trained.join(" / ") + (analysis.model_kind === "spectral_gru" ? driftModel(analysis) ? " · 寻峰漂移六维力研究模型" : " · 全谱六维力研究模型" : analysis.model_kind === "pilot" ? " · 少量点研究模型" : " · 完整模型") : "待加载模型点位定义");
    put("expTargetComparison", targetComparison(targetPoint, view));
    put("expValidationMapState", stale ? "实时状态已失效，识别灯已熄灭；本次已固定的判断读数保留。" : "橙色圈是选定目标，绿色灯仅为模型识别，目标不输入模型。" + (targetPoint && !trained.has(targetPoint) ? " 当前目标未训练，可观察并记录识别失败。" : ""));
    renderLivePrediction(view);
    return view;
  }
  function renderLivePrediction(live) {
    const prediction = targetPredictionView(live, targetPoint);
    put("expPredictionState", prediction.status); put("expPredictionPoint", prediction.point || "—"); put("expPredictionForce", number(prediction.force)); put("expPredictionTruth", number(prediction.truth)); put("expPredictionError", number(prediction.error));
    put("expPredictionConfidence", prediction.confidence == null ? "—" : number(prediction.confidence, 3)); put("expPredictionReason", prediction.reason);
    put("expPredictionSelectionState", prediction.selectionNote);
    $("expScalarPrediction").hidden = !!prediction.spectral;
    $("expWrenchPrediction").hidden = !prediction.spectral;
    if (prediction.spectral) $("expWrenchPrediction").innerHTML = '<div class="wrench-summary"><span>识别位置 <strong>' + escapeHTML(prediction.point || '—') + '</strong></span><span>预测三维合力 <strong>' + number(prediction.forceNorm) + '</strong> N</span><span>PX6D 三维合力 <strong>' + number(prediction.wrenchTruth ? Math.hypot(...prediction.wrenchTruth.slice(0, 3)) : null) + '</strong> N</span>' + (prediction.contactMethod === 'binary_contact_head_v1' ? '<span>接触概率 <strong>' + (prediction.contactProbability === null ? '—' : number(prediction.contactProbability * 100, 1) + '%') + '</strong></span>' : '') + '</div>' + wrenchTable(prediction.wrenchPred, prediction.wrenchTruth, prediction.wrenchError) + '<p class="display-note">六轴原点与方向保持 PX6D 定义；PX6D 三维合力 = √(Fx²＋Fy²＋Fz²)，与下表对照同源；力矩不计入合力，六轴表保留正负号。' + (prediction.contactMethod === 'binary_contact_head_v1' ? '接触判断来自模型学习的接触概率，无接触时不显示位置灯。' : prediction.contactMethod === 'legacy_predicted_force_hysteresis_v1' ? '当前为旧版模型，接触判断沿用预测合力门槛；重新训练后可学习无接触类别。' : '定位灯依服务端光谱推理的有效位置与接触判断显示。') + '</p>';
  }
  function adoptFrozenSample(sample) {
    const view = frozenSampleView(sample);
    if (!view || frozenSample?.sample_id === view.id) return;
    frozenSample = JSON.parse(JSON.stringify(sample)); frozenSaved = view.saved; frozenDiscarded = false;
    const recordedTarget = sample.target_point_id || sample.actual_point_id;
    judgementTarget = /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(recordedTarget || "") ? recordedTarget : null;
    targetPoint = judgementTarget;
    $("expPositionCorrect").checked = false; $("expForceCorrect").checked = false;
  }
  function renderFrozenSample() {
    const view = frozenSampleView(frozenSample);
    put("expFrozenStatus", !view ? "尚未开始本次判断；选择目标并实际按压即可观察。" : frozenSaved ? "本次判断已保存；再次真实按压后可开始下一次判断。" : !judgementTarget ? "之前未完成的判断没有目标记录，请点击“重新判断”后选择目标，不猜测旧目标。" : !view.valid ? "本次数据无效，不能计入成绩：" + view.reason : !view.inferenceValid ? "本次未输出有效预测，可将两项均判为不正确并保存失败。" + view.reason : "本次判断读数已固定，目标暂不能切换；请保存判断，或点击“重新判断”。");
    $("expFrozenStatus").className = "experiment-statusline" + (view && (!view.valid || !view.inferenceValid) && !frozenSaved ? " error" : frozenSaved ? " success" : "");
    put("expJudgementTarget", view ? judgementTarget || "未记录" : targetPoint || "—");
    put("expFrozenPoint", view?.point || "—"); put("expFrozenForce", number(view?.force)); put("expFrozenTruth", number(view?.truth)); put("expFrozenError", number(view?.error));
    $("expFrozenScalarSummary").hidden = !!view?.spectral || (!view && spectralModel(latest.analysis));
    $("expWrenchFrozen").hidden = !view?.spectral;
    if (view?.spectral) $("expWrenchFrozen").innerHTML = '<p class="display-note">冻结目标 ' + escapeHTML(judgementTarget || "未记录") + ' · 冻结识别 ' + escapeHTML(view.point || "—") + '</p>' + wrenchTable(view.wrenchPred, view.wrenchTruth, view.wrenchError);
    put("expJudgementNote", "保存时未勾选就表示不正确；两项都不勾也可保存失败按压。" + (view?.spectral || spectralModel(latest.analysis) ? " 六维力须为六轴分别配置容差并逐轴通过，容差与固定读数随本次记录保存。" : view && finite(view.error) && !forceWithinTolerance(view.error) ? " 本次绝对力差超过 0.3 N，不能勾选“力正确”。" : " 力正确须手动核对绝对差值 ≤ 0.3 N。") + "已固定读数不会随新帧改变；点击“重新判断”会放弃当前未保存判断，不计入分母。");
  }
  function hasPendingJudgement() { return !!frozenSample && !frozenSaved && !frozenDiscarded; }
  function liveValidationStale() { return expired || !receivedAt || Date.now() - receivedAt > 1500 || !backendCompatible(latest) || latest.running !== true || latest.experiment?.running === true; }
  function clearJudgement() {
    frozenSample = null; frozenSaved = false; frozenDiscarded = false; judgementTarget = null;
    $("expPositionCorrect").checked = false; $("expForceCorrect").checked = false;
  }
  async function captureJudgement() {
    if (!backendCompatible(latest)) throw new Error(compatibilityMessage(latest));
    if (!targetPoint) throw new Error("请先点击要识别的目标点位，再实际按压。");
    if (hasPendingJudgement() || latest.validation?.pending_sample) throw new Error("请先保存本次判断，或点击“重新判断”。");
    const live = validationLiveView(latest.analysis, liveValidationStale());
    if (live.spectral && $("expSpectralValidationMode").value !== "judge") throw new Error("请明确切换为人工六轴验证模式，再保存判断。");
    if (!live.comparisonReady) throw new Error(live.spectral ? "当前没有可核验的时序输入窗口与六维 PX6D 真值；请检查本次参考、同步与实际接触状态。纯光谱观察不能保存为真值核验。" : "当前没有可核验的九峰与 PX6D 同步按压；请核对本次参考与设备状态，实际按压力须大于 0.05 N。识别失败但数据有效时可保存为失败判断。");
    const selectedTarget = targetPoint;
    const result = await request("/api/validation/freeze", { actual_point_id: selectedTarget, ...(live.spectral ? { wrench_tolerances: readWrenchTolerances() } : {}) });
    const sample = result.sample || result.validation?.pending_sample;
    if (!frozenSampleView(sample)?.valid) throw new Error("服务端未返回有效的本次判断编号，未建立待判断读数。");
    if (result.validation) latest = { ...latest, validation: result.validation };
    adoptFrozenSample(sample); renderFrozenSample(); renderValidationStatistics(latest.validation);
    if (judgementTarget !== selectedTarget) throw new Error("本次目标记录不一致，请点击“重新判断”，不猜测或改写已记录目标。");
  }
  function applyJudgementChecks(position, force) {
    if (!backendCompatible(latest)) throw new Error(compatibilityMessage(latest));
    const sample = frozenSampleView(frozenSample);
    if (!sample?.valid || !hasPendingJudgement() || sample.model !== latest.analysis?.model_id || latest.experiment?.running) throw new Error("当前没有本模型可保存的有效判断读数。");
    if (!judgementTarget) throw new Error("本次判断没有目标记录，请点击“重新判断”后重新选择目标。");
    if ((position || force) && !sample.inferenceValid) {
      $("expPositionCorrect").checked = false; $("expForceCorrect").checked = false;
      throw new Error("本次未输出有效预测，只能将两项均判为不正确并保存失败判断。");
    }
    if (position && judgementTarget !== sample.point) {
      $("expPositionCorrect").checked = false;
      throw new Error("本次目标与识别点不同，不能保存为“位置识别正确”。");
    }
    if (force && !selectedForceCorrect(sample)) {
      $("expForceCorrect").checked = false;
      throw new Error(sample.spectral ? "六轴容差尚未全部配置或存在超差分量，不能保存为“六维力正确”。" : "本次绝对力差超过 0.3 N，不能保存为“力正确”。");
    }
    $("expPositionCorrect").checked = position; $("expForceCorrect").checked = force;
    return sample;
  }
  function renderValidationStatistics(validation) {
    const stats = validationResultView(validation), pct = value => value === null ? "—" : number(value * 100, 1) + "%";
    put("expValidationStatsState", stats.total ? (validation?.finished ? "本轮判断报告已保存：" : "本轮已保存 ") + stats.total + " 次有效实机按压；位置与力分别统计。" : "尚无已保存判断：本轮没有已保存判断，正确率为空。");
    const spectral = spectralModel(latest.analysis), raw = validation?.session_stats || validation?.current_stats || validation?.stats || {};
    const cards = [["位置正确 / 已判定", stats.position + " / " + stats.total + " · " + pct(stats.positionRate)], [spectral ? "六维力正确 / 已判定" : "力正确 / 已判定", stats.force + " / " + stats.total + " · " + pct(stats.forceRate)], ...(spectral ? [] : [["按压力 MAE / N", number(stats.mae)], ["按压力 RMSE / N", number(stats.rmse)]]), ["模型拒识 / 已判定", stats.rejected + " / " + stats.total], ["误差有效样本", spectral ? raw.wrench_error_count ?? "—" : stats.errors ?? "—"], ["未判定（不入分母）", stats.pending], ["无效（不入分母）", stats.invalid]];
    $("expValidationStats").innerHTML = cards.map(([label, value]) => '<div class="experiment-report-card">' + escapeHTML(label) + '<strong>' + escapeHTML(value) + '</strong></div>').join("") + (spectral && stats.total ? '<div class="wrench-statistics">' + wrenchTable(wrenchVector(raw.wrench_mae), wrenchVector(raw.wrench_rmse), null, ["MAE", "RMSE", ""]) + '</div>' : "");
    const saved = validation?.latest_report || (validationResult?.model_id === latest.analysis?.model_id ? validationResult : null);
    put("expValidationReportState", saved ? (validation?.finished ? "本轮报告已保存" : "上次完成的验证报告") + " · " + sessionTimeText(saved.created_utc) + "（北京时间） · " + (saved.report_id || "") + (saved.path ? " · " + saved.path : "") + (validation?.finished ? "；下一次实际判断将建立新轮次。" : "；上方为当前轮次统计。") : "保存本轮真实按压判断报告；未完成判断请先保存或重新判断。力误差只统计有限的有效预测；拒识仍计入位置与力正确率分母。");
    $("expValidationFinish").textContent = validation?.finished ? "查看已保存结果" : "保存本轮判断报告";
  }
  function bindPreferences() {
    const preferences = global.NineFBGPreferences;
    if (!preferences?.bind) return;
    const confirmations = ["expDirectionConfirmed", "expBaselineConfirmed", "expTestUnloaded"];
    for (const id of confirmations) $(id).checked = false;
    preferences.bind("experiment", {
      finger_id: { id: "expFingerID", type: "string" },
      force_sign: { id: "expForceSign", type: "number" },
      temperature_C: { id: "expTemperature", type: "number" },
      repetitions_per_point: { id: "expRepetitions", type: "number" }, capture_mode: { id: "expCaptureMode", type: "string" }, acquisition_protocol: { id: "expAcquisitionProtocol", type: "string" }, max_force_norm_N: { id: "expMaxForceNorm", type: "number" }, target_force_norm_N: { id: "expTargetForceNorm", type: "number" }
    }, saved => {
      for (const id of confirmations) $(id).checked = false;
      if (!["full_spectrum", "peak_drift"].includes($("expCaptureMode").value)) $("expCaptureMode").value = "full_spectrum";
      renderProtocolSelection(latest.experiment);
      if (!latest.experiment?.running && !protocolTouched && saved.acquisition_protocol === "timed_wrench_v1") migrateTimedSelection();
      if (!latest.experiment?.running && !pendingPointTouched && (/^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(saved.selected_point_id || "") || saved.selected_point_id === NO_CONTACT && captureUsesTargetProtocol(latest.experiment))) activePoint = saved.selected_point_id;
      renderPointSelection(pointLayout(latest.experiment?.settings, !!latest.experiment?.session_id));
      put("expDirectionDescription", "请按当前安装核对法向方向：按压力 F = " + ($("expForceSign").value === "-1" ? "−" : "+") + "(Fz − Fz₀)；指腹朝上对应 −PX6D Z");
      renderGuided(latest.experiment || {}); renderLiveForce(latest.experiment?.live_force); renderPointMap(pointLayout(latest.experiment?.settings, !!latest.experiment?.session_id)); updateControls();
    }).catch(error => message("恢复采集设置失败：" + error.message, "error"));
    preferences.bind("analysis", { method: { id: "expTrainingMethod", type: "string" }, adapt_legacy_unloaded: { id: "expAdaptLegacyUnloaded", type: "checkbox" }, ...Object.fromEntries(Object.entries(TRAINING_PARAMETER_FIELDS).map(([key, id]) => [key, { id, type: "number" }])) }, saved => { if (!["linear", "mlp", "spectral_gru", "drift_gru"].includes($("expTrainingMethod").value)) $("expTrainingMethod").value = "spectral_gru"; applyCumulativeSpectralSelection(); renderSessions(); renderBaseModels(); })
      .catch(error => message("恢复训练设置失败：" + error.message, "error"));
  }
  function captureServiceNotice() {
    if (expired && receivedAt) return { text: "状态已过期，暂停本轮操作；等待服务端恢复后核对采集状态。", tone: "retry", urgent: true, stale: true };
    if (!backendCompatible(latest)) return { text: compatibilityMessage(latest), tone: "retry", urgent: true };
    return null;
  }
  function renderCapturePrompt(notice) {
    const experiment = latest.experiment || {}, now = Date.now(), session = experiment.session_id || null;
    if (capturePromptSession !== session) {
      capturePromptSession = session; capturePromptKey = ""; capturePromptHistory.length = 0; captureDetailAt = null;
    }
    const context = [session, experiment.trial_id, experiment.point_id, experiment.guided?.step, notice.text, notice.tone].join("|");
    const changed = context !== capturePromptKey;
    if ($("expGuidedMessage").textContent !== notice.text) put("expGuidedMessage", notice.text);
    $("expGuidedMessage").className = "guided-instruction" + (notice.tone ? " " + notice.tone : "");
    if (notice.stale) put("expTargetForceState", "状态已过期，结束本轮已停用；等待服务端恢复后核对当前阶段。");
    const detail = notice.detail && notice.detail !== notice.text ? notice.detail : "";
    if (changed || notice.urgent || captureDetailAt === null || now - captureDetailAt >= CAPTURE_DETAIL_INTERVAL_MS) {
      captureDetail = detail; captureDetailAt = now;
      if ($("expGuidedDiagnosticText").textContent !== detail) put("expGuidedDiagnosticText", detail);
      $("expGuidedDiagnostic").hidden = !detail;
    }
    if (changed) {
      capturePromptKey = context;
      const point = experiment.point_id ? experiment.point_id + " · " : "";
      capturePromptHistory.unshift({ text: point + notice.text, detail: captureDetail, tone: notice.tone,
        time: new Date(now).toLocaleTimeString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }) });
      capturePromptHistory.length = Math.min(capturePromptHistory.length, 8);
    } else if (capturePromptHistory.length) capturePromptHistory[0].detail = captureDetail;
    const historyHTML = capturePromptHistory.map(item => '<li' + (item.tone ? ' class="' + item.tone + '"' : '') + '><time>' + escapeHTML(item.time) + '</time><span>' + escapeHTML(item.text) + (item.detail ? '<small>' + escapeHTML(item.detail) + '</small>' : '') + '</span></li>').join("");
    if ($("expGuidedHistory").innerHTML !== historyHTML) $("expGuidedHistory").innerHTML = historyHTML;
  }
  function renderGuided(experiment) {
    const guided = guidedView(experiment), preload = preloadView(experiment), timed = captureUsesWrenchProtocol(experiment), targetMode = captureUsesTargetProtocol(experiment);
    const noContact = noContactSelection(experiment);
    if (preload.timedOut && !previousPreloadTimedOut) $("expBaselineConfirmed").checked = false;
    previousPreloadTimedOut = preload.timedOut;
    renderCapturePrompt(captureServiceNotice() || captureInstruction(experiment));
    put("expRoundLabel", guided.enabled && experiment.point_id ? selectionLabel(experiment.point_id) + " · 第 " + guided.round + (noContact ? " 轮" : " / " + guided.repetitions + " 轮") : "当前轮次：待开始");
    put("expAttemptLabel", (noContact ? "无接触尝试 " + guided.noContactAttempts : "当前点尝试 " + guided.pointAttempts) + " 次 · 已通过 " + (noContact ? guided.noContactAccepted : guided.pointAccepted) + " 次");
    put("expTotalProgressText", guided.totalAccepted + " / " + guided.totalRequired + " · " + number(guided.totalPercent, 0) + "%");
    put("expPointProgressLabel", noContact ? "无接触 · 独立累计（不计入 25 点）" : (selectionLabel(experiment.point_id) || "当前采集点") + " · 合格轮次进度");
    put("expPointProgressText", noContact ? "合格 " + guided.noContactAccepted + " 次 · 尝试 " + guided.noContactAttempts + " 次" : guided.pointAccepted + " / " + guided.repetitions + " · " + number(guided.pointPercent, 0) + "%");
    $("expPointProgress").hidden = noContact;
    const elapsed = finite(experiment?.guided?.elapsed_s) ? experiment.guided.elapsed_s : null, duration = finite(experiment?.guided?.duration_s) ? experiment.guided.duration_s : 3;
    const target = experiment?.running ? experiment.guided?.target_force_norm_N ?? experiment.settings?.target_force_norm_N ?? 4 : Number($("expTargetForceNorm").value) || 4;
    const reached = guided.enabled && experiment.guided?.target_reached === true;
    const currentForce = finite(experiment?.guided?.force_norm_N) ? experiment.guided.force_norm_N : null;
    const trialProgress = noContact ? experiment.guided?.can_finish === true ? 100 : 0 : targetMode ? reached ? 100 : currentForce !== null ? Math.min(100, Math.max(0, currentForce / target * 100)) : 0 : timed && elapsed !== null ? Math.min(100, Math.max(0, elapsed / duration * 100)) : guided.forcePercent;
    put("expTrialProgressLabel", targetMode ? "合力目标 · 达标提示后仍连续记录" : timed ? "本轮主按压时间 · 服务端源帧" : "本轮加载到 3 N");
    put("expForceProgressText", targetMode ? (reached ? "已达目标，请卸载" : "目标 " + number(target, 2) + " N") + (elapsed !== null ? " · 已录 " + number(elapsed, 2) + " s" : " · 手动结束本轮") : timed ? (elapsed === null ? "等待主按压" : number(elapsed, 2) + " / " + number(duration, 1) + " s · " + number(trialProgress, 0) + "%") + (experiment?.guided?.awaiting_training_pause ? " · 等待训练暂停确认" : "") : number(guided.forcePercent, 0) + "% · 目标 3 N");
    if (noContact) {
      put("expTrialProgressLabel", "无接触数据检查 · 服务端确认后手动结束");
      put("expForceProgressText", (experiment.guided?.can_finish === true ? "数据检查已通过" : "等待有效无接触数据") + (elapsed !== null ? " · 已录 " + number(elapsed, 2) + " s" : ""));
    }
    const finishAllowed = experimentGates(latest, expired).finish;
    const finishReason = experiment.guided?.finish_block_reason || "等待达到配置目标并通过服务端按压数据检查。";
    put("expTargetForceState", expired ? "状态已过期，结束本轮已停用；等待服务端恢复后核对当前阶段。" : experiment.guided?.awaiting_training_pause ? "正在等待后台训练 paused 确认；确认后先连续记录 2 秒无载基线，提示后再按压。" : experiment.phase === "saving" ? "本轮已结束，正在等待保存确认；选点和训练保护仍保持。" : experiment.guided?.finish_requested || experiment.guided?.step === "zeroing" ? "已确认卸载，正在直接建立下一轮软件零点与" + captureReference(experiment) + "；请保持无接触，保存完成后继续下一轮。" : finishAllowed ? "系统已确认可结束本轮，请先移开压力棒再点击“结束本轮”；点击即确认已卸载，直接软件回零并保存。" : experiment.phase === "preload" || experiment.guided?.press_prompt_ready === false && guided.step === "waiting_contact" ? "当前不能结束本轮：先保持无接触，正在连续记录 2 秒基线；提示后再按压。本模式光学数据与六轴持续保存，界面计时不会放行按压或结束本轮。" : (reached ? "已达合力目标，当前不能结束本轮：" : "当前不能结束本轮：") + finishReason + " 手动结束前持续保存全过程。基线记录完成后可找点移动；找点误触或采集异常须中止并重新采集。");
    if (noContact && !expired) put("expTargetForceState", experiment.guided?.awaiting_training_pause ? "正在等待后台训练暂停确认；本轮全过程保持无接触。" : experiment.phase === "saving" ? "本轮无接触数据已结束，正在等待保存确认。" : experiment.guided?.finish_requested || experiment.guided?.step === "zeroing" ? "正在保存无接触数据并建立下一轮参考，请保持无接触。" : finishAllowed ? "系统已确认可结束本轮；保持无接触并点击“结束本轮”保存。" : "无接触轮次不需要达到合力目标；保持无接触，等待真实数据通过检查。" + (experiment.guided?.finish_block_reason ? " 当前不能结束：" + experiment.guided.finish_block_reason : ""));
    for (const [id, value] of [["expTotalProgress", guided.totalPercent], ["expPointProgress", guided.pointPercent], ["expForceProgress", trialProgress]]) { $(id).value = value; $(id).setAttribute("aria-valuenow", String(value)); }
    $("expGuidedFlow").innerHTML = targetMode ? captureUsesDriftMode(experiment) ? '<li>确认无载并建立寻峰漂移参考与六轴软件零偏</li><li>确认点位并开始；先保持无接触连续记录 2 秒基线，九路峰心中位数作为本轮固定参考</li><li>看到开始按压提示后找点加载；连续保存九路漂移及六轴标签，不保存 512 点原谱</li><li>系统确认可结束后，先移开压力棒再点击结束本轮；直接软件回零，保存后继续</li>' : '<li>确认无载并建立真实 512 点 S0 与软件零偏</li><li>确认点位并开始；等待训练暂停确认后，先保持无接触连续记录 2 秒基线</li><li>看到开始按压提示后找点加载；合力达到目标后卸载，记录不停</li><li>系统确认可结束后，先移开压力棒再点击结束本轮；点击即确认已卸载，直接建立下一轮软件零点与 S0，保存后继续</li>' : timed ? '<li>确认无载并建立 512 点参考与软件零偏</li><li>确认点位，等待训练暂停及约 1 s 采前记录</li><li>保持接触点，主按压记录 3 s，可改变方向</li><li>按提示松开，等待真实无载和光谱恢复自动检查</li>' : '<li>确认无载并手动标定本轮</li><li>点击开始，保持无接触等待提示加载</li><li>缓慢加载至 3 N，按提示卸载</li><li>移开接触棒，等待判定；每轮重新标定</li>';
    if (legacyDriftCapture(experiment)) $("expGuidedFlow").innerHTML = $("expGuidedFlow").innerHTML.replace("不保存 512 点原谱", "旧会话同时保留 512 点原谱备份");
    if (noContact) $("expGuidedFlow").innerHTML = '<li>选择并确认“无接触”，确认无载并标定本轮</li><li>点击开始，保持无接触连续记录 2 秒基线</li><li>继续保持无接触，保存真实光学数据与六轴标签</li><li>服务端确认数据足够后点击结束本轮；保存后可继续无接触或选择点位</li>';
    put("expPreloadCriteriaNote", targetMode && captureUsesDriftMode(experiment) ? "真实九峰与六轴连续记录 2 秒无载基线后才提示按压；本轮寻峰漂移以这段峰心中位数为固定参考。达到目标并通过按压数据检查后允许结束，移开压力棒后点击即直接软件回零。寻峰漂移模式仅保存九路时序与标签；历史含原谱备份的数据保持原样。" : targetMode ? "真实源帧连续记录 2 秒无载基线后才提示按压；达到目标并通过按压数据检查后允许结束。先移开压力棒，点击结束即确认已卸载，用新的真实六轴与光谱数据直接建立下一轮零点和 S0，保存后生效。" : timed ? "全谱原始帧与六轴标签独立核验；3 s 结束后继续保存卸载和恢复，界面计时不会推进阶段。" : "连续时长仅由服务端根据真实九峰与六轴同步结果累计；界面计时不会放行加载。");
    if (noContact) put("expPreloadCriteriaNote", "本轮以无接触类别记录，不提供空间点位标签；先记录 2 秒无载基线，再由服务端核验连续有效光学与六轴数据，满足检查后手动结束。");
    put("expRoundQuality", targetMode ? roundQualityText(guided.lastQuality).replace("请卸载后手动标定重采", experiment.baseline?.ready === true ? "下一轮参考已就绪，可重新采集" : "请保持无载，重新标定后采集") : roundQualityText(guided.lastQuality));
    $("expRoundQuality").className = "experiment-statusline" + (guided.lastQuality?.accepted === true ? " success" : guided.lastQuality?.accepted === false ? " error" : "");
    $("expPreloadStatus").hidden = timed || !preload.visible;
    $("expPreloadStatus").className = "preload-status" + (preload.timedOut ? " error" : "");
    put("expPreloadProgressText", preload.continuous === null ? "等待服务端诊断" : number(preload.continuous, 0) + " / " + number(preload.target, 0) + " ms · " + number(preload.percent, 0) + "%");
    $("expPreloadProgress").max = preload.target;
    $("expPreloadProgress").value = Math.min(preload.target, preload.continuous || 0);
    $("expPreloadProgress").setAttribute("aria-valuenow", String(Math.min(preload.target, preload.continuous || 0)));
    $("expPreloadProgress").setAttribute("aria-valuemax", String(preload.target));
    put("expPreloadElapsed", "已等待：" + (preload.elapsed === null ? "—" : number(preload.elapsed / 1000, 1) + " s") + (preload.timeout > 0 ? " · 超时上限 " + number(preload.timeout / 1000, 1) + " s" : ""));
    put("expPreloadResets", "计时重置：" + (preload.resetCount === null ? "—" : preload.resetCount + " 次"));
    put("expPreloadBlockReason", "当前阻塞原因：" + (preload.reason || "无；保持无接触，等待连续有效同步满 " + number(preload.target, 0) + " ms。"));
    $("expPreloadTimeout").hidden = !preload.timedOut;
  }
  function stoppedInferenceState(analysis = {}) {
    return { ...analysis, inference_active: false,
      test_reference: { ready: false, pending: false, reason: INFERENCE_STOPPED_MESSAGE },
      truth_zero: { ready: false, count: 0, reason: "本次推理参考与真值对照零点已清空；实时设备显示继续。" },
      prediction: { ...(spectralModel(analysis) ? { schema: "spectral_prediction_v1", model_kind: "spectral_gru" } : {}), status: "已结束", reason: INFERENCE_STOPPED_MESSAGE, valid: false, comparison_ready: false, point_id: null, force_N: null, truth_force_N: null, error_N: null, wrench_pred: null, wrench_truth: null, wrench_error: null, force_norm_N: null, confidence: null, contact: false } };
  }
  function render(state, freshState = false) {
    if (guiPriority?.isFrozen() === true || guiNeedsFreshState && !freshState) {
      guiNeedsFreshState = true;
      if (mounted) updateControls();
      return;
    }
    // Releasing pulse priority alone cannot make a held state current again.
    // Only the parent's next response (including an explicit failure) resumes.
    guiNeedsFreshState = false;
    if (!state) {
      if (!mounted) return;
      expired = true; invalidateLiveValidation("本机服务状态更新已中断，实时识别失效；本次已固定的判断读数保留。");
      renderCapturePrompt(captureServiceNotice());
      updateControls(); return;
    }
    if (removedModelIDs.has(state.analysis?.model_id)) state = { ...state, validation: { ...state.validation, model_id: null, model_sha256: null, pending_sample: null }, analysis: { ...state.analysis, model_id: null, model_sha256: null, model_kind: null, method: null, trained_point_ids: [], point_ids: [], model_definition: null, inference_active: false, test_reference: { ready: false, pending: false }, truth_zero: { ready: false }, prediction: {} } };
    if (inferenceStoppedLocally) state = { ...state, analysis: stoppedInferenceState(state.analysis) };
    latest = state;
    receivedAt = Date.now(); expired = !state;
    if (!mounted) return;
    const experiment = latest.experiment || {}, analysis = latest.analysis || {}, gates = experimentGates(latest, busy);
    renderProtocolSelection(experiment);
    const layout = pointLayout(experiment.settings, !!experiment.session_id);
    if (observedSessionID !== (experiment.session_id || null)) $("expSessionSetup").open = !(experiment.session_id && experiment.running);
    if (observedSessionID !== (experiment.session_id || null) || observedPointID !== (experiment.point_id || null)) {
      if (experiment.point_id) activePoint = experiment.point_id;
      observedSessionID = experiment.session_id || null; observedPointID = experiment.point_id || null;
    }
    renderPointMap(layout);
    renderPointSelection(layout); renderGuided(experiment);
    put("expSessionID", experiment.session_id || "尚未建立"); put("expCurrentBatch", experiment.settings?.batch || "—"); put("expSessionState", experiment.running ? "正在连续记录" : experiment.status === "stopped" ? "已停止记录" : "等待会话"); put("expSessionPath", experiment.path || "会话路径将在记录开始后显示");
    const dataNumber = latest.data_number || {}, activeDataID = dataNumber.active_data_id || (experiment.running ? experiment.settings?.data_id : null);
    put("expDataNumberState", dataNumber.error ? "自动编号状态异常：" + dataNumber.error : activeDataID ? "当前数据编号 " + activeDataID + (dataNumber.active_source === "legacy_manual" ? " · 兼容手工编号会话。" : " · 成功保存会话后自动递增。") : dataNumber.next_data_id ? "下次数据编号 " + dataNumber.next_data_id + " · 成功保存会话后自动 +1。" : "数据编号由服务端自动分配，成功保存会话后递增。");
    put("expRecordBadge", experiment.error ? "记录异常" : experiment.running ? "会话记录中" : "等待会话"); $("expRecordBadge").className = "quality-pill " + (experiment.error ? "bad" : experiment.running ? "good" : "neutral");
    put("expTrialPoint", selectionLabel(experiment.point_id) || "—"); put("expTrialID", experiment.trial_id || "尚未开始"); put("expTrialPhase", phaseLabel(experiment.phase, experiment)); put("expWriterStatus", experiment.error || (experiment.running ? captureDrift(experiment) ? experiment.settings?.capture_mode === "peak_drift" ? "九路峰心与漂移 / 六轴 / 事件连续保存；不保存 512 点原谱" : "旧九路漂移：九路结果与 512 点原谱备份连续保存" : "原始光谱 / 六轴 / 结果 / 事件连续保存" : "等待会话"));
    renderLiveForce(experiment.live_force);
    const baseline = experiment.baseline || {};
    put("expBaselineState", (baseline.ready || baseline.status === "ready" ? "本轮无载基线已就绪" : baseline.status === "collecting" ? "正在采集并核验本轮基线" : "本轮无载基线未就绪") + (baseline.reason ? " · " + baseline.reason : ""));
    const counts = experiment.counts || {};
    put("expOpticalCountLabel", experiment.settings?.capture_mode === "peak_drift" ? "九路寻峰帧" : "原始光谱");
    put("expSpectraCount", experiment.settings?.capture_mode === "peak_drift" ? counts.drift_frames ?? 0 : counts.raw_spectra ?? 0); put("expForceCount", counts.raw_force ?? 0); put("expSyncCount", (counts.sync_valid ?? 0) + " / " + (counts.sync_invalid ?? 0)); put("expTrialCount", (counts.trials_completed ?? 0) + " / " + (counts.trials_aborted ?? 0));
    if (experiment.error || analysis.error) message(experiment.error || analysis.error, "error");
    renderTraining(analysis);
    if (Array.isArray(analysis.models)) applyModelList({ models: analysis.models, deleted_models: analysis.deleted_models || deletedModels });
    else renderModelManagement();
    put("expActiveModel", analysis.model_id || "未加载");
    put("expModelDefinition", modelDefinitionText(spectralModel(analysis) ? { ...analysis.model_definition, model_kind: "spectral_gru", ...(driftModel(analysis) ? { input_kind: "delta_lambda_9" } : {}) } : analysis.model_definition));
    const inferenceStopped = analysis.inference_active === false && !!analysis.model_id;
    put("expInferenceState", inferenceStopped ? INFERENCE_STOPPED_MESSAGE : analysis.model_id ? analysis.test_reference?.ready === true ? "模型推理已启用；结束推理只停止当前模型，实时设备显示继续。" : "模型已载入；建立本次无接触参考后开始推理。" : "尚未载入模型；载入后建立本次无接触参考。");
    put("expTestReferenceState", inferenceStopped ? INFERENCE_STOPPED_MESSAGE + "；本次参考已清空。" : testReferenceText(analysis.test_reference, experiment.running === true));
    $("expTestReferenceState").className = "experiment-statusline" + (!inferenceStopped && analysis.test_reference?.ready === true && analysis.test_reference?.pending !== true ? " success" : "");
    const truthZero = analysis.truth_zero || {};
    put("expTruthZeroState", inferenceStopped ? "本次推理参考与真值对照零点已清空；实时设备显示继续。" : truthZero.reason || (truthZero.ready ? "测试真值六轴零点已就绪" : truthZero.count ? "正在收集测试真值零点 " + truthZero.count + "/12 帧" : spectralModel(analysis) ? driftModel(analysis) ? "寻峰漂移参考必需；请保持无接触收集 2 秒峰心参考，PX6D 真值仅对照。" : "真实 512 点无载参考必需；未连接 PX6D 时可纯光谱推理，六维真值与误差为空。" : "光学参考必需；PX6D 未连接时可仅使用九路 FBG 测试。"));
    const validation = latest.validation || {}, nextValidationModel = analysis.model_sha256 || analysis.model_id || "";
    if (nextValidationModel !== validationModelKey) {
      validationModelKey = nextValidationModel; frozenSample = null; frozenSaved = false; frozenDiscarded = false; validationResult = null;
      $("expPositionCorrect").checked = false; $("expForceCorrect").checked = false; targetPoint = null; judgementTarget = null;
    }
    adoptFrozenSample(validation.pending_sample);
    renderValidationMap(analysis, liveValidationStale());
    renderFrozenSample(); renderValidationStatistics(validation);
    updateControls();
  }
  function refreshFreshness() {
    if (!mounted || guiPaused()) return;
    if (!expired) renderLiveForce(latest.experiment?.live_force, Math.max(0, Date.now() - receivedAt));
    if (expired || !receivedAt || Date.now() - receivedAt <= 1500) return;
    expired = true;
    invalidateLiveValidation("超过 1.5 秒未收到状态更新，等待本机服务恢复。");
    renderCapturePrompt(captureServiceNotice());
    put("expForceProgressText", "状态已过期 · 最后服务端加载进度");
    if (!$("expPreloadStatus").hidden) put("expPreloadBlockReason", "状态已过期；上方为最后服务端预采样诊断，等待更新后核对。");
    message("状态更新已中断，试次阶段操作已停用；可尝试停止会话。", "error");
    updateControls();
  }
  function invalidateLiveValidation(reason) {
    if (trainingView(latest.analysis?.training).busy) put("expTrainingStage", "状态更新已中断；上方为最后服务端训练进度，等待恢复。");
    renderValidationMap(latest.analysis || {}, true);
    put("expPredictionState", "状态过期 · 预测与真值已失效");
    for (const id of ["expPredictionPoint", "expPredictionForce", "expPredictionTruth", "expPredictionError", "expPredictionConfidence"]) put(id, "—");
    if (spectralModel(latest.analysis)) $("expWrenchPrediction").innerHTML = '<p class="display-note">状态已过期，六轴预测、真值与误差已失效。</p>';
    put("expPredictionReason", reason);
    put("expTestReferenceState", "测试参考状态已过期，等待服务更新。"); $("expTestReferenceState").className = "experiment-statusline";
  }
  function mount() {
    if (mounted || !global.document || !$("experimentWorkspace")) return;
    $("experimentWorkspace").innerHTML = html(); mounted = true;
    if (!$("expCaptureMode").value) $("expCaptureMode").value = "full_spectrum";
    if (!$("expAcquisitionProtocol").value) $("expAcquisitionProtocol").value = "force_target_wrench_v1";
    if (!$("expTrainingMethod").value) $("expTrainingMethod").value = "spectral_gru";
    if (!$("expSpectralValidationMode").value) $("expSpectralValidationMode").value = "observe";
    renderPointMap(pointLayout(null));
    for (const name of ["session", "training", "test"]) $("expTab" + name[0].toUpperCase() + name.slice(1)).addEventListener("click", () => switchTab(name));
    const selectCapturePoint = id => {
      activePoint = id;
      pendingPointTouched = true;
      global.NineFBGPreferences?.save?.("experiment", { selected_point_id: activePoint });
      renderPointSelection(pointLayout(latest.experiment?.settings, !!latest.experiment?.session_id));
      updateControls();
      message("已预选 " + selectionLabel(activePoint) + (latest.experiment?.running ? "；请点击" : "；会话建立后点击") + (activePoint === NO_CONTACT ? "“确认采集‘无接触’”" : "“确认采集点位”") + "。");
    };
    $("expPointMap").addEventListener("click", event => {
      const button = event.target.closest("button[data-point-id]"); if (!button || button.disabled) return;
      selectCapturePoint(button.dataset.pointId);
    });
    $("expNoContact").addEventListener("click", () => { if (!$("expNoContact").disabled) selectCapturePoint(NO_CONTACT); });
    $("expConfirmPointBtn").addEventListener("click", () => run(async () => {
      if (activePoint === NO_CONTACT && !captureUsesTargetProtocol(latest.experiment)) throw new Error("原有引导协议不支持无接触标签；请使用目标合力连续采集协议。");
      const result = await request("/api/experiment/point", { point_id: activePoint });
      $("expBaselineConfirmed").checked = false;
      if (result.experiment) render(result);
      message("已提交采集" + (activePoint === NO_CONTACT ? "类别“无接触”" : "点位 " + activePoint) + (result.experiment?.baseline?.ready === true ? "；无载参考已就绪，可以开始本轮采集。" : "；请移开接触棒并手动标定本轮。"), "success");
    }));
    $("expStartBtn").addEventListener("click", () => run(async () => {
      const finger = $("expFingerID").value; if (!finger.trim()) throw new Error("请填写实际手指 / 样件标识。");
      const mode = $("expCaptureMode").value, protocol = $("expAcquisitionProtocol").value, wrench = protocol === "force_target_wrench_v1", targetMode = wrench;
      if (!["full_spectrum", "peak_drift"].includes(mode)) throw new Error("请选择原始全谱或寻峰漂移采集模式。");
      if (mode === "peak_drift" && protocol !== "force_target_wrench_v1") throw new Error("寻峰漂移采集使用合力目标＋手动结束协议。");
      if (!["guided_round_v1", "force_target_wrench_v1"].includes(protocol)) throw new Error("请选择可用于新建会话的采集协议；固定 3 s 仅保留历史兼容。");
      if (!wrench && !$("expDirectionConfirmed").checked) throw new Error("建立会话前，请先根据实际安装核对法向按压力方向。");
      if (wrench && actualSampleRate(latest) !== 25) throw new Error("六维时序采集需先核对当前光谱请求为 25 Hz；请停止连接后明确设置并重新连接，界面不会自动热改设备。");
      const repetitions = repetitionsValue($("expRepetitions").value);
      const limit = wrench ? numericInput("expMaxForceNorm", "三维合力上限") : null, target = targetMode ? numericInput("expTargetForceNorm", "三维合力目标") : null;
      if (wrench && (limit <= 0 || limit > 100)) throw new Error("三维合力上限须为大于零且不超过 100 N 的有限数值。");
      if (targetMode && (target <= 0 || target >= limit)) throw new Error("三维合力目标须大于零且低于合力上限；目标与上限相同会使轻微超调判为超限，请明确设置不同数值。");
      // Operator-selected fixed defaults; per-point selection and unloaded
      // confirmation remain separate live actions.
      const result = await request("/api/experiment/start", { finger_id: finger, batch: "A", force_sign: Number($("expForceSign").value), force_direction_confirmed: $("expDirectionConfirmed").checked, coordinates_confirmed: true, coordinate_mode: "normalized", conditions: { fixation: "", temperature_C: numericInput("expTemperature", "温度", true) }, max_force_N: 3, guided_acquisition: true, repetitions_per_point: repetitions, acquisition_protocol: protocol, capture_mode: mode, input_kind: mode === "peak_drift" ? "delta_lambda_9" : "delta_S", ...(wrench ? { max_force_norm_N: limit } : {}), ...(targetMode ? { target_force_norm_N: target } : {}) });
      $("expBaselineConfirmed").checked = false;
      if (result.experiment) render(result);
      message("采集会话已建立；请在右侧明确确认实际采集点位，再标定本轮。", "success");
    }));
    $("expStopBtn").addEventListener("click", () => run(async () => {
      const raw = !latest.experiment?.running && legacyRawRecording(latest);
      if (!latest.experiment?.running && !raw) throw new Error("当前没有可停止并保存的采集会话。");
      const result = await request(raw ? "/api/disconnect" : "/api/experiment/stop", {});
      if (result.experiment || result.version) render(result);
      message(raw ? "旧版原始采集已受控停止并保存，请关闭程序后重新打开。" : "会话已停止，原始数据与完整性记录已保留。", "success");
      if (!raw) await refreshSessions();
    }));
    $("expBaselineConfirmed").addEventListener("change", updateControls);
    $("expCaptureMode").addEventListener("change", () => {
      if (busy || latest.experiment?.running) { renderProtocolSelection(latest.experiment); updateControls(); return; }
      if (!["full_spectrum", "peak_drift"].includes($("expCaptureMode").value)) $("expCaptureMode").value = "full_spectrum";
      $("expDirectionConfirmed").checked = false; $("expBaselineConfirmed").checked = false;
      renderProtocolSelection(latest.experiment); renderGuided(latest.experiment || {}); updateControls();
    });
    $("expAcquisitionProtocol").addEventListener("change", () => { protocolTouched = true; $("expDirectionConfirmed").checked = false; $("expBaselineConfirmed").checked = false; $("expProtocolUpgradeNotice").hidden = true; global.NineFBGPreferences?.save?.("experiment", { target_force_norm_N: Number($("expTargetForceNorm").value) || 4 }); renderGuided(latest.experiment || {}); renderLiveForce(latest.experiment?.live_force); renderPointMap(pointLayout(latest.experiment?.settings, !!latest.experiment?.session_id)); renderPointSelection(pointLayout(latest.experiment?.settings, !!latest.experiment?.session_id)); updateControls(); });
    for (const id of ["expTargetForceNorm", "expMaxForceNorm"]) $(id).addEventListener("input", () => { renderGuided(latest.experiment || {}); renderLiveForce(latest.experiment?.live_force); });
    $("expTrialFinish").addEventListener("click", () => run(async () => { if (!experimentGates(latest, expired).finish) throw new Error(latest.experiment?.guided?.finish_block_reason || (noContactSelection(latest.experiment) ? "系统尚未允许结束本轮；请保持无接触，等待真实数据通过检查。" : "系统尚未允许结束本轮；请等待达到配置目标并通过按压数据检查，异常时中止本轮。")); const wasNoContact = noContactSelection(latest.experiment); const result = await request("/api/experiment/trial", { action: "finish" }); if (result.experiment) render(result); message(wasNoContact ? "正在保存本轮无接触数据并建立下一轮参考；请保持无接触，保存完成后再开始下一轮。" : "已确认卸载，正在直接建立下一轮软件零点与" + captureReference(latest.experiment) + "并保存；请保持压力棒移开，保存完成后再开始下一轮。"); }));
    $("expForceSign").addEventListener("change", () => { $("expDirectionConfirmed").checked = false; put("expDirectionDescription", "已按当前安装核对法向方向：按压力 F = " + ($("expForceSign").value === "-1" ? "−" : "+") + "(Fz − Fz₀)"); message("按压力符号已改变，请根据实际安装重新核对法向方向。"); });
    $("expBaselineBtn").addEventListener("click", () => run(async () => { if (!$("expBaselineConfirmed").checked) throw new Error("请先确认已移开接触棒且当前无载。"); const result = await request("/api/experiment/baseline", { unloaded_confirmed: true }); $("expBaselineConfirmed").checked = false; if (result.experiment) render(result); message("正在标定本轮，请保持无接触，等待服务端基线就绪。"); }));
    for (const [id, action] of Object.entries({ expTrialStart: "start", expTrialAbort: "abort" })) $(id).addEventListener("click", () => run(async () => { const result = await request("/api/experiment/trial", { action }); if (result.experiment) render(result); message(action === "abort" ? "本轮已中止，原始记录保留；请卸载后手动标定重采。" : noContactSelection(latest.experiment) ? "本轮无接触采集已开始；全过程保持无接触，先记录 2 秒基线，待服务端检查通过后手动结束。" : captureUsesTargetProtocol(latest.experiment) ? "本轮已开始连续记录；先保持无接触记录 2 秒基线，等待服务端提示后再按压。" : "本轮采集已开始，保持无接触并跟随服务端提示。", action === "abort" ? "" : "success"); }));
    $("expRefreshSessions").addEventListener("click", () => run(async () => { await refreshSessions(); message("会话列表已刷新。", "success"); }));
    $("expShowDeleted").addEventListener("change", () => { if (historyPendingAction || busy) return; selectedSessions.clear(); applyCumulativeSpectralSelection(); renderSessions(); });
    $("expSelectTrainable").addEventListener("click", () => { if (busy || historyPendingAction || $("expShowDeleted").checked) return; if (sequenceMethod($("expTrainingMethod").value)) manualTrainingSelection(); selectedSessions.clear(); for (const row of sessions) if (historyTrainingStatus(row, $("expTrainingMethod").value).allowed) selectedSessions.add(row.session_id); renderSessions(); message("已选择全部可训练记录；请核对数据兼容性与模型方法后明确点击批量训练。"); });
    $("expClearSessions").addEventListener("click", () => { if (busy || historyPendingAction) return; if (sequenceMethod($("expTrainingMethod").value)) manualTrainingSelection(); selectedSessions.clear(); renderSessions(); });
    $("expSessionList").addEventListener("change", event => { const id = event.target.dataset.sessionId; if (busy || historyPendingAction || !id || !historyRows().some(row => row.session_id === id)) return; if (sequenceMethod($("expTrainingMethod").value)) manualTrainingSelection(); if (event.target.checked) selectedSessions.add(id); else selectedSessions.delete(id); renderSessions(); });
    $("expSessionList").addEventListener("click", event => {
      const button = event.target.closest("button[data-open-session]"); if (!button || button.disabled) return;
      event.preventDefault?.(); event.stopPropagation?.();
      return run(async () => { const result = await request("/api/history/open", { session_id: button.dataset.openSession, deleted: button.dataset.deleted === "true" }); message(result.opened ? "已打开保存位置：" + (result.path || "") : "保存位置未能打开。", result.opened ? "success" : "error"); });
    });
    $("expDeleteSessions").addEventListener("click", () => openHistoryDialog("delete"));
    $("expRestoreSessions").addEventListener("click", () => openHistoryDialog("restore"));
    $("expHistoryCancel").addEventListener("click", () => { if (!busy) closeHistoryDialog(); });
    $("expHistoryDialog").addEventListener("keydown", event => {
      if (event.key === "Escape" && !busy) { event.preventDefault(); closeHistoryDialog(); }
      if (event.key === "Tab") { const first = $("expHistoryCancel"), last = $("expHistoryConfirm"); if (event.shiftKey && global.document.activeElement === first) { event.preventDefault(); last.focus?.(); } else if (!event.shiftKey && global.document.activeElement === last) { event.preventDefault(); first.focus?.(); } }
    });
    $("expHistoryConfirm").addEventListener("click", () => run(async () => {
      const pending = historyPendingAction; if (!pending) throw new Error("请先明确选择需要删除或恢复的记录。");
      if (historyMutationBlocked()) throw new Error("采集或训练进行中，或服务状态尚未就绪，不能删除或恢复记录。");
      try {
        const result = await request("/api/history/" + pending.action, { session_ids: pending.ids });
        selectedSessions.clear(); closeHistoryDialog(); applyHistoryList(result);
        message(pending.action === "delete" ? "所选记录已移入可恢复回收区；可勾选“查看已删除”并恢复。" : "所选记录已恢复至原保存位置。", "success");
      } catch (error) { put("expHistoryDialogError", error.message); $("expHistoryDialogError").hidden = false; throw error; }
    }));
    $("expTrainingMethod").addEventListener("change", () => { const selectedMethod = $("expTrainingMethod").value; if (sequenceMethod(selectedMethod)) global.NineFBGPreferences?.save?.("analysis", { method: selectedMethod, input_kind: selectedMethod === "drift_gru" ? "delta_lambda_9" : "delta_S" }); if (!sequenceMethod($("expTrainingMethod").value) && previousTrainingMode === "finetune") changeTrainingMode("scratch"); else { applyCumulativeSpectralSelection(); renderSessions(); } renderBaseModels(); if (sequenceMethod($("expTrainingMethod").value) && !modelsLoaded) return run(refreshModels); });
    $("expTrainingMode").addEventListener("change", () => { if (busy || latest.analysis?.busy || trainingView(latest.analysis?.training).busy || historyPendingAction || modelPendingAction) { $("expTrainingMode").value = previousTrainingMode; return; } changeTrainingMode($("expTrainingMode").value); renderBaseModels(); });
    $("expBaseModelSelect").addEventListener("change", () => { renderBaseModels(); updateControls(); });
    $("expRefreshTrainingModels").addEventListener("click", () => run(async () => { await refreshModels(); message("基础模型列表已刷新；不会载入或替换当前推理模型。"); }));
    $("expCumulativeSources").addEventListener("change", () => { if (fineTuneSelected()) { cumulativeSpectralSelection = false; $("expCumulativeSources").checked = false; return; } cumulativeSpectralSelection = $("expCumulativeSources").checked; scratchCumulativeSelection = cumulativeSpectralSelection; applyCumulativeSpectralSelection(); renderSessions(); });
    $("expTrainingStart").addEventListener("click", () => run(async () => {
      const method = $("expTrainingMethod").value;
      if (expired || latest.experiment?.running && !sequenceMethod(method) || latest.analysis?.busy === true || trainingView(latest.analysis?.training).busy) throw new Error("当前状态不允许开始训练，请先保存采集会话并等待已有任务结束。");
      if ($("expShowDeleted").checked || historyPendingAction || modelPendingAction) throw new Error("请先恢复回收区记录，或关闭记录/模型操作确认框，再训练。");
      if (sequenceMethod(method) && fineTuneSelected() && !selectedBaseModel()) throw new Error("请明确选择当前可用的同输入基础模型，并只勾选新增会话。");
      const sessionIDs = historyTrainingIDs(sessions, checkedSessionIDs(), method);
      if (!["linear", "mlp", "spectral_gru", "drift_gru"].includes(method)) throw new Error("请选择有效模型方法。");
      if (latest.experiment?.session_id && sessionIDs.includes(latest.experiment.session_id) && latest.experiment.running) throw new Error("当前采集会话尚未完整保存，只能留到停止并保存后的下一轮训练。");
      const result = await request("/api/training/start", { session_ids: sessionIDs, method, ...(sequenceMethod(method) ? spectralTrainingParameters() : {}) });
      const training = result.training || result.analysis?.training;
      if (training) { latest = { ...latest, analysis: { ...latest.analysis, training } }; renderTraining(latest.analysis); }
      message("训练任务已提交，等待服务端真实进度；模型保存后再去模型推理。");
    }));
    for (const [id, action] of [["expTrainingPause", "pause"], ["expTrainingResume", "resume"], ["expTrainingCancel", "cancel"]]) $(id).addEventListener("click", () => run(async () => {
      const result = await request("/api/training/" + action, {});
      const training = result.training || result.analysis?.training;
      if (training) { latest = { ...latest, analysis: { ...latest.analysis, training } }; renderTraining(latest.analysis); }
      message(action === "resume" ? "已解除用户暂停；采集保护暂停仍由服务端保留，等待状态确认。" : action === "pause" ? "已请求用户暂停，等待当前批次和服务端确认。" : "已请求取消训练，等待进程退出与资源回收。");
    }));
    $("expSpectralValidationMode").addEventListener("change", updateControls);
    for (let axis = 0; axis < 6; axis++) $("expWrenchTolerance" + axis).addEventListener("input", updateControls);
    $("expGoValidation").addEventListener("click", () => run(async () => {
      const view = trainingView(latest.analysis?.training);
      if (!view.complete) throw new Error("模型尚未确认保存完成。");
      await refreshModels(view.modelID); switchTab("test");
      message("已选中刚训练的模型，请手动点击“载入模型”，再明确建立本次无接触参考。");
    }));
    $("expRefreshModels").addEventListener("click", () => run(async () => { await refreshModels(); message("已保存模型列表已刷新。", "success"); }));
    $("expModelManagement").addEventListener("toggle", () => { if ($("expModelManagement").open && !busy && !modelPendingAction) return run(refreshModels); });
    $("expShowDeletedModels").addEventListener("change", () => { if (busy || modelPendingAction) return; selectedModels.clear(); renderModelManagement(); updateControls(); });
    $("expSelectModels").addEventListener("click", () => { if (busy || modelMutationBlocked() || modelPendingAction || historyPendingAction) return; selectedModels.clear(); for (const model of modelRows()) if (!modelRowBlockReason(model)) selectedModels.add(model.model_id); renderModelManagement(); updateControls(); });
    $("expClearModels").addEventListener("click", () => { if (busy || modelPendingAction) return; selectedModels.clear(); renderModelManagement(); updateControls(); });
    $("expModelList").addEventListener("change", event => {
      const id = event.target.dataset.modelId, row = modelRows().find(model => model.model_id === id);
      if (busy || modelMutationBlocked() || modelPendingAction || !row || modelRowBlockReason(row)) return;
      if (event.target.checked) selectedModels.add(id); else selectedModels.delete(id); renderModelManagement(); updateControls();
    });
    $("expDeleteModels").addEventListener("click", () => openModelDialog("delete"));
    $("expRestoreModels").addEventListener("click", () => openModelDialog("restore"));
    $("expModelCancel").addEventListener("click", () => { if (!busy) closeModelDialog(); });
    $("expModelDialog").addEventListener("keydown", event => {
      if (event.key === "Escape" && !busy) { event.preventDefault(); closeModelDialog(); }
      if (event.key === "Tab") { const first = $("expModelCancel"), last = $("expModelConfirm"); if (event.shiftKey && global.document.activeElement === first) { event.preventDefault(); last.focus?.(); } else if (!event.shiftKey && global.document.activeElement === last) { event.preventDefault(); first.focus?.(); } }
    });
    $("expModelConfirm").addEventListener("click", () => run(async () => {
      const pending = modelPendingAction;
      if (!pending || modelMutationBlocked() || historyPendingAction || pending.deleted !== $("expShowDeletedModels").checked || pending.ids.some(id => modelRowBlockReason(modelRows().find(row => row.model_id === id), pending.deleted))) throw new Error("当前不能操作所选模型；请核对采集、训练、推理和待保存判断状态后重试。");
      try {
        const result = await request("/api/analysis/models/" + pending.action, { model_ids: pending.ids });
        if (!result.analysis || !Array.isArray(result.models) || !Array.isArray(result.deleted_models)) throw new Error("服务端尚未确认模型操作完成，请刷新列表核对后重试。");
        for (const id of pending.ids) { selectedModels.delete(id); if (pending.action === "delete") removedModelIDs.add(id); }
        render(result); closeModelDialog(); applyModelList(result);
        message(pending.action === "delete" ? "所选模型已移入可恢复回收区；训练数据和已保存人工判断报告保留。" : "所选模型已恢复至候选列表；请手动载入并建立新的无接触参考后推理。", "success");
      } catch (error) { put("expModelDialogError", error.message); $("expModelDialogError").hidden = false; throw error; }
    }));
    $("expModelSelect").addEventListener("change", updateControls);
    $("expTestUnloaded").addEventListener("change", updateControls);
    $("expTestReferenceBtn").addEventListener("click", () => run(async () => {
      if (latest.analysis?.inference_active !== true || !latest.analysis?.model_id) throw new Error(latest.analysis?.model_id ? INFERENCE_STOPPED_MESSAGE : "请先载入模型，再明确确认无接触并建立本次参考。");
      if (!$("expTestUnloaded").checked) throw new Error("请先确认当前无接触。");
      await request("/api/analysis/reference", { unloaded_confirmed: true }); $("expTestUnloaded").checked = false;
      render({ ...latest, analysis: { ...latest.analysis, test_reference: { ready: false, pending: true, reason: "等待本次无接触参考与服务端状态" }, prediction: { status: "等待本次无接触参考", reason: "本次参考完成后再真实按压。" } } });
      message("已请求本次模型推理光学参考；若连接 PX6D，将同时收集服务端无载真值零点。");
    }));
    $("expLoadModelBtn").addEventListener("click", () => run(async () => {
      const id = $("expModelSelect").value; if (!id) throw new Error("请先选择真实保存的模型。");
      if (models.find(model => model.model_id === id)?.loadable === false) throw new Error("所选模型当前不可载入，请查看模型管理列表中的原因。");
      const spectral = models.find(model => model.model_id === id)?.model_kind === "spectral_gru" || /^S[A-Za-z0-9_-]+$/.test(id);
      if (spectral && !$("expTestUnloaded").checked) throw new Error("应用六维时序模型前，请先移开接触棒并明确勾选当前无接触；模型切换后需重新建立参考与预热。");
      const result = await request("/api/analysis/load", { model_id: id, ...(spectral ? { unloaded_confirmed: true } : {}) });
      if (result.analysis?.inference_active !== true) throw new Error("服务端尚未确认模型载入，请刷新模型状态后重试。");
      inferenceStoppedLocally = false; removedModelIDs.delete(id); $("expTestUnloaded").checked = false;
      render({ ...latest, ...result, analysis: { ...latest.analysis, ...result.analysis, test_reference: { ready: false, pending: false, reason: "模型已载入，请重新明确确认当前无接触并建立本次参考" }, prediction: { status: "等待本次无接触参考", reason: "模型已更换，等待服务端模型定义及本次参考。" } } });
      message("模型已载入，请移开接触棒，重新明确确认无接触并建立本次参考。", "success");
    }));
    $("expInferenceStop").addEventListener("click", () => run(async () => {
      if (hasPendingJudgement() || latest.validation?.pending_sample) throw new Error("请先保存本次判断或点击“重新判断”，再结束推理。");
      if (!inferenceStopAllowed(latest, expired || !receivedAt || Date.now() - receivedAt > 1500)) throw new Error("当前状态不能结束推理；请核对模型推理状态，先停止并保存活动采集会话。");
      const result = await request("/api/analysis/stop", {});
      if (result.analysis?.inference_active !== false) throw new Error("服务端尚未确认推理结束，请刷新状态后核对。");
      inferenceStoppedLocally = true; $("expTestUnloaded").checked = false;
      render(result);
      message(INFERENCE_STOPPED_MESSAGE + "。实时设备显示与后台训练继续。", "success");
      try { await refreshModels(); } catch (error) { message("推理已结束；模型管理列表刷新失败，请手动刷新：" + error.message, "error"); }
    }));
    $("expValidationMap").addEventListener("click", event => {
      const button = event.target.closest("button[data-target-point]");
      if (!button || button.disabled || busy) return;
      if (!backendCompatible(latest)) { message(compatibilityMessage(latest), "error"); return; }
      if (hasPendingJudgement()) { message("请先保存本次判断，或点击“重新判断”，再切换目标。", "error"); return; }
      const selected = button.dataset.targetPoint;
      if (!/^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(selected || "")) return;
      clearJudgement(); targetPoint = selected;
      renderValidationMap(latest.analysis || {}, liveValidationStale()); renderFrozenSample(); updateControls();
      message("目标已选为 " + targetPoint + "；请实际按压对应位置，观察绿色识别灯和力差。目标不输入模型。");
    });
    for (const id of ["expPositionCorrect", "expForceCorrect"]) $(id).addEventListener("change", () => {
      if (!$(id).checked) { updateControls(); return; }
      const wantedPosition = $("expPositionCorrect").checked, wantedForce = $("expForceCorrect").checked;
      // A click is an explicit judgement intent. It never advances acquisition
      // or supplies a target to inference; the server fixes its own evidence.
      if (!hasPendingJudgement()) { $("expPositionCorrect").checked = false; $("expForceCorrect").checked = false; }
      return run(async () => {
        if (!hasPendingJudgement()) await captureJudgement();
        applyJudgementChecks(wantedPosition, wantedForce);
        renderFrozenSample();
        message("本次判断读数已固定；请核对后保存，或点击“重新判断”。", "success");
      });
    });
    $("expValidationSave").addEventListener("click", () => run(async () => {
      const position = $("expPositionCorrect").checked, force = $("expForceCorrect").checked;
      if (!hasPendingJudgement()) await captureJudgement();
      const sample = applyJudgementChecks(position, force);
      const result = await request("/api/validation/judge", { sample_id: sample.id, position_correct: position, force_correct: force, actual_point_id: judgementTarget });
      if (result.validation) latest = { ...latest, validation: result.validation };
      frozenSaved = true; $("expPositionCorrect").checked = false; $("expForceCorrect").checked = false;
      renderFrozenSample(); renderValidationStatistics(latest.validation);
      message("本次判断已保存：目标 " + judgementTarget + "，位置" + (position ? "正确" : "不正确") + "，力" + (force ? "正确" : "不正确") + "。", "success");
    }));
    $("expValidationDiscard").addEventListener("click", () => run(async () => {
      const sample = frozenSampleView(frozenSample);
      if (!sample || sample.model !== latest.analysis?.model_id) throw new Error("当前没有本模型可重新判断的读数。");
      if (hasPendingJudgement()) {
        const result = await request("/api/validation/discard", { sample_id: sample.id });
        if (result.validation) latest = { ...latest, validation: result.validation };
      }
      clearJudgement(); renderFrozenSample(); renderValidationStatistics(latest.validation);
      renderValidationMap(latest.analysis || {}, liveValidationStale());
      message("已清除本次待判断状态；未保存判断不计入分母。请重新实际按压，再核对并保存。");
    }));
    $("expValidationFinish").addEventListener("click", () => run(async () => {
      if (hasPendingJudgement() || latest.validation?.pending_sample) throw new Error("请先保存本次判断或点击“重新判断”，再保存本轮判断报告。");
      if (!validationResultView(latest.validation).total) throw new Error("尚无已保存判断，暂无已保存判断可生成结果。");
      const result = await request("/api/validation/finish", {});
      if (result.validation) latest = { ...latest, validation: result.validation };
      validationResult = result.report || result.validation?.latest_report || null;
      renderValidationStatistics(latest.validation);
      message("本轮真实按压判断报告已保存；保存报告不改变模型推理状态。", "success");
    }));
    render(latest);
    bindPreferences();
    guiPriority?.subscribe(() => {
      guiNeedsFreshState = true;
      if (guiPriority.isFrozen()) for (const id of ["expBaselineConfirmed", "expTestUnloaded"]) $(id).checked = false;
      updateControls();
      message(guiPriority.isFrozen() ? "脉搏曲线优先 · 本页为暂停前快照，后台采集、保存与训练继续；可在上方恢复本页显示。" : "正在恢复本页显示，等待最新服务状态后再操作。", "info");
    });
    global.setInterval(refreshFreshness, 250);
  }
  return { mount, render, captureDrift, captureMode, sessionCaptureLabel, sequenceMethod, driftModel, modelTrainingMethod, pointGrid, visualPointGrid, pointLayout, COORDINATE_SCHEMA, REQUIRED_SERVICE_VERSION, backendCompatible, legacyRawRecording, compatibilityMessage, phaseLabel, experimentGates, inferenceStopAllowed, guidedView, preloadView, repetitionsValue, roundQualityText, predictionView, trainedPointIDs, validationLiveView, frozenSampleView, forceWithinTolerance, wrenchWithinTolerance, actualSampleRate, targetComparison, targetPredictionView, validationStatistics, validationResultView, trainingView, trainingMethodLabel, modelOptionLabel, liveForceView, trialCurves, summaryRows, splitMetrics, hasSplitData, defaultReportSplit, modelSessionIDs, sessionHasTrainingData, visibleSessions, latestEffectiveSession, sessionTimeText, sessionPointSummary, historyTrainingStatus, historyTrainingIDs, cumulativeSpectralSourceIDs, historyRecordLabel, selectedRoundSummary, pilotSessionIDs, pilotReportView, analysisProgressText, modelDefinitionText, testReferenceText };
});




