import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../frontend/preferences.js", import.meta.url), "utf8");
const listeners = new Map();
const nodes = new Map(["finger", "repetitions", "settingsStatus"].map(id => [id, {
  value: id === "repetitions" ? "3" : "", textContent: "",
  addEventListener(type, callback) { listeners.set(id + ":" + type, callback); }
}]));
const calls = [];
let resolveLoad;
const browser = vm.createContext({ document: { getElementById: id => nodes.get(id) },
  fetch: async (path, options) => {
    calls.push({ path, options });
    if (!options?.method) return new Promise(resolve => { resolveLoad = () => resolve({ ok: true, json: async () => ({ values: { experiment: { finger_id: "旧样件", repetitions_per_point: 7 } } }) }); });
    const patch = JSON.parse(options.body);
    return { ok: true, json: async () => ({ values: { [patch.section]: patch.values } }) };
  }
});
vm.runInContext(source, browser);
const api = browser.NineFBGPreferences;
assert.equal(calls.length, 0, "Loading the module cannot create settings or operate hardware");
let restored;
const binding = api.bind("experiment", { finger_id: { id: "finger", type: "string" }, repetitions_per_point: { id: "repetitions", type: "number" } }, values => { restored = values; });
nodes.get("finger").value = "刚输入的样件";
listeners.get("finger:input")();
resolveLoad();
await binding;
await api.save("experiment", { selected_point_id: "P21" });
assert.equal(nodes.get("finger").value, "刚输入的样件", "Delayed restore must not overwrite current typing");
assert.equal(nodes.get("repetitions").value, "7");
assert.equal(restored.repetitions_per_point, 7);
assert.equal(calls.filter(call => call.options.method === "POST").length, 2);
assert.deepEqual(calls.filter(call => call.options.method === "POST").map(call => JSON.parse(call.options.body).values), [
  { finger_id: "刚输入的样件" }, { selected_point_id: "P21" }
]);
assert.ok(calls.every(call => call.path === "/api/settings"), "Preference restore must never connect, calibrate or start recording");
assert.match(nodes.get("settingsStatus").textContent, /已保存/);
console.log("设置持久化界面验证通过：恢复、即时保存、延迟恢复保护与无设备副作用。");
