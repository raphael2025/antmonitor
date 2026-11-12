"use strict";
/* Rates are TH/s. Billing delivery remains TH·h; never relabel or divide it here. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AntCustomerRates = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = v => v == null || v === "" || typeof v === "boolean" ||
    !Number.isFinite(Number(v)) || Number(v) < 0 ? null : Number(v);
  const period = r => number(r.hashrate_hours) || number(r.hours);
  const short = n => Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
  const value = (n, format, hint = "") => '<span class="rate-value"' +
    (hint ? ' title="' + esc(hint) + '"' : '') + '>' + (n == null ? "—" : esc(format(n))) + '</span>';
  function age(r, key, timestamp) {
    // An explicit null is the server's "unknown/untrusted time" decision.
    if (Object.prototype.hasOwnProperty.call(r, key)) return number(r[key]);
    const ts = number(r[timestamp]), now = Date.now() / 1000;
    return ts && ts <= now + 300 ? Math.max(0, now - ts) : null;
  }
  function header(rows, t) {
    const periods = new Set();
    for (const r of rows || []) {
      if (Array.isArray(r.periods)) r.periods.forEach(h => { if (number(h)) periods.add(Number(h)); });
      const h = period(r);
      if (h) periods.add(h);
    }
    return periods.size > 1 ? t("rate.average") :
      t("rate.average_period", { h: short(periods.size ? [...periods][0] : 24) });
  }
  function cell(r, kind, t, formatHash, ago) {
    const display = kind === "current" && Object.prototype.hasOwnProperty.call(r, "display_hashrate_ths");
    const n = number(display ? r.display_hashrate_ths : r[kind === "current" ? "hashrate_ths" : "avg_hashrate_ths"]);
    const hash = n => formatHash(n, "/s");
    if (kind === "current") {
      const sampleAge = display ? age({ sample_ts: r.display_scan_ts }, "sample_age_s", "sample_ts")
        : age(r, "hashrate_age_s", "hashrate_ts");
      const missing = r.hashrate_unavailable_reason === "not_reported" ? "rate.not_reported" :
        r.hashrate_unavailable_reason === "partial_sites" ? "rate.partial_sites" :
          r.hashrate_unavailable_reason === "missing_data" ? "rate.missing_measurement" :
          r.hashrate_unavailable_reason === "stale_sites" ? "rate.stale" :
          r.hashrate_unavailable_reason === "unverified_time" ? "rate.time_unknown" : "rate.waiting";
      const hint = n == null ? t(missing) : !display && r.hashrate_stale === true ? t("rate.stale") :
        sampleAge != null ? t("rate.sample_age", { age: ago(sampleAge) }) : t("rate.time_unknown");
      return value(n, hash, hint);
    }
    const h = period(r), coverage = number(r.coverage_pct), observed = number(r.observed_hours);
    const mixed = r.avg_unavailable_reason === "mixed_periods" ||
      (Array.isArray(r.periods) && new Set(r.periods.filter(x => number(x)).map(Number)).size > 1);
    const average = h && observed !== 0 ? n : null;
    const complete = average != null && (r.avg_complete === true ||
      (r.avg_complete !== false && coverage != null && coverage >= 99.99));
    // Cross-site averages still require comparable, aligned observation windows.
    const badWindow = ["unaligned_windows", "unverified_windows"].includes(r.avg_unavailable_reason) ||
      (number(r.sites) > 1 && ["unaligned", "unverified"].includes(r.window_alignment));
    const staleReports = r.avg_unavailable_reason === "stale_reports";
    const partialSites = r.avg_unavailable_reason === "partial_sites" ||
      (number(r.sites) > 1 && (r.avg_unavailable_reason === "partial_coverage" || (average != null && !complete)));
    const displayed = !mixed && !partialSites && !badWindow && !staleReports ? average : null;
    let hint = "";
    if (badWindow) hint = t("quality.windows");
    else if (staleReports || r.report_stale === true) hint = t("rate.report_stale");
    else if (mixed || partialSites) hint = t(mixed ? "rate.mixed_periods" : "rate.partial_sites");
    else if (displayed == null) hint = t(r.avg_unavailable_reason === "not_reported" ? "rate.not_reported" : "rate.waiting");
    else if (!complete) hint = observed != null && h
      ? t("rate.coverage", { observed: short(observed), hours: short(h) }) : t("rate.incomplete");
    // One concise cell, with optional context on hover instead of repeated prose.
    return value(displayed, hash, hint);
  }
  function machines(r, t) {
    const current = number(r.current_machines);
    if (current != null) return short(current);
    if (r.machines_source === "legacy" || (r.machines_source == null && r.billing_source === "legacy_only")) return "—";
    const historical = number(r.machines);
    return historical == null ? "—" : value(historical, short,
      t(number(r.sites) > 1 ? "rate.site_peak_sum" : "rate.period_machines"));
  }
  return { number, period, header, cell, machines };
});

