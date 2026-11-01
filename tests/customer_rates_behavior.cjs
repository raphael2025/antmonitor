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
  delivered_th_h: 7032, machines: 1, current_machines: 1 };
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
const partial = cell({...steady, observed_hours: 6, coverage_pct: 25});
assert.match(partial, /rate-value">—/);
assert.match(partial, /已观测平均：293 TH\/s/);
assert.match(partial, /6\/24h/);
assert.match(cell({...steady, avg_complete: false}), /rate-value">—/);
assert.match(cell({...steady, periods: [24, 72]}), /统计周期不一致/);
assert.doesNotMatch(cell({...steady, periods: [24, 72]}), /293 TH/);
assert.match(cell({...steady, hashrate_stale: true}, "current"), /数据已过期/);
assert.match(cell({...steady, hashrate_stale: true}, "current"), /293 TH\/s/);
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
console.log("Customer rate UI: units, full/partial windows, zero/unknown, stale data, mixed periods and six languages passed");

