import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";
const require = createRequire(import.meta.url);
const { PulseSession, FILTER_CONFIG_SCHEMA, FILTER_LIMITS_HZ, CANDIDATE_BAND_HZ, validateFilterBandHz, NOISE_SECONDS, CENTER_CHANNEL, CENTER_MIN_WEIGHT, NOISE_FLOOR_PM, CALIBRATION_METHOD, STREAM_MODE, NOISE_POLICY, MAX_GAP_SECONDS, PAUSE_GAP_SECONDS } = require("../frontend/pulse_signal.js");
assert.equal(NOISE_SECONDS, 10);
assert.equal(CENTER_CHANNEL, 4);
assert.equal(CENTER_MIN_WEIGHT, 0.5);
assert.equal(NOISE_FLOOR_PM, 0.02);
assert.equal(CALIBRATION_METHOD, "unloaded-noise-fbg5-prior-v1");
assert.equal(STREAM_MODE, "continuous-noise-fixed-v1");
assert.equal(NOISE_POLICY, "effective-source-time-and-samples-v1");
assert.equal(MAX_GAP_SECONDS, 0.12, "120 ms remains the filter segment boundary");
assert.equal(PAUSE_GAP_SECONDS, 1);
assert.equal(FILTER_CONFIG_SCHEMA, "pulse-bandpass-config-v1");
assert.deepEqual(FILTER_LIMITS_HZ, [0.1, 8]);
assert.deepEqual(CANDIDATE_BAND_HZ, [0.7, 3]);

let seed = 743199;
const random = () => { seed = (1664525 * seed + 1013904223) >>> 0; return seed / 4294967296; };
const noise = () => (random() + random() + random() - 1.5) * 0.3;
const gratings = Array.from({ length: 9 }, (_, j) => j);
function source(rate = 50, binding = "reference-a", jitter = false) {
  let seq = 0, t = 100;
  return {
    next(values, extra = {}) {
      const sample = { seq: ++seq, t_ns: Math.round(t * 1e9), frame_id: seq, stream_epoch: "epoch-a", source_kind: "hardware", binding, values_pm: values, ...extra };
      t += 1 / rate * (jitter ? (seq % 2 ? 0.6 : 1.4) : 1);
      return sample;
    },
    advance(seconds) { t += seconds; },
    get count() { return seq; },
    feed(session, duration, fn) {
      const list = [];
      for (let i = 0; i <= duration * rate; i++) list.push(this.next(fn(t)));
      // Source batch size can vary without changing the acquisition rate.
      for (let i = 0; i < list.length; i += 37) session.push(list.slice(i, i + 37));
    }
  };
}
const quiet = () => gratings.map(() => 100 + noise());
const pulse = t => gratings.map(() => 110 + 2 * Math.sin(2 * Math.PI * 1.2 * t) + 0.2 * Math.sin(2 * Math.PI * 2.4 * t) + noise());
function learned(rate = 50, jitter = false) {
  const session = new PulseSession(), stream = source(rate, "reference-a", jitter);
  session.beginNoise({ confirmed: true }); stream.feed(session, 10.1, quiet);
  const snap = session.snapshot();
  assert.equal(snap.phase, "monitor", snap.reason);
  assert.equal(snap.curveKind, "monitor");
  assert.ok(snap.points.length >= 126, "Preview is actual unloaded filtered samples");
  assert.equal(snap.calibration.method, CALIBRATION_METHOD);
  assert.equal(snap.noisePolicy, NOISE_POLICY);
  assert.equal(snap.calibration.noisePolicy, NOISE_POLICY);
  assert.equal(snap.calibration.rawSampleCount, snap.noiseRawSamples);
  assert.equal(snap.calibration.filteredSampleCount, snap.noiseFilteredSamples);
  assert.ok(snap.noiseRawSamples >= 181); assert.ok(snap.noiseFilteredSamples >= 126);
  assert.equal(snap.calibration.centerChannel, "FBG5");
  assert.equal(snap.calibration.responseDirection, "not-inferred");
  assert.equal(snap.calibration.responseSensitivity, "not-measured");
  assert.equal(snap.candidateBpm, null, "Unloaded preparation never qualifies as pulse");
  session.beginMonitor({ confirmed: true });
  assert.equal(session.snapshot().phase, "monitor", session.snapshot().reason);
  return { session, stream };
}

// Short gaps retain actual unloaded samples, count only contiguous coverage,
// and restart each filter's warmup. Extra real samples complete the baseline;
// elapsed wall time and missing source intervals never fill the ten seconds.
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 4, quiet);
  const before = session.snapshot();
  const rowsBefore = session.rawNoiseRows.length;
  stream.advance(0.2); session.push(stream.next(quiet()));
  const split = session.snapshot();
  assert.equal(split.phase, "noise", split.reason);
  assert.equal(split.gapCount, 1);
  assert.ok(Math.abs(split.maxGapMs - 220) < 0.001);
  assert.equal(split.effectiveNoiseSeconds, before.effectiveNoiseSeconds);
  assert.equal(split.noiseFilteredSeconds, before.noiseFilteredSeconds);
  assert.equal(session.rawNoiseRows.length, rowsBefore + 1, "The gap boundary is a real retained raw sample");
  stream.feed(session, 6, quiet);
  assert.equal(session.snapshot().phase, "noise", "Bandpass coverage must be replenished after segment warmup");
  assert.ok(session.snapshot().effectiveNoiseSeconds >= 10);
  assert.ok(session.snapshot().noiseFilteredSeconds < 7);
  stream.feed(session, 3, quiet);
  const done = session.snapshot();
  assert.equal(done.phase, "monitor", done.reason);
  assert.ok(done.effectiveNoiseSeconds >= 10);
  assert.ok(done.noiseFilteredSeconds >= 7 - 1e-9);
  assert.ok(done.baselinePm.every(x => Math.abs(x - 100) < 0.1));
}
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 4.5, quiet);
  stream.advance(0.8); stream.feed(session, 4.5, quiet);
  assert.equal(session.snapshot().phase, "noise", "Over ten wall seconds do not represent ten valid seconds");
  assert.ok(session.snapshot().effectiveNoiseSeconds < 10);
}

// Frequent gaps that never permit three seconds of filter warmup cannot
// silently qualify or accumulate unbounded memory. Diagnostics remain visible.
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true });
  for (let i = 0; i < 65 && session.snapshot().phase === "noise"; i++) {
    stream.feed(session, 1, quiet); stream.advance(0.15);
  }
  const snap = session.snapshot();
  assert.equal(snap.phase, "failed");
  assert.match(snap.reason, /有效采样不足|缺帧/);
  assert.ok(snap.gapCount > 20);
  assert.equal(snap.noiseFilteredSeconds, 0);
  assert.equal(snap.noise, null);
}

assert.throws(() => new PulseSession().beginNoise(), /确认/);
assert.throws(() => new PulseSession().beginMonitor({ confirmed: true }), /无载/);
assert.throws(() => new PulseSession().beginMonitor(), /无载/);

// All ten seconds are required. Raw unloaded statistics retain the first
// three seconds, while only the pulse bandpass needs a filter warmup.
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 8.1, quiet);
  assert.equal(session.snapshot().phase, "noise");
  assert.ok(session.snapshot().progress < 1);
  stream.feed(session, 2.1, quiet);
  assert.equal(session.snapshot().phase, "monitor");
}

// A completely quiet baseline still yields finite, positive sum-one weights.
// Equal measured noise gives FBG5 5/9; every other FBG receives 1/18.
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 10.1, () => gratings.map(() => 100));
  const snap = session.snapshot();
  assert.equal(snap.phase, "monitor", snap.reason);
  assert.ok(Math.abs(snap.weights[CENTER_CHANNEL] - 5 / 9) < 1e-12);
  for (const j of gratings.filter(j => j !== CENTER_CHANNEL)) assert.ok(Math.abs(snap.weights[j] - 1 / 18) < 1e-12);
  assert.ok(Math.abs(snap.weights.reduce((sum, w) => sum + w, 0) - 1) < 1e-12);
  assert.ok(snap.weights.every(w => Number.isFinite(w) && w > 0));
  assert.equal(snap.calibration.probeDiameterMm, undefined, "No physical press is claimed");
  assert.equal(snap.calibration.responsePm, undefined);
  assert.equal(typeof session.beginLearn, "undefined", "No press stage remains");
  const preview = snap.points.map(point => ({ ...point }));
  const frozen = snap.weights.slice();
  snap.weights[0] = 999; snap.calibration.channels[0] = 999;
  snap.calibration.filterBandHz[0] = 999; snap.points[0].value = 999;
  assert.deepEqual(session.snapshot().weights, frozen);
  assert.notEqual(session.snapshot().calibration.channels[0], 999);
  assert.notEqual(session.snapshot().calibration.filterBandHz[0], 999);
  assert.deepEqual(session.snapshot().points, preview);
  session.beginMonitor({ confirmed: true }); stream.feed(session, 12, () => gratings.map(() => 100));
  assert.equal(session.snapshot().candidateBpm, null);
}

// Only measured band noise and the declared FBG5 prior determine the formula.
// Quieter gratings get more auxiliary weight; centre remains >=50% even when
// noisy. This is not a mechanical sensitivity or polarity calibration.
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true });
  stream.feed(session, 10.1, t => gratings.map(j => 100 + (j === 0 ? 2 : j === CENTER_CHANNEL ? 6 : 0.05) * Math.sin(2 * Math.PI * 1.4 * t) + noise()));
  const snap = session.snapshot();
  const precision = snap.noise.map(sigma => 1 / Math.max(sigma, NOISE_FLOOR_PM) ** 2);
  const total = precision.reduce((sum, value) => sum + value, 0);
  gratings.forEach(j => assert.ok(Math.abs(snap.weights[j] - (0.5 * precision[j] / total + (j === CENTER_CHANNEL ? 0.5 : 0))) < 1e-12));
  assert.ok(snap.weights[CENTER_CHANNEL] >= 0.5);
  assert.ok(snap.weights[0] < snap.weights[1]);
  assert.ok(snap.weights.every(w => w >= 0));
  assert.equal(snap.candidateBpm, null);
}

// Real source timing and fixed positive weights produce the candidate curve.
{
  const { session, stream } = learned();
  const weights = session.snapshot().weights;
  assert.ok(Math.abs(weights.reduce((sum, w) => sum + Math.abs(w), 0) - 1) < 1e-12);
  assert.ok(weights.every(w => w > 0));
  assert.ok(weights[CENTER_CHANNEL] >= 0.5);
  assert.ok(session.snapshot().rateHz >= 20, "Monitoring resumes immediately from real source timing");
  assert.equal(session.snapshot().baselinePm.length, 9);
  assert.ok(session.snapshot().baselinePm.every(x => Math.abs(x - 100) < 0.1));
  assert.equal(session.snapshot().candidateBpm, null, "Monitoring begins with a fresh 3-second warmup");
  stream.feed(session, 12, pulse);
  const snap = session.snapshot();
  assert.ok(snap.quality.valid, snap.reason);
  assert.ok(Math.abs(snap.candidateBpm - 72) < 2, String(snap.candidateBpm));
  assert.ok(snap.quality.centerSnr >= 3);
  assert.ok(snap.quality.centerCorrelation >= 0.65);
  assert.deepEqual(snap.weights, weights, "Contact and monitoring never recalculate weights");
  stream.feed(session, 65, pulse);
  const longer = session.snapshot();
  assert.ok(longer.points.at(-1).t - longer.points[0].t > 59.8);
  assert.ok(longer.points.at(-1).t - longer.points[0].t <= 60.001);
  assert.ok(longer.quality.valid, "Sixty-second history must match twelve-second quality rows");
}

// A pulse localized at the known centre can qualify even when auxiliary
// gratings contain only noise. Their separate correlation is not required.
{
  const { session, stream } = learned();
  const weights = session.snapshot().weights;
  stream.feed(session, 12, t => gratings.map(j => 110 + (j === CENTER_CHANNEL ? 2 * Math.sin(2 * Math.PI * 1.2 * t) : 0) + noise()));
  assert.ok(session.snapshot().quality.valid, session.snapshot().reason);
  assert.ok(Math.abs(session.snapshot().candidateBpm - 72) < 2);
  assert.deepEqual(session.snapshot().weights, weights);
}

// Strong periodic auxiliary signals cannot certify pulse while FBG5 remains
// at its unloaded noise level, even if the fused waveform looks periodic.
{
  const { session, stream } = learned();
  stream.feed(session, 12, t => gratings.map(j => 110 + (j === CENTER_CHANNEL ? 0 : 4 * Math.sin(2 * Math.PI * 1.2 * t)) + noise()));
  const snap = session.snapshot();
  assert.equal(snap.quality.valid, false);
  assert.equal(snap.candidateBpm, null);
  assert.ok(snap.quality.centerSnr < 3);
}

// Noise cannot infer a channel's mechanical response direction. Opposite
// contact responses may cancel; they must never silently reverse weights.
{
  const { session, stream } = learned();
  const frozen = session.snapshot().weights;
  stream.feed(session, 12, t => gratings.map(j => 110 + (j % 2 ? -1 : 1) * 2 * Math.sin(2 * Math.PI * 1.2 * t) + noise()));
  assert.deepEqual(session.snapshot().weights, frozen);
  assert.ok(session.snapshot().weights.every(w => w > 0));
}

// Stop, long preparation pauses and repeated measurement all retain the exact
// same calibration. Stale duplicated frames remain rejected after a restart.
{
  const { session, stream } = learned();
  const frozen = session.snapshot().weights;
  stream.feed(session, 12, pulse);
  assert.ok(session.snapshot().candidateBpm);
  session.stopMonitor();
  const stopped = session.snapshot();
  assert.equal(stopped.phase, "calibrated_ready");
  assert.equal(stopped.curveKind, "monitor");
  assert.equal(stopped.candidateBpm, null);
  assert.deepEqual(stopped.weights, frozen);
  session.stopMonitor();
  const old = stream.next(quiet()); session.push(old);
  stream.advance(30); session.push(stream.next(quiet()));
  assert.equal(session.snapshot().phase, "calibrated_ready", "A preparation pause is not an acquisition gap");
  session.stopMonitor();
  const count = session.snapshot().processedCount;
  session.beginMonitor({ confirmed: true }); session.push(old);
  assert.equal(session.snapshot().processedCount, count);
  stream.feed(session, 12, pulse);
  assert.equal(session.snapshot().phase, "monitor", session.snapshot().reason);
  assert.deepEqual(session.snapshot().weights, frozen);
  assert.ok(session.snapshot().candidateBpm, session.snapshot().reason);
  session.reset();
  assert.equal(session.snapshot().weights, null);
  assert.equal(session.snapshot().calibration, null);
}

// Actual unloaded random noise establishes the weights, never a candidate.
{
  const { session, stream } = learned();
  stream.feed(session, 12, quiet);
  assert.equal(session.snapshot().candidateBpm, null);
  assert.ok(session.snapshot().quality.snr < 3);
}

// Narrow-band interference already present unloaded cannot qualify merely
// by looking periodic after contact. Full covariance retains common noise.
for (const common of [
  t => gratings.map(j => 100 + (j % 2 ? -1 : 1) * Math.sin(2 * Math.PI * 1.3 * t) + noise()),
  t => gratings.map(() => 100 + 2 * Math.sin(2 * Math.PI * 1.3 * t) + noise())
]) {
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 10.1, common);
  assert.equal(session.snapshot().phase, "monitor");
  session.beginMonitor({ confirmed: true }); stream.feed(session, 12, common);
  const snap = session.snapshot();
  assert.equal(snap.candidateBpm, null, snap.reason);
  assert.ok(snap.quality.snr < 3, "Common unloaded vibration must not gain artificial sqrt(9) SNR");
}

// A noisy centre cannot be replaced by a quieter distant periodic channel.
{
  const session = new PulseSession(), stream = source();
  const noisyCentre = () => gratings.map(j => 100 + noise() * (j === CENTER_CHANNEL ? 30 : 1));
  session.beginNoise({ confirmed: true }); stream.feed(session, 10.1, noisyCentre);
  const frozen = session.snapshot().weights;
  assert.ok(frozen[CENTER_CHANNEL] >= 0.5);
  session.beginMonitor({ confirmed: true });
  stream.feed(session, 12, t => gratings.map(j => 110 + (j === CENTER_CHANNEL ? noise() * 30 : 4 * Math.sin(2 * Math.PI * 1.2 * t) + noise())));
  assert.equal(session.snapshot().candidateBpm, null);
  assert.deepEqual(session.snapshot().weights, frozen);
}

for (const [name, mutate] of [
  ["reference", sample => ({ ...sample, binding: "reference-b" })],
  ["epoch", sample => ({ ...sample, stream_epoch: "epoch-b" })],
  ["epoch with restarted frame counter", sample => ({ ...sample, seq: 0, frame_id: 0, stream_epoch: "epoch-b" })],
  ["changed hardware source", sample => ({ ...sample, source_kind: "other-hardware" })],
  ["missing weighted channel", sample => ({ ...sample, values_pm: sample.values_pm.map((x, j) => j === 0 ? null : x) })],
  ["missing FBG5 centre", sample => ({ ...sample, values_pm: sample.values_pm.map((x, j) => j === CENTER_CHANNEL ? null : x) })]
]) {
  const { session, stream } = learned();
  stream.feed(session, 12, pulse); assert.ok(session.snapshot().candidateBpm);
  session.push(mutate(stream.next(pulse(1))));
  const snap = session.snapshot();
  assert.equal(snap.phase, "failed", name); assert.equal(snap.weights, null, name);
  assert.equal(snap.candidateBpm, null, name); assert.equal(snap.points.length, 0, name);
  assert.equal(snap.baselinePm, null, name);
}

// A short acquisition stall splits the curve and periodic estimator while
// retaining the exact fixed weights. Nothing crosses the missing interval,
// and warmup stays visible without contributing to a candidate.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  const weights = session.snapshot().weights;
  const beforePoints = session.snapshot().points;
  assert.ok(session.snapshot().candidateBpm);
  stream.advance(0.2); session.push(stream.next(pulse(1)));
  let snap = session.snapshot();
  assert.equal(snap.phase, "monitor", snap.reason);
  assert.deepEqual(snap.weights, weights);
  assert.equal(snap.points.length, beforePoints.length + 1); assert.equal(snap.candidateBpm, null);
  assert.deepEqual(snap.points.slice(0, beforePoints.length), beforePoints);
  assert.equal(snap.points.at(-1).breakBefore, true);
  assert.equal(snap.points.at(-1).warmup, true);
  assert.equal(snap.gapCount, 1);
  stream.feed(session, 2.9, pulse);
  assert.ok(session.snapshot().points.length > snap.points.length);
  assert.equal(session.snapshot().points.at(-1).warmup, true);
  assert.equal(session.snapshot().candidateBpm, null);
  stream.feed(session, 9, pulse);
  snap = session.snapshot();
  assert.ok(Math.abs(snap.candidateBpm - 72) < 2, snap.reason);
  assert.deepEqual(snap.weights, weights);
}

// The original 120 ms boundary remains inclusive. Just above it resets the
// pulse segment, while anything below one second remains recoverable.
{
  const { session, stream } = learned();
  stream.feed(session, 4, pulse);
  stream.advance(0.1); session.push(stream.next(pulse(1)));
  assert.equal(session.snapshot().gapCount, 0);
  stream.advance(0.101); session.push(stream.next(pulse(1)));
  assert.equal(session.snapshot().phase, "monitor");
  assert.equal(session.snapshot().gapCount, 1);
  assert.equal(session.snapshot().points.at(-1).breakBefore, true);
  stream.advance(0.979); session.push(stream.next(pulse(1)));
  assert.equal(session.snapshot().phase, "monitor", "999 ms is segmented, not treated as a full-second interruption");
  assert.equal(session.snapshot().gapCount, 2);
}

// A long source gap retains the real curve and same-binding weights, splits
// the history, and automatically resumes without carrying a stale candidate.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  const frozen = session.snapshot().weights, calibration = session.snapshot().calibration;
  stream.advance(0.98); const gap = stream.next(pulse(1)); session.push(gap);
  const paused = session.snapshot();
  assert.equal(paused.phase, "monitor", paused.reason);
  assert.match(paused.reason, /缺口/);
  assert.deepEqual(paused.weights, frozen); assert.deepEqual(paused.calibration, calibration);
  assert.ok(Number.isFinite(paused.valuePm)); assert.equal(paused.candidateBpm, null); assert.ok(paused.points.length > 0);
  assert.equal(paused.points.at(-1).breakBefore, true);
  assert.equal(paused.points.at(-1).warmup, true);
  stream.feed(session, 1, pulse);
  assert.equal(session.snapshot().phase, "monitor");
  session.beginMonitor({ confirmed: true }); const count = session.snapshot().processedCount;
  session.push(gap); assert.equal(session.snapshot().processedCount, count);
  stream.feed(session, 12, pulse);
  assert.ok(session.snapshot().candidateBpm, session.snapshot().reason);
  assert.deepEqual(session.snapshot().weights, frozen);
}

// Transport pause uses the same retention rules; a hard interrupt or a changed
// reference/epoch still revokes every reusable value.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  const frozen = session.snapshot().weights;
  session.pause("网络暂时中断");
  assert.equal(session.snapshot().phase, "monitor");
  assert.equal(session.snapshot().suspended, true);
  assert.deepEqual(session.snapshot().weights, frozen);
  assert.equal(session.snapshot().candidateBpm, null);
  session.push(stream.next(quiet(), { stream_epoch: "epoch-b" }));
  assert.equal(session.snapshot().phase, "failed"); assert.equal(session.snapshot().weights, null);
}
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 4, quiet);
  session.pause("采集超时");
  assert.equal(session.snapshot().phase, "failed"); assert.equal(session.snapshot().noise, null);
  assert.match(session.snapshot().reason, /无载.*未完成/);
}
// Even while waiting with reusable weights, changed reference/configuration
// binding, source epoch or unavailable participating channels invalidate them.
for (const mutate of [
  sample => ({ ...sample, binding: "reference-a|different-config-or-peak-algorithm" }),
  sample => ({ ...sample, stream_epoch: "epoch-b" }),
  sample => ({ ...sample, values_pm: sample.values_pm.map((x, j) => j === 0 ? null : x) })
]) {
  const { session, stream } = learned(); session.stopMonitor();
  session.push(mutate(stream.next(quiet())));
  assert.equal(session.snapshot().phase, "failed");
  assert.equal(session.snapshot().weights, null);
  assert.equal(session.snapshot().calibration, null);
}

// A persistent low source rate leaves the real curve visible with fixed weights.
// The quality threshold is unchanged and cannot yield a candidate at ten hertz.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  const frozen = session.snapshot().weights;
  const first = stream.next(quiet());
  for (let i = 0; i < 45; i++) session.push({ ...first, seq: first.seq + i, frame_id: first.frame_id + i, t_ns: first.t_ns + i * 100e6 });
  const snap = session.snapshot();
  assert.equal(snap.phase, "monitor", snap.reason);
  assert.match(snap.reason, /20 Hz/);
  assert.deepEqual(snap.weights, frozen);
  assert.equal(snap.candidateBpm, null); assert.ok(snap.points.length > 0);
}

// Simulation and replay identifiers never seed a calibration session.
for (const sourceKind of ["synthetic", "replay", "模拟光谱"]) {
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true }); session.push(stream.next(quiet(), { source_kind: sourceKind }));
  assert.equal(session.snapshot().phase, "failed");
  assert.equal(session.snapshot().processedCount, 0);
}

// Sudden motion clears the candidate and filter state immediately while
// preserving visible real history and showing the new warmup segment.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  assert.ok(session.snapshot().candidateBpm);
  const weights = session.snapshot().weights;
  const oldHistory = session.snapshot().points;
  session.push(stream.next(gratings.map(() => 180)));
  assert.equal(session.snapshot().phase, "monitor");
  assert.equal(session.snapshot().candidateBpm, null);
  assert.deepEqual(session.snapshot().points.slice(0, oldHistory.length), oldHistory);
  assert.equal(session.snapshot().points.at(-1).breakBefore, true);
  assert.equal(session.snapshot().points.at(-1).warmup, true);
  assert.deepEqual(session.snapshot().weights, weights);
  assert.match(session.snapshot().reason, /明显移动/);
  stream.feed(session, 2.9, pulse);
  assert.equal(session.snapshot().candidateBpm, null);
  stream.feed(session, 10, pulse);
  assert.ok(session.snapshot().candidateBpm, session.snapshot().reason);
  assert.deepEqual(session.snapshot().weights, weights);
}

// A single low-weight auxiliary jump above the old per-channel threshold
// must not wipe a useful centre waveform. A large centre jump starts a new segment.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  const before = session.snapshot(), values = before.current.slice();
  values[0] += 10;
  session.push(stream.next(values));
  assert.ok(session.snapshot().points.length >= before.points.length);
  assert.ok(session.snapshot().candidateBpm, session.snapshot().reason);
  assert.deepEqual(session.snapshot().weights, before.weights);
  values[CENTER_CHANNEL] += 10;
  session.push(stream.next(values));
  assert.ok(session.snapshot().points.length > before.points.length);
  assert.equal(session.snapshot().points.at(-1).breakBefore, true);
  assert.equal(session.snapshot().candidateBpm, null);
  assert.deepEqual(session.snapshot().weights, before.weights);
}

// Movement that materially changes the fused signal restarts the window
// even when FBG5 itself did not jump; auxiliary influences cannot pass unseen.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  const before = session.snapshot();
  session.push(stream.next(before.current.map((value, j) => value + (j === CENTER_CHANNEL ? 0 : 100))));
  assert.ok(session.snapshot().points.length > before.points.length);
  assert.equal(session.snapshot().points.at(-1).breakBefore, true);
  assert.equal(session.snapshot().candidateBpm, null);
  assert.deepEqual(session.snapshot().weights, before.weights);
}

// Slow baseline drift and natural small displacement need not erase the
// curve or recalibrate weights. Only a qualified periodic centre signal is
// displayed; this synthetic check is not physical movement qualification.
{
  const { session, stream } = learned();
  const weights = session.snapshot().weights;
  let firstT;
  stream.feed(session, 14, t => {
    firstT ??= t;
    return pulse(t).map(value => value + 0.2 * (t - firstT));
  });
  assert.ok(session.snapshot().candidateBpm, session.snapshot().reason);
  assert.ok(session.snapshot().points.at(-1).t - session.snapshot().points[0].t >= 10);
  assert.deepEqual(session.snapshot().weights, weights);
}

// Duplicate/reordered frames never become samples, even in repeated API polls.
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true });
  const first = stream.next(quiet()); session.push(first);
  for (let i = 0; i < 50; i++) session.push(first);
  session.push({ ...first, seq: first.seq + 1, frame_id: 2, t_ns: first.t_ns - 1 });
  assert.equal(session.snapshot().processedCount, 1);
  assert.equal(session.snapshot().rejectedCount, 51);
  assert.equal(session.snapshot().progress, 0);
}

// Preparation pauses keep their watermark: an old unloaded frame cannot
// seed a new monitor or be consumed twice after a thirty-second contact pause.
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 10.1, quiet);
  const last = stream.next(quiet()); session.push(last);
  const count = session.snapshot().processedCount;
  session.beginMonitor({ confirmed: true }); session.push(last);
  assert.equal(session.snapshot().processedCount, count);
  stream.advance(30); session.push(stream.next(pulse(1)));
  assert.equal(session.snapshot().phase, "monitor");
  assert.equal(session.snapshot().processedCount, count + 1);
  assert.equal(session.snapshot().candidateBpm, null);
}

// An unloaded estimate needs actual duration and actual sample counts, not a
// hard twenty-hertz gate shared with the candidate estimator. Slower sources
// keep collecting, and their timestamps remain honest after auto-completion.
for (const rate of [19.5, 18, 15, 10]) {
  const session = new PulseSession(), stream = source(rate);
  session.beginNoise({ confirmed: true }); stream.feed(session, 9.8, quiet);
  const beforeTen = session.snapshot();
  assert.equal(beforeTen.phase, "noise", `${rate} Hz: ${beforeTen.reason}`);
  assert.ok(beforeTen.effectiveNoiseSeconds < 10);
  assert.ok(beforeTen.progress < 1, "Actual ten-second coverage is required");
  assert.equal(beforeTen.noiseRawSamples, stream.count);
  assert.equal(beforeTen.processedCount, stream.count);
  assert.equal(beforeTen.rejectedCount, 0);
  stream.feed(session, 0.3, quiet);
  const afterTen = session.snapshot();
  if (rate < 18) {
    assert.equal(afterTen.phase, "noise", afterTen.reason);
    assert.ok(afterTen.effectiveNoiseSeconds >= 10);
    assert.ok(afterTen.noiseFilteredSeconds >= 7);
    assert.ok(afterTen.noiseRawSamples < 181 || afterTen.noiseFilteredSamples < 126);
    assert.ok(afterTen.progress < 1, "Ten seconds alone cannot show full progress while sample counts are short");
    assert.match(afterTen.reason, /有效提峰率.*继续采集/);
  }
  stream.feed(session, 12, quiet);
  const completed = session.snapshot();
  assert.equal(completed.phase, "monitor", `${rate} Hz: ${completed.reason}`);
  assert.ok(completed.noiseRawSamples >= 181);
  assert.ok(completed.noiseFilteredSamples >= 126);
  assert.ok(completed.effectiveNoiseSeconds >= 10);
  assert.ok(completed.noiseFilteredSeconds >= 7 - 1e-9);
  assert.equal(completed.calibration.noisePolicy, NOISE_POLICY);
  assert.equal(completed.calibration.rawSampleCount, completed.noiseRawSamples);
  assert.equal(completed.calibration.filteredSampleCount, completed.noiseFilteredSamples);
  assert.ok(Math.abs(completed.calibration.sourceSpanRateHz - rate) < 1e-7);
  assert.equal(completed.processedCount, stream.count, "Each real source frame is processed once");
  assert.equal(completed.rejectedCount, 0);
  const frozen = completed.weights, calibration = completed.calibration;
  stream.feed(session, 12, pulse);
  const monitored = session.snapshot();
  assert.equal(monitored.phase, "monitor");
  assert.ok(monitored.points.length > completed.points.length);
  assert.deepEqual(monitored.weights, frozen);
  assert.deepEqual(monitored.calibration, calibration, "Weights and metadata are calculated only once");
  assert.ok(Math.abs(monitored.rateHz - rate) < 1e-7);
  assert.equal(monitored.candidateBpm, null, "A lower real rate never acquires a twenty-hertz candidate qualification");
  assert.match(monitored.reason, /有效提峰率不足 20 Hz/);
}

// A two-second processing slowdown at fifteen hertz does not throw away a
// valid unloaded run. Recovery to twenty-five hertz uses only original frames.
{
  const session = new PulseSession(); session.beginNoise({ confirmed: true });
  let seq = 0, t = 100;
  const feed = (count, rate) => {
    const frames = [];
    for (let i = 0; i < count; i++) {
      frames.push({ seq: ++seq, t_ns: Math.round(t * 1e9), frame_id: seq, stream_epoch: "epoch-a", source_kind: "hardware", binding: "reference-a", values_pm: quiet() });
      t += 1 / rate;
    }
    session.push(frames);
  };
  feed(34, 15);
  const slow = session.snapshot();
  assert.equal(slow.phase, "noise", slow.reason);
  assert.equal(slow.noiseRawSamples, 34);
  assert.ok(slow.effectiveNoiseSeconds > 2);
  assert.ok(Math.abs(slow.rateHz - 15) < 1e-7);
  assert.equal(slow.gapCount, 0);
  feed(250, 25);
  const completed = session.snapshot();
  assert.equal(completed.phase, "monitor", completed.reason);
  assert.ok(completed.noiseRawSamples > slow.noiseRawSamples);
  assert.ok(completed.effectiveNoiseSeconds >= 10);
  assert.ok(completed.noiseFilteredSeconds >= 7 - 1e-9);
  assert.equal(completed.processedCount, seq);
  assert.equal(completed.rejectedCount, 0);
  assert.equal(completed.gapCount, 0);
  const frozen = completed.weights, calibration = completed.calibration;
  feed(50, 25);
  assert.deepEqual(session.snapshot().weights, frozen);
  assert.deepEqual(session.snapshot().calibration, calibration);
}

// Exactly the allowed 20 Hz still works; source timing, not batch timing,
// determines the Butterworth coefficients and the candidate frequency.
{
  const { session, stream } = learned(20);
  stream.feed(session, 12, pulse);
  assert.ok(Math.abs(session.snapshot().candidateBpm - 72) < 2, session.snapshot().reason);
}

// These relative timestamps were observed from the existing processed-source
// buffer. Only timing is real: identities and quiet optical values below are
// artificial software fixtures and never establish unloaded/physical acceptance.
{
  const timing = JSON.parse(readFileSync(new URL("./fixtures/pulse_noise_rate_jitter.json", import.meta.url), "utf8"));
  assert.equal(timing.schema, "nine_fbg_processed_timestamp_jitter_fixture_v1");
  const observed = timing.relative_ms;
  assert.equal(observed.length, 233);
  assert.ok(Math.abs(observed.at(-1) / 1000 - timing.available_span_seconds) < 1e-9);
  assert.ok(observed.at(-1) < 10000, "The available real timing trace is shorter than ten seconds");
  for (let i = 1; i < observed.length; i++) assert.ok(observed[i] > observed[i - 1] && observed[i] - observed[i - 1] <= 120);
  const oldGateIndex = observed.findIndex(ms => ms >= 2000);
  const oldGateRate = oldGateIndex / (observed[oldGateIndex] / 1000);
  assert.equal(oldGateIndex, 38);
  assert.ok(Math.abs(oldGateRate - 18.8518) < 0.0001);
  assert.ok(oldGateRate < 19.9, "The old two-second noise gate rejected this observed source timing");
  const session = new PulseSession(); session.beginNoise({ confirmed: true });
  let seq = 0;
  const frame = ms => ({ seq: ++seq, t_ns: Math.round((100 + ms / 1000) * 1e9), frame_id: seq, stream_epoch: "timing-regression", source_kind: "hardware", binding: "artificial-timing-fixture", values_pm: quiet() });
  session.push(observed.slice(0, oldGateIndex + 1).map(frame));
  const slowStart = session.snapshot();
  assert.equal(slowStart.phase, "noise", slowStart.reason);
  assert.ok(Math.abs(slowStart.rateHz - oldGateRate) < 1e-7);
  assert.equal(slowStart.noiseRawSamples, 39);
  session.push(observed.slice(oldGateIndex + 1).map(frame));
  const observedOnly = session.snapshot();
  assert.equal(observedOnly.phase, "noise", "The actual shorter trace cannot fabricate ten seconds of coverage");
  assert.equal(observedOnly.processedCount, 233); assert.equal(observedOnly.noiseRawSamples, 233);
  assert.equal(observedOnly.rejectedCount, 0); assert.equal(observedOnly.gapCount, 0);
  assert.ok(Math.abs(observedOnly.effectiveNoiseSeconds - 9.8853313) < 1e-8);
  assert.ok(observedOnly.progress < 1); assert.equal(observedOnly.weights, null);
  // Explicitly synthetic 25 Hz continuation is separate from the captured
  // fixture. It tests completion after more genuine input samples to the core;
  // these added times must never be described as observed device timestamps.
  let syntheticMs = observed.at(-1), added = 0;
  while (session.snapshot({ includePoints: false }).phase === "noise" && added < 5) {
    syntheticMs += 40; added++; session.push(frame(syntheticMs));
  }
  const completed = session.snapshot();
  assert.equal(added, 3);
  assert.equal(completed.phase, "monitor", completed.reason);
  assert.equal(completed.processedCount, 236); assert.equal(completed.rejectedCount, 0);
  assert.equal(completed.calibration.rawSampleCount, 236);
  assert.ok(completed.calibration.filteredSampleCount >= 126);
  assert.ok(completed.calibration.effectiveNoiseSeconds >= 10);
  assert.ok(completed.calibration.filteredSeconds >= 7 - 1e-9);
  assert.ok(Math.abs(completed.calibration.sourceSpanRateHz - 235 / completed.calibration.durationSeconds) < 1e-9);
  assert.equal(completed.calibration.noisePolicy, NOISE_POLICY);
  const frozen = completed.weights, calibration = completed.calibration;
  syntheticMs += 40; session.push(frame(syntheticMs));
  assert.deepEqual(session.snapshot().weights, frozen);
  assert.deepEqual(session.snapshot().calibration, calibration);
}

// Alternating 12/28 ms spacing still uses each actual source interval. There
// is no unsafe downsampling to a nominal browser or target sample cadence.
{
  const { session, stream } = learned(50, true);
  stream.feed(session, 12, pulse);
  assert.ok(Math.abs(session.snapshot().candidateBpm - 72) < 2, session.snapshot().reason);
}

// Transport faults have a dedicated explicit reset and cannot leave a stale
// candidate frequency on screen. Empty polls do not invent source samples.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  const count = session.snapshot().processedCount;
  session.push([]); assert.equal(session.snapshot().processedCount, count);
  session.interrupt("光谱源帧已过期");
  assert.equal(session.snapshot().phase, "failed"); assert.equal(session.snapshot().candidateBpm, null);
  assert.equal(session.snapshot().points.length, 0);
}

// This same pure file can load in a browser without CommonJS or hardware APIs.
{
  const browser = vm.createContext({});
  vm.runInContext(readFileSync(new URL("../frontend/pulse_signal.js", import.meta.url), "utf8"), browser);
  assert.equal(typeof browser.NineFBGPulse.PulseSession, "function");
}

// No contact confirmation or start action follows noise acquisition. The same
// real curve remains live while unloaded, during contact, and after removing
// the hand; only candidates require centre signal and periodic quality.
{
  const session = new PulseSession(), stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 10.1, quiet);
  const ready = session.snapshot();
  assert.equal(ready.phase, "monitor");
  assert.equal(ready.streamMode, STREAM_MODE);
  const frozen = ready.weights, calibration = ready.calibration;
  stream.feed(session, 12, quiet);
  const unloaded = session.snapshot();
  assert.ok(unloaded.points.length > ready.points.length);
  assert.ok(unloaded.points.every(point => Number.isFinite(point.value)));
  assert.equal(unloaded.candidateBpm, null);
  stream.feed(session, 12, pulse);
  const contact = session.snapshot();
  assert.ok(contact.quality.valid, contact.reason);
  assert.ok(Math.abs(contact.candidateBpm - 72) < 2);
  stream.feed(session, 12, quiet);
  const removed = session.snapshot();
  assert.equal(removed.phase, "monitor");
  assert.ok(removed.points.length > contact.points.length);
  assert.equal(removed.candidateBpm, null);
  assert.deepEqual(removed.weights, frozen);
  assert.deepEqual(removed.calibration, calibration);
  stream.feed(session, 65, quiet);
  const later = session.snapshot();
  assert.equal(later.phase, "monitor");
  assert.equal(later.candidateBpm, null);
  assert.ok(later.points.at(-1).t - later.points[0].t > 59.8);
  assert.deepEqual(later.weights, frozen);
}

// Transport suspension retains history without manufacturing samples. A
// valid same-binding frame resumes automatically as a new visible segment.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  const before = session.snapshot();
  session.pause("页面暂时挂起");
  const paused = session.snapshot();
  assert.equal(paused.phase, "monitor"); assert.equal(paused.suspended, true);
  assert.deepEqual(paused.points, before.points);
  assert.equal(paused.valuePm, null); assert.equal(paused.candidateBpm, null);
  session.push([]);
  assert.equal(session.snapshot().processedCount, before.processedCount);
  stream.advance(2); const frame = stream.next(pulse(1)); session.push(frame);
  const resumed = session.snapshot();
  assert.equal(resumed.suspended, false);
  assert.equal(resumed.processedCount, before.processedCount + 1);
  assert.equal(resumed.points.length, before.points.length + 1);
  assert.equal(resumed.points.at(-1).t, frame.t_ns / 1e9);
  assert.equal(resumed.points.at(-1).breakBefore, true);
  assert.equal(resumed.points.at(-1).warmup, true);
  assert.equal(resumed.candidateBpm, null);
  assert.deepEqual(resumed.weights, before.weights);
  stream.feed(session, 12, pulse);
  assert.ok(session.snapshot().candidateBpm, session.snapshot().reason);
}

// The UI metadata path avoids copying the sixty-second curve on each poll.
// Ordinary snapshots still isolate consumer mutations; the synchronous view
// is explicitly read-only and supplies all original timestamps for drawing.
{
  const { session, stream } = learned(); stream.feed(session, 12, pulse);
  const view = session.readPoints(), full = session.snapshot();
  const light = session.snapshot({ includePoints: false });
  assert.equal(Object.hasOwn(light, "points"), false);
  assert.equal(light.pointCount, view.length);
  assert.notEqual(full.points, view);
  assert.deepEqual(full.points, view);
  const next = session.push(stream.next(pulse(1)), { includePoints: false });
  assert.equal(Object.hasOwn(next, "points"), false);
  assert.ok(next.pointVersion > light.pointVersion);
  assert.equal(next.pointCount, session.readPoints().length);
  assert.equal(view, session.readPoints());
  full.points[0].value = 1e12;
  assert.notEqual(view[0].value, 1e12);
}

// At high source rates the quality rows and fused points share the same
// bound. A shorter actual window must retain finite, matching correlation
// inputs; the frequency estimate still uses its real timestamps.
{
  const rate = 1100, session = new PulseSession(), stream = source(rate);
  const feed = (duration, fn) => {
    const samples = Array.from({ length: Math.floor(duration * rate) + 1 }, () => {
      const sample = stream.next([]);
      sample.values_pm = fn(sample.t_ns / 1e9);
      return sample;
    });
    session.push(samples, { includePoints: false });
  };
  session.beginNoise({ confirmed: true }); feed(10.1, quiet);
  assert.equal(session.snapshot({ includePoints: false }).phase, "monitor");
  feed(15, pulse);
  const snap = session.snapshot({ includePoints: false });
  assert.equal(snap.pointCount, 12000);
  assert.equal(session.rows.length, 12000);
  const actualSeconds = session.rows.at(-1).t - session.rows[0].t;
  assert.ok(actualSeconds > 10 && actualSeconds < 11, "Bounded rows do not pretend to retain a full 12 seconds");
  assert.ok(Number.isFinite(snap.quality.centerCorrelation));
  assert.ok(snap.quality.valid, snap.reason);
  assert.ok(Math.abs(snap.candidateBpm - 72) < 2);
}

// A very high rate can fill the same memory bound in under five seconds.
// Eight seconds of arriving frames cannot turn its shorter retained history
// into a claimed full-duration quality window or a candidate frequency.
{
  const { session, stream } = learned();
  const first = stream.next([]), stepNs = 333333;
  const samples = Array.from({ length: 24001 }, (_, i) => {
    const t_ns = first.t_ns + i * stepNs;
    return { ...first, seq: first.seq + i, frame_id: first.frame_id + i, t_ns, values_pm: pulse(t_ns / 1e9) };
  });
  session.push(samples, { includePoints: false });
  const snap = session.snapshot({ includePoints: false });
  assert.equal(snap.phase, "monitor");
  assert.equal(snap.pointCount, 12000); assert.equal(session.rows.length, 12000);
  assert.ok(session.rows.at(-1).t - session.rows[0].t < 5);
  assert.equal(snap.candidateBpm, null);
  assert.equal(snap.quality.valid, false);
  assert.match(snap.reason, /等待足够的连续周期/);
  assert.ok(Number.isFinite(snap.quality.centerCorrelation));
}

// Compare the optimized phase recurrence with the prior direct-trigonometry
// least-squares definition, including irregular source intervals. Expose the
// private pure function only in this VM test, without widening the app API.
{
  let trigCalls = 0;
  const countedMath = Object.create(Math);
  countedMath.sin = value => { trigCalls++; return Math.sin(value); };
  countedMath.cos = value => { trigCalls++; return Math.cos(value); };
  const browser = vm.createContext({ Math: countedMath });
  const instrumented = readFileSync(new URL("../frontend/pulse_signal.js", import.meta.url), "utf8")
    .replace("return Object.freeze({ PulseSession,", "return Object.freeze({ spectralPeak, PulseSession,");
  vm.runInContext(instrumented, browser);
  const referenceSpectrum = points => {
    const average = points.reduce((sum, point) => sum + point.value, 0) / points.length;
    const y = points.map(point => point.value - average);
    const power = y.reduce((sum, value) => sum + value * value, 0) / y.length;
    if (!(power > 1e-12)) return { frequency: null, fraction: 0 };
    const t0 = points[0].t;
    let best = { frequency: null, fraction: 0 };
    for (let f = 0.7; f <= 3.0001; f += 0.01) {
      let ss = 0, cc = 0, sc = 0, ys = 0, yc = 0;
      for (let i = 0; i < points.length; i++) {
        const phase = 2 * Math.PI * f * (points[i].t - t0);
        const s = Math.sin(phase), c = Math.cos(phase);
        const dt = i ? points[i].t - points[i - 1].t : points[1].t - t0;
        ss += s * s * dt; cc += c * c * dt; sc += s * c * dt;
        ys += y[i] * s * dt; yc += y[i] * c * dt;
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
  };
  for (const irregular of [false, true]) {
    let t = 100;
    const points = Array.from({ length: 601 }, (_, i) => {
      t += 0.02 * (irregular ? (i % 2 ? 0.6 : 1.4) : 1);
      return { t, value: 2 * Math.sin(2 * Math.PI * 1.23 * t) + 0.2 * Math.cos(2 * Math.PI * 2.46 * t) + noise() };
    });
    trigCalls = 0;
    const optimized = browser.NineFBGPulse.spectralPeak(points), reference = referenceSpectrum(points);
    assert.equal(optimized.frequency, reference.frequency);
    assert.ok(Math.abs(optimized.fraction - reference.fraction) < 1e-10);
    assert.equal(trigCalls, points.length * 4, "Only per-sample setup needs trigonometry");
  }
}
// Configuration is explicit, bounded, copied and retained across a fresh noise
// recording. Changing the measured band invalidates its noise-derived weights;
// entering the current band or an invalid value does not disturb a live curve.
{
  const supplied = [0.5, 5], session = new PulseSession({ filterBandHz: supplied });
  supplied[0] = 7;
  assert.deepEqual(session.snapshot().filterBandHz, [0.5, 5]);
  assert.deepEqual(session.snapshot().candidateBandHz, [0.7, 3]);
  assert.equal(session.snapshot().filterConfigSchema, FILTER_CONFIG_SCHEMA);
  assert.equal(session.snapshot().warmupSeconds, 4.2);
  assert.equal(session.snapshot().maxGapSeconds, 0.09);
  assert.doesNotMatch(session.snapshot().filterDelayDescription, /90|410/);
  const valid = validateFilterBandHz([0.1, 8]);
  valid[0] = 99;
  assert.deepEqual(validateFilterBandHz([0.1, 8]), [0.1, 8]);
  for (const invalid of [undefined, null, [], [1], [1, 2, 3], ["0.7", 3], [NaN, 3], [0.7, Infinity], [-1, 3], [0, 3], [0.09, 3], [0.7, 8.01], [3, 3], [5, 1]]) {
    assert.throws(() => validateFilterBandHz(invalid), /带通/);
    if (invalid !== undefined) assert.throws(() => new PulseSession({ filterBandHz: invalid }), /带通/);
  }
  const stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 12, quiet);
  const before = session.snapshot();
  assert.equal(before.phase, "monitor", before.reason);
  assert.deepEqual(before.calibration.filterBandHz, [0.5, 5]);
  assert.deepEqual(before.calibration.candidateBandHz, [0.7, 3]);
  assert.equal(before.calibration.filterConfigSchema, FILTER_CONFIG_SCHEMA);
  assert.equal(before.calibration.warmupSeconds, 4.2);
  assert.equal(before.calibration.maxGapSeconds, 0.09);
  session.setFilterBand([0.5, 5]);
  assert.deepEqual(session.snapshot(), before, "A same-value apply preserves the calibrated curve exactly");
  assert.throws(() => session.setFilterBand([5, 1]), /带通/);
  assert.deepEqual(session.snapshot(), before, "An invalid apply cannot mutate the session");
  const copied = session.snapshot();
  copied.filterBandHz[0] = 99; copied.candidateBandHz[0] = 99; copied.calibration.candidateBandHz[0] = 99;
  assert.deepEqual(session.snapshot(), before, "All public frequency metadata is copied");
  session.setFilterBand([1, 2]);
  const changed = session.snapshot();
  assert.equal(changed.phase, "idle"); assert.equal(changed.weights, null); assert.equal(changed.noise, null);
  assert.equal(changed.calibration, null); assert.equal(changed.baselinePm, null); assert.equal(changed.candidateBpm, null);
  assert.equal(changed.pointCount, 0); assert.deepEqual(changed.filterBandHz, [1, 2]);
  assert.deepEqual(changed.candidateBandHz, [1, 2]); assert.match(changed.reason, /重新采集噪声/);
  session.beginNoise({ confirmed: true }); assert.deepEqual(session.snapshot().filterBandHz, [1, 2]);
  session.reset(); assert.deepEqual(session.snapshot().filterBandHz, [1, 2]);
}

// The default constructor and explicitly entered original band produce the
// same weights, actual source-time samples, quality and periodic estimate.
{
  const implicit = new PulseSession(), explicit = new PulseSession({ filterBandHz: [0.7, 3] });
  const stream = source(50, "reference-a", true);
  implicit.beginNoise({ confirmed: true }); explicit.beginNoise({ confirmed: true });
  const baseline = [];
  for (let i = 0; i < 510; i++) baseline.push(stream.next(gratings.map(j => 100 + 0.05 * Math.sin(i * (0.13 + j / 100)))));
  implicit.push(baseline); explicit.push(baseline);
  const contact = [];
  for (let i = 0; i < 1250; i++) {
    const sample = stream.next(gratings.map(() => 100));
    sample.values_pm = gratings.map(() => 100 + 2 * Math.sin(2 * Math.PI * 1.2 * sample.t_ns / 1e9));
    contact.push(sample);
  }
  implicit.push(contact); explicit.push(contact);
  assert.deepEqual(implicit.snapshot(), explicit.snapshot());
  assert.equal(implicit.snapshot().warmupSeconds, 3);
  assert.equal(implicit.snapshot().maxGapSeconds, 0.12);
  assert.ok(Math.abs(implicit.snapshot().candidateBpm - 72) < 2, implicit.snapshot().reason);
}

// Lower cutoffs receive enough actual settling time before noise statistics.
// Seven seconds of warmed samples remain mandatory even if warmup exceeds the
// nominal ten-second unloaded duration; no synthetic samples fill the gap.
{
  const session = new PulseSession({ filterBandHz: [0.1, 5] }), stream = source();
  session.beginNoise({ confirmed: true }); stream.feed(session, 10.1, quiet);
  assert.equal(session.snapshot().warmupSeconds, 21);
  assert.equal(session.snapshot().phase, "noise"); assert.equal(session.snapshot().noiseFilteredSamples, 0);
  stream.feed(session, 17.8, quiet);
  assert.equal(session.snapshot().phase, "noise", "Nearly seven warmed seconds cannot round up to completion");
  stream.feed(session, 0.3, quiet);
  const done = session.snapshot();
  assert.equal(done.phase, "monitor", done.reason);
  assert.ok(done.calibration.durationSeconds >= 28 - 1e-9);
  assert.ok(done.noiseFilteredSeconds >= 7 - 1e-9);
  assert.ok(done.noiseRawSamples >= 1400);
}

const configuredLearned = (band, rate = 50) => {
  const session = new PulseSession({ filterBandHz: band }), stream = source(rate);
  session.beginNoise({ confirmed: true });
  stream.feed(session, Math.max(10.1, session.snapshot().warmupSeconds + 7.1), t => gratings.map(j => 100 + 0.01 * Math.sin(2 * Math.PI * (1.1 + j / 100) * t)));
  assert.equal(session.snapshot().phase, "monitor", session.snapshot().reason);
  return { session, stream };
};
const fittedAmplitude = (points, frequency) => {
  const tail = points.filter(point => point.t >= points.at(-1).t - 10 && !point.warmup);
  let s = 0, c = 0;
  for (const point of tail) {
    s += point.value * Math.sin(2 * Math.PI * frequency * point.t);
    c += point.value * Math.cos(2 * Math.PI * frequency * point.t);
  }
  return 2 * Math.hypot(s, c) / tail.length;
};

// Behavioural response: a wider user-entered band retains slow and 4 Hz
// components attenuated by the original band, while preserving the 1.2 Hz
// component. This checks that both entered edges reach the actual filters.
{
  const original = configuredLearned([0.7, 3]), wider = configuredLearned([0.2, 5]);
  const mixed = t => gratings.map(() => 100 + Math.sin(2 * Math.PI * 0.25 * t) + Math.sin(2 * Math.PI * 1.2 * t) + Math.sin(2 * Math.PI * 4 * t));
  original.stream.feed(original.session, 30, mixed); wider.stream.feed(wider.session, 30, mixed);
  const oldPoints = original.session.readPoints(), newPoints = wider.session.readPoints();
  assert.ok(fittedAmplitude(newPoints, 0.25) > 4 * fittedAmplitude(oldPoints, 0.25));
  assert.ok(fittedAmplitude(newPoints, 4) > 1.6 * fittedAmplitude(oldPoints, 4));
  assert.ok(fittedAmplitude(newPoints, 1.2) > 0.85);
  assert.ok(fittedAmplitude(oldPoints, 1.2) > 0.85);
  assert.ok(newPoints.every(point => Number.isFinite(point.value)));
}

// The display band never expands the established candidate search outside
// 0.7–3 Hz. A wider 4 Hz waveform remains visible without reporting 240 bpm;
// a narrower band searches only its intersection with that established range.
{
  const narrowed = configuredLearned([1.0035, 1.5]);
  assert.deepEqual(narrowed.session.snapshot().candidateBandHz, [1.0035, 1.5]);
  narrowed.stream.feed(narrowed.session, 24, pulse);
  assert.ok(Math.abs(narrowed.session.snapshot().candidateBpm - 72) < 2, narrowed.session.snapshot().reason);
  const wider = configuredLearned([0.5, 5]);
  wider.stream.feed(wider.session, 24, t => gratings.map(() => 100 + 2 * Math.sin(2 * Math.PI * 4 * t)));
  assert.ok(fittedAmplitude(wider.session.readPoints(), 4) > 1.5);
  assert.equal(wider.session.snapshot().candidateBpm, null);
  const outside = configuredLearned([3.5, 5]);
  assert.equal(outside.session.snapshot().candidateBandHz, null);
  outside.stream.feed(outside.session, 24, t => gratings.map(() => 100 + 2 * Math.sin(2 * Math.PI * 4 * t)));
  assert.equal(outside.session.snapshot().candidateBpm, null);
  assert.match(outside.session.snapshot().reason, /未覆盖.*仅显示曲线/);
  assert.ok(outside.session.snapshot().pointCount > 500);
}

// Selected high cutoffs impose a source-interval safety boundary below
// Nyquist. The inclusive boundary is usable; exceeding it splits the real
// curve and retains its fixed weights without running unstable coefficients.
{
  const { session, stream } = configuredLearned([0.7, 8]);
  stream.feed(session, 4, quiet);
  const before = session.snapshot(), limit = before.maxGapSeconds;
  assert.equal(limit, 0.45 / 8);
  stream.advance(limit - 0.02); session.push(stream.next(quiet()));
  assert.equal(session.snapshot().gapCount, 0, "The selected safety boundary remains inclusive");
  stream.advance(limit - 0.02 + 1e-7); session.push(stream.next(quiet()));
  const split = session.snapshot();
  assert.equal(split.phase, "monitor"); assert.equal(split.gapCount, 1);
  assert.equal(split.filterIntervalLimitCount, 1); assert.deepEqual(split.weights, before.weights);
  assert.match(split.reason, /源采样间隔不足以支持所选滤波上限/);
  assert.equal(split.points.at(-1).breakBefore, true); assert.equal(split.points.at(-1).warmup, true);
  assert.ok(Number.isFinite(split.points.at(-1).value)); assert.equal(split.candidateBpm, null);
  stream.feed(session, 4, quiet);
  assert.equal(session.snapshot().points.at(-1).warmup, false);
  assert.deepEqual(session.snapshot().weights, before.weights);
  session.stopMonitor();
  const stopped = session.snapshot();
  for (let i = 0; i < 4; i++) { stream.advance(0.08); session.push(stream.next(quiet())); }
  const paused = session.snapshot();
  assert.equal(paused.phase, "calibrated_ready"); assert.deepEqual(paused.weights, stopped.weights);
  assert.deepEqual(paused.points, stopped.points); assert.ok(Math.abs(paused.rateHz - 10) < 1e-6);
  assert.ok(session.filters.every(filter => !filter.hp.ready && !filter.lp.ready), "A paused high-band curve never evaluates coefficients at unsafe source intervals");
  session.beginMonitor(); stream.feed(session, 4, quiet);
  const resumed = session.snapshot();
  assert.equal(resumed.phase, "monitor"); assert.deepEqual(resumed.weights, stopped.weights);
  assert.equal(resumed.points.at(-1).warmup, false); assert.ok(Number.isFinite(resumed.points.at(-1).value));
}
{
  const session = new PulseSession({ filterBandHz: [0.7, 8] }), stream = source(10);
  session.beginNoise({ confirmed: true }); stream.feed(session, 61, quiet);
  const snap = session.snapshot();
  assert.equal(snap.phase, "failed"); assert.equal(snap.weights, null);
  assert.ok(snap.filterIntervalLimitCount > 500); assert.equal(snap.noiseFilteredSeconds, 0);
  assert.match(snap.reason, /源采样间隔不足以支持所选滤波上限/);
  assert.doesNotMatch(snap.reason, /缺帧/);
}

console.log("pulse_signal: configurable source-time bandpass, safe source intervals, band-bound noise weights, bounded candidate search and original default regressions passed");
