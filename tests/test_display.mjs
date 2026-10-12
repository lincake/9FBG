import { createRequire } from "node:module";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const display = require("../frontend/display.js");
const { AXES, UNITS, normalizeForce, DisplayZero, validateSpectrum, validateRange, axisTicks, forceRange } = display;
const raw = (id, ms, values = [1, 2, 3, 4, 5, 6], epoch = "force-a") => ({
  frame_id: id, stream_epoch: epoch, host_rx_monotonic_ns: 1e9 + ms * 1e6,
  force_values: values, axes: AXES.slice(), units: UNITS.slice(), age_ms: 0
});
const normalized = (id, ms, values, epoch) => normalizeForce(raw(id, ms, values, epoch));

// Source identities and unit mapping must survive arbitrary wire ordering.
const shuffled = raw(1, 0, [60, 20, 40, 10, 50, 30]);
shuffled.axes = ["Mz", "Fy", "Mx", "Fx", "My", "Fz"];
shuffled.units = ["N·m", "N", "N·m", "N", "N·m", "N"];
const original = JSON.stringify(shuffled);
assert.deepEqual(normalizeForce(shuffled).values, [10, 20, 30, 40, 50, 60]);
assert.equal(JSON.stringify(shuffled), original, "Normalization cannot edit raw packets");
assert.equal(normalizeForce({ ...raw(1, 0), axes: undefined, units: undefined }).valid, true, "Existing fixed-order packets remain readable");
for (const bad of [
  { ...raw(1, 0), axes: ["Fx", "Fx", "Fz", "Mx", "My", "Mz"] },
  { ...raw(1, 0), axes: ["Q", "Fy", "Fz", "Mx", "My", "Mz"] },
  { ...raw(1, 0), axes: [] },
  { ...raw(1, 0), units: ["N", "N", "N", "N", "N·m", "N·m"] },
  { ...raw(1, 0), force_values: [1, 2, 3, 4, 5, Infinity] },
  { ...raw(1, 0), force_values: [1, 2, 3, 4, 5, "6"] },
  { ...raw(1, 0), force_values: [1, 2, 3, 4, 5] }
]) assert.equal(normalizeForce(bad).valid, false);
assert.equal(normalizeForce({ ...raw(1, 0), age_ms: 250 }).valid, true);
assert.equal(normalizeForce({ ...raw(1, 0), age_ms: 250.01 }).stale, true);
assert.equal(normalizeForce({ ...raw(1, 0), stale: true }).valid, false);
assert.equal(normalizeForce({ ...raw(1, 0), valid: false }).valid, false);
assert.equal(normalizeForce(raw(1, 0), { running: false }).valid, false);
assert.equal(normalizeForce(raw(1, 0), { connected: false }).valid, false);

// Exactly twelve unique source frames form the median; API polling adds none.
const zero = new DisplayZero();
assert.throws(() => zero.request(false, raw(1, 0)), /确认/);
assert.throws(() => zero.request(true, { force_values: [0, 0, 0, 0, 0, 0] }), /采集时段/);
assert.throws(() => zero.request(true, { ...raw(1, 0), age_ms: 400 }), /250/);
zero.request(true, raw(1, 0, [1000, 10, 20, 30, 40, 50]));
for (let repeat = 0; repeat < 20; repeat++) zero.update(normalized(1, 0, [1000, 10, 20, 30, 40, 50]));
assert.equal(zero.snapshot().count, 1);
for (let index = 1; index < 12; index++) zero.update(normalized(index + 1, index * 40, [index, 10, 20, 30, 40, 50]));
assert.equal(zero.snapshot().active, true);
assert.equal(zero.snapshot().pending, false);
assert.deepEqual(zero.snapshot().offsets, [6.5, 10, 20, 30, 40, 50], "A large isolated outlier cannot determine the median");
const values = Object.freeze([7.5, 11, 21, 31, 41, 51]);
assert.deepEqual(zero.correct(values), [1, 1, 1, 1, 1, 1]);
assert.deepEqual(values, [7.5, 11, 21, 31, 41, 51]);
const snapshot = zero.snapshot();
snapshot.offsets[0] = 999;
assert.equal(zero.snapshot().offsets[0], 6.5, "Snapshots cannot mutate applied display offsets");
assert.equal(zero.snapshot().version, 1);

// At the actual 50 Hz request, 12 frames are insufficient to cover 400 ms.
const rapid = new DisplayZero();
rapid.request(true, raw(1, 0, [1, 1, 1, 1, 1, 1]));
for (let index = 1; index < 12; index++) rapid.update(normalized(index + 1, index * 20, [1, 1, 1, 1, 1, 1]));
assert.equal(rapid.snapshot().pending, true);
assert.equal(rapid.snapshot().active, false);
assert.match(rapid.snapshot().reason, /400 ms/);
for (let index = 12; index <= 20; index++) rapid.update(normalized(index + 1, index * 20, [1, 1, 1, 1, 1, 1]));
assert.deepEqual(rapid.snapshot().offsets, [1, 1, 1, 1, 1, 1]);

// No applied or pending zero can cross an outage, reconnect, or reordered stream.
zero.update(normalizeForce(raw(13, 480), { connected: false }));
assert.equal(zero.snapshot().active, false);
assert.equal(zero.snapshot().offsets, null);
assert.equal(zero.snapshot().version, 2);
assert.deepEqual(zero.correct(values), values);
rapid.update(normalized(1, 420, [9, 9, 9, 9, 9, 9], "force-b"));
assert.equal(rapid.snapshot().active, false);
assert.equal(rapid.snapshot().pending, false);
assert.match(rapid.snapshot().reason, /采集时段/);
for (const interruption of [
  normalized(2, -1), // acquisition time went backwards
  normalized(0, 20), // source receive sequence went backwards
  normalizeForce({ ...raw(2, 20), stale: true })
]) {
  const pending = new DisplayZero();
  pending.request(true, raw(1, 0));
  pending.update(interruption);
  assert.equal(pending.snapshot().pending, false);
  assert.equal(pending.snapshot().count, 0);
}
rapid.request(true, raw(2, 440, [1, 1, 1, 1, 1, 1], "force-b"));
assert.equal(rapid.reset().count, 0);
assert.equal(rapid.snapshot().pending, false);

// A 350 ms state poll skips live frames without indicating a sensor outage.
const polled = new DisplayZero();
polled.request(true, raw(1, 0, [2, 2, 2, 2, 2, 2]));
for (let index = 1; index < 12; index++) polled.update(normalized(index * 20 + 1, index * 400, [2, 2, 2, 2, 2, 2]));
assert.deepEqual(polled.snapshot().offsets, [2, 2, 2, 2, 2, 2], "Fresh source frames separated by GUI polling can complete zeroing");
polled.update(normalized(261, 5200, [3, 3, 3, 3, 3, 3]));
assert.equal(polled.snapshot().active, true, "Skipped live frames cannot invalidate a fresh display zero");

// Actual nonuniform calibration coordinates are preserved, never resampled.
const spectrum = {
  wavelength_nm: Array.from({ length: 512 }, (_, index) => 1510 + index * .03 + index * index * 1e-6),
  intensity: Array.from({ length: 512 }, (_, index) => index * 2)
};
const spectrumBefore = JSON.stringify(spectrum);
assert.equal(validateSpectrum(spectrum).valid, true);
assert.deepEqual(validateSpectrum(spectrum).xRange, [spectrum.wavelength_nm[0], spectrum.wavelength_nm[511]]);
assert.equal(JSON.stringify(spectrum), spectrumBefore);
assert.equal(validateSpectrum({ ...spectrum, wavelength_nm: spectrum.wavelength_nm.slice(1) }).valid, false);
const duplicateAxis = spectrum.wavelength_nm.slice();
duplicateAxis[100] = duplicateAxis[99];
assert.equal(validateSpectrum({ ...spectrum, wavelength_nm: duplicateAxis }).valid, false);
assert.equal(validateSpectrum({ ...spectrum, wavelength_nm: spectrum.wavelength_nm.slice().reverse() }).valid, false);
const invalidValues = spectrum.intensity.slice(); invalidValues[0] = NaN;
assert.equal(validateSpectrum({ ...spectrum, intensity: invalidValues }).valid, false);
const flatRange = validateSpectrum({ ...spectrum, intensity: Array(512).fill(20) }).yRange;
assert.ok(flatRange[0] < 20 && flatRange[1] > 20);

assert.deepEqual(validateRange(-2, 3, "力范围"), [-2, 3]);
assert.throws(() => validateRange(3, 3, "力范围"), /力范围/);
assert.throws(() => validateRange("1", 3), /有限数值/);
const ticks = axisTicks([-.002, .002]);
assert.deepEqual(ticks.values, [-.002, -.001, 0, .001, .002]);
assert.equal(ticks.format(.001), "0.001");
assert.equal(ticks.format(-0), "0.000");
assert.ok(axisTicks([1510, 1590]).values.every(value => value >= 1510 && value <= 1590));
assert.deepEqual(forceRange([]), [-1, 1]);
assert.deepEqual(forceRange([0, 0]), [-1, 1]);
assert.deepEqual(forceRange([1, -2]), [-2.3, 2.3]);
assert.deepEqual(forceRange([99], 5), [-5, 5]);
assert.throws(() => forceRange([1], -1), /正数/);

// The browser export needs no DOM, network, hardware command or persistence.
const browser = vm.createContext({});
vm.runInContext(readFileSync(new URL("../frontend/display.js", import.meta.url), "utf8"), browser);
assert.equal(typeof browser.NineFBGDisplay.DisplayZero, "function");
assert.equal(typeof browser.NineFBGDisplay.normalizeForce, "function");
console.log("显示模块验证通过：六轴身份和单位、时效、12 帧中位数置零、断线失效、实际谱轴及原始数据保全。");
