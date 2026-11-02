"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const rates = require("../web/customer-rates.js");
const languages = {};
for (const lang of ["zh", "en", "ru", "es", "de", "ar"]) {
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../web/i18n", lang + ".js"), "utf8"),
    { I18N: { register: (name, values) => { languages[name] = values; } } });
}
const t = (key, args = {}) => (languages.zh[key] || key).replace(/\{(\w+)\}/g, (_, k) => args[k] ?? "");
const format = (v, suffix) => {
  const div = v >= 1e6 ? 1e6 : v >= 1e3 ? 1e3 : 1;
  return (v / div) + " " + (div === 1e6 ? "EH" : div === 1e3 ? "PH" : "TH") + suffix;
};
const ago = age => age + " 秒前";
const cell = (r, kind = "average") => rates.cell(r, kind, t, format, ago);
const steady = { hashrate_ths: 293, avg_hashrate_ths: 293, hashrate_hours: 24,
  observed_hours: 24, coverage_pct: 100, hashrate_ts: 1, hashrate_age_s: 60,
  delivered_th_h: 7032, machines: 1, current_machines: 1, report_age_s: 360 };
assert.match(cell(steady), /293 TH\/s/);
assert.doesNotMatch(cell(steady), /7032|·h/);
assert.match(cell(steady, "current"), /293 TH\/s/);
assert.match(cell({...steady, avg_hashrate_ths: 29300}), /29.3 PH\/s/);
assert.match(cell({...steady, avg_hashrate_ths: 140000, delivered_th_h: 3360000}), /140 PH\/s/);
assert.match(cell({...steady, avg_hashrate_ths: 297.5}), /297.5 TH\/s/);
assert.match(cell({...steady, avg_hashrate_ths: 0}), />0 TH\/s/);
assert.match(cell({...steady, hashrate_ths: 0}, "current"), />0 TH\/s/);
assert.doesNotMatch(cell({delivered_th_h: 7032, hours: 24}), /293|7032|0 TH/);
assert.doesNotMatch(cell({delivered_th_h: 7032}, "current"), /293|7032|0 TH/);
for (const hours of [23.26, 6, 1]) {
  const partial = cell({...steady, observed_hours: hours, coverage_pct: hours / 24 * 100, avg_complete: false});
  assert.match(partial, /rate-value">293 TH\/s/);
  assert.match(partial, /按有效时段计算/);
  assert.ok(partial.includes(hours + "/24h"));
}
assert.match(cell({...steady, avg_complete: false}), /rate-value">293 TH\/s/);
assert.match(cell({...steady, avg_complete: false}), /按有效时段计算/);
assert.match(cell({...steady, avg_hashrate_ths: 0, observed_hours: 6, coverage_pct: 25}), /rate-value">0 TH\/s/);
assert.match(cell({...steady, observed_hours: 0, coverage_pct: 0}), /rate-value">—/);
assert.doesNotMatch(cell({...steady, observed_hours: 0, coverage_pct: 0}), /293 TH\/s/);
assert.doesNotMatch(cell({...steady, hashrate_hours: null}), /293 TH\/s/);
assert.match(cell({...steady, periods: [24, 72]}), /统计周期不一致/);
assert.doesNotMatch(cell({...steady, periods: [24, 72]}), /293 TH/);
assert.doesNotMatch(cell({...steady, avg_unavailable_reason: "mixed_periods"}), /293 TH/);
for (const r of [
  {...steady, sites: 2, observed_hours: 6, coverage_pct: 25},
  {...steady, sites: 2, avg_unavailable_reason: "partial_coverage"},
  {...steady, avg_unavailable_reason: "partial_sites"}
]) {
  assert.match(cell(r), /部分场地数据不足/);
  assert.doesNotMatch(cell(r), /293 TH/);
}
const legacy = {delivered_th_h: 7032, hours: 24, hashrate_age_s: null, hashrate_stale: false,
  hashrate_unavailable_reason: "not_reported", avg_unavailable_reason: "not_reported", report_age_s: 360};
assert.match(cell(legacy, "current"), /本地未上报/);
assert.match(cell(legacy), /本地未上报/);
assert.match(cell(legacy), /报表：360 秒前/);
assert.doesNotMatch(cell(legacy, "current") + cell(legacy), /过期|0 TH|7032/);
assert.match(cell({...steady, hashrate_ths: null, hashrate_unavailable_reason: "missing_data"}, "current"), /本轮数据不完整/);
assert.match(cell({...steady, hashrate_ths: null, hashrate_unavailable_reason: "partial_sites"}, "current"), /部分场地数据不足/);
assert.match(cell({...steady, hashrate_stale: true}, "current"), /数据已过期/);
assert.match(cell({...steady, hashrate_stale: true}, "current"), /293 TH\/s/);
// A known stale site still invalidates a sum when another site's timestamp is unknown.
assert.match(cell({...steady, sites: 2, hashrate_age_s: null, hashrate_stale: true}, "current"), /数据已过期/);
assert.match(cell({...steady, sites: 2, report_age_s: null, report_stale: true}), /报表已过期/);
const now = Date.now() / 1000;
for (const ts of [now - 60, now + 3600]) {
  const unknown = {...steady, hashrate_ts: ts, hashrate_age_s: null, updated_ts: ts, report_age_s: null};
  assert.match(cell(unknown, "current"), /采样时间未知/);
  assert.match(cell(unknown), /报表时间未知/);
  assert.doesNotMatch(cell(unknown, "current") + cell(unknown), /采样：|报表：|过期/);
}
const legacyTime = {...steady, hashrate_ts: now - 60, updated_ts: now - 60};
delete legacyTime.hashrate_age_s;
delete legacyTime.report_age_s;
assert.match(cell(legacyTime, "current"), /采样：/);
assert.match(cell(legacyTime), /报表：/);
assert.match(cell({...legacyTime, hashrate_ts: now + 3600}, "current"), /采样时间未知/);
assert.match(cell({...legacyTime, updated_ts: now + 3600}), /报表时间未知/);
assert.equal(rates.header([steady], t), "24h 平均算力");
assert.equal(rates.header([{hashrate_hours: 72}], t), "72h 平均算力");
assert.equal(rates.header([steady, {hashrate_hours: 72}], t), "平均算力");
assert.equal(rates.machines(steady, t), "1");
assert.match(rates.machines({machines: 465}, t), /周期内峰值台数/);
for (const bad of ["<img src=x onerror=alert(1)>", Infinity, NaN, true, -1]) {
  assert.equal(rates.number(bad), null);
  assert.doesNotMatch(cell({...steady, avg_hashrate_ths: bad}), /<img|NaN|Infinity/);
}
const needed = Object.keys(languages.zh).filter(k => k.startsWith("rate."));
for (const [lang, dict] of Object.entries(languages))
  for (const key of needed) {
    assert.ok(dict[key], lang + ": " + key);
    assert.deepEqual([...dict[key].matchAll(/\{\w+\}/g)].map(x => x[0]).sort(),
      [...languages.zh[key].matchAll(/\{\w+\}/g)].map(x => x[0]).sort(), lang + ": " + key);
  }
console.log("Customer rate UI: units, visible partial means, missing/stale/unknown times, safe site totals and six languages passed");

