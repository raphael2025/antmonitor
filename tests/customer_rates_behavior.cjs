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
const text = html => html.replace(/<[^>]*>/g, "");
const cell = (r, kind = "average") => rates.cell(r, kind, t, format, ago);
const steady = { hashrate_ths: 293, avg_hashrate_ths: 293, hashrate_hours: 24,
  observed_hours: 24, coverage_pct: 100, hashrate_ts: 1, hashrate_age_s: 60,
  delivered_th_h: 7032, machines: 1, current_machines: 1, report_age_s: 360 };
assert.equal(text(cell(steady)), "293 TH/s");
assert.doesNotMatch(cell(steady), /7032|·h|rate-note|报表：|统计截止/);
assert.equal(text(cell(steady, "current")), "293 TH/s");
assert.equal(text(cell({...steady, avg_hashrate_ths: 29300})), "29.3 PH/s");
assert.equal(text(cell({...steady, avg_hashrate_ths: 140000, delivered_th_h: 3360000})), "140 PH/s");
assert.equal(text(cell({...steady, avg_hashrate_ths: 1000000})), "1 EH/s");
assert.equal(text(cell({...steady, avg_hashrate_ths: 297.5})), "297.5 TH/s");
assert.equal(text(cell({...steady, avg_hashrate_ths: 0})), "0 TH/s");
assert.equal(text(cell({...steady, hashrate_ths: 0}, "current")), "0 TH/s");
assert.equal(text(cell({delivered_th_h: 7032, hours: 24})), "—");
assert.equal(text(cell({delivered_th_h: 7032}, "current")), "—");
for (const hours of [23.26, 6, 1]) {
  const partial = cell({...steady, observed_hours: hours, coverage_pct: hours / 24 * 100, avg_complete: false});
  assert.equal(text(partial), "293 TH/s");
  assert.ok(partial.includes(hours + "/24h"));
  assert.match(partial, /title="/);
  assert.doesNotMatch(partial, /rate-note|按有效时段计算/);
}
assert.equal(text(cell({...steady, avg_complete: false})), "293 TH/s");
assert.equal(text(cell({...steady, avg_hashrate_ths: 0, observed_hours: 6, coverage_pct: 25})), "0 TH/s");
for (const r of [
  {...steady, observed_hours: 0, coverage_pct: 0},
  {...steady, hashrate_hours: null},
  {...steady, periods: [24, 72]},
  {...steady, avg_unavailable_reason: "mixed_periods"},
  {...steady, sites: 2, observed_hours: 6, coverage_pct: 25},
  {...steady, sites: 2, avg_unavailable_reason: "partial_coverage"},
  {...steady, avg_unavailable_reason: "partial_sites"},
  {...steady, avg_unavailable_reason: "unaligned_windows"},
  {...steady, avg_unavailable_reason: "unverified_windows"},
  {...steady, avg_unavailable_reason: "stale_reports"},
]) assert.equal(text(cell(r)), "—");
const missing={delivered_th_h:7032,hours:24,hashrate_unavailable_reason:"not_reported",avg_unavailable_reason:"not_reported"};
assert.equal(text(cell(missing,"current")), "—");
assert.equal(text(cell(missing)), "—");
assert.doesNotMatch(cell(missing,"current")+cell(missing),/7032|0 TH|旧口径|<details/);
assert.match(cell({...steady,hashrate_ths:null,hashrate_unavailable_reason:"missing_data"},"current"),/title="本轮数据不完整"/);
assert.equal(text(cell({...steady,hashrate_stale:true},"current")),"293 TH/s");
assert.match(cell({...steady,hashrate_stale:true},"current"),/title="数据已过期"/);
assert.match(cell({...steady,report_stale:true}),/title="报表已过期"/);
const now=Date.now()/1000;
for(const ts of [now-60,now+3600]){
  const unknown={...steady,hashrate_ts:ts,hashrate_age_s:null};
  assert.equal(text(cell(unknown,"current")),"293 TH/s");
  assert.match(cell(unknown,"current"),/title="采样时间未知"/);
}
const inferredTime={...steady,hashrate_ts:now-60};
delete inferredTime.hashrate_age_s;
assert.match(cell(inferredTime,"current"),/title="采样：/);
assert.match(cell({...inferredTime,hashrate_ts:now+3600},"current"),/title="采样时间未知"/);
// Completed-scan display is separate from strict per-measurement freshness.
assert.equal(text(cell({...steady,hashrate_ths:null,hashrate_stale:true,
  display_hashrate_ths:300,display_scan_ts:now-30},"current")),"300 TH/s");
assert.doesNotMatch(cell({...steady,hashrate_stale:true,display_hashrate_ths:300,display_scan_ts:now-30},"current"),/已过期/);
assert.equal(text(cell({...steady,display_hashrate_ths:0,display_scan_ts:now-30},"current")),"0 TH/s");
assert.equal(text(cell({...steady,display_hashrate_ths:null},"current")),"—");
assert.equal(rates.header([steady],t),"24h 平均算力");
assert.equal(rates.header([{hashrate_hours:72}],t),"72h 平均算力");
assert.equal(rates.header([steady,{hashrate_hours:72}],t),"平均算力");
assert.equal(text(rates.machines(steady,t)),"1");
assert.equal(text(rates.machines({...steady,current_machines:0},t)),"0");
assert.equal(text(rates.machines({machines:465},t)),"465");
assert.equal(text(rates.machines({machines:465,machines_source:"legacy"},t)),"—");
assert.equal(text(rates.machines({machines:465,billing_source:"legacy_only"},t)),"—");
assert.equal(text(rates.machines({machines:465,machines_source:"observed",sites:2},t)),"465");
for(const bad of ["<img src=x onerror=alert(1)>",Infinity,NaN,true,-1]){
  assert.equal(rates.number(bad),null);
  assert.doesNotMatch(cell({...steady,avg_hashrate_ths:bad}),/<img|NaN|Infinity/);
}
assert.doesNotMatch(rates.cell(steady,"current",()=>'"><img src=x>',()=>'<script>x</script>',ago),/<img|<script/);
const needed=Object.keys(languages.zh).filter(k=>k.startsWith("rate."));
for(const [lang,dict] of Object.entries(languages)) for(const key of needed){
  assert.ok(dict[key],lang+": "+key);
  assert.deepEqual([...dict[key].matchAll(/\{\w+\}/g)].map(x=>x[0]).sort(),
    [...languages.zh[key].matchAll(/\{\w+\}/g)].map(x=>x[0]).sort(),lang+": "+key);
}
console.log("Customer rate UI: concise values, accurate units, completed snapshots, missing/zero, safe windows and six languages passed");
