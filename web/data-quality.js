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
  const value = (n, format) => '<span class="rate-value">' + (number(n) == null ? "—" : esc(format(number(n)))) + '</span>';
  function partial(total, known, complete, t, format) {
    return value(total, format) + (complete === false && number(known) != null
      ? note(t("quality.known_part", { value: format(number(known)) }), true) : "");
  }
  function total(o, key, t, format) {
    const complete = o.metrics_complete && o.metrics_complete[key];
    const known = number((o.observed_totals || {})[key]);
    return value((o.totals || {})[key], format) + (complete === false && known != null
      ? note(t("quality.known_sites", { value: format(known), known: (o.metric_known_sites || {})[key], total: o.totals.sites }), true) : "");
  }
  function legacy(n, t, format) {
    if (number(n) == null) return "";
    return '<details class="legacy-data"><summary>' + esc(t("quality.legacy")) + '</summary>' +
      esc(format(number(n))) + note(t("quality.legacy_help"), true) + '</details>';
  }
  function billing(r, kind, t, format) {
    const fields = { delivery: "delivered_th_h", power: "power_kwh", uptime: "uptime_pct" };
    const key = fields[kind];
    const modern = r.billing_method === "time_weighted_v2" || r.billing_source === "observed";
    const reason = r[kind + "_unavailable_reason"];
    const badWindow = ["mixed_periods", "unaligned_windows", "unverified_windows"].includes(reason) ||
      (number(r.sites) > 1 && ["unaligned", "unverified"].includes(r.billing_window_alignment));
    const n = modern && !badWindow ? number(r[key]) : null;
    const old = number(r["legacy_" + key]) ?? (!modern ? number(r[key]) : null);
    let out = value(n, format);
    const observed = number(r[kind + "_observed_hours"]), hours = number(r.billing_hours);
    const coverage = number(r[kind + "_coverage_pct"]);
    if (badWindow) out += note(t("quality.windows"), true);
    else if (n != null && (coverage == null || coverage < 99.99 || observed == null || !hours || observed + 1 / 3600 < hours)) {
      out += note(t(kind === "uptime" ? "rate.partial_average" : "quality.observed_integral"), true);
      out += note(observed != null && hours ? t("rate.coverage", { observed: short(observed), hours: short(hours) })
        : t("rate.incomplete"), true);
    } else if (n == null) {
      const subtotal = number(r["observed_" + key]);
      if (subtotal != null) out += note(t("quality.known_part", { value: format(subtotal) }), true);
      else if (old == null) out += note(t("rate.waiting"), true);
    }
    if (r.report_stale === true) out += note(t("rate.report_stale"), true);
    return out + legacy(old, t, format);
  }
  function daily(r, key, metric, t, format) {
    const n = number(r[key]);
    let out = value(n, format);
    if (r.method !== "time_weighted_v2")
      return out + note(t("quality.legacy_help"), true);
    const hours = number(r.elapsed_hours);
    const observed = number((r.metric_observed_hours || {})[metric]);
    const coverage = number((r.metric_coverage_pct || {})[metric]);
    if (n != null && (coverage == null || coverage < 99.99 || observed == null || !hours || observed + 1 / 3600 < hours))
      out += note(observed != null && hours ? t("rate.coverage", { observed: short(observed), hours: short(hours) })
        : t("rate.incomplete"), true);
    return out + legacy(r["legacy_" + key], t, format);
  }
  function points(rows, x, y, divisor = 1) {
    return rows.map(r => ({ x: r[x], y: number(r[y]) == null ? null : number(r[y]) / divisor }));
  }
  return { number, short, note, partial, total, legacy, billing, daily, points };
});
