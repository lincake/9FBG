(function (global, factory) {
  "use strict";
  const api = factory(global);
  if (typeof module === "object" && module.exports) module.exports = api;
  global.NineFBGPreferences = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (global) {
  "use strict";
  let loaded, values = {}, chain = Promise.resolve();
  const dirty = new Set();
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
  return { load, save, bind };
});
