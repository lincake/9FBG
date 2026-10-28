import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../frontend/pulse.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../frontend/pulse.html", import.meta.url), "utf8");
const version = JSON.parse(readFileSync(new URL("./fixtures/history/VERSION.web.json", import.meta.url), "utf8")).version;
const method = "unloaded-noise-fbg5-prior-v1", mode = "continuous-noise-fixed-v1";
const noisePolicy = "effective-source-time-and-samples-v1";
const filterConfigSchema = "pulse-bandpass-config-v1";
const actualCore = require("../frontend/pulse_signal.js");
const plain = value => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(resolve => setImmediate(resolve));
const sample = (seq, signal = 0) => ({ seq, t_ns: seq * 40_000_000, frame_id: seq,
  binding: "reference-1", stream_epoch: "actual-optical-stream", source_kind: "hardware",
  values_pm: Array.from({ length: 9 }, (_, j) => signal * (j + 1) + 0.01 * Math.sin(seq * 1.123 + j)), valid: true });
const batch = (samples = [], extra = {}) => ({ schema: "nine_fbg_pulse_samples_v1", version,
  running: true, stopping: false, mode: "hardware", blocked: false, age_ms: 0,
  cursor: samples.at(-1)?.seq || 0, samples, latest_binding: "reference-1", ...extra });
const fixedWeights = [0.05, 0.05, 0.05, 0.05, 0.6, 0.05, 0.05, 0.05, 0.05];

// The real mounted entry point runs against observable DOM/canvas doubles.
// The final integration case uses the actual DSP, without hardware actions.
function page({ realCore = false, coreMethod = method, coreMode = mode, coreNoisePolicy = noisePolicy, coreFilterSchema = filterConfigSchema, missingNodes = [] } = {}) {
  const nodes = new Map(), handlers = new Map(), windowHandlers = new Map(), documentHandlers = new Map();
  const requests = [], deferred = [], intervals = [], frames = new Map(), sessionCalls = [];
  const drawingCalls = [], writes = new Map(), leaseCalls = [], subscribers = new Set();
  const drawing = new Proxy({}, { get(target, name) { return target[name] || ((...args) => drawingCalls.push([name, ...args])); },
    set(target, name, value) { target[name] = value; return true; } });
  let clock = 100_000, timerId = 0, instance, focused = false, width = 1000, height = 300, resize;
  const lease = { isActive: () => focused, isFrozen: () => false,
    setActive(value, reason = "activity") { const next = value === true; if (next === focused) return; focused = next; leaseCalls.push(next);
      for (const fn of subscribers) fn({ active: focused, frozen: false, reason }); },
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
    requestResume() { this.setActive(false, "resume"); }, dispose() { this.setActive(false); } };
  for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const tag = match[0], id = match[1]; if (missingNodes.includes(id)) continue;
    const node = { id, checked: /\bchecked\b/.test(tag), disabled: /\bdisabled\b/.test(tag), hidden: /\bhidden\b/.test(tag),
      value: id === "pulseWindow" ? "30" : tag.match(/\bvalue="([^"]*)"/)?.[1] || "", textContent: "", innerHTML: "", className: "",
      classList: { add() {}, remove() {}, toggle() {} }, setAttribute(name, value) { this[name] = value; },
      addEventListener(type, fn) { handlers.set(id + ":" + type, fn); },
      getBoundingClientRect() { return { width, height }; }, getContext() { return drawing; } };
    for (const property of ["width", "height", "textContent", "innerHTML", "className"]) {
      let value = node[property]; Object.defineProperty(node, property, { get() { return value; }, set(next) {
        writes.set(id + ":" + property, (writes.get(id + ":" + property) || 0) + 1); value = next;
      } });
    }
    nodes.set(id, node);
  }
  class Session {
    constructor() { instance = this; this.state = { phase: "idle", points: [], weights: null, noise: null,
      revision: 0, pointVersion: 0, pointCount: 0, suspended: false, filterBandHz: [.7, 3],
      warmupSeconds: 3, maxGapSeconds: .12, candidateBandHz: [.7, 3] }; }
    snapshot(options) { sessionCalls.push({ action: "snapshot", options });
      const state = { ...this.state, pointCount: this.state.points.length }; if (options?.includePoints === false) delete state.points; return state; }
    readPoints() { sessionCalls.push({ action: "readPoints" }); return this.state.points; }
    reset(reason) { sessionCalls.push({ action: "reset", reason }); Object.assign(this.state,
      { phase: "idle", points: [], weights: null, noise: null, revision: 0, pointVersion: this.state.pointVersion + 1, suspended: false, reason }); }
    setFilterBand(input) {
      const band = actualCore.validateFilterBandHz(input); sessionCalls.push({ action: "filterBand", band: plain(band) });
      if (band.every((frequency, i) => frequency === this.state.filterBandHz[i])) return this.snapshot();
      this.state.filterBandHz = band; this.state.warmupSeconds = Math.max(3, 2.1 / band[0]);
      this.state.maxGapSeconds = Math.min(.12, .45 / band[1]);
      this.state.candidateBandHz = Math.max(.7, band[0]) < Math.min(3, band[1]) ? [Math.max(.7, band[0]), Math.min(3, band[1])] : null;
      this.reset("滤波频带已改变，请重新确认无载并录制噪声"); return this.snapshot();
    }
    beginNoise(options) { sessionCalls.push({ action: "noise", options: plain(options) }); this.state.phase = "noise"; }
    beginMonitor(options) { sessionCalls.push({ action: "monitor", options: options && plain(options) });
      this.state.phase = "monitor"; this.state.suspended = false; this.state.curveKind = "monitor"; }
    stopMonitor() { sessionCalls.push({ action: "stop" }); this.state.phase = "calibrated_ready"; this.state.suspended = false; }
    pause(reason) { sessionCalls.push({ action: "pause", reason }); this.state.reason = reason;
      this.state.candidateBpm = null; this.state.quality = null; this.state.valuePm = null;
      this.state.phase = this.state.weights ? "monitor" : "failed"; this.state.suspended = !!this.state.weights; }
    interrupt(reason) { sessionCalls.push({ action: "interrupt", reason }); this.state.phase = "failed";
      this.state.reason = reason; this.state.weights = null; this.state.candidateBpm = null; }
    push(samples, options) { sessionCalls.push({ action: "push", phase: this.state.phase, samples: plain(samples), options });
      if (this.completeNoise && this.state.phase === "noise") { this.completeNoise = false; calibrate(this); }
      if (samples.length && this.state.phase === "monitor") { this.state.suspended = false;
        for (const row of samples) this.state.points.push({ t: row.t_ns / 1e9, value: row.values_pm[4], warmup: false });
        this.state.pointVersion++; this.state.valuePm = samples.at(-1).values_pm[4]; }
      return this.snapshot(options); }
  }
  class ClockDate extends Date { static now() { return clock; } }
  const document = { hidden: false, getElementById(id) {
    if (missingNodes.includes(id) || ["pulseLearnBtn", "pulseContact", "pulseReady", "pulseStopBtn"].includes(id)) return null;
    assert.ok(nodes.has(id), "Unknown pulse DOM target: " + id); return nodes.get(id);
  }, addEventListener(type, fn) { documentHandlers.set(type, fn); } };
  const browser = vm.createContext({ document,
    NineFBGPulse: { PulseSession: Session, CALIBRATION_METHOD: coreMethod, STREAM_MODE: coreMode, NOISE_POLICY: coreNoisePolicy,
      FILTER_CONFIG_SCHEMA: coreFilterSchema, validateFilterBandHz: actualCore.validateFilterBandHz },
    NineFBGGuiPriority: { shared: () => lease }, Date: ClockDate, Math, AbortController, devicePixelRatio: 1, console,
    performance: { now: () => clock }, fetch(path, options = {}) { requests.push({ path, options });
      return new Promise((resolve, reject) => deferred.push({ resolve, reject, path, options })); },
    addEventListener(type, fn) { windowHandlers.set(type, fn); },
    setInterval(fn, ms) { intervals.push({ fn, ms }); return ++timerId; }, clearInterval() {},
    setTimeout() { return ++timerId; }, clearTimeout() {},
    requestAnimationFrame(fn) { const id = ++timerId; frames.set(id, fn); return id; }, cancelAnimationFrame(id) { frames.delete(id); },
    ResizeObserver: class { constructor(fn) { resize = fn; } observe() {} disconnect() {} } });
  if (realCore) {
    vm.runInContext(readFileSync(new URL("../frontend/pulse_signal.js", import.meta.url), "utf8"), browser, { filename: "pulse_signal.js" });
    const ActualSession = browser.NineFBGPulse.PulseSession;
    browser.NineFBGPulse = { ...browser.NineFBGPulse, PulseSession: class extends ActualSession { constructor() { super(); instance = this; } } };
  }
  vm.runInContext(source, browser, { filename: "pulse.js" });
  async function paint() { const pending = [...frames]; frames.clear(); for (const [, fn] of pending) fn(clock); await flush(); }
  async function respond(payload, status = 200, json, path) {
    if (!path) assert.equal(deferred.length, 1, "Exactly one operation may be in flight");
    const index = path ? deferred.findIndex(call => call.path === path) : 0; assert.ok(index >= 0, "Expected in-flight operation " + (path || ""));
    deferred.splice(index, 1)[0].resolve({ ok: status >= 200 && status < 300, status, json: json || (async () => plain(payload)) });
    await flush(); await paint();
  }
  async function fire(id, type = "click") { assert.ok(handlers.has(id + ":" + type), id + " has a real " + type + " listener");
    handlers.get(id + ":" + type)({ preventDefault() {} }); await flush(); await paint(); }
  async function tick() { clock += 100; for (const { fn } of intervals) fn(); await flush(); await paint(); }
  async function reject(error = new TypeError("Failed to fetch")) { assert.equal(deferred.length, 1); deferred.shift().reject(error); await flush(); await paint(); }
  return { nodes, handlers, requests, deferred, sessionCalls, respond, reject, fire, tick, paint, document,
    documentHandlers, windowHandlers, drawingCalls, writes, lease, leaseCalls, browser,
    get session() { return instance; }, advance(ms) { clock += ms; },
    async check(id, checked = true) { nodes.get(id).checked = checked; await fire(id, "change"); },
    async resize(w, h = height, ratio = browser.devicePixelRatio) { width = w; height = h; browser.devicePixelRatio = ratio;
      if (resize) resize(); else windowHandlers.get("resize")(); await paint(); },
    async ready() { await respond(batch([sample(10)])); } };
}
const actions = (p, name) => p.sessionCalls.filter(call => call.action === name);
const measured = p => actions(p, "push").filter(call => ["noise", "monitor"].includes(call.phase));
const posts = p => p.requests.filter(call => call.options?.method === "POST");
function calibrate(session) { Object.assign(session.state, { phase: "monitor", weights: fixedWeights.slice(),
  noise: Array(9).fill(0.02), baselinePm: Array(9).fill(2), binding: "reference-1", curveKind: "monitor",
  points: [{ t: 1, value: 0.01 }, { t: 1.04, value: -0.01 }, { t: 1.08, value: 0 }],
  revision: 1, pointVersion: session.state.pointVersion + 1, suspended: false }); }
async function observing(p) { await p.ready(); await p.check("pulseUnloaded"); await p.fire("pulseNoiseBtn");
  await p.respond(batch([sample(11)])); p.session.completeNoise = true; await p.tick(); await p.respond(batch([sample(12)])); }

assert.match(html, /<html lang="zh-CN">/);
assert.ok(html.indexOf('src="/pulse_signal.js?version=' + version + '"') < html.indexOf('src="/pulse.js?version=' + version + '"'));
assert.match(html, /10 s/); assert.match(html, /FBG5/);
for (const id of ["pulseLearnBtn", "pulseContact", "pulseReady", "pulseStopBtn"]) assert.ok(!html.includes('id="' + id + '"'));
assert.match(html, /id="pulseFreezeOthers"[^>]*checked/);
const flow = html.match(/<section\b[^>]*class="panel pulse-flow"[^>]*>([\s\S]*?)<\/section>/)?.[1];
assert.ok(flow); assert.doesNotMatch(flow, /学习中心点权重|学习权重|(?:8|15)\s*s/);
const returnLinks = Array.from(html.matchAll(/<a\b[^>]*class="(?:brand|button secondary pulse-nav)"[^>]*href="([^"]+)"/g), match => new URL(match[1], "http://127.0.0.1"));
assert.equal(returnLinks.length, 2);
for (const link of returnLinks) { assert.equal(link.pathname, "/"); assert.equal(link.searchParams.get("version"), version); }
assert.ok(!html.includes("https://"));
assert.match(html, /<form\b[^>]*id="pulseFilterForm"[^>]*\bnovalidate\b/);
assert.ok(html.indexOf('id="pulseFilterForm"') > html.indexOf('id="pulseChart"'), "Band controls sit below the pulse curve");
assert.ok(html.indexOf('id="pulseFilterForm"') < html.indexOf('id="pulseWeights"'));
for (const id of ["pulseBandLow", "pulseBandHigh"]) assert.match(html, new RegExp('<input[^>]*id="' + id + '"[^>]*type="number"|<input[^>]*type="number"[^>]*id="' + id + '"'));

// Frequency drafts are separate from the running filter and fixed noise weights.
// Applying a changed band explicitly discards its old calibration, never starts
// another unloaded recording, and never performs a hardware/reference command.
{
  const p = page(); await observing(p);
  assert.equal(Number(p.nodes.get("pulseBandLow").value), .7); assert.equal(Number(p.nodes.get("pulseBandHigh").value), 3);
  assert.match(p.nodes.get("pulseFilterCurrent").textContent, /0\.7.*3.*Hz/);
  assert.match(p.nodes.get("pulseBandMetric").textContent, /0\.7.*3.*Hz/);
  const before = plain(p.session.state), resets = actions(p, "reset").length;
  p.nodes.get("pulseBandLow").value = "0.5"; await p.fire("pulseBandLow", "input");
  p.nodes.get("pulseBandHigh").value = "5"; await p.fire("pulseBandHigh", "change");
  assert.deepEqual(plain(p.session.state), before, "Typing alone cannot alter the running signal or learned noise");
  assert.equal(actions(p, "filterBand").length, 0); assert.equal(p.lease.isActive(), true);
  await p.tick(); await p.respond(batch([sample(13)]));
  assert.equal(p.nodes.get("pulseBandLow").value, "0.5"); assert.equal(p.nodes.get("pulseBandHigh").value, "5", "Polling must not overwrite the frequency draft");
  await p.fire("pulseFilterApply");
  assert.equal(p.session.state.phase, "idle"); assert.equal(p.session.state.weights, null); assert.equal(p.session.state.noise, null);
  assert.deepEqual(plain(p.session.state.filterBandHz), [.5, 5]); assert.equal(actions(p, "reset").length, resets + 1);
  assert.equal(p.lease.isActive(), false); assert.equal(p.nodes.get("pulseUnloaded").checked, false);
  assert.equal(p.nodes.get("pulseNoiseBtn").disabled, true); assert.equal(actions(p, "noise").length, 1);
  assert.match(p.nodes.get("pulseFilterCurrent").textContent, /0\.5.*5.*Hz/); assert.match(p.nodes.get("pulseBandMetric").textContent, /0\.5.*5.*Hz/);
  assert.match(p.nodes.get("pulseChartNote").textContent, /0\.5.*5.*Hz/); assert.match(p.nodes.get("pulseFilterStatus").textContent, /重.*(?:录|采)|无负载/);
  await p.tick(); assert.equal(p.requests.at(-1).path, "/api/pulse/samples?after=" + Number.MAX_SAFE_INTEGER);
  await p.respond(batch([sample(100)])); await p.tick(); await p.respond(batch([sample(101)]));
  assert.equal(p.session.state.phase, "idle"); assert.equal(actions(p, "noise").length, 1); assert.equal(posts(p).length, 0);
  await p.fire("pulseNoiseBtn"); assert.equal(actions(p, "noise").length, 1, "Band application cannot replace explicit unloaded confirmation");
  await p.check("pulseUnloaded"); await p.fire("pulseNoiseBtn"); await p.respond(batch([sample(102)]));
  assert.equal(actions(p, "noise").length, 2); assert.deepEqual(plain(p.session.state.filterBandHz), [.5, 5]);
  for (const id of ["pulseBandLow", "pulseBandHigh", "pulseFilterApply"]) assert.equal(p.nodes.get(id).disabled, true, "A recording keeps one fixed band");
  const fixedBand = plain(p.session.state.filterBandHz), filters = actions(p, "filterBand").length;
  p.nodes.get("pulseBandHigh").value = "4"; await p.fire("pulseFilterForm", "submit");
  assert.deepEqual(plain(p.session.state.filterBandHz), fixedBand); assert.equal(actions(p, "filterBand").length, filters);
}
for (const [low, high] of [["", "3"], ["0.7", ""], ["abc", "3"], ["0.7", "Infinity"], ["0.09", "3"], ["0.7", "8.01"], ["3", "3"], ["4", "3"]]) {
  const p = page(); await observing(p); const before = plain(p.session.state), resets = actions(p, "reset").length;
  p.nodes.get("pulseBandLow").value = low; await p.fire("pulseBandLow", "input");
  p.nodes.get("pulseBandHigh").value = high; await p.fire("pulseBandHigh", "input");
  await p.fire("pulseFilterApply");
  assert.deepEqual(plain(p.session.state), before, "Invalid band preserves calibration: " + low + "," + high);
  assert.equal(actions(p, "reset").length, resets); assert.equal(actions(p, "filterBand").length, 0); assert.equal(p.lease.isActive(), true);
  assert.match(p.nodes.get("pulseFilterStatus").textContent, /[\u4e00-\u9fff]/, "Input error is Chinese"); assert.equal(posts(p).length, 0);
}
{
  const p = page(); await observing(p); p.session.state.rateHz = 12;
  const before = plain(p.session.state); p.nodes.get("pulseBandHigh").value = "6"; await p.fire("pulseBandHigh", "input");
  await p.fire("pulseFilterApply"); assert.deepEqual(plain(p.session.state), before);
  assert.match(p.nodes.get("pulseFilterStatus").textContent, /采样|提峰|频率|上限/); assert.equal(actions(p, "filterBand").length, 0);
}
{
  const p = page(); await observing(p); const before = plain(p.session.state), resets = actions(p, "reset").length;
  p.nodes.get("pulseBandLow").value = "0.70"; p.nodes.get("pulseBandHigh").value = "3.0";
  await p.fire("pulseFilterForm", "submit");
  assert.deepEqual(plain(p.session.state), before, "Submitting the same numeric band preserves its fixed weights and trace");
  assert.equal(actions(p, "reset").length, resets); assert.equal(p.lease.isActive(), true); assert.equal(posts(p).length, 0);
}
// Late samples, including a response already awaiting JSON, cannot cross an
// applied filter boundary and become noise data for the new band.
for (const stage of ["fetch", "json"]) {
  const p = page(); await observing(p); await p.tick(); let finishJson;
  if (stage === "json") await p.respond(null, 200, () => new Promise(resolve => { finishJson = resolve; }));
  const before = measured(p).length;
  p.nodes.get("pulseBandLow").value = "0.5"; p.nodes.get("pulseBandHigh").value = "5"; await p.fire("pulseFilterApply");
  if (stage === "fetch") await p.respond(batch([sample(99)])); else { finishJson(batch([sample(99)])); await flush(); await p.paint(); }
  assert.equal(measured(p).length, before); assert.equal(p.session.state.phase, "idle");
  assert.deepEqual(plain(p.session.state.filterBandHz), [.5, 5]); assert.equal(p.session.state.weights, null);
  await p.tick(); await p.respond(batch([sample(100)])); assert.equal(actions(p, "noise").length, 1); assert.equal(posts(p).length, 0);
}

// Cached learning/preview-only assets are rejected before any read or mutation.
for (const mismatch of [{ coreMethod: null }, { coreMethod: "center-response-v1" }, { coreMode: null }, { coreMode: "preview-only" },
  { coreNoisePolicy: null }, { coreNoisePolicy: "fixed-20hz-noise-reject-v1" },
  { coreFilterSchema: null }, { coreFilterSchema: "fixed-band-legacy-v1" },
  ...["pulseMonitorBtn", "pulseWeightSummary", "pulseFreezeOthers", "pulseBandLow", "pulseBandHigh", "pulseFilterApply", "pulseFilterStatus", "pulseFilterCurrent", "pulseBandMetric", "pulseFilterHelp", "pulseFilterForm"].map(id => ({ missingNodes: [id] }))]) {
  const p = page(mismatch); assert.equal(p.session, undefined);
  assert.match(p.nodes.get("pulseServiceNotice").textContent, /版本不一致.*新版工作台/);
  await p.tick(); assert.equal(p.requests.length, 0); assert.equal(p.handlers.size, 0);
}
{
  const p = page(); assert.equal(p.nodes.get("pulseNoiseBtn").disabled, true);
  assert.equal(p.requests[0].path, "/api/pulse/samples?after=" + Number.MAX_SAFE_INTEGER,
    "Initial anchoring reads the current watermark instead of downloading discarded 4096-frame history");
  await p.respond(batch([], { cursor: 10, reset_required: true, truncated: true }));
  assert.equal(actions(p, "interrupt").length, 0, "A deliberate future-cursor anchor accepts its expected reset marker");
  assert.equal(actions(p, "noise").length, 0); assert.equal(measured(p).length, 0); assert.equal(p.lease.isActive(), false);
  await p.fire("pulseNoiseBtn"); await p.fire("pulseMonitorBtn"); assert.equal(actions(p, "noise").length, 0);
  await p.check("pulseUnloaded"); assert.equal(p.nodes.get("pulseNoiseBtn").disabled, false); assert.equal(posts(p).length, 0);
}
for (const can_initialize of [undefined, false, "true"]) {
  const p = page(); await p.respond(batch([sample(10)], { blocked: true, can_initialize }));
  await p.check("pulseUnloaded"); assert.equal(p.nodes.get("pulseReferenceBtn").disabled, true);
  await p.fire("pulseReferenceBtn"); assert.equal(posts(p).length, 0);
}
{
  const p = page(); await p.respond(batch([sample(10)], { blocked: true, can_initialize: true }));
  assert.equal(p.nodes.get("pulseReferenceBtn").hidden, false); await p.fire("pulseReferenceBtn"); assert.equal(posts(p).length, 0);
  await p.check("pulseUnloaded"); await p.fire("pulseReferenceBtn"); assert.equal(posts(p).length, 1);
  assert.equal(posts(p)[0].path, "/api/pulse/reference"); assert.deepEqual(JSON.parse(posts(p)[0].options.body), { unloaded_confirmed: true });
  assert.equal(p.nodes.get("pulseUnloaded").checked, false);
  for (const id of ["pulseReferenceBtn", "pulseNoiseBtn", "pulseMonitorBtn", "pulseResetBtn"]) assert.equal(p.nodes.get(id).disabled, true);
  for (const id of ["pulseBandLow", "pulseBandHigh", "pulseFilterApply"]) assert.equal(p.nodes.get(id).disabled, true);
  p.nodes.get("pulseBandLow").value = "0.5"; p.nodes.get("pulseBandHigh").value = "5"; await p.fire("pulseFilterForm", "submit");
  assert.equal(actions(p, "filterBand").length, 0, "A pending reference operation cannot change the filter");
  await p.fire("pulseReferenceBtn"); assert.equal(posts(p).length, 1);
  const count = p.requests.length; await p.tick(); assert.equal(p.requests.length, count);
  await p.respond({ ok: true }); await p.respond(batch([sample(11)]));
  assert.equal(actions(p, "noise").length, 0); assert.equal(p.nodes.get("pulseNoiseBtn").disabled, true);
  await p.check("pulseUnloaded"); await p.fire("pulseNoiseBtn"); await p.respond(batch([sample(12)]));
  assert.equal(actions(p, "noise").length, 1);
  assert.ok(p.requests.every(call => call.options.method === "POST" ? call.path === "/api/pulse/reference" : call.path.startsWith("/api/pulse/samples?after=")));
}
{
  const p = page(); await p.respond(batch([sample(10)], { blocked: true, can_initialize: true }));
  await p.check("pulseUnloaded"); await p.fire("pulseReferenceBtn"); await p.respond({ error: "实验正在运行，拒绝初始化" }, 409);
  assert.match(p.nodes.get("pulseServiceNotice").textContent, /实验正在运行/);
  await p.respond(batch([sample(11)], { blocked: true })); assert.equal(posts(p).length, 1);
}
// Old reads cannot override a new explicit reference operation, and a late POST
// acknowledgement never starts polling again after the page has been hidden.
{
  const p = page(); await p.respond(batch([sample(10)], { blocked: true, can_initialize: true }));
  await p.check("pulseUnloaded"); await p.tick(); const oldRead = p.requests.at(-1).path;
  await p.fire("pulseReferenceBtn"); assert.equal(p.deferred.length, 2);
  await p.respond(batch([sample(100)]), 200, undefined, oldRead);
  assert.equal(p.nodes.get("pulseNoiseBtn").disabled, true); assert.equal(p.nodes.get("pulseReferenceBtn").disabled, true);
  await p.respond({ ok: true }); await p.respond(batch([sample(101)], { blocked: true }));
  assert.equal(posts(p).length, 1); assert.equal(actions(p, "noise").length, 0);
}
for (const closing of ["hidden", "pagehide"]) {
  const p = page(); await p.respond(batch([sample(10)], { blocked: true, can_initialize: true }));
  await p.check("pulseUnloaded"); await p.fire("pulseReferenceBtn"); let finishJson;
  await p.respond(null, 200, () => new Promise(resolve => { finishJson = resolve; }));
  if (closing === "hidden") { p.document.hidden = true; p.documentHandlers.get("visibilitychange")(); }
  else p.windowHandlers.get("pagehide")();
  await flush(); const count = p.requests.length; finishJson({ ok: true }); await flush(); await p.tick();
  assert.equal(p.requests.length, count); assert.equal(posts(p).length, 1); assert.equal(actions(p, "noise").length, 0);
}

// One unloaded recording starts the curve automatically. Explicit pause keeps
// its last trace, and resume never performs a second noise weight calculation.
{
  const p = page(); await observing(p); assert.equal(p.session.state.phase, "monitor"); assert.equal(p.lease.isActive(), true);
  assert.equal(actions(p, "noise").length, 1); assert.equal(actions(p, "monitor").length, 0); assert.equal(p.nodes.get("pulseNoiseBtn").disabled, true);
  const calibration = plain({ weights: p.session.state.weights, noise: p.session.state.noise }), points = plain(p.session.state.points);
  await p.fire("pulseMonitorBtn"); assert.equal(p.session.state.phase, "calibrated_ready");
  assert.deepEqual(plain(p.session.state.points), points); assert.equal(p.lease.isActive(), false);
  await p.tick(); await p.respond(batch([sample(15)])); await p.tick(); await p.respond(batch([sample(16)]));
  assert.equal(p.session.state.phase, "calibrated_ready", "New source frames cannot undo an intentional pause");
  assert.deepEqual(plain(p.session.state.points), points);
  await p.fire("pulseMonitorBtn"); assert.equal(p.requests.at(-1).path, "/api/pulse/samples?after=" + Number.MAX_SAFE_INTEGER);
  await p.respond(batch([sample(20)])); assert.equal(p.session.state.phase, "monitor"); assert.equal(p.lease.isActive(), true);
  await p.tick(); await p.respond(batch([sample(21)]));
  assert.deepEqual(plain({ weights: p.session.state.weights, noise: p.session.state.noise }), calibration); assert.equal(actions(p, "noise").length, 1);
  await p.check("pulseFreezeOthers", false); assert.equal(p.lease.isActive(), false);
  await p.tick(); await p.respond(batch([sample(22)])); assert.equal(p.session.state.phase, "monitor");
  await p.check("pulseFreezeOthers"); assert.equal(p.lease.isActive(), true);
  p.lease.requestResume(); await p.paint(); assert.equal(p.nodes.get("pulseFreezeOthers").checked, false);
  await p.tick(); await p.respond(batch([sample(23)])); assert.equal(p.lease.isActive(), false, "External GUI resume is not immediately re-frozen by pulse rendering");
  await p.check("pulseFreezeOthers"); await p.fire("pulseResetBtn"); assert.equal(p.session.state.phase, "idle");
  assert.equal(p.session.state.weights, null); assert.equal(p.lease.isActive(), false); assert.equal(p.nodes.get("pulseUnloaded").checked, false);
  assert.ok(p.requests.every(call => !call.options.method), "Observation and GUI freezing never connect hardware or change a reference");
}
for (const interruption of ["network", "hidden", "pagehide"]) {
  const p = page(); await observing(p); await p.fire("pulseMonitorBtn");
  const points = plain(p.session.state.points);
  if (interruption === "network") { await p.tick(); await p.reject(); }
  if (interruption === "hidden") { p.document.hidden = true; p.documentHandlers.get("visibilitychange")(); await flush(); }
  if (interruption === "pagehide") { p.windowHandlers.get("pagehide")(); await flush(); }
  assert.equal(p.session.state.phase, "calibrated_ready", "An intentional pause survives " + interruption);
  if (interruption === "hidden") { p.document.hidden = false; p.documentHandlers.get("visibilitychange")(); }
  else if (interruption === "pagehide") p.windowHandlers.get("pageshow")();
  else await p.tick();
  await flush(); await p.respond(batch([sample(100)])); await p.tick(); await p.respond(batch([sample(101)]));
  assert.equal(p.session.state.phase, "calibrated_ready"); assert.deepEqual(plain(p.session.state.points), points);
  assert.equal(p.lease.isActive(), false); assert.equal(actions(p, "monitor").length, 0);
}
for (const blocked of [{ mode: "replay" }, { running: false }, { stopping: true }, { blocked: true, reason: "等待明确无载确认" }]) {
  const p = page(); await p.respond(batch([sample(10)], blocked)); await p.check("pulseUnloaded");
  assert.equal(p.nodes.get("pulseNoiseBtn").disabled, true); await p.fire("pulseNoiseBtn"); assert.equal(actions(p, "noise").length, 0);
}
for (const fault of [{ truncated: true }, { reset_required: true }, { blocked: true, reason: "九峰参考已改变" }]) {
  const p = page(); await p.ready(); await p.check("pulseUnloaded"); await p.fire("pulseNoiseBtn");
  await p.respond(batch([sample(11)])); await p.tick(); await p.respond(batch([sample(12)], fault));
  assert.equal(p.session.state.phase, "failed"); assert.equal(measured(p).length, 0);
  await p.tick(); await p.respond(batch([sample(13)])); assert.equal(p.session.state.phase, "failed"); assert.equal(actions(p, "noise").length, 1);
}
for (const fault of ["network", "watchdog", "stale", "age", "truncated"]) {
  const p = page(); await observing(p); const calibration = plain(p.session.state.weights), before = measured(p).length;
  Object.assign(p.session.state, { candidateBpm: 72, quality: { valid: true } });
  if (fault === "watchdog") { await p.tick(); p.advance(1001); }
  await p.tick();
  if (fault === "network") await p.reject(); else await p.respond(batch([sample(13)], {
    ...(fault === "stale" ? { blocked: true, age_ms: 1500, reason: "光谱超过 1 秒未更新或缺少源帧接收时间" } : {}),
    ...(fault === "age" ? { age_ms: 1500 } : {}), ...(fault === "truncated" ? { truncated: true } : {}) }));
  assert.equal(p.session.state.phase, "monitor", fault + " preserves continuous observation intent");
  assert.equal(p.session.state.suspended, true); assert.deepEqual(plain(p.session.state.weights), calibration);
  assert.equal(p.session.state.candidateBpm, null); assert.equal(measured(p).length, before); assert.equal(p.lease.isActive(), false, fault + " releases GUI priority while live data is unavailable");
  await p.tick(); await p.respond(batch([sample(100)])); await p.tick(); await p.respond(batch([sample(101)]));
  assert.equal(p.session.state.phase, "monitor"); assert.equal(p.session.state.suspended, false); assert.equal(p.lease.isActive(), true);
  assert.deepEqual(plain(p.session.state.weights), calibration); assert.equal(actions(p, "noise").length, 1);
}
for (const fault of [{ reset_required: true }, { blocked: true, reason: "九峰参考已改变" },
  { latest_binding: "reference-2", blocked: true, age_ms: 1500 }, { running: false }, { stopping: true }, { mode: "replay" },
  { blocked: true, reason: "其他采集、实验或模型推理正在运行" }]) {
  const p = page(); await observing(p); await p.tick(); await p.respond(batch([sample(13)], fault));
  assert.equal(p.session.state.phase, "failed", JSON.stringify(fault)); assert.equal(p.session.state.weights, null); assert.equal(p.lease.isActive(), false);
  await p.tick(); await p.respond(batch([sample(100)])); assert.equal(p.session.state.phase, "failed"); assert.equal(actions(p, "noise").length, 1);
}

// Late fetch/JSON results cannot cross reset, departure or visibility boundaries.
for (const action of ["reset", "hidden", "pagehide"]) for (const stage of ["fetch", "json"]) {
  const p = page(); await observing(p); const weights = plain(p.session.state.weights); await p.tick(); let finishJson;
  if (stage === "json") await p.respond(null, 200, () => new Promise(resolve => { finishJson = resolve; })); const before = measured(p).length;
  if (action === "reset") await p.fire("pulseResetBtn");
  if (action === "hidden") { p.document.hidden = true; p.documentHandlers.get("visibilitychange")(); await flush(); }
  if (action === "pagehide") { p.lease.setActive(false, "lifecycle"); p.windowHandlers.get("pagehide")(); await flush(); }
  if (stage === "fetch") await p.respond(batch([sample(100)])); else { finishJson(batch([sample(100)])); await flush(); await p.paint(); }
  assert.equal(measured(p).length, before); assert.equal(p.lease.isActive(), false);
  if (action === "reset") { assert.equal(p.session.state.phase, "idle"); continue; }
  assert.deepEqual(plain(p.session.state.weights), weights);
  assert.equal(p.nodes.get("pulseFreezeOthers").checked, true, "Lifecycle lease release preserves the user's GUI priority preference");
  const count = p.requests.length; p.advance(5000); await p.tick(); assert.equal(p.requests.length, count);
  if (action === "hidden") { p.document.hidden = false; p.documentHandlers.get("visibilitychange")(); } else p.windowHandlers.get("pageshow")();
  await flush(); await p.respond(batch([sample(101)])); await p.tick(); await p.respond(batch([sample(102)]));
  assert.equal(p.session.state.phase, "monitor"); assert.deepEqual(plain(p.session.state.weights), weights);
  assert.equal(actions(p, "noise").length, 1); assert.equal(p.lease.isActive(), true);
}
// A late pre-action read cannot start the newly requested noise acquisition.
for (const stage of ["fetch", "json"]) {
  const p = page(); await p.ready(); await p.check("pulseUnloaded"); await p.tick(); let finishJson;
  if (stage === "json") await p.respond(null, 200, () => new Promise(resolve => { finishJson = resolve; }));
  await p.fire("pulseNoiseBtn");
  if (stage === "fetch") await p.respond(batch([sample(99)])); else { finishJson(batch([sample(99)])); await flush(); await p.paint(); }
  assert.equal(actions(p, "noise").length, 0); await p.tick(); await p.respond(batch([sample(100)]));
  assert.equal(actions(p, "noise").length, 1); assert.equal(measured(p).length, 0);
  await p.tick(); await p.respond(batch([sample(101)])); assert.deepEqual(measured(p).at(-1).samples.map(row => row.seq), [101]);
}

// Hot render paths neither copy the full trace nor rebuild immutable weights.
{
  const p = page(); await observing(p); const tableWrites = p.writes.get("pulseWeights:innerHTML");
  const sizes = p.writes.get("pulseChart:width") + p.writes.get("pulseChart:height");
  for (let seq = 13; seq < 23; seq++) { await p.tick(); await p.respond(batch([sample(seq)])); }
  assert.equal(p.writes.get("pulseWeights:innerHTML"), tableWrites);
  assert.equal(p.writes.get("pulseChart:width") + p.writes.get("pulseChart:height"), sizes);
  assert.ok(actions(p, "snapshot").every(call => call.options?.includePoints === false));
  assert.ok(actions(p, "push").every(call => call.options?.includePoints === false));
  await p.resize(600, 300, 2); assert.equal(p.nodes.get("pulseChart").width, 1200); assert.equal(p.nodes.get("pulseChart").height, 600);
}
{
  const p = page(); await p.respond(batch([sample(10)], { version: "0.14.0" }));
  assert.ok(p.nodes.get("pulseServiceVersion").textContent.includes(version)); assert.match(p.nodes.get("pulseServiceVersion").textContent, /采集服务 0\.14\.0（接口兼容）/);
}
{
  const p = page(); await p.respond({}, 404); await p.check("pulseUnloaded"); assert.equal(p.nodes.get("pulseNoiseBtn").disabled, true);
  assert.match(p.nodes.get("pulseServiceNotice").textContent, /受控关闭服务.*重新打开/); assert.equal(posts(p).length, 0);
}

// Real DSP: noise directly starts the live trace, hand placement/removal keeps
// calibration, user pause freezes its history, and transport recovery is automatic.
{
  const p = page({ realCore: true }); await p.ready();
  p.nodes.get("pulseBandLow").value = "0.5"; p.nodes.get("pulseBandHigh").value = "5";
  await p.fire("pulseFilterForm", "submit"); await p.tick(); await p.respond(batch([sample(11)]));
  let current = plain(p.session.snapshot());
  assert.deepEqual(current.filterBandHz, [.5, 5]); assert.equal(current.filterConfigSchema, filterConfigSchema);
  assert.equal(current.warmupSeconds, 4.2); assert.equal(current.maxGapSeconds, .09); assert.deepEqual(current.candidateBandHz, [.7, 3]);
  assert.equal(current.phase, "idle"); assert.equal(current.weights, null); assert.equal(p.nodes.get("pulseNoiseBtn").disabled, true);
  assert.match(p.nodes.get("pulseFilterHelp").textContent, /4\.2 s/); assert.match(p.nodes.get("pulseFilterHelp").textContent, /0\.7.*3.*Hz/);
  await p.check("pulseUnloaded"); await p.fire("pulseNoiseBtn"); await p.respond(batch([sample(12)]));
  await p.tick(); await p.respond(batch(Array.from({ length: 501 }, (_, i) => sample(i + 13))));
  current = plain(p.session.snapshot());
  assert.equal(current.phase, "monitor"); assert.deepEqual(current.filterBandHz, [.5, 5]);
  assert.deepEqual(current.calibration.filterBandHz, [.5, 5]); assert.deepEqual(current.calibration.candidateBandHz, [.7, 3]);
  assert.equal(current.calibration.filterConfigSchema, filterConfigSchema); assert.ok(current.weights[4] >= .5);
  assert.match(p.nodes.get("pulseFilterCurrent").textContent, /0\.5.*5.*Hz/); assert.match(p.nodes.get("pulseBandMetric").textContent, /0\.5.*5.*Hz/);
  assert.match(p.nodes.get("pulseChartNote").textContent, /0\.5.*5.*Hz/); assert.equal(posts(p).length, 0);
  const weights = plain(current.weights); await p.fire("pulseResetBtn");
  assert.deepEqual(plain(p.session.snapshot().filterBandHz), [.5, 5], "Re-recording noise retains the selected display band");
  assert.equal(p.session.snapshot().weights, null); assert.ok(weights); assert.equal(p.nodes.get("pulseUnloaded").checked, false);
}
{
  const p = page({ realCore: true }); await p.respond(batch([sample(1)]));
  await p.check("pulseUnloaded"); await p.fire("pulseNoiseBtn"); await p.respond(batch([sample(2)]));
  await p.tick(); await p.respond(batch(Array.from({ length: 251 }, (_, i) => sample(i + 3)))); const calibrated = plain(p.session.snapshot());
  assert.equal(calibrated.phase, "monitor"); assert.equal(calibrated.streamMode, mode); assert.ok(calibrated.weights[4] >= 0.5);
  assert.ok(calibrated.weights.every(weight => weight >= 0)); assert.ok(Math.abs(calibrated.weights.reduce((sum, weight) => sum + weight, 0) - 1) < 1e-12);
  assert.ok(calibrated.points.length > 0); assert.equal(calibrated.candidateBpm, null);
  for (const [start, signal] of [[254, 50], [281, 20], [308, 0]]) {
    await p.tick(); await p.respond(batch(Array.from({ length: 27 }, (_, i) => sample(i + start, signal)))); const current = plain(p.session.snapshot());
    assert.equal(current.phase, "monitor"); assert.deepEqual(current.weights, calibrated.weights); assert.deepEqual(current.noise, calibrated.noise);
    assert.deepEqual(current.baselinePm, calibrated.baselinePm); assert.ok(current.points.length >= calibrated.points.length);
  }
  const beforePause = plain(p.session.snapshot().points); await p.fire("pulseMonitorBtn");
  assert.equal(p.session.snapshot().phase, "calibrated_ready"); assert.deepEqual(plain(p.session.snapshot().points), beforePause);
  await p.fire("pulseMonitorBtn"); await p.respond(batch([sample(335)])); await p.tick(); await p.respond(batch([sample(336)]));
  assert.equal(p.session.snapshot().phase, "monitor"); await p.tick(); await p.reject(); const interrupted = plain(p.session.snapshot());
  assert.equal(interrupted.phase, "monitor"); assert.equal(interrupted.suspended, true); assert.deepEqual(interrupted.weights, calibrated.weights);
  assert.equal(interrupted.candidateBpm, null); assert.ok(interrupted.points.length > 0);
  await p.tick(); await p.respond(batch([sample(500)])); await p.tick(); await p.respond(batch([sample(501)])); const resumed = plain(p.session.snapshot());
  assert.equal(resumed.phase, "monitor"); assert.equal(resumed.suspended, false); assert.deepEqual(resumed.weights, calibrated.weights);
  assert.ok(resumed.points.at(-1).breakBefore);
}
// A usable source rate below the BPM-quality threshold must not discard the
// unloaded record. We accumulate original source time and enough real samples,
// then keep displaying the fixed-weight curve without claiming a pulse rate.
{
  const p = page({ realCore: true });
  const lowRateSample = (seq, signal = 0) => ({ ...sample(seq, signal), t_ns: Math.round(seq * 1e9 / 19) });
  await p.respond(batch([lowRateSample(1)]));
  await p.check("pulseUnloaded"); await p.fire("pulseNoiseBtn"); await p.respond(batch([lowRateSample(2)]));
  assert.equal(p.lease.isActive(), true, "Other GUI refreshes stay frozen throughout the unloaded acquisition");
  await p.tick(); await p.respond(batch(Array.from({ length: 45 }, (_, i) => lowRateSample(i + 3))));
  const recording = plain(p.session.snapshot());
  assert.equal(recording.phase, "noise", "A 19 Hz actual source remains a usable unloaded record after the former 2-second rejection point");
  assert.ok(recording.effectiveNoiseSeconds > 2); assert.ok(recording.rateHz < 20);
  assert.equal(p.lease.isActive(), true); assert.equal(p.nodes.get("pulseUnloaded").disabled, true);
  await p.tick(); await p.respond(batch(Array.from({ length: 166 }, (_, i) => lowRateSample(i + 48))));
  const calibrated = plain(p.session.snapshot());
  assert.equal(calibrated.phase, "monitor"); assert.equal(calibrated.calibration.noisePolicy, noisePolicy);
  assert.ok(calibrated.weights[4] >= .5); assert.ok(calibrated.effectiveNoiseSeconds >= 10 - 1e-9);
  assert.ok(calibrated.noiseFilteredSeconds >= 7 - 1e-9); assert.ok(calibrated.points.length >= 126);
  assert.equal(p.lease.isActive(), true); assert.equal(p.nodes.get("pulseNoiseBtn").disabled, true);
  for (let start = 214; start < 514; start += 50) {
    await p.tick(); await p.respond(batch(Array.from({ length: 50 }, (_, i) => {
      const seq = start + i; return lowRateSample(seq, Math.sin(seq / 19 * 2 * Math.PI));
    })));
  }
  const observed = plain(p.session.snapshot());
  assert.equal(observed.phase, "monitor"); assert.deepEqual(observed.weights, calibrated.weights);
  assert.ok(observed.points.length > calibrated.points.length); assert.equal(observed.candidateBpm, null);
  assert.equal(observed.quality.valid, false); assert.match(observed.reason, /不足 20 Hz.*曲线继续显示/);
  assert.equal(p.nodes.get("pulseBpm").innerHTML, "— <small>次/分</small>");
  assert.equal(p.lease.isActive(), true); assert.equal(posts(p).length, 0);
}
// Pixel reduction preserves source extrema and explicit gaps without changing
// stored source data, and stable geometry never reallocates the canvas.
{
  const renderer = require("../frontend/pulse.js"), calls = [];
  const ctx = new Proxy({}, { get(target, key) { return target[key] || ((...args) => calls.push([key, ...args])); }, set(target, key, value) { target[key] = value; return true; } });
  let dimensions = 0, canvasWidth = 0, canvasHeight = 0;
  const canvas = { getBoundingClientRect: () => ({ width: 300, height: 200 }), getContext: () => ctx,
    get width() { return canvasWidth; }, set width(value) { dimensions++; canvasWidth = value; },
    get height() { return canvasHeight; }, set height(value) { dimensions++; canvasHeight = value; } };
  const points = Array.from({ length: 12000 }, (_, i) => ({ t: i * 0.005, value: i === 11900 ? 4 : Math.sin(i / 10), breakBefore: i === 11950 }));
  const before = JSON.stringify(points), reduced = renderer.displayPoints(points, 0, 60, 215);
  assert.ok(reduced.some(point => point.value === 4), "A narrow peak survives pixel reduction");
  assert.ok(reduced.some(point => point.breakBefore), "Source discontinuities survive pixel reduction");
  renderer.draw(canvas, points, 60, 1); const firstDimensions = dimensions; renderer.draw(canvas, points, 60, 1);
  assert.equal(dimensions, firstDimensions); assert.equal(JSON.stringify(points), before);
  assert.ok(calls.filter(call => call[0] === "lineTo").length < 3000);
  assert.ok(calls.filter(call => call[0] === "moveTo").length >= 14);
  const continuous = Array.from({ length: 3000 }, (_, i) => ({ t: i * 0.02, value: i }));
  const narrow = renderer.displayPoints(continuous, 0, 60, 15);
  assert.ok(narrow.every(point => !point.breakBefore), "Pixel reduction cannot turn a continuous source into apparent missing data");
  const count = calls.length;
  renderer.draw({ width: 0, height: 0, getContext: () => ctx, getBoundingClientRect: () => ({ width: 100, height: 200 }) }, continuous, 60, 1);
  assert.equal(calls.slice(count).filter(call => call[0] === "moveTo").length, 6, "Five grid strokes and one continuous curve subpath remain at a narrow viewport");
}
console.log("Pulse UI continuous noise-fixed curve, explicit unloaded/reference gates, pause/resume, transient recovery, source boundaries, GUI priority and bounded drawing passed.");
