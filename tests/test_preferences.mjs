import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../frontend/preferences.js", import.meta.url), "utf8");
const listeners = new Map();
const nodes = new Map(["finger", "repetitions", "captureMode", "settingsStatus"].map(id => [id, {
  value: id === "repetitions" ? "3" : "", textContent: "",
  addEventListener(type, callback) { listeners.set(id + ":" + type, callback); }
}]));
const calls = [];
let resolveLoad;
const browser = vm.createContext({ document: { getElementById: id => nodes.get(id) },
  fetch: async (path, options) => {
    calls.push({ path, options });
    if (!options?.method) return new Promise(resolve => { resolveLoad = () => resolve({ ok: true, json: async () => ({ values: { experiment: { finger_id: "旧样件", repetitions_per_point: 7, capture_mode: "peak_drift" } } }) }); });
    const patch = JSON.parse(options.body);
    return { ok: true, json: async () => ({ values: { [patch.section]: patch.values } }) };
  }
});
vm.runInContext(source, browser);
const api = browser.NineFBGPreferences;
assert.equal(calls.length, 0, "Loading the module cannot create settings or operate hardware");
let restored;
const binding = api.bind("experiment", { finger_id: { id: "finger", type: "string" }, repetitions_per_point: { id: "repetitions", type: "number" }, capture_mode: { id: "captureMode", type: "string" } }, values => { restored = values; });
nodes.get("finger").value = "刚输入的样件";
listeners.get("finger:input")();
resolveLoad();
await binding;
await api.save("experiment", { selected_point_id: "P21" });
assert.equal(nodes.get("finger").value, "刚输入的样件", "Delayed restore must not overwrite current typing");
assert.equal(nodes.get("repetitions").value, "7");
assert.equal(restored.repetitions_per_point, 7);
assert.equal(nodes.get("captureMode").value, "peak_drift", "Capture mode restores independently of model input preferences");
assert.equal(calls.filter(call => call.options.method === "POST").length, 2);
assert.deepEqual(calls.filter(call => call.options.method === "POST").map(call => JSON.parse(call.options.body).values), [
  { finger_id: "刚输入的样件" }, { selected_point_id: "P21" }
]);
assert.ok(calls.every(call => call.path === "/api/settings"), "Preference restore must never connect, calibrate or start recording");
assert.match(nodes.get("settingsStatus").textContent, /已保存/);
api.validate("analysis", { method: "spectral_gru", epochs: 100 });
api.validate("analysis", { method: "drift_gru", input_kind: "delta_lambda_9" });
api.validate("analysis", { method: "spectral_gru", input_kind: "delta_S" });
assert.throws(() => api.validate("analysis", { input_kind: "bad" }), /无效/);
api.validate("experiment", { capture_mode: "full_spectrum" });
api.validate("experiment", { capture_mode: "peak_drift" });
assert.throws(() => api.validate("experiment", { capture_mode: "bad" }), /无效/);
api.validate("experiment", { acquisition_protocol: "timed_wrench_v1", max_force_norm_N: 5 });
api.validate("experiment", { acquisition_protocol: "force_target_wrench_v1", target_force_norm_N: 4, max_force_norm_N: 5 });
await api.save("experiment", { acquisition_protocol: "force_target_wrench_v1", target_force_norm_N: 4, max_force_norm_N: 5 });
assert.deepEqual(JSON.parse(calls.at(-1).options.body).values, { acquisition_protocol: "force_target_wrench_v1", target_force_norm_N: 4, max_force_norm_N: 5 });
for (const [section, patch] of [["experiment", { unloaded_confirmed: true }], ["analysis", { S0: [] }], ["analysis", { wrench_tolerances: [] }], ["analysis", { method: "unknown" }], ["experiment", { acquisition_protocol: "unknown" }]]) assert.throws(() => api.validate(section, patch), /不支持|无效/);
nodes.get("captureMode").value = "full_spectrum"; listeners.get("captureMode:change")();
await api.save("analysis", { method: "drift_gru", input_kind: "delta_lambda_9" });
assert.deepEqual(JSON.parse(calls.at(-2).options.body), { section: "experiment", values: { capture_mode: "full_spectrum" } });
assert.deepEqual(JSON.parse(calls.at(-1).options.body), { section: "analysis", values: { method: "drift_gru", input_kind: "delta_lambda_9" } });
const beforeRejectedSetting = calls.length;
await assert.rejects(api.save("experiment", { unloaded_confirmed: true }), /确认不能持久化/);
assert.equal(calls.length, beforeRejectedSetting, "Client allowlist blocks measurement authorization before any request");
console.log("设置持久化界面验证通过：恢复、即时保存、延迟恢复保护与无设备副作用。");
