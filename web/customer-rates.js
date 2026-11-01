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
      const age = number(r.hashrate_age_s) ?? (number(r.hashrate_ts) ?
        Math.max(0, Date.now() / 1000 - Number(r.hashrate_ts)) : null);
      const stale = r.hashrate_stale === true;
      return '<span class="rate-value">' + (value == null ? "—" : hash(value)) + '</span>' +
        (stale ? note(t("rate.stale"), true) : "") +
        (age != null ? note(t("rate.sample_age", { age: ago(age) })) :
          (value == null ? note(t("rate.waiting")) : note(t("rate.time_unknown"), true)));
    }
    const h = period(r), coverage = number(r.coverage_pct), observed = number(r.observed_hours);
    const mixed = Array.isArray(r.periods) && new Set(r.periods.filter(x => number(x)).map(Number)).size > 1;
    if (mixed) return "—" + note(t("rate.mixed_periods"), true);
    if (r.avg_unavailable_reason === "partial_coverage" && number(r.sites) > 1)
      return "—" + note(t("rate.partial_sites"), true);
    const complete = r.avg_complete === true || (r.avg_complete !== false && coverage != null && coverage >= 99.99);
    let out = '<span class="rate-value">' + (value != null && complete ? hash(value) : "—") + '</span>';
    if (value != null && !complete)
      out += note(t("rate.observed_average", { rate: formatHash(value, "/s") }));
    if (!complete) {
      out += observed != null && h ?
        note(t("rate.coverage", { observed: short(observed), hours: short(h) }), true) :
        note(t(value == null ? "rate.waiting" : "rate.incomplete"), true);
    }
    const reportAge = number(r.report_age_s) ?? (number(r.updated_ts) ?
      Math.max(0, Date.now() / 1000 - Number(r.updated_ts)) : null);
    if (reportAge != null) out += note(t("rate.report_age", { age: ago(reportAge) }));
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

