import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const chart = require("../frontend/wavelength_chart.js");
const fixture = (id, ns = id * 1e8, changes = {}) => {
  const frame = { frame_id: id, stream_epoch: "actual-stream", host_rx_monotonic_ns: ns, valid: true };
  const tracker = { ...frame, reference_version: 2, reference_nm: Array.from({ length: 9 }, (_, index) => 1510 + index), config_version: "frozen-config", algorithm_version: "raw-local-fit", display_age_ms: 0,
    peaks: Array.from({ length: 9 }, (_, index) => ({ id: "FBG" + (index + 1), value_frame_id: id, valid: true, stale: false, center_nm: 1510 + index + .003 * (index + 1), drift_nm: .003 * (index + 1) })), ...changes };
  return { running: true, frame, tracker };
};

// Polling reuses source frames. Only genuinely new monotonic source identities
// enter the chart, and polling cannot turn one measurement into a flat line.
const history = new chart.History();
assert.equal(history.ingest(fixture(1), 0), true);
assert.deepEqual(history.points[0].values.map(value => Number(value.toFixed(8))), [3, 6, 9, 12, 15, 18, 21, 24, 27]);
for (let index = 0; index < 15; index++) history.ingest(fixture(1), index * 100);
assert.equal(history.points.length, 1);
assert.equal(history.current.every(value => value === null), true, "Duplicate polls must not keep a stale source current");
history.ingest(fixture(2), 1600);
assert.equal(history.points[1].starts.every(Boolean), true, "A restored source starts a new segment after expiry");
assert.equal(history.current[0].nm, 1510.003);

// Fixed IDs retain color/order when results are shuffled or one peak is invalid.
const partial = fixture(3); partial.tracker.peaks.reverse();
partial.tracker.peaks.find(peak => peak.id === "FBG2").valid = false;
partial.tracker.peaks.find(peak => peak.id === "FBG4").drift_nm = NaN;
partial.tracker.peaks.find(peak => peak.id === "FBG5").stale = true;
partial.tracker.peaks.find(peak => peak.id === "FBG6").value_frame_id = 2;
history.ingest(partial, 1700);
assert.deepEqual(history.points.at(-1).values.map(value => value === null ? null : Number(value.toFixed(8))), [3, null, 9, null, null, null, 21, 24, 27]);
assert.equal(history.current[1], null);
history.ingest(fixture(4), 1800);
assert.equal(history.points.at(-1).starts[0], false);
for (const index of [1, 3, 4, 5]) assert.equal(history.points.at(-1).starts[index], true, "A valid peak after a missing/invalid/stale result cannot bridge its gap");
for (const value of [null, undefined, "other-source"]) {
  const model = new chart.History(), state = fixture(1);
  state.tracker.peaks[0].value_frame_id = value; model.ingest(state, 0);
  assert.equal(model.current[0], null, "A missing, null or wrong peak source ID cannot claim the current wavelength");
  assert.equal(model.current[1].pm, 6);
}
const sameFrame = new chart.History(); sameFrame.ingest(fixture(1), 0);
const sameFrameBad = fixture(1); sameFrameBad.tracker.peaks[0].valid = false;
sameFrame.ingest(sameFrameBad, 100); sameFrame.ingest(fixture(1), 200);
assert.equal(sameFrame.points.length, 1, "Changing quality within one source frame cannot append measurements");
assert.equal(sameFrame.current[0], null, "Same-frame recovery cannot revive a previously invalidated value");
assert.equal(sameFrame.current[1].pm, 6);
sameFrame.ingest(fixture(2), 300);
assert.equal(sameFrame.points.at(-1).starts[0], true);
assert.equal(sameFrame.points.at(-1).starts[1], false);

const count = history.points.length;
history.ingest(fixture(2, 5e8), 1900);
assert.equal(history.points.length, count, "A repeated non-adjacent identity is rejected even with a new timestamp");
history.ingest(fixture(5, 3e8), 2000);
assert.equal(history.points.length, count, "Out-of-order source clocks are rejected");
history.ingest(fixture(5, 5e8), 2100);
assert.equal(history.points.at(-1).starts.every(Boolean), true);
history.ingest(fixture(6, 2e9), 2200);
assert.equal(history.points.at(-1).starts.every(Boolean), true, "A source-time gap over 1 s breaks every line even if polling continues");

for (const [label, change] of [
  ["reference version", state => { state.tracker.reference_version++; }],
  ["reference wavelengths", state => { state.tracker.reference_nm[0] += .1; }],
  ["configuration", state => { state.tracker.config_version = "another-config"; }],
  ["algorithm", state => { state.tracker.algorithm_version = "another-algorithm"; }],
  ["stream", state => { state.tracker.stream_epoch = state.frame.stream_epoch = "next-stream"; }]
]) {
  const model = new chart.History(); model.ingest(fixture(1), 0); model.ingest(fixture(2), 100);
  const next = fixture(3); change(next); model.ingest(next, 200);
  assert.equal(model.points.length, 1, label + " changes clear history before accepting a point");
  assert.equal(model.points[0].starts.every(Boolean), true);
}
for (const mutate of [state => { state.running = false; }, state => { state.tracker.reference_nm = []; }, state => { state.tracker.reference_pending = true; }, state => { state.tracker.display_stale = true; }, state => { state.tracker.display_age_ms = 1001; }, state => { state.frame.valid = false; }, state => { state.tracker.valid = false; }, state => { state.frame.frame_id = "different-source"; }, state => { state.frame.host_rx_monotonic_ns++; }, state => { delete state.tracker.frame_id; }]) {
  const model = new chart.History(); model.ingest(fixture(1), 0);
  const bad = fixture(2); mutate(bad); model.ingest(bad, 100);
  assert.ok(model.points.length <= 1, "No invalid source may append a valid or zero sample");
  assert.equal(model.current.every(value => value === null), true);
  model.ingest(fixture(3), 200);
  assert.equal(model.points.at(-1).starts.every(Boolean), true);
}
const outage = new chart.History(); outage.ingest(fixture(1), 0); outage.ingest(null, 50);
assert.equal(outage.current.every(value => value === null), true);
outage.ingest(fixture(2), 100); assert.equal(outage.points[1].starts.every(Boolean), true);
const aging = new chart.History(); aging.ingest(fixture(1, 1e8, { display_age_ms: 950 }), 0); aging.tick(51);
assert.equal(aging.current.every(value => value === null), true, "Source age and time since reception both count independently of state requests");
const bounded = new chart.History();
for (let index = 1; index <= 4000; index++) bounded.ingest(fixture(index, index * 1e6), index);
assert.equal(bounded.points.length, 2400, "Even unusually fast callers cannot grow memory indefinitely");
bounded.ingest(fixture(4001, 200e9), 4001);
assert.equal(bounded.points.length, 1, "Source-time retention keeps at most 120 s");

// Exercise the real mounted renderer: source data, hidden tabs, stale values,
// window controls, resize and DPR changes all use an inert canvas/DOM.
const calls = [], nodes = new Map(), handlers = new Map();
let time = 0, shown = true, width = 420, height = 238, resize;
const ctx = new Proxy({}, { get(target, key) { return target[key] || ((...args) => calls.push([key, ...args])); }, set(target, key, value) { target[key] = value; return true; } });
const canvas = { width: 0, height: 0, getContext: () => ctx, getBoundingClientRect: () => ({ width, height }) };
const global = { Date: { now: () => time }, devicePixelRatio: 1,
  document: { getElementById: id => nodes.get(id), addEventListener: (type, callback) => handlers.set(type, callback) },
  ResizeObserver: class { constructor(callback) { resize = callback; } observe(target) { assert.equal(target, canvas); } } };
const status = { textContent: "" }, windowSelect = { value: "60", addEventListener: (type, callback) => handlers.set(type, callback) };
const legend = { set innerHTML(value) { for (const match of value.matchAll(/id="([^"]+)"/g)) nodes.set(match[1], { textContent: "" }); } };
const mounted = chart.mount({ canvas, status, legend, windowSelect, global, visible: () => shown });
mounted.update(fixture(1), time);
assert.equal(canvas.width, 420); assert.equal(canvas.height, 238);
assert.equal(nodes.get("expWavelengthValue1").textContent, "3.00 pm");
assert.equal(nodes.get("expWavelengthCenter1").textContent, "1510.00300 nm");
assert.match(status.textContent, /九峰参考 第 2 版.*九路有效.*60 s/);
assert.ok(calls.some(call => call[0] === "fillText" && call[1].includes("相对接收时间")));
const renderCount = () => calls.filter(call => call[0] === "clearRect").length;
const firstDraws = renderCount();
for (time = 1; time < 100; time++) mounted.update(fixture(time + 1, (time + 1) * 1e8), time);
assert.equal(renderCount(), firstDraws, "Paint rate is limited to 10 Hz even for fast input");
time = 100; mounted.tick(time); assert.equal(renderCount(), firstDraws + 1);
shown = false; handlers.get("visibilitychange")();
const hiddenCount = mounted.history.points.length, hiddenDraws = renderCount();
time = 300; mounted.update(fixture(200, 20e9), time);
assert.equal(mounted.history.points.length, hiddenCount); assert.equal(renderCount(), hiddenDraws);
shown = true; handlers.get("visibilitychange")();
assert.equal(nodes.get("expWavelengthValue1").textContent, "—", "Returning to a hidden chart never presents old values as current");
time = 400; mounted.update(fixture(201, 20.1e9), time);
assert.equal(mounted.history.points.at(-1).starts.every(Boolean), true, "Hidden time does not produce a connecting line");
time = 1501; mounted.tick(time);
assert.equal(nodes.get("expWavelengthValue1").textContent, "—");
assert.match(status.textContent, /超过 1 秒/);
time = 1700; width = 620; global.devicePixelRatio = 2; resize();
assert.equal(canvas.width, 1240); assert.equal(canvas.height, 476);
time = 1800; windowSelect.value = "30"; handlers.get("change")(); assert.match(status.textContent, /最近 30 s/);
time = 1900; windowSelect.value = "120"; handlers.get("change")(); assert.match(status.textContent, /最近 120 s/);
time = 2000; width = 0; const beforeZeroSize = renderCount(); resize(); assert.equal(renderCount(), beforeZeroSize, "Zero-sized hidden layouts do not repaint");
time = 2100; width = 320; global.devicePixelRatio = 3; resize(); assert.equal(canvas.width, 960);
// A collection pause holds every part of the demonstration renderer, including
// callbacks triggered outside update. Real acquisition is outside this module.
time=2200; mounted.update(fixture(300,30e9),time);
const beforeFreeze={points:JSON.stringify(mounted.history),draws:renderCount(),value:nodes.get("expWavelengthValue1").textContent,center:nodes.get("expWavelengthCenter1").textContent};
mounted.setFrozen(true); assert.equal(windowSelect.disabled,true);
assert.match(status.textContent,/采集期间显示已冻结.*快照/);
for(let index=1;index<=20;index++) {time+=100;mounted.update(fixture(300+index,30e9+index*1e8),time);mounted.tick(time);}
time+=2000; mounted.update(null,time); mounted.tick(time);width=500;global.devicePixelRatio=2;resize();
shown=false;handlers.get("visibilitychange")();shown=true;handlers.get("visibilitychange")();
windowSelect.value="30";handlers.get("change")();
assert.equal(renderCount(),beforeFreeze.draws);
assert.equal(JSON.stringify(mounted.history),beforeFreeze.points,"Frozen history neither grows nor ages into an invalid current value");
assert.equal(nodes.get("expWavelengthValue1").textContent,beforeFreeze.value);
assert.equal(nodes.get("expWavelengthCenter1").textContent,beforeFreeze.center);
mounted.setFrozen(false);assert.equal(windowSelect.disabled,false);assert.equal(mounted.history.points.length,0);
assert.equal(windowSelect.value,"120");
mounted.update(fixture(400,40e9),time);
assert.equal(mounted.history.points.length,1);assert.ok(mounted.history.points[0].starts.every(Boolean),"After collection ends, preview begins with a fresh unconnected sample");
assert.equal(mounted.history.points[0].t,40);assert.ok(renderCount()>beforeFreeze.draws);
mounted.update(null,time+100);assert.equal(nodes.get("expWavelengthValue1").textContent,"—","An idle service interruption immediately invalidates the preview");
assert.match(status.textContent,/服务状态中断/);
const emptyChart=chart.mount({canvas,status,legend,windowSelect,global,visible:()=>true});emptyChart.setFrozen(true);
const emptyDraws=renderCount();emptyChart.update(fixture(500,50e9),time+200);
assert.equal(emptyChart.history.points.length,0);assert.equal(renderCount(),emptyDraws);assert.match(status.textContent,/等待采集会话结束后显示新帧/);
assert.equal(chart.COLORS.length, 9); assert.equal(new Set(chart.COLORS).size, 9);
console.log("九光栅波长图验证通过：真实源帧去重、接收时钟与参考隔离、逐路失效断线、断流/乱序/隐藏恢复、120秒有界缓存、10Hz绘制、时间窗口/尺寸/DPR更新、采集冻结零缓冲增长与零绘图、所有事件旁路受控、结束后全新历史与空参考首屏。仅合成状态与惰性画布，无设备或训练请求。");
