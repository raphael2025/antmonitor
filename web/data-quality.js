"use strict";
/* Missing measurements stay missing. Observed integrals are never full-period bills. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AntDataQuality = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = v => (typeof v !== "number" && typeof v !== "string") ||
    (typeof v === "string" && !v.trim()) || !Number.isFinite(Number(v)) || Number(v) < 0
    ? null : Number(v);
  const short = n => Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
  const note = (s, warn = false) => '<span class="rate-note muted' + (warn ? ' rate-warning' : '') + '">' + esc(s) + '</span>';
  const value = (n, format, hint = "") => '<span class="rate-value"' +
    (hint ? ' title="' + esc(hint) + '"' : '') + '>' +
    (number(n) == null ? "—" : esc(format(number(n)))) + '</span>';
  function partial(total, known, complete, t, format) {
    const hint = complete === false && number(known) != null
      ? t("quality.known_part", { value: format(number(known)) }) : "";
    return value(total, format, hint);
  }
  function total(o, key, t, format) {
    const complete = o.metrics_complete && o.metrics_complete[key];
    const known = number((o.observed_totals || {})[key]);
    const hint = complete === false && known != null
      ? t("quality.known_sites", { value: format(known), known: (o.metric_known_sites || {})[key], total: o.totals.sites }) : "";
    return value((o.totals || {})[key], format, hint);
  }
  function billing(r, kind, t, format) {
    const fields = { delivery: "delivered_th_h", power: "power_kwh", uptime: "uptime_pct" };
    const key = fields[kind];
    const modern = r.billing_method === "time_weighted_v2" || r.billing_source === "observed";
    const reason = r[kind + "_unavailable_reason"];
    const badWindow = ["mixed_periods", "unaligned_windows", "unverified_windows"].includes(reason) ||
      (number(r.sites) > 1 && ["unaligned", "unverified"].includes(r.billing_window_alignment));
    const n = modern && !badWindow ? number(r[key]) : null;
    const observed = number(r[kind + "_observed_hours"]), hours = number(r.billing_hours);
    const coverage = number(r[kind + "_coverage_pct"]);
    let hint = "";
    if (badWindow) hint = t("quality.windows");
    else if (r.report_stale === true) hint = t("rate.report_stale");
    else if (n == null) hint = t("rate.waiting");
    else if (coverage == null || coverage < 99.99 || observed == null || !hours || observed + 1 / 3600 < hours)
      hint = observed != null && hours ? t("rate.coverage", { observed: short(observed), hours: short(hours) }) : t("rate.incomplete");
    // Migration removes incompatible records. Never revive them as expandable UI rows.
    return value(n, format, hint);
  }
  function daily(r, key, metric, t, format) {
    const n = r.method === "time_weighted_v2" ? number(r[key]) : null;
    const hours = number(r.elapsed_hours), observed = number((r.metric_observed_hours || {})[metric]);
    const coverage = number((r.metric_coverage_pct || {})[metric]);
    let hint = n == null ? t("rate.waiting") : "";
    if (n != null && (coverage == null || coverage < 99.99 || observed == null || !hours || observed + 1 / 3600 < hours))
      hint = observed != null && hours ? t("rate.coverage", { observed: short(observed), hours: short(hours) }) : t("rate.incomplete");
    return value(n, format, hint);
  }
  function points(rows, x, y, divisor = 1) {
    return rows.map(r => ({ x: r[x], y: number(r[y]) == null ? null : number(r[y]) / divisor }));
  }
  return { number, short, note, partial, total, billing, daily, points };
});
