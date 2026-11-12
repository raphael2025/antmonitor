/* Shared connection logic: browser UI + isolated regression tests. */
(function (root) {
  "use strict";
  const TYPES = ["air", "hydro", "mixed"];

  function parseCode(value) {
    const raw = String(value || "").trim();
    if (raw.length > 16384) return null;
    const match = raw.match(/^AMC1-([A-Za-z0-9_-]+)$/);
    if (!match) return null;
    try {
      const encoded = match[1].replace(/-/g, "+").replace(/_/g, "/");
      const binary = atob(encoded + "=".repeat((4 - encoded.length % 4) % 4));
      const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
      const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      const d = JSON.parse(text);
      if (!d || Array.isArray(d) || typeof d.u !== "string" || !d.u.trim()
          || typeof d.t !== "string" || !d.t.trim()) return null;
      if (/[\s\u0000-\u001f\u007f]/.test(d.u.trim()) || /[\u0000-\u001f\u007f]/.test(d.t.trim())) return null;
      if (!/^https?:\/\//i.test(d.u.trim())) return null;
      const url = new URL(d.u.trim());
      if (!["http:", "https:"].includes(url.protocol) || !url.hostname
          || url.username || url.password || url.search || url.hash) return null;
      for (const key of ["f", "s", "n", "y"]) {
        if (d[key] != null && typeof d[key] !== "string") return null;
      }
      // Match the cloud's bound-token target validation, including legacy IDs
      // made from a Chinese site name or a name with spaces. Do not rewrite IDs.
      const sid = d.s || "";
      if (sid && ([...sid].length > 64 || /[\u0000-\u001f\u007f]/.test(sid)
          || /^\p{White_Space}|\p{White_Space}$/u.test(sid))) return null;
      if (d.y && !TYPES.includes(d.y)) return null;
      if (d.n && ([...d.n.trim()].length > 64 || /[\u0000-\u001f\u007f]/.test(d.n))) return null;
      return { u: d.u.trim(), t: d.t.trim(), f: d.f || "", s: sid,
               n: (d.n || "").trim(), y: d.y || "" };
    } catch (e) { return null; }
  }

  class ConnectionError extends Error {
    constructor(step, message) {
      super(message || "");
      this.step = step;
    }
  }

  function validate(cloud, hasSavedToken) {
    if (!cloud.site_name.trim()) return "site_name";
    if (!cloud.url.trim()) return "url";
    if (!cloud.token.trim() && !hasSavedToken) return "token";
    return "";
  }

  async function connect(cloud, request, onPhase) {
    if (onPhase) onPhase("testing");
    let test;
    try { test = await request("/api/cloud/test", { cloud }); }
    catch (e) { throw new ConnectionError("test_network", ""); }
    if (!test || !test.ok) {
      throw new ConnectionError("test", (test && (test.message || test.error || test.detail)) || "");
    }
    if (onPhase) onPhase("saving");
    let saved;
    try { saved = await request("/api/settings", { cloud }); }
    catch (e) { throw new ConnectionError("save_network", ""); }
    if (!saved || !saved.ok) {
      throw new ConnectionError("save", (saved && (saved.error || saved.detail)) || "");
    }
    return saved;
  }

  const api = { parseCode, validate, connect, ConnectionError };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.AntMonitorCloud = api;
})(typeof window !== "undefined" ? window : null);
