(function (global) {
  "use strict";
  const PREFIX = "9fbg.gui-priority.v1.", RESUME = PREFIX + "resume", TTL = 4000;
  let singleton;
  function create() {
    const owner = global.crypto?.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2);
    const key = PREFIX + owner, listeners = new Set(), leases = new Map();
    let active = false, frozen = false, disposed = false, channel;
    const now = () => Date.now();
    try { channel = new global.BroadcastChannel("9fbg-gui-priority-v1"); } catch (_) { /* Storage remains available. */ }
    function emit() {
      const next = Array.from(leases).some(([id, until]) => id !== owner && until > now());
      const changed = next !== frozen; frozen = next;
      if (changed) notify();
    }
    function notify(reason = "lease") { for (const fn of listeners) fn({ frozen, active, reason }); }
    function accept(id, until) {
      if (typeof id !== "string" || id === "resume") return;
      if (Number.isFinite(until) && until > now() && until <= now() + TTL + 1000) leases.set(id, until);
      else leases.delete(id);
    }
    function scan() {
      if (disposed) return;
      for (const [id, until] of leases) if (until <= now()) leases.delete(id);
      try {
        const storage = global.localStorage;
        for (let i = 0; i < storage.length; i++) {
          const name = storage.key(i);
          if (name?.startsWith(PREFIX) && name !== RESUME) accept(name.slice(PREFIX.length), Number(storage.getItem(name)));
        }
      } catch (_) { /* BroadcastChannel leases still expire. */ }
      emit();
    }
    function publish() {
      const until = active ? now() + TTL : 0;
      try { if (active) global.localStorage.setItem(key, String(until)); else global.localStorage.removeItem(key); } catch (_) { /* Optional cross-tab optimization. */ }
      channel?.postMessage({ type: "lease", owner, until });
    }
    function setActive(value, reason = "activity") {
      if (disposed || active === (value === true)) return;
      active = value === true; publish(); notify(reason);
    }
    function resume() { setActive(false, "lifecycle"); }
    function requestedResume() { if (active) setActive(false, "resume"); else notify("resume"); }
    function requestResume() {
      if (disposed) return;
      requestedResume();
      try { global.localStorage.setItem(RESUME, owner + ":" + now() + ":" + Math.random()); } catch (_) { /* Use the channel when storage is unavailable. */ }
      channel?.postMessage({ type: "resume" });
    }
    function storageEvent(event) {
      if (event.key === RESUME && event.newValue) requestedResume();
      else if (event.key?.startsWith(PREFIX)) { accept(event.key.slice(PREFIX.length), Number(event.newValue)); emit(); }
      else if (event.key === null) scan();
    }
    function message(event) {
      const data = event.data;
      if (data?.type === "resume") requestedResume();
      else if (data?.type === "lease") { accept(data.owner, data.until); emit(); }
      else if (data?.type === "probe" && active) publish();
    }
    function dispose() {
      if (disposed) return;
      setActive(false); disposed = true; global.clearInterval(timer);
      global.removeEventListener?.("storage", storageEvent); global.removeEventListener?.("pagehide", resume);
      channel?.close(); listeners.clear();
    }
    global.addEventListener?.("storage", storageEvent);
    global.addEventListener?.("pagehide", resume);
    if (channel) channel.onmessage = message;
    const timer = global.setInterval(() => { if (active) publish(); scan(); }, 1000);
    scan(); channel?.postMessage({ type: "probe" });
    return Object.freeze({ isFrozen: () => frozen, isActive: () => active, setActive,
      subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }, requestResume, dispose });
  }
  const api = Object.freeze({ create, shared: () => singleton || (singleton = create()) });
  global.NineFBGGuiPriority = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
