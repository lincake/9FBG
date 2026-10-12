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
  const PHASES = ["preload", "loading", "hold", "removed", "recovery"];
  const PHASE_LABELS = { idle: "等待试次", unloaded: "无载基线", preload: "无接触预采样", loading: "加载", hold: "保持", removed: "已移开", recovery: "恢复观察", finish: "已完成", aborted: "已中止", stopped: "会话已停止" };
  const COORDINATE_SCHEMA = "bottom_left_xy_v1";
  const REQUIRED_SERVICE_VERSION = "0.7.1";
  function backendCompatible(state) { return state?.version === REQUIRED_SERVICE_VERSION; }
  function compatibilityMessage(state) {
    return state?.version ? "旧版服务仍在运行，请先停止并保存会话，再关闭程序重新打开。（当前服务 " + state.version + "，界面要求 " + REQUIRED_SERVICE_VERSION + "）" + (legacyRawRecording(state) && !state.experiment?.running ? " 当前旧版原始采集仍在写入；点击“停止并保存会话”会停止设备、排空队列并保存原始记录。" : "") : "正在核验服务版本；确认使用 " + REQUIRED_SERVICE_VERSION + " 后才能进行引导采集。";
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
  function phaseLabel(phase) { return PHASE_LABELS[phase] || phase || "等待会话"; }
  function experimentGates(state, busy = false, pendingPoint = null) {
    const experiment = state.experiment || {}, phase = experiment.phase || "idle";
    const running = experiment.running === true;
    const guided = experiment.guided || {}, enabled = guided.enabled === true;
    const active = running && (!!experiment.trial_id || PHASES.includes(phase) || ["preload", "loading", "unloading", "checking"].includes(guided.step));
    const baselineReady = experiment.baseline?.ready === true || experiment.baseline?.status === "ready";
    const confirmed = experiment.settings?.force_direction_confirmed === true && (experiment.settings?.coordinates_confirmed === true || experiment.settings?.point_grid_confirmed === true);
    const selectionMatches = pendingPoint === null || pendingPoint === experiment.point_id;
    const compatible = backendCompatible(state);
    return {
      start: compatible && !busy && !state.analysis?.busy && !running && state.running === true && state.mode === "hardware" && state.force_connected === true,
      stop: !busy && (running || legacyRawRecording(state)), point: compatible && !busy && !active,
      pointConfirm: compatible && !busy && running && enabled && !active,
      baseline: compatible && !busy && running && enabled && !active && selectionMatches && ["needs_baseline", "retry", "round_complete"].includes(guided.step) && !!experiment.point_id,
      trialStart: compatible && !busy && running && enabled && !active && selectionMatches && guided.step === "ready" && baselineReady && confirmed && !!experiment.point_id,
      loading: false, hold: false, removed: false, recovery: false, finish: false,
      abort: compatible && !busy && active
    };
  }
  function repetitionsValue(value) {
    const count = Number(value);
    if (!String(value).trim() || !Number.isInteger(count) || count < 3 || count > 999) throw new Error("每点重复次数必须为 3～999 的整数。");
    return count;
  }
  function guidedView(experiment) {
    const source = experiment?.guided || {}, enabled = !!experiment?.session_id && source.enabled === true;
    const count = value => enabled && Number.isInteger(value) && value >= 0 ? value : 0;
    const percent = value => enabled && finite(value) ? Math.max(0, Math.min(100, value)) : 0;
    return { enabled, step: enabled ? source.step : "waiting", message: enabled ? source.message || "等待服务端更新本轮状态。" : "待开始：建立会话后，选择并确认实际采集点位。",
      repetitions: enabled ? count(source.repetitions_per_point) : 0, round: count(source.current_round),
      pointAccepted: count(source.point_accepted), pointAttempts: count(source.point_attempts), totalAccepted: count(source.total_accepted), totalRequired: count(source.total_required),
      totalPercent: percent(source.progress_percent), pointPercent: percent(source.point_progress_percent), forcePercent: percent(source.force_progress_percent),
      acceptedByPoint: enabled ? source.accepted_by_point || {} : {}, lastQuality: enabled ? source.last_quality || null : null, complete: enabled && source.complete === true };
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
    const view = predictionView(analysis?.prediction), trained = trainedPointIDs(analysis);
    const ready = !stale && !!analysis?.model_id && analysis?.test_reference?.ready === true && analysis.test_reference.pending !== true && analysis?.prediction?.stale !== true;
    const valid = ready && trained.includes(view.point) && analysis?.prediction?.valid !== false;
    const comparisonReady = ready && (typeof analysis?.prediction?.comparison_ready === "boolean" ? analysis.prediction.comparison_ready : valid && view.truth !== null);
    return { ...view, point: valid ? view.point : null, force: valid ? view.force : null, error: valid ? view.error : null, truth: stale ? null : view.truth, valid, trained, comparisonReady };
  }
  function frozenSampleView(sample) {
    if (!sample || typeof sample.sample_id !== "string" || !sample.sample_id) return null;
    const prediction = predictionView(sample.prediction);
    const inferenceValid = sample.inference_valid !== false && !!prediction.point && prediction.force !== null && prediction.truth !== null;
    return { id: sample.sample_id, model: sample.model_id || "未记录", valid: sample.valid === true, inferenceValid,
      point: inferenceValid ? prediction.point : null, force: inferenceValid ? prediction.force : null, truth: prediction.truth, error: inferenceValid ? prediction.error : null,
      created: sample.created_utc || null, reason: sample.reason || prediction.reason, saved: sample.judged === true || sample.saved === true };
  }
  function forceWithinTolerance(error) { return finite(error) && Math.abs(error) <= .3 + 1e-12; }
  function targetComparison(target, live) {
    if (!target) return "请先点击要识别的目标点位。";
    if (live?.valid && live.point) return "目标 " + target + " · 识别 " + live.point + " · " + (target === live.point ? "位置一致" : "位置不一致");
    return "目标 " + target + (live?.comparisonReady ? " · 当前未输出有效识别位置。" : " · 待按压或数据尚未就绪。");
  }
  function validationStatistics(source) {
    const count = value => Number.isInteger(value) && value >= 0 ? value : 0;
    const total = count(source?.judged_total), position = count(source?.position_correct), force = count(source?.force_correct);
    return { total, position: Math.min(position, total), force: Math.min(force, total), pending: count(source?.pending_count), invalid: count(source?.invalid_count),
      positionRate: total ? Math.min(position, total) / total : null, forceRate: total ? Math.min(force, total) / total : null };
  }
  function modelOptionLabel(model) {
    const ids = Array.isArray(model?.point_ids) ? model.point_ids : [];
    const kind = model?.model_kind === "pilot" ? "少量点研究模型" + (ids.length ? " · " + ids.length + " 点" : "") : model?.model_kind === "full" ? "完整 25 点模型" : "模型范围待核对";
    const dataID = model?.data_id || (Array.isArray(model?.data_ids) ? model.data_ids.filter(Boolean).join(" / ") : "") || "未编号";
    const acquired = model?.data_started_utc || model?.data_started_minute;
    const acquiredMinute = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(acquired || "") ? acquired : sessionTimeText(acquired);
    return (model?.model_id || "编号未记录") + " · " + kind + " · 数据 " + dataID + (acquired ? " · 采集 " + acquiredMinute : "") + (model?.method ? " · " + (model.method_label || trainingMethodLabel(model.method)) : "") + (model?.created_local_minute ? " · 训练 " + model.created_local_minute : model?.created_utc ? " · 训练 " + sessionTimeText(model.created_utc) : "");
  }
  function liveForceView(force, elapsed = 0) {
    const age = finite(force?.age_ms) ? force.age_ms + Math.max(0, elapsed) : null;
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
    return session?.complete === true && session.has_training_data === true && Number.isInteger(session.accepted_trial_count) && session.accepted_trial_count > 0;
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
  function historyTrainingStatus(session) {
    if (!session) return { allowed: false, reason: "记录已变化，请刷新历史列表。" };
    if (session.deleted === true) return { allowed: false, reason: "记录在回收区，请先恢复。" };
    const batch = session.settings?.batch || session.batch;
    if (batch === "C") return { allowed: false, reason: "C 批是保留数据，不参与训练。" };
    if (!["A", "B"].includes(batch)) return { allowed: false, reason: "批次不明，不能训练。" };
    if (session.diagnostic === true) return { allowed: false, reason: "诊断记录不能训练。" };
    if (session.trainable === false) return { allowed: false, reason: session.history_error || "该记录未通过训练数据核验。" };
    if (!sessionHasTrainingData(session)) return { allowed: false, reason: session.history_error || (session.complete !== true ? "记录尚未完整保存。" : "尚无合格按压数据。") };
    return { allowed: true, reason: "可训练" };
  }
  function historyTrainingIDs(available, selected) {
    const ids = Array.from(selected || []);
    if (!ids.length) throw new Error("请勾选一条或多条采集数据。");
    const invalid = ids.map(id => ({ id, status: historyTrainingStatus((available || []).find(session => session.session_id === id)) })).filter(item => !item.status.allowed);
    if (invalid.length) throw new Error("所选记录不能全部用于训练：" + invalid.map(item => item.id + "：" + item.status.reason).join("；") + " 请取消这些记录，程序不会静默跳过。");
    return ids;
  }
  function historyRecordLabel(session) {
    return sessionTimeText(session?.started_utc) + " · 数据编号 " + (session?.data_id || session?.settings?.data_id || "未编号") + " · " + (session?.session_id || "编号未记录");
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
    const force = definition.force_sign === -1 ? "−" : definition.force_sign === 1 ? "+" : null;
    return (definition.finger_id || "模型样件未记录") + " · " + (force ? "F = " + force + "(Fz − Fz₀)" : "力方向尚未核实");
  }
  function testReferenceText(reference, experimentRunning = false) {
    const source = reference || {};
    const scope = experimentRunning ? "本会话基线参考" : "本次实机验证参考";
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

  let mounted = false, latest = {}, busy = false, sessions = [], deletedSessions = [], models = [], tab = "session";
  let activePoint = "P01", lastModelKey = "", receivedAt = 0, expired = false, sessionsLoaded = false, modelsLoaded = false, pointLayoutKey = "", previousPreloadTimedOut = false;
  let observedSessionID = null, observedPointID = null, pendingPointTouched = false;
  let frozenSample = null, frozenSaved = false, frozenDiscarded = false, validationMapKey = "", validationModelKey = "";
  let targetPoint = null, judgementTarget = null;
  let completedTrainingKey = "", preferredModelID = null, validationResult = null, historyPendingAction = null;
  const selectedSessions = new Set();
  const $ = id => global.document.getElementById(id);
  const put = (id, value) => { const element = $(id); if (element) element.textContent = value; };
  const trainingMethodLabel = method => ({ linear: "加权最小二乘", mlp: "小型 MLP" }[method] || "方法待核实");
  function trainingView(source) {
    const status = ["idle", "running", "completed", "failed"].includes(source?.status) ? source.status : "idle";
    const progress = finite(source?.progress_percent) ? Math.min(100, Math.max(0, source.progress_percent)) : null;
    const complete = status === "completed" && progress === 100 && typeof source?.model_id === "string" && !!source.model_id;
    const iteration = Number.isInteger(source?.current_iteration) ? source.current_iteration : Number.isInteger(source?.epoch) ? source.epoch : null;
    const iterations = Number.isInteger(source?.max_iterations) ? source.max_iterations : Number.isInteger(source?.epochs) ? source.epochs : null;
    return { status, busy: source?.busy === true || status === "running", progress, complete,
      stage: source?.stage || (status === "idle" ? "请选择真实会话和模型方法，明确点击开始训练。" : "等待服务端阶段更新。"),
      error: source?.error || null, modelID: complete ? source.model_id : null,
      method: source?.method, methodLabel: source?.method_label || trainingMethodLabel(source?.method),
      created: source?.created_local_minute || (source?.created_utc ? sessionTimeText(source.created_utc) : null),
      points: Array.isArray(source?.point_ids) ? source.point_ids.filter(id => /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(id)) : [],
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
    return '<section id="expPanelTraining" class="panel experiment-section" role="tabpanel" aria-labelledby="expTabTraining" hidden><div class="training-layout"><div><h3>采集数据与历史记录</h3><label class="confirmation session-filter"><input id="expShowDeleted" type="checkbox"><span>查看已删除</span></label><p id="expSessionFilterState" class="display-note">默认显示全部采集记录，包括无合格按压、不完整与诊断记录。</p><p id="expHistorySelectionState" class="display-note">已选择 0 条记录</p><div id="expSessionList" class="experiment-session-list"><p class="experiment-empty">尚未读取会话列表</p></div><div class="display-action-row"><button id="expRefreshSessions" class="button subtle">刷新会话列表</button><button id="expSelectTrainable" class="button secondary" disabled>全选可训练数据</button><button id="expClearSessions" class="button subtle" disabled>清空选择</button><button id="expDeleteSessions" class="button history-delete" disabled>删除所选</button><button id="expRestoreSessions" class="button secondary" hidden disabled>恢复所选</button></div></div><div class="training-controls"><h3>训练模型</h3><label class="display-field" for="expTrainingMethod"><span>模型方法</span><select id="expTrainingMethod"><option value="linear">加权最小二乘</option><option value="mlp">小型 MLP</option></select></label><p class="display-note">两种方法均支持已采集的少量点（例如四个角点）。可多选兼容的 A / B 采集数据，合并合格整轮按压训练；每个训练点合计至少 3 轮。C 批、诊断与不完整记录不能训练；不兼容数据由服务端明确拒绝，不会静默跳过。训练后的识别范围以实际训练点为准。</p><button id="expTrainingStart" class="button primary" disabled>批量训练所选数据</button><section class="training-progress"><div class="guided-progress-label"><span id="expTrainingState">尚未开始训练</span><strong id="expTrainingPercent">—</strong></div><progress id="expTrainingProgress" max="100" value="0" aria-label="服务端真实训练进度"></progress><p id="expTrainingStage" role="status" aria-live="polite">等待训练任务</p><p id="expTrainingIteration" class="display-note"></p><p id="expTrainingError" class="experiment-statusline error" hidden></p><p class="display-note">进度来自文件核验、光谱补算和实际拟合回调；只有模型文件保存成功才显示 100%。</p></section><section id="expTrainingResult" class="training-result" hidden><h3>模型已保存</h3><dl class="experiment-dl"><div><dt>模型编号</dt><dd id="expTrainingModelID">—</dd></div><div><dt>模型方法</dt><dd id="expTrainingModelMethod">—</dd></div><div><dt>保存时间（北京时间）</dt><dd id="expTrainingModelTime">—</dd></div><div><dt>训练点位</dt><dd id="expTrainingModelPoints">—</dd></div></dl><button id="expGoValidation" class="button secondary" disabled>去实机验证</button><p class="display-note">到实机验证页后，手动载入模型并明确建立本次无接触参考，再实际按压验证。</p></section></div></div></section>';
  }
  function historyDialogHTML() {
    return '<div id="expHistoryDialog" class="history-dialog" role="dialog" aria-modal="true" aria-labelledby="expHistoryDialogTitle" hidden><div class="history-dialog-card"><h3 id="expHistoryDialogTitle">确认记录操作</h3><p id="expHistoryDialogSummary"></p><ul id="expHistoryDialogItems"></ul><p id="expHistoryDialogError" class="experiment-statusline error" hidden></p><div class="display-action-row"><button id="expHistoryCancel" class="button subtle">取消</button><button id="expHistoryConfirm" class="button secondary">确认操作</button></div></div></div>';
  }
  function testHTML() {
    return [
      '<section id="expPanelTest" class="panel experiment-section" role="tabpanel" aria-labelledby="expTabTest" hidden><div class="validation-layout"><div class="validation-preparation"><h3>① 准备实机验证</h3><p class="display-note">载入模型 → 明确建立本次无接触参考 → 点击要识别的目标点 → 在手指对应位置真实按压。橙色圈表示目标，绿色灯只表示模型识别。</p>',
      '<div class="model-select-row"><label class="display-field" for="expModelSelect"><span>研究 / 完整模型编号</span><select id="expModelSelect"><option value="">尚无可用模型</option></select></label><button id="expLoadModelBtn" class="button secondary" disabled>载入模型</button><button id="expRefreshModels" class="button subtle">刷新模型</button></div>',
      '<dl class="experiment-dl"><div><dt>当前模型</dt><dd id="expActiveModel">未加载</dd></div><div><dt>模型样件 / 力定义</dt><dd id="expModelDefinition">待加载</dd></div><div><dt>已训练点位</dt><dd id="expTrainedPoints">待加载</dd></div><div><dt>候选区分度</dt><dd id="expPredictionConfidence">—</dd></div></dl>',
      '<label class="confirmation"><input id="expTestUnloaded" type="checkbox"><span>确认当前无接触，建立本次实机验证光学参考</span></label><div class="display-action-row"><button id="expTestReferenceBtn" class="button secondary" disabled>建立本次无接触参考</button></div><div id="expTestReferenceState" class="experiment-statusline" role="status">本次实机验证参考尚未就绪</div><div id="expTruthZeroState" class="experiment-statusline">PX6D 真值仅用于对照；模型输入只有九路 FBG 特征。</div><p class="display-note">目标点只作人工对照，不输入模型。未训练点为灰色；无接触、不确定、超范围或陈旧结果不亮识别灯。</p>',
      '</div><div class="validation-live"><h3>② 选择目标点，实际按压观察</h3><div id="expTargetComparison" class="experiment-statusline">请先点击要识别的目标点位。</div><div id="expPredictionState" class="prediction-state">待加载模型与本次无接触参考</div><p id="expValidationAxes" class="validation-axis">待载入模型并核对其点阵定义；当前编号排列不声明空间方向。</p><div id="expValidationMap" class="validation-point-map" role="group" aria-label="5乘5实机识别矩阵，点击选目标，绿色灯仅为有效模型识别"></div><p id="expValidationMapState" class="display-note">橙色圈：选定目标；绿色灯：当前模型识别。两者独立。</p>',
      '<div class="prediction-grid"><div class="prediction-card"><span>模型识别位置</span><strong id="expPredictionPoint">—</strong><small>模型输出，与目标选择分开</small></div><div class="prediction-card"><span>模型按压力</span><strong id="expPredictionForce">—</strong><small>N</small></div><div class="prediction-card"><span>PX6D 按压力 · 仅对照</span><strong id="expPredictionTruth">—</strong><small>N</small></div><div class="prediction-card"><span>模型 − PX6D</span><strong id="expPredictionError">—</strong><small>N</small></div></div><p id="expPredictionReason" class="display-note">可以只选目标并观察识别灯与力差；需要计入统计时再勾选或保存判断。</p></div></div>',
      '<section class="validation-judgement-panel"><h3>本次判断（可选保存统计）</h3><p id="expFrozenStatus" class="display-note">尚未开始本次判断；选择目标并实际按压即可观察。</p><div class="validation-judgement-summary"><span>目标 <strong id="expJudgementTarget">—</strong></span><span>识别 <strong id="expFrozenPoint">—</strong></span><span>模型力 <strong id="expFrozenForce">—</strong> N</span><span>PX6D <strong id="expFrozenTruth">—</strong> N</span><span>差值 <strong id="expFrozenError">—</strong> N</span></div>',
      '<div class="validation-judgement"><label class="confirmation"><input id="expPositionCorrect" type="checkbox" disabled><span>位置识别正确</span></label><label class="confirmation"><input id="expForceCorrect" type="checkbox" disabled><span>力正确：绝对差值 ≤ 0.3 N</span></label><button id="expValidationSave" class="button primary" disabled>保存本次判断</button><button id="expValidationDiscard" class="button subtle" disabled>重新判断</button></div><p id="expJudgementNote" class="display-note">勾选后本次判断读数保持不变。保存时未勾选就表示不正确；两项都不勾也可保存失败按压。重新判断会放弃当前未保存判断，不计入分母。</p></section>',
      '<section class="validation-statistics"><div class="validation-result-heading"><h3>本轮实机验证结果</h3><button id="expValidationFinish" class="button primary" disabled>结束验证并查看结果</button></div><p id="expValidationReportState" class="display-note">结束后保存本轮真实判断统计；未完成判断请先保存或重新判断。</p><div id="expValidationStatsState" class="experiment-statusline">尚未实机验证</div><div id="expValidationStats" class="experiment-report-cards"></div><p class="display-note">位置与力分别统计正确次数 / 已判定有效按压次数。识别失败但采集证据有效的真实按压也可保存为失败；未判定或采集无效不进入分母。正确率只来自你实际按压并保存的人工判断。</p></section></section>'
    ].join("");
  }
  function trialForceHTML() {
    return '<div id="expLiveForceCard" class="experiment-live-force"><span>PX6D 当前按压力 / N</span><strong id="expLiveForceValue">—</strong><div id="expLiveForceRaw">原始 Fz：— N · 本试次零点：— N</div><div id="expLiveForceAge">最新六轴 · 非光谱配对</div><p id="expLiveForceReason">本试次基线与有效 PX6D 原始帧就绪后显示。</p></div>';
  }
  function captureHTML() {
    return [
      '<section id="expPanelSession" class="panel experiment-section" role="tabpanel" aria-labelledby="expTabSession"><div class="capture-session-card"><div class="capture-session-toolbar"><h3><span class="capture-number">1</span>采集会话</h3><div class="display-action-row"><button id="expStartBtn" class="button primary" disabled>建立采集会话</button><button id="expStopBtn" class="button subtle" disabled>停止并保存会话</button></div></div>',
      '<details id="expSessionSetup" class="capture-session-setup" open><summary>会话设置与安装确认<span>目标 3 N · 每点至少 3 轮</span></summary><div class="experiment-form-grid">',
      '<label class="display-field" for="expFingerID"><span>手指 / 样件标识</span><input id="expFingerID" type="text" placeholder="输入实际样件标识" maxlength="120"></label>',
      '<label class="display-field" for="expBatch"><span>独立批次</span><select id="expBatch"><option value="A">A 批 · 拟合</option><option value="B">B 批 · 验证</option><option value="C">C 批 · 留出测试</option></select></label>',
      '<label class="display-field" for="expRepetitions"><span>每点合格重复次数</span><input id="expRepetitions" type="number" min="3" max="999" step="1" value="3"></label>',
      '<label class="display-field" for="expForceSign"><span>法向按压力方向</span><select id="expForceSign"><option value="-1">−Fz 为按压力</option><option value="1">+Fz 为按压力</option></select></label>',
      '<label class="display-field" for="expDataID"><span>数据编号</span><input id="expDataID" type="text" maxlength="128" placeholder="填写本次数据编号，可保留中文与前导零"></label>',
      '<label class="display-field" for="expTemperature"><span>环境温度 / °C</span><input id="expTemperature" type="number" step="any" placeholder="可留空"></label>',
      '<label class="display-field experiment-wide" for="expFixation"><span>装夹条件</span><input id="expFixation" type="text" placeholder="记录夹持位置、朝向与装夹方式" maxlength="500"></label></div>',
      '<label class="confirmation"><input id="expDirectionConfirmed" type="checkbox"><span id="expDirectionDescription">确认当前安装：向下按压时 Fz 更负，F = −(Fz − Fz₀)；指腹朝上对应 −PX6D Z</span></label>',
      '<label class="confirmation"><input id="expCoordinatesConfirmed" type="checkbox"><span>已核对点位：左下为第 0 行、第 0 列，+x 向右，+y 向上至指尖</span></label>',
      '</details></div><div class="capture-layout">',
      '<aside class="capture-point-panel"><h3><span class="capture-number">2</span>选择并确认采集点位</h3><div class="point-map-caption"><span id="expXAxisDirection">+x / +u → 右</span><span id="expYAxisDirection">+y / +v ↑ 指尖</span></div><div id="expPointMap" class="point-map" role="group" aria-label="25 点归一化接触位置"></div>',
      '<div id="expPointMapDefinition" class="point-map-side">原点：左下 P01（第 0 行、第 0 列）；+x 向右、+y 向上至指尖，指腹朝上对应 −PX6D Z。</div>',
      '<div class="experiment-statusline capture-selected-point"><strong id="expSelectedPoint">P01 · 行 0 / 列 0</strong><span id="expSelectedCoordinates">x/u = 0.00，y/v = 0.00</span><div id="expConfirmedPoint">尚未确认采集点位</div></div>',
      '<button id="expConfirmPointBtn" class="button secondary" disabled>确认采集点位</button><p class="display-note">网格只预选，须点击确认；活动轮次不能切换。</p>',
      '<details class="capture-session-details"><summary>会话编号与保存位置</summary><dl class="experiment-dl"><div><dt>会话编号</dt><dd id="expSessionID">尚未建立</dd></div><div><dt>当前批次</dt><dd id="expCurrentBatch">—</dd></div><div><dt>运行状态</dt><dd id="expSessionState">等待设备</dd></div></dl><p id="expSessionPath" class="experiment-path">会话路径将在记录开始后显示</p></details></aside>',
      '<div class="capture-step-card capture-round-panel"><h3><span class="capture-number">3</span>本轮标定与采集</h3><div id="expGuidedMessage" class="guided-instruction" role="status" aria-live="polite">待开始：先建立会话并确认采集点位。</div>',
      '<div id="expPreloadStatus" class="preload-status" hidden><div class="guided-progress-label"><span>连续有效无载同步</span><strong id="expPreloadProgressText">等待服务端诊断</strong></div><progress id="expPreloadProgress" max="500" value="0" aria-label="连续有效无载同步时长"></progress><div class="preload-meta"><span id="expPreloadElapsed">已等待：—</span><span id="expPreloadResets">计时重置：—</span></div><p id="expPreloadBlockReason" role="status" aria-live="polite">等待服务端预采样诊断。</p><strong id="expPreloadTimeout" hidden>预采样超时，本轮失败且不计数。请保持无接触，重新明确确认无载并手动标定本轮。</strong><small>连续时长仅由服务端根据真实九峰与六轴同步结果累计；界面计时不会放行加载。</small></div>',
      '<div class="guided-round-meta"><strong id="expRoundLabel">当前轮次：待开始</strong><span id="expAttemptLabel">当前点尝试 0 次</span></div>',
      '<label class="confirmation"><input id="expBaselineConfirmed" type="checkbox"><span>已移开接触棒，确认当前无接触；本轮重新建立六轴零点与九峰参考</span></label>',
      '<div class="display-action-row capture-round-actions"><button id="expBaselineBtn" class="button secondary" disabled>标定本轮</button><button id="expTrialStart" class="button primary" disabled>开始本轮采集</button><button id="expTrialAbort" class="button subtle" disabled>中止本轮</button></div>',
      '<div id="expBaselineState" class="experiment-statusline">本轮无载基线尚未就绪</div>',
      '<div class="guided-progress-group"><div class="guided-progress-label"><span>25 点总采集进度</span><strong id="expTotalProgressText">0 / 0 · 0%</strong></div><progress id="expTotalProgress" max="100" value="0" aria-label="25点总采集进度"></progress>',
      '<div class="guided-progress-label"><span id="expPointProgressLabel">当前采集点进度</span><strong id="expPointProgressText">0 / 0 · 0%</strong></div><progress id="expPointProgress" max="100" value="0" aria-label="当前点合格轮次进度"></progress>',
      '<div class="guided-progress-label"><span>本轮加载到 3 N</span><strong id="expForceProgressText">0%</strong></div><progress id="expForceProgress" max="100" value="0" aria-label="服务端当前加载进度"></progress></div>',
      trialForceHTML(), '<div id="expRoundQuality" class="experiment-statusline">尚无本轮判定；合格次数只由服务端质量检查计入。</div>',
      '<details class="capture-record-details"><summary>操作步骤、原始记录与同步质量</summary><ol class="guided-flow"><li>确认无载并手动标定本轮</li><li>点击开始，保持无接触等待提示加载</li><li>缓慢加载至 3 N，按提示卸载</li><li>移开接触棒，等待判定；每轮重新标定</li></ol><dl class="experiment-dl"><div><dt>采集点</dt><dd id="expTrialPoint">—</dd></div><div><dt>本轮试次编号</dt><dd id="expTrialID">尚未开始</dd></div><div><dt>当前阶段</dt><dd id="expTrialPhase">等待本轮</dd></div><div><dt>记录状态</dt><dd id="expWriterStatus">等待会话</dd></div></dl>',
      '<div class="experiment-counters"><div class="experiment-counter"><span>原始光谱</span><strong id="expSpectraCount">0</strong></div><div class="experiment-counter"><span>原始六轴</span><strong id="expForceCount">0</strong></div><div class="experiment-counter"><span>有效 / 无效同步</span><strong id="expSyncCount">0 / 0</strong></div><div class="experiment-counter"><span>完成 / 中止试次</span><strong id="expTrialCount">0 / 0</strong></div></div><p class="display-note">检查通过才计数，失败或中止不计数；原始数据连续保留。</p></details></div></div></section>'
    ].join("");
  }
  function html() {
    return [
      '<div class="experiment-heading"><div><h2>采集、训练、实机验证</h2><p>采集真实按压 → 选择方法训练 → 点选目标并实际按压验证</p></div><span id="expRecordBadge" class="quality-pill neutral">等待会话</span></div>',
      '<div id="expCompatibilityNotice" class="experiment-statusline error" role="alert" hidden></div>',
      '<div class="experiment-tabs" role="tablist" aria-label="实验工作区"><button class="experiment-tab" id="expTabSession" role="tab" aria-selected="true" aria-controls="expPanelSession">① 采集</button><button class="experiment-tab" id="expTabTraining" role="tab" aria-selected="false" aria-controls="expPanelTraining">② 训练</button><button class="experiment-tab" id="expTabTest" role="tab" aria-selected="false" aria-controls="expPanelTest">③ 实机验证</button></div>',
      '<div id="expMessage" class="experiment-statusline" role="status" aria-live="polite">先连接光谱与 PX6D，核对无载参考后建立采集会话。</div>',
      captureHTML(), trainingHTML(), testHTML(), historyDialogHTML()
    ].join("");
  }
  function message(text, tone = "") {
    put("expMessage", text); $("expMessage").className = "experiment-statusline" + (tone ? " " + tone : "");
  }
  async function request(path, body, timeoutMs = 1500) {
    if (body !== undefined && !backendCompatible(latest) && !["/api/experiment/stop", "/api/disconnect"].includes(path)) throw new Error(compatibilityMessage(latest));
    const controller = typeof global.AbortController === "function" ? new global.AbortController() : null;
    const timer = controller ? global.setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await global.fetch(path, body === undefined ? { cache: "no-store", ...(controller ? { signal: controller.signal } : {}) } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), ...(controller ? { signal: controller.signal } : {}) });
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(result.detail || result.error || result.message || "操作未完成（" + response.status + "）");
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("本机服务响应超时，请检查状态后再操作。");
      throw error;
    } finally { if (timer !== null) global.clearTimeout(timer); }
  }
  async function run(callback) {
    if (busy) return;
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
    if (next === "training" && !sessionsLoaded) run(refreshSessions);
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
    for (const id of selectedSessions) if (!available.has(id)) selectedSessions.delete(id);
    renderSessions();
  }
  function renderSessions() {
    const visible = historyRows(), deleted = $("expShowDeleted").checked, count = value => Number.isInteger(value) && value >= 0 ? value : "—";
    $("expSessionList").innerHTML = visible.length ? visible.map(session => {
      const batch = session.settings?.batch || session.batch || "—", status = historyTrainingStatus(session);
      const checked = selectedSessions.has(session.session_id), points = sessionPointSummary(session);
      const pointMarkup = points.length ? '<span class="session-point-counts">' + points.map(point => '<span>' + escapeHTML(point.id) + ' · ' + escapeHTML(point.location) + ' · 合格 ' + (point.accepted ?? "—") + ' / 尝试 ' + point.attempts + ' 轮</span>').join("") + '</span>' : '<small>尚无逐点合格按压记录</small>';
      const eligibility = deleted ? (session.restorable === true ? "已删除 · 可恢复" : "已删除 · 恢复受限") : status.allowed ? "可训练" : "不可训练：" + status.reason;
      return '<div class="experiment-session-row history-record-row' + (status.allowed ? " qualified" : " unqualified") + '"><input type="checkbox" aria-label="选择 ' + escapeHTML(historyRecordLabel(session)) + '" data-session-id="' + escapeHTML(session.session_id) + '"' + (checked ? " checked" : "") + '><div class="history-record-info"><strong class="session-title">样件 ' + escapeHTML(session.settings?.finger_id || "未记录") + ' · ' + escapeHTML(batch) + ' 批 · 数据编号 ' + escapeHTML(session.data_id || session.settings?.data_id || '未编号') + ' · 合格按压 ' + count(session.accepted_trial_count) + ' 轮</strong><small>开始 ' + escapeHTML(sessionTimeText(session.started_utc)) + ' · 结束 ' + escapeHTML(sessionTimeText(session.ended_utc)) + '</small><small>' + (session.complete ? "文件已完整保存" : "文件完整性待确认") + ' · 总尝试 ' + count(session.trial_count) + ' 轮 · 未通过 ' + count(session.rejected_trial_count) + ' 轮' + (Number.isInteger(session.repetitions_per_point) ? ' · 目标每点 ' + session.repetitions_per_point + ' 轮' : '') + (session.acquisition_complete === true ? ' · 25 点采集已达次数' : ' · 25 点采集尚未完成') + '</small>' + pointMarkup + '<small class="history-eligibility">' + escapeHTML(eligibility) + '</small>' + (session.history_error ? '<small class="history-record-error">' + escapeHTML(session.history_error) + '</small>' : '') + '<small class="session-id">' + escapeHTML(session.session_id) + '</small><small class="history-record-path">' + escapeHTML(session.path || "保存位置未记录") + '</small></div><button type="button" class="button subtle history-open" data-open-session="' + escapeHTML(session.session_id) + '" data-deleted="' + deleted + '">打开保存位置</button></div>';
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
    if (busy || historyMutationBlocked()) { message("采集或训练进行中，或服务状态尚未就绪，不能删除或恢复记录。", "error"); return; }
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
  function renderModels() {
    const newest = models.slice().sort((a, b) => String(b.created_utc || b.created_local_minute || b.model_id).localeCompare(String(a.created_utc || a.created_local_minute || a.model_id)))[0]?.model_id || "";
    const selected = preferredModelID || $("expModelSelect").value || newest;
    const nextKey = JSON.stringify(models);
    if (nextKey !== lastModelKey) {
      $("expModelSelect").innerHTML = '<option value="">选择已保存模型</option>' + models.map(model => '<option value="' + escapeHTML(model.model_id) + '">' + escapeHTML(modelOptionLabel(model)) + '</option>').join("");
      lastModelKey = nextKey;
    }
    $("expModelSelect").value = models.some(model => model.model_id === selected) ? selected : newest;
    if (preferredModelID && models.some(model => model.model_id === preferredModelID)) preferredModelID = null;
    updateControls();
  }
  async function refreshModels(preferred = null) {
    if (preferred) preferredModelID = preferred;
    const result = await request("/api/analysis/models");
    models = Array.isArray(result.models) ? result.models : [];
    modelsLoaded = true; renderModels();
  }
  function renderTraining(analysis) {
    const view = trainingView(analysis?.training);
    put("expTrainingState", ({ idle: "尚未开始训练", running: "正在训练", completed: view.complete ? "训练完成 · 模型已保存" : "等待确认模型保存完成", failed: "训练失败" })[view.status]);
    put("expTrainingPercent", view.progress === null ? "—" : number(view.progress, 0) + "%");
    $("expTrainingProgress").hidden = view.progress === null;
    $("expTrainingProgress").value = view.progress ?? 0;
    put("expTrainingStage", view.stage);
    put("expTrainingIteration", (view.iteration !== null ? "实际迭代 " + view.iteration + (view.iterations !== null ? " / " + view.iterations : "") : "") + (view.fold !== null ? " · 整轮验证折 " + view.fold + (view.folds !== null ? " / " + view.folds : "") : ""));
    $("expTrainingError").hidden = !view.error; put("expTrainingError", view.error || "");
    $("expTrainingResult").hidden = !view.complete;
    put("expTrainingModelID", view.modelID || "—"); put("expTrainingModelMethod", view.methodLabel);
    put("expTrainingModelTime", view.created || "等待服务端保存时间"); put("expTrainingModelPoints", view.points.length ? view.points.join(" / ") : "等待模型点位定义");
    if (view.complete && completedTrainingKey !== (analysis.training.job_id || view.modelID)) {
      completedTrainingKey = analysis.training.job_id || view.modelID;
      preferredModelID = view.modelID;
      refreshModels(view.modelID).catch(error => message("模型已保存，刷新列表失败：" + error.message, "error"));
      message("训练完成，模型已保存。可前往实机验证，手动载入后建立本次无接触参考。", "success");
    }
  }
  function updateControls() {
    if (!mounted) return;
    const compatible = backendCompatible(latest);
    const gates = experimentGates(latest, busy || expired, activePoint), analysisBusy = busy || latest.analysis?.busy === true || trainingView(latest.analysis?.training).busy || !compatible;
    const buttonGates = { expStartBtn: gates.start, expStopBtn: gates.stop, expConfirmPointBtn: gates.pointConfirm, expBaselineBtn: gates.baseline && $("expBaselineConfirmed").checked, expTrialStart: gates.trialStart, expTrialAbort: gates.abort };
    for (const [id, enabled] of Object.entries(buttonGates)) $(id).disabled = !enabled;
    for (const button of $("expPointMap").querySelectorAll("button")) button.disabled = !gates.point;
    const running = latest.experiment?.running === true;
    for (const id of ["expFingerID", "expBatch", "expForceSign", "expRepetitions", "expDataID", "expTemperature", "expFixation", "expDirectionConfirmed", "expCoordinatesConfirmed"]) $(id).disabled = busy || running;
    $("expBaselineConfirmed").disabled = !gates.baseline;
    const selected = checkedSessionIDs();
    $("expTrainingStart").disabled = analysisBusy || expired || running || !!historyPendingAction || $("expShowDeleted").checked || !selected.length;
    $("expTrainingMethod").disabled = analysisBusy || running || !!historyPendingAction;
    $("expGoValidation").disabled = busy || !compatible || !trainingView(latest.analysis?.training).complete;
    $("expLoadModelBtn").disabled = analysisBusy || running || hasPendingJudgement() || !$("expModelSelect").value;
    $("expTestReferenceBtn").disabled = !compatible || busy || expired || latest.running !== true || running || !$("expTestUnloaded").checked;
    $("expTestUnloaded").disabled = !compatible || busy || expired || running || latest.running !== true;
    const staleValidation = expired || !receivedAt || Date.now() - receivedAt > 1500;
    const live = validationLiveView(latest.analysis, staleValidation || !compatible || latest.running !== true || running);
    const frozen = frozenSampleView(frozenSample), pending = !!frozen && !frozenSaved && !frozenDiscarded;
    const judge = compatible && !busy && !running && pending && frozen.valid && frozen.model === latest.analysis?.model_id && !!judgementTarget;
    const begin = compatible && !busy && !running && !pending && !latest.validation?.pending_sample && !!targetPoint && live.comparisonReady;
    $("expValidationSave").disabled = !(judge || begin);
    $("expValidationDiscard").disabled = !compatible || busy || !frozen || frozen.model !== latest.analysis?.model_id;
    const selectedView = pending ? frozen : live, selectedTarget = pending ? judgementTarget : targetPoint;
    $("expPositionCorrect").disabled = !(judge || begin) || !(pending ? frozen.inferenceValid : live.valid) || selectedView.point !== selectedTarget;
    $("expForceCorrect").disabled = !(judge || begin) || !(pending ? frozen.inferenceValid : live.valid) || !forceWithinTolerance(selectedView.error);
    for (const button of $("expValidationMap").querySelectorAll("button")) button.disabled = !compatible || busy || pending;
    $("expValidationFinish").disabled = !compatible || busy || running || pending || !latest.analysis?.model_id || validationResultView(latest.validation).total === 0;
    $("expCompatibilityNotice").hidden = compatible;
    put("expCompatibilityNotice", compatible ? "" : compatibilityMessage(latest));
    if (!compatible) {
      $("expBaselineConfirmed").checked = false;
      put("expGuidedMessage", compatibilityMessage(latest));
      $("expGuidedMessage").className = "guided-instruction retry";
    }
    $("expRefreshSessions").disabled = $("expRefreshModels").disabled = busy;
    const deletedView = $("expShowDeleted").checked, chosen = selected.map(id => historyRows().find(row => row.session_id === id));
    const historyBlocked = busy || historyMutationBlocked() || !!historyPendingAction;
    $("expSelectTrainable").disabled = busy || !!historyPendingAction || deletedView || !sessions.some(row => historyTrainingStatus(row).allowed);
    $("expClearSessions").disabled = busy || !!historyPendingAction || !selected.length;
    $("expDeleteSessions").hidden = deletedView; $("expRestoreSessions").hidden = !deletedView;
    $("expDeleteSessions").disabled = historyBlocked || deletedView || !selected.length || chosen.some(row => row?.deletable !== true);
    $("expRestoreSessions").disabled = historyBlocked || !deletedView || !selected.length || chosen.some(row => row?.restorable !== true);
    $("expShowDeleted").disabled = busy || !!historyPendingAction;
    $("expRefreshSessions").disabled = busy || !!historyPendingAction;
    const invalidTraining = chosen.filter(row => !historyTrainingStatus(row).allowed);
    put("expHistorySelectionState", "已选择 " + selected.length + " 条记录" + (deletedView ? " · 回收区记录不能训练，恢复后再选择。" : invalidTraining.length ? " · 包含 " + invalidTraining.length + " 条不可训练记录；训练时将明确报错，请先取消这些项。" : " · 可多选兼容的 A / B 数据一并训练。"));
    for (const input of $("expSessionList").querySelectorAll("input[data-session-id]")) input.disabled = busy || !!historyPendingAction;
    $("expHistoryConfirm").disabled = busy || historyMutationBlocked() || !historyPendingAction;
    $("expHistoryCancel").disabled = busy;
    if (expired) {
      // Stop remains an explicit operator action while stale stage changes are blocked.
      $("expStopBtn").disabled = busy || (!running && !legacyRawRecording(latest));
    }
  }
  function renderLiveForce(force, elapsed = 0) {
    const view = liveForceView(force, elapsed);
    $("expLiveForceCard").className = "experiment-live-force" + (view.outside ? " over-range" : "");
    put("expLiveForceValue", number(view.force));
    put("expLiveForceRaw", "原始 Fz：" + number(view.raw) + " N · 本试次零点：" + number(view.zero) + " N");
    put("expLiveForceAge", (view.age !== null ? "距接收 " + number(view.age, 0) + " ms · " : "") + "最新六轴 · 非光谱配对");
    put("expLiveForceReason", view.outside ? "已超过 3 N 暂定范围，保留真实数值；请结束加载并记录该情况。" : view.valid ? "来自本次服务端无载基线，与上方浏览器显示置零独立；不是模型预测。" : view.reason);
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
    put("expPointMapDefinition", layout.current ? "原点左下 P01（第 0 行、第 0 列），顶部为行 4；指腹朝上对应 −PX6D Z。" : layout.known ? "历史会话按其已存点阵保留行列与顺序，不套用新原点。" : "历史点阵方向未核实；只保留原始编号，不推定坐标。");
  }
  function renderPointSelection(layout) {
    const point = layout.points.find(point => point.id === activePoint) || layout.points[0];
    activePoint = point.id;
    for (const button of $("expPointMap").querySelectorAll("button")) button.setAttribute("aria-pressed", String(button.dataset.pointId === activePoint));
    put("expSelectedPoint", point.id + (layout.known ? " · " + (layout.current ? "" : "历史 ") + "行 " + point.row + " / 列 " + point.col : " · 历史坐标方向未核实"));
    put("expSelectedCoordinates", layout.known ? (layout.current ? "x/u" : "已存 u") + " = " + number(point.u, 2) + "，" + (layout.current ? "y/v" : "已存 v") + " = " + number(point.v, 2) : "保留原始点编号，坐标待核实。");
    const confirmed = latest.experiment?.point_id;
    put("expConfirmedPoint", confirmed ? "服务端已确认采集点：" + confirmed + (confirmed !== activePoint ? "；当前预选尚未确认" : "") : "当前仅预选；尚未确认采集点位");
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
    put("expTrainedPoints", view.trained.length ? view.trained.join(" / ") + (analysis.model_kind === "pilot" ? " · 少量点研究模型" : " · 完整模型") : "待加载模型点位定义");
    put("expTargetComparison", targetComparison(targetPoint, view));
    put("expValidationMapState", stale ? "实时状态已失效，识别灯已熄灭；本次已固定的判断读数保留。" : "橙色圈是选定目标，绿色灯仅为模型识别，目标不输入模型。" + (targetPoint && !trained.has(targetPoint) ? " 当前目标未训练，可观察并记录识别失败。" : ""));
    return view;
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
    put("expJudgementNote", "保存时未勾选就表示不正确；两项都不勾也可保存失败按压。" + (view && finite(view.error) && !forceWithinTolerance(view.error) ? " 本次绝对力差超过 0.3 N，不能勾选“力正确”。" : " 力正确须手动核对绝对差值 ≤ 0.3 N。") + "已固定读数不会随新帧改变；点击“重新判断”会放弃当前未保存判断，不计入分母。");
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
    if (!live.comparisonReady) throw new Error("当前没有可核验的九峰与 PX6D 同步按压；请核对本次参考与设备状态，实际按压力须大于 0.05 N。识别失败但数据有效时可保存为失败判断。");
    const selectedTarget = targetPoint;
    const result = await request("/api/validation/freeze", { actual_point_id: selectedTarget });
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
    if (force && !forceWithinTolerance(sample.error)) {
      $("expForceCorrect").checked = false;
      throw new Error("本次绝对力差超过 0.3 N，不能保存为“力正确”。");
    }
    $("expPositionCorrect").checked = position; $("expForceCorrect").checked = force;
    return sample;
  }
  function renderValidationStatistics(validation) {
    const stats = validationResultView(validation), pct = value => value === null ? "—" : number(value * 100, 1) + "%";
    put("expValidationStatsState", stats.total ? (validation?.finished ? "本轮验证已结束：" : "本轮已保存 ") + stats.total + " 次有效实机按压；位置与力分别统计。" : "尚未实机验证：本轮没有已保存判断，正确率为空。");
    const cards = [["位置正确 / 已判定", stats.position + " / " + stats.total + " · " + pct(stats.positionRate)], ["力正确 / 已判定", stats.force + " / " + stats.total + " · " + pct(stats.forceRate)], ["按压力 MAE / N", number(stats.mae)], ["按压力 RMSE / N", number(stats.rmse)], ["模型拒识 / 已判定", stats.rejected + " / " + stats.total], ["误差有效样本", stats.errors ?? "—"], ["未判定（不入分母）", stats.pending], ["无效（不入分母）", stats.invalid]];
    $("expValidationStats").innerHTML = cards.map(([label, value]) => '<div class="experiment-report-card">' + escapeHTML(label) + '<strong>' + escapeHTML(value) + '</strong></div>').join("");
    const saved = validation?.latest_report || (validationResult?.model_id === latest.analysis?.model_id ? validationResult : null);
    put("expValidationReportState", saved ? (validation?.finished ? "本轮报告已保存" : "上次完成的验证报告") + " · " + sessionTimeText(saved.created_utc) + "（北京时间） · " + (saved.report_id || "") + (saved.path ? " · " + saved.path : "") + (validation?.finished ? "；下一次实际判断将建立新轮次。" : "；上方为当前轮次统计。") : "结束后保存本轮真实判断统计；未完成判断请先保存或重新判断。力误差只统计有限的有效预测；拒识仍计入位置与力正确率分母。");
    $("expValidationFinish").textContent = validation?.finished ? "查看已保存结果" : "结束验证并查看结果";
  }
  function bindPreferences() {
    const preferences = global.NineFBGPreferences;
    if (!preferences?.bind) return;
    const confirmations = ["expDirectionConfirmed", "expCoordinatesConfirmed", "expBaselineConfirmed", "expTestUnloaded"];
    for (const id of confirmations) $(id).checked = false;
    preferences.bind("experiment", {
      finger_id: { id: "expFingerID", type: "string" }, batch: { id: "expBatch", type: "string" },
      force_sign: { id: "expForceSign", type: "number" }, data_id: { id: "expDataID", type: "string" },
      temperature_C: { id: "expTemperature", type: "number" }, fixation: { id: "expFixation", type: "string" },
      repetitions_per_point: { id: "expRepetitions", type: "number" }
    }, saved => {
      for (const id of confirmations) $(id).checked = false;
      if (!latest.experiment?.running && !pendingPointTouched && /^P(?:0[1-9]|1[0-9]|2[0-5])$/.test(saved.selected_point_id || "")) activePoint = saved.selected_point_id;
      renderPointSelection(pointLayout(latest.experiment?.settings, !!latest.experiment?.session_id));
      put("expDirectionDescription", "请按当前安装核对法向方向：按压力 F = " + ($("expForceSign").value === "-1" ? "−" : "+") + "(Fz − Fz₀)；指腹朝上对应 −PX6D Z");
      updateControls();
    }).catch(error => message("恢复采集设置失败：" + error.message, "error"));
    preferences.bind("analysis", { method: { id: "expTrainingMethod", type: "string" } }, () => { if (!["linear", "mlp"].includes($("expTrainingMethod").value)) $("expTrainingMethod").value = "linear"; updateControls(); })
      .catch(error => message("恢复训练设置失败：" + error.message, "error"));
  }
  function renderGuided(experiment) {
    const guided = guidedView(experiment), preload = preloadView(experiment);
    if (preload.timedOut && !previousPreloadTimedOut) $("expBaselineConfirmed").checked = false;
    previousPreloadTimedOut = preload.timedOut;
    put("expGuidedMessage", guided.message);
    $("expGuidedMessage").className = "guided-instruction" + (guided.complete ? " success" : guided.step === "retry" ? " retry" : "");
    put("expRoundLabel", guided.enabled && experiment.point_id ? experiment.point_id + " · 第 " + guided.round + " / " + guided.repetitions + " 轮" : "当前轮次：待开始");
    put("expAttemptLabel", "当前点尝试 " + guided.pointAttempts + " 次 · 已通过 " + guided.pointAccepted + " 次");
    put("expTotalProgressText", guided.totalAccepted + " / " + guided.totalRequired + " · " + number(guided.totalPercent, 0) + "%");
    put("expPointProgressLabel", (experiment.point_id || "当前采集点") + " · 合格轮次进度");
    put("expPointProgressText", guided.pointAccepted + " / " + guided.repetitions + " · " + number(guided.pointPercent, 0) + "%");
    put("expForceProgressText", number(guided.forcePercent, 0) + "% · 目标 3 N");
    for (const [id, value] of [["expTotalProgress", guided.totalPercent], ["expPointProgress", guided.pointPercent], ["expForceProgress", guided.forcePercent]]) { $(id).value = value; $(id).setAttribute("aria-valuenow", String(value)); }
    put("expRoundQuality", roundQualityText(guided.lastQuality));
    $("expRoundQuality").className = "experiment-statusline" + (guided.lastQuality?.accepted === true ? " success" : guided.lastQuality?.accepted === false ? " error" : "");
    $("expPreloadStatus").hidden = !preload.visible;
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
  function render(state) {
    if (!state) {
      if (!mounted) return;
      expired = true; invalidateLiveValidation("本机服务状态更新已中断，实时识别失效；本次已固定的判断读数保留。");
      updateControls(); return;
    }
    latest = state;
    receivedAt = Date.now(); expired = !state;
    if (!mounted) return;
    const experiment = latest.experiment || {}, analysis = latest.analysis || {}, gates = experimentGates(latest, busy);
    const layout = pointLayout(experiment.settings, !!experiment.session_id);
    if (observedSessionID !== (experiment.session_id || null)) $("expSessionSetup").open = !(experiment.session_id && experiment.running);
    if (observedSessionID !== (experiment.session_id || null) || observedPointID !== (experiment.point_id || null)) {
      if (experiment.point_id) activePoint = experiment.point_id;
      observedSessionID = experiment.session_id || null; observedPointID = experiment.point_id || null;
    }
    renderPointMap(layout);
    renderPointSelection(layout); renderGuided(experiment);
    put("expSessionID", experiment.session_id || "尚未建立"); put("expCurrentBatch", experiment.settings?.batch || "—"); put("expSessionState", experiment.running ? "正在连续记录" : experiment.status === "stopped" ? "已停止记录" : "等待会话"); put("expSessionPath", experiment.path || "会话路径将在记录开始后显示");
    put("expRecordBadge", experiment.error ? "记录异常" : experiment.running ? "会话记录中" : "等待会话"); $("expRecordBadge").className = "quality-pill " + (experiment.error ? "bad" : experiment.running ? "good" : "neutral");
    put("expTrialPoint", experiment.point_id || "—"); put("expTrialID", experiment.trial_id || "尚未开始"); put("expTrialPhase", phaseLabel(experiment.phase)); put("expWriterStatus", experiment.error || (experiment.running ? "原始光谱 / 六轴 / 结果 / 事件连续保存" : "等待会话"));
    renderLiveForce(experiment.live_force);
    const baseline = experiment.baseline || {};
    put("expBaselineState", (baseline.ready || baseline.status === "ready" ? "本轮无载基线已就绪" : baseline.status === "collecting" ? "正在采集并核验本轮基线" : "本轮无载基线未就绪") + (baseline.reason ? " · " + baseline.reason : ""));
    const counts = experiment.counts || {};
    put("expSpectraCount", counts.raw_spectra ?? 0); put("expForceCount", counts.raw_force ?? 0); put("expSyncCount", (counts.sync_valid ?? 0) + " / " + (counts.sync_invalid ?? 0)); put("expTrialCount", (counts.trials_completed ?? 0) + " / " + (counts.trials_aborted ?? 0));
    if (experiment.error || analysis.error) message(experiment.error || analysis.error, "error");
    renderTraining(analysis);
    if (Array.isArray(analysis.models)) { models = analysis.models; renderModels(); }
    put("expActiveModel", analysis.model_id || "未加载");
    put("expModelDefinition", modelDefinitionText(analysis.model_definition));
    put("expTestReferenceState", testReferenceText(analysis.test_reference, experiment.running === true));
    $("expTestReferenceState").className = "experiment-statusline" + (analysis.test_reference?.ready === true && analysis.test_reference?.pending !== true ? " success" : "");
    const truthZero = analysis.truth_zero || {};
    put("expTruthZeroState", truthZero.reason || (truthZero.ready ? "测试真值六轴零点已就绪" : truthZero.count ? "正在收集测试真值零点 " + truthZero.count + "/12 帧" : "光学参考必需；PX6D 未连接时可仅使用九路 FBG 测试。"));
    const prediction = validationLiveView(analysis, expired || !backendCompatible(latest) || latest.running !== true);
    put("expPredictionState", prediction.status); put("expPredictionPoint", prediction.point || "—"); put("expPredictionForce", number(prediction.force)); put("expPredictionTruth", number(prediction.truth)); put("expPredictionError", number(prediction.error));
    put("expPredictionConfidence", prediction.confidence == null ? "—" : number(prediction.confidence, 3)); put("expPredictionReason", prediction.reason);
    const validation = latest.validation || {}, nextValidationModel = analysis.model_sha256 || analysis.model_id || "";
    if (nextValidationModel !== validationModelKey) {
      validationModelKey = nextValidationModel; frozenSample = null; frozenSaved = false; frozenDiscarded = false; validationResult = null;
      $("expPositionCorrect").checked = false; $("expForceCorrect").checked = false; targetPoint = null; judgementTarget = null;
    }
    adoptFrozenSample(validation.pending_sample);
    renderValidationMap(analysis, expired || !backendCompatible(latest) || latest.running !== true);
    renderFrozenSample(); renderValidationStatistics(validation);
    updateControls();
  }
  function refreshFreshness() {
    if (!mounted) return;
    if (!expired) renderLiveForce(latest.experiment?.live_force, Math.max(0, Date.now() - receivedAt));
    if (expired || !receivedAt || Date.now() - receivedAt <= 1500) return;
    expired = true;
    invalidateLiveValidation("超过 1.5 秒未收到状态更新，等待本机服务恢复。");
    put("expGuidedMessage", "状态已过期，暂停本轮操作；等待服务端恢复后核对采集状态。");
    $("expGuidedMessage").className = "guided-instruction retry";
    put("expForceProgressText", "状态已过期 · 最后服务端加载进度");
    if (!$("expPreloadStatus").hidden) put("expPreloadBlockReason", "状态已过期；上方为最后服务端预采样诊断，等待更新后核对。");
    message("状态更新已中断，试次阶段操作已停用；可尝试停止会话。", "error");
    updateControls();
  }
  function invalidateLiveValidation(reason) {
    if (trainingView(latest.analysis?.training).busy) put("expTrainingStage", "状态更新已中断；上方为最后服务端训练进度，等待恢复。");
    put("expPredictionState", "状态过期 · 预测与真值已失效");
    for (const id of ["expPredictionPoint", "expPredictionForce", "expPredictionTruth", "expPredictionError", "expPredictionConfidence"]) put(id, "—");
    put("expPredictionReason", reason);
    renderValidationMap(latest.analysis || {}, true);
    put("expTestReferenceState", "测试参考状态已过期，等待服务更新。"); $("expTestReferenceState").className = "experiment-statusline";
  }
  function mount() {
    if (mounted || !global.document || !$("experimentWorkspace")) return;
    $("experimentWorkspace").innerHTML = html(); mounted = true;
    renderPointMap(pointLayout(null));
    for (const name of ["session", "training", "test"]) $("expTab" + name[0].toUpperCase() + name.slice(1)).addEventListener("click", () => switchTab(name));
    $("expPointMap").addEventListener("click", event => {
      const button = event.target.closest("button[data-point-id]"); if (!button || button.disabled) return;
      activePoint = button.dataset.pointId;
      pendingPointTouched = true;
      global.NineFBGPreferences?.save?.("experiment", { selected_point_id: activePoint });
      renderPointSelection(pointLayout(latest.experiment?.settings, !!latest.experiment?.session_id));
      updateControls();
      message("已预选 " + activePoint + "；会话建立后点击“确认采集点位”。");
    });
    $("expConfirmPointBtn").addEventListener("click", () => run(async () => {
      const result = await request("/api/experiment/point", { point_id: activePoint });
      $("expBaselineConfirmed").checked = false;
      if (result.experiment) render(result);
      message("已提交采集点位 " + activePoint + "；请移开接触棒并手动标定本轮。", "success");
    }));
    $("expStartBtn").addEventListener("click", () => run(async () => {
      const finger = $("expFingerID").value; if (!finger.trim()) throw new Error("请填写实际手指 / 样件标识。");
      const dataID = $("expDataID").value; if (!dataID.trim() || dataID.length > 128) throw new Error("请填写本次数据编号，最多 128 个字符。");
      if (!$("expCoordinatesConfirmed").checked) throw new Error("建立会话前，请先核对 25 点编号和方向。");
      if (!$("expDirectionConfirmed").checked) throw new Error("建立会话前，请先根据实际安装核对法向按压力方向。");
      const repetitions = repetitionsValue($("expRepetitions").value);
      const result = await request("/api/experiment/start", { finger_id: finger, data_id: dataID, batch: $("expBatch").value, force_sign: Number($("expForceSign").value), force_direction_confirmed: $("expDirectionConfirmed").checked, coordinates_confirmed: $("expCoordinatesConfirmed").checked, coordinate_mode: "normalized", conditions: { fixation: $("expFixation").value.trim(), temperature_C: numericInput("expTemperature", "温度", true) }, max_force_N: 3, guided_acquisition: true, repetitions_per_point: repetitions });
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
    $("expForceSign").addEventListener("change", () => { $("expDirectionConfirmed").checked = false; put("expDirectionDescription", "已按当前安装核对法向方向：按压力 F = " + ($("expForceSign").value === "-1" ? "−" : "+") + "(Fz − Fz₀)"); message("按压力符号已改变，请根据实际安装重新核对法向方向。"); });
    $("expBaselineBtn").addEventListener("click", () => run(async () => { if (!$("expBaselineConfirmed").checked) throw new Error("请先确认已移开接触棒且当前无载。"); const result = await request("/api/experiment/baseline", { unloaded_confirmed: true }); $("expBaselineConfirmed").checked = false; if (result.experiment) render(result); message("正在标定本轮，请保持无接触，等待服务端基线就绪。"); }));
    for (const [id, action] of Object.entries({ expTrialStart: "start", expTrialAbort: "abort" })) $(id).addEventListener("click", () => run(async () => { const result = await request("/api/experiment/trial", { action }); if (result.experiment) render(result); message(action === "abort" ? "本轮已中止，原始记录保留；请卸载后手动标定重采。" : "本轮采集已开始，保持无接触并跟随服务端提示。", action === "abort" ? "" : "success"); }));
    $("expRefreshSessions").addEventListener("click", () => run(async () => { await refreshSessions(); message("会话列表已刷新。", "success"); }));
    $("expShowDeleted").addEventListener("change", () => { if (historyPendingAction || busy) return; selectedSessions.clear(); renderSessions(); });
    $("expSelectTrainable").addEventListener("click", () => { if (busy || historyPendingAction || $("expShowDeleted").checked) return; selectedSessions.clear(); for (const row of sessions) if (historyTrainingStatus(row).allowed) selectedSessions.add(row.session_id); renderSessions(); message("已选择全部可训练记录；请核对数据兼容性与模型方法后明确点击批量训练。"); });
    $("expClearSessions").addEventListener("click", () => { if (busy || historyPendingAction) return; selectedSessions.clear(); renderSessions(); });
    $("expSessionList").addEventListener("change", event => { const id = event.target.dataset.sessionId; if (busy || historyPendingAction || !id || !historyRows().some(row => row.session_id === id)) return; if (event.target.checked) selectedSessions.add(id); else selectedSessions.delete(id); renderSessions(); });
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
        const result = await request("/api/history/" + pending.action, { session_ids: pending.ids }, 5000);
        selectedSessions.clear(); closeHistoryDialog(); applyHistoryList(result);
        message(pending.action === "delete" ? "所选记录已移入可恢复回收区；可勾选“查看已删除”并恢复。" : "所选记录已恢复至原保存位置。", "success");
      } catch (error) { put("expHistoryDialogError", error.message); $("expHistoryDialogError").hidden = false; throw error; }
    }));
    $("expTrainingMethod").addEventListener("change", updateControls);
    $("expTrainingStart").addEventListener("click", () => run(async () => {
      if (expired || latest.experiment?.running || latest.analysis?.busy === true || trainingView(latest.analysis?.training).busy) throw new Error("当前状态不允许开始训练，请先保存采集会话并等待已有任务结束。");
      if ($("expShowDeleted").checked || historyPendingAction) throw new Error("请先恢复回收区记录，或关闭记录操作确认框，再训练。");
      const sessionIDs = historyTrainingIDs(sessions, checkedSessionIDs()), method = $("expTrainingMethod").value;
      if (!["linear", "mlp"].includes(method)) throw new Error("请选择加权最小二乘或小型 MLP。");
      const result = await request("/api/training/start", { session_ids: sessionIDs, method }, 5000);
      const training = result.training || result.analysis?.training;
      if (training) { latest = { ...latest, analysis: { ...latest.analysis, training } }; renderTraining(latest.analysis); }
      message("训练任务已提交，等待服务端真实进度；模型保存后再去实机验证。");
    }));
    $("expGoValidation").addEventListener("click", () => run(async () => {
      const view = trainingView(latest.analysis?.training);
      if (!view.complete) throw new Error("模型尚未确认保存完成。");
      await refreshModels(view.modelID); switchTab("test");
      message("已选中刚训练的模型，请手动点击“载入模型”，再明确建立本次无接触参考。");
    }));
    $("expRefreshModels").addEventListener("click", () => run(async () => { await refreshModels(); message("已保存模型列表已刷新。", "success"); }));
    $("expModelSelect").addEventListener("change", updateControls);
    $("expTestUnloaded").addEventListener("change", updateControls);
    $("expTestReferenceBtn").addEventListener("click", () => run(async () => {
      if (!$("expTestUnloaded").checked) throw new Error("请先确认当前无接触。");
      await request("/api/analysis/reference", { unloaded_confirmed: true }); $("expTestUnloaded").checked = false;
      render({ ...latest, analysis: { ...latest.analysis, test_reference: { ready: false, pending: true, reason: "等待本次无接触参考与服务端状态" }, prediction: { status: "等待本次无接触参考", reason: "本次参考完成后再真实按压。" } } });
      message("已请求本次实机验证光学参考；若连接 PX6D，将同时收集服务端无载真值零点。");
    }));
    $("expLoadModelBtn").addEventListener("click", () => run(async () => {
      const id = $("expModelSelect").value; if (!id) throw new Error("请先选择真实保存的模型。");
      await request("/api/analysis/load", { model_id: id }); $("expTestUnloaded").checked = false;
      render({ ...latest, analysis: { ...latest.analysis, test_reference: { ready: false, pending: false, reason: "模型已载入，请重新明确确认当前无接触并建立本次参考" }, prediction: { status: "等待本次无接触参考", reason: "模型已更换，等待服务端模型定义及本次参考。" } } });
      message("模型已载入，请移开接触棒，重新明确确认无接触并建立本次参考。", "success");
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
      if (hasPendingJudgement() || latest.validation?.pending_sample) throw new Error("请先保存本次判断或点击“重新判断”，再结束验证。");
      if (!validationResultView(latest.validation).total) throw new Error("尚未实机验证，暂无已保存判断可生成结果。");
      const result = await request("/api/validation/finish", {}, 5000);
      if (result.validation) latest = { ...latest, validation: result.validation };
      validationResult = result.report || result.validation?.latest_report || null;
      renderValidationStatistics(latest.validation);
      message("本轮验证已结束，真实判断结果已保存。", "success");
    }));
    render(latest);
    bindPreferences();
    global.setInterval(refreshFreshness, 250);
  }
  return { mount, render, pointGrid, visualPointGrid, pointLayout, COORDINATE_SCHEMA, REQUIRED_SERVICE_VERSION, backendCompatible, legacyRawRecording, compatibilityMessage, phaseLabel, experimentGates, guidedView, preloadView, repetitionsValue, roundQualityText, predictionView, trainedPointIDs, validationLiveView, frozenSampleView, forceWithinTolerance, targetComparison, validationStatistics, validationResultView, trainingView, trainingMethodLabel, modelOptionLabel, liveForceView, trialCurves, summaryRows, splitMetrics, hasSplitData, defaultReportSplit, modelSessionIDs, sessionHasTrainingData, visibleSessions, latestEffectiveSession, sessionTimeText, sessionPointSummary, historyTrainingStatus, historyTrainingIDs, historyRecordLabel, pilotSessionIDs, pilotReportView, analysisProgressText, modelDefinitionText, testReferenceText };
});




