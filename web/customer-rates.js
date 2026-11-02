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
  const note = (s, warn) => '<span class="rate-note muted' + (warn ? ' rate-warning' : '') + '">' + esc(s) + '</span>';
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
    const value = number(r[kind === "current" ? "hashrate_ths" : "avg_hashrate_ths"]);
    const hash = n => esc(formatHash(n, "/s"));
    if (kind === "current") {
      const sampleAge = age(r, "hashrate_age_s", "hashrate_ts");
      const stale = r.hashrate_stale === true;
      const missing = r.hashrate_unavailable_reason === "not_reported" ? "rate.not_reported" :
        r.hashrate_unavailable_reason === "partial_sites" ? "rate.partial_sites" :
          r.hashrate_unavailable_reason === "missing_data" ? "rate.missing_measurement" : "rate.waiting";
      return '<span class="rate-value">' + (value == null ? "—" : hash(value)) + '</span>' +
        (value == null ? note(t(missing), true) : "") +
        (stale ? note(t("rate.stale"), true) : "") +
        (sampleAge != null ? note(t("rate.sample_age", { age: ago(sampleAge) })) :
          (value != null ? note(t("rate.time_unknown"), true) : ""));
    }
    const h = period(r), coverage = number(r.coverage_pct), observed = number(r.observed_hours);
    const mixed = r.avg_unavailable_reason === "mixed_periods" ||
      (Array.isArray(r.periods) && new Set(r.periods.filter(x => number(x)).map(Number)).size > 1);
    const average = h && observed !== 0 ? value : null;
    const complete = average != null && (r.avg_complete === true ||
      (r.avg_complete !== false && coverage != null && coverage >= 99.99));
    // Partial windows from different sites need interval intersections, not a sum.
    const partialSites = r.avg_unavailable_reason === "partial_sites" ||
      (number(r.sites) > 1 && (r.avg_unavailable_reason === "partial_coverage" || (average != null && !complete)));
    let out = '<span class="rate-value">' + (!mixed && !partialSites && average != null ? hash(average) : "—") + '</span>';
    if (mixed || partialSites) out += note(t(mixed ? "rate.mixed_periods" : "rate.partial_sites"), true);
    else if (!complete) {
      if (average != null) out += note(t("rate.partial_average"), true);
      else out += note(t(r.avg_unavailable_reason === "not_reported" ? "rate.not_reported" : "rate.waiting"), true);
      if (observed != null && h)
        out += note(t("rate.coverage", { observed: short(observed), hours: short(h) }), true);
      else if (average != null) out += note(t("rate.incomplete"), true);
    }
    const reportAge = age(r, "report_age_s", "updated_ts");
    if (reportAge != null) out += note(t("rate.report_age", { age: ago(reportAge) }));
    else if (average != null && !mixed && !partialSites) out += note(t("rate.report_time_unknown"), true);
    if (r.report_stale === true) out += note(t("rate.report_stale"), true);
    return out;
  }
  function machines(r, t) {
    const current = number(r.current_machines);
    if (current != null) return short(current);
    const historical = number(r.machines);
    return historical == null ? "—" : short(historical) + note(t("rate.period_machines"));
  }
  return { number, period, header, cell, machines };
});

