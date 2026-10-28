(function (global, factory) {
  "use strict";
  const api = factory(global);
  if (typeof module === "object" && module.exports) module.exports = api;
  global.NineFBGPreferences = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (global) {
  "use strict";
  let loaded, values = {}, chain = Promise.resolve();
  const dirty = new Set();
  const FIELDS = {
    connection: ["source_mode", "integration_us", "sample_rate_hz"],
    display: ["spectrum_mode", "spectrum_x_min", "spectrum_x_max", "spectrum_y_min", "spectrum_y_max", "show_windows"],
    experiment: ["finger_id", "data_id", "batch", "force_sign", "tip_diameter_mm", "temperature_C", "fixation", "repetitions_per_point", "selected_point_id", "acquisition_protocol", "capture_mode", "max_force_norm_N", "target_force_norm_N"],
    analysis: ["method", "input_kind", "include_mlp", "epochs", "batch_size", "learning_rate", "patience", "seed", "position_loss_weight", "wrench_loss_weight", "cpu_threads", "adapt_legacy_unloaded"]
  };
  const ENUMS = { capture_mode: ["full_spectrum", "peak_drift"], source_mode: ["hardware", "replay"], spectrum_mode: ["auto", "manual"], batch: ["A", "B", "C"], input_kind: ["delta_S", "delta_lambda_9"], method: ["linear", "piecewise", "mlp", "spectral_gru", "drift_gru"], acquisition_protocol: ["guided_round_v1", "timed_wrench_v1", "force_target_wrench_v1"] };
  function validate(section, patch) {
    if (!Object.hasOwn(FIELDS, section) || !patch || typeof patch !== "object" || Array.isArray(patch) || Object.keys(patch).some(key => !FIELDS[section].includes(key))) throw new Error("设置包含不支持的分类或字段；测量状态与确认不能持久化");
    for (const [key, value] of Object.entries(patch)) {
      if (ENUMS[key] && !ENUMS[key].includes(value)) throw new Error("设置选项无效：" + key);
    }
    return patch;
  }
  const status = text => { const el = global.document?.getElementById("settingsStatus"); if (el) el.textContent = text; };
  async function request(method, body) {
    const response = await global.fetch("/api/settings", method === "GET" ? { cache: "no-store" } : {
      method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive: true
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "设置保存失败");
    return data;
  }
  function load() {
    if (!loaded) loaded = request("GET").then(data => {
      values = data.values || {};
      status(Object.keys(values).length ? "已恢复上次设置 · 修改后自动保存" : "设置修改后自动保存");
      return values;
    }).catch(error => { status(error.message); return values; });
    return loaded;
  }
  function save(section, patch) {
    try { validate(section, patch); } catch (error) { status(error.message); return Promise.reject(error); }
    for (const key of Object.keys(patch)) dirty.add(section + ":" + key);
    chain = chain.then(async () => {
      await load();
      const data = await request("POST", { section, values: patch });
      values = data.values;
      status("设置已保存 · 下次打开自动恢复");
    }).catch(error => { status(error.message); });
    return chain;
  }
  function bind(section, mapping, afterRestore) {
    validate(section, Object.fromEntries(Object.keys(mapping).map(key => [key, ENUMS[key]?.[0]])));
    const elements = Object.entries(mapping).flatMap(([key, field]) => {
      const el = global.document?.getElementById(field.id);
      if (!el) return [];
      const read = () => field.type === "checkbox" ? el.checked : field.type === "number" ?
        el.value === "" ? null : Number(el.value) : el.value;
      el.addEventListener("input", () => { dirty.add(section + ":" + key); if (field.type !== "checkbox") save(section, { [key]: read() }); });
      el.addEventListener("change", () => save(section, { [key]: read() }));
      return [{ key, field, el }];
    });
    return load().then(data => {
      for (const { key, field, el } of elements) {
        if (dirty.has(section + ":" + key) || !Object.prototype.hasOwnProperty.call(data[section] || {}, key)) continue;
        const value = data[section][key];
        if (field.type === "checkbox") el.checked = value === true;
        else el.value = value == null ? "" : String(value);
      }
      if (afterRestore) afterRestore(data[section] || {});
    });
  }
  return { load, save, bind, validate };
});
