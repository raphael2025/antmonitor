"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");
const q = require("../web/data-quality.js");
const rates = require("../web/customer-rates.js");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
const isCloud = app.includes("function renderOverview");
const dicts = {};
for (const lang of ["zh", "en", "ru", "es", "de", "ar"])
  vm.runInNewContext(fs.readFileSync(path.join(root,"web/i18n",lang+".js"),"utf8"),
    {I18N:{register:(name,values)=>{dicts[name]=values;}}});
const t = (key,args={}) => (dicts.zh[key] || key).replace(/\{(\w+)\}/g,(_,k)=>args[k] ?? "");
const format = n => n + " kWh";
const observed = {billing_method:"time_weighted_v2",billing_source:"observed",billing_hours:24,
  power_kwh:3, power_observed_hours:1,power_coverage_pct:100/24,
  delivered_th_h:293,delivery_observed_hours:1,delivery_coverage_pct:100/24,
  uptime_pct:100,uptime_observed_hours:1,uptime_coverage_pct:100/24};

test("unknown, explicit zero and archived estimates remain different", () => {
  for (const n of [null,undefined,true,false,-1,Infinity,NaN,"", " ", [], {}, "bad"])
    assert.equal(q.number(n),null);
  assert.equal(q.number(0),0);
  assert.equal(q.number("293"),293);
  const empty=q.billing({},"power",t,format);
  assert.match(empty,/rate-value">—/);
  assert.doesNotMatch(empty,/0 kWh/);
  const zero=q.billing({...observed,power_kwh:0},"power",t,format);
  assert.match(zero,/rate-value">0 kWh/);
  const old=q.billing({power_kwh:99},"power",t,format);
  assert.match(old,/rate-value">—/);
  assert.match(old,/<details.*旧口径记录/);
  assert.match(old,/99 kWh.*完整性未验证/);
});

test("energy and availability use their own measured coverage", () => {
  const row={...observed,power_observed_hours:6,power_coverage_pct:25,observed_hours:24,coverage_pct:100};
  const cell=q.billing(row,"power",t,format);
  assert.match(cell,/rate-value">3 kWh/);
  assert.match(cell,/仅累计有效时段/);
  assert.match(cell,/6\/24h/);
  const up=q.billing({...row,uptime_observed_hours:12,uptime_coverage_pct:50},"uptime",t,n=>n+"%");
  assert.match(up,/12\/24h/);
  assert.doesNotMatch(up,/6\/24h/);
  assert.match(q.billing({...observed,power_observed_hours:24-5/3600,power_coverage_pct:99.9942},"power",t,format),/仅累计有效时段/);
});

test("different site windows and stale reports cannot look like a full common window", () => {
  for (const alignment of ["unaligned","unverified"]){
    const r={...observed,sites:2,billing_window_alignment:alignment,power_unavailable_reason:alignment+"_windows"};
    const cell=q.billing(r,"power",t,format);
    assert.match(cell,/rate-value">—/);
    assert.match(cell,/时间未对齐/);
    assert.doesNotMatch(cell,/rate-value">3 kWh/);
    const avg=rates.cell({avg_hashrate_ths:586,hashrate_hours:24,observed_hours:24,coverage_pct:100,sites:2,window_alignment:alignment},"average",t,n=>n+" TH/s",n=>n+"s");
    assert.match(avg,/rate-value">—/);
    assert.doesNotMatch(avg,/586 TH/);
  }
  const stale=q.billing({...observed,report_stale:true},"power",t,format);
  assert.match(stale,/报表已过期/);
});

test("today's coverage uses elapsed hours; old daily values stay explicitly legacy",()=>{
  const d={method:"time_weighted_v2",elapsed_hours:6,power_kwh:12,
    metric_observed_hours:{power_kw:3},metric_coverage_pct:{power_kw:50},legacy_power_kwh:36};
  const cell=q.daily(d,"power_kwh","power_kw",t,format);
  assert.match(cell,/rate-value">12 kWh/);
  assert.match(cell,/3\/6h/);
  assert.doesNotMatch(cell,/3\/24h/);
  assert.match(cell,/36 kWh/);
  const old=q.daily({method:"legacy_sample_mean",power_kwh:36},"power_kwh","power_kw",t,format);
  assert.match(old,/rate-value">36 kWh/);
  assert.match(old,/完整性未验证/);
});

test("partial totals and plotting never manufacture measured zero",()=>{
  const cell=q.total({totals:{sites:2,power_kw:null},observed_totals:{power_kw:3},
    metric_known_sites:{power_kw:1},metrics_complete:{power_kw:false}},"power_kw",t,format);
  assert.match(cell,/rate-value">—/);
  assert.match(cell,/已知部分：3 kWh（1\/2 个场地）/);
  assert.deepEqual(q.points([[1,null],[2,0],[3,293],[4,true]],0,1),[
    {x:1,y:null},{x:2,y:0},{x:3,y:293},{x:4,y:null}]);
});

test("all six locales cover quality and unit statements and preserve placeholders",()=>{
  const powerKeys = ["card.power", "th.power", "worker.kwh", "worker.hint", "miner.detail_hr",
    "seg.stat", "cbox.power_detail", "cbox.detail_power", "kpi.power", "chart.power", "th.kwh", "th.kwh24"];
  const keys=Object.keys(dicts.zh).filter(k=>k.startsWith("quality.") || k.startsWith("power.") ||
    powerKeys.includes(k) || k==="kind.inventory");
  for (const [lang,values] of Object.entries(dicts)) for (const key of keys){
    assert.ok(values[key],lang+": "+key);
    assert.deepEqual((values[key].match(/\{\w+\}/g)||[]).sort(),(dicts.zh[key].match(/\{\w+\}/g)||[]).sort(),lang+": "+key);
  }
});

function setup(){
  const elements=new Map(), configs={};
  function el(id){
    assert.ok(ids.has(id),"real element exists: "+id);
    if(!elements.has(id)){
      const cls=new Set(["hidden"]),body={innerHTML:""};
      elements.set(id,{id,value:"24",textContent:"",innerHTML:"",title:"",style:{},disabled:false,
        classList:{contains:k=>cls.has(k),add:k=>cls.add(k),remove:k=>cls.delete(k),toggle:(k,v)=>v?cls.add(k):cls.delete(k)},
        querySelector:()=>body,addEventListener(){},focus(){}});
    }
    return elements.get(id);
  }
  class Chart{
    constructor(canvas,config){this.data=config.data;this.options=config.options;configs[canvas.id]=config;}
    destroy(){} update(){}
  }
  let response={};
  const ctx=vm.createContext({document:{getElementById:el,addEventListener(){}},window:{Chart},Chart,
    localStorage:{getItem:()=>null,setItem(){}},AntDataQuality:q,AntCustomerRates:rates,t,Date,console,
    updateProgress(){},setSiteBusy(){},renderSiteAlerts(){},setInterval(){},setTimeout(){}});
  if(isCloud){
    vm.runInContext(app.slice(0,app.indexOf("// ---- HTTP ----")),ctx);
    ctx.api=async()=>response;
    vm.runInContext("let curSite=null,curSiteData=null,curRole='admin';",ctx);
    vm.runInContext(app.slice(app.indexOf("// ---- 图表 ----"),app.indexOf("async function refresh()")),ctx);
    vm.runInContext(app.slice(app.indexOf("async function loadSite()"),app.indexOf("// ---- 版本更新")),ctx);
  }else{
    vm.runInContext(app.slice(0,app.indexOf("async function ackAlert")),ctx);
    ctx.jget=async()=>response;
    vm.runInContext(app.slice(app.indexOf("function trendYScale"),app.indexOf("let lastMiners")),ctx);
    vm.runInContext(app.slice(app.indexOf("let lastMiners"),app.indexOf("/* ---------------- 货架视图 ---------------- */")),ctx);
    vm.runInContext(app.slice(app.indexOf("async function openMiner"),app.indexOf("async function triggerScan")),ctx);
    vm.runInContext("let view='worker';",ctx);
    vm.runInContext(app.slice(app.indexOf("const CBOX_PUMP_KEYS"),app.indexOf("function refreshAll()")),ctx);
    vm.runInContext(app.slice(app.indexOf("async function refreshWorkers()"),app.indexOf('let view = "list"')),ctx);
  }
  return {ctx,el,configs,respond:r=>{response=r;}};
}

test("real power and energy renderers keep kW distinct from period kWh at site scale",async()=>{
  const h=setup();
  const compact = s => s.replace(/[\s,]/g, "");
  const rawPower = kw => isCloud ? kw : kw * 1000;
  for (const kw of [0, 5.355, 2870, 7200, 10070]) {
    assert.equal(compact(h.ctx.fmtPower(rawPower(kw))), kw + "kW");
  }
  for (const kwh of [0, 54080, 134568, 1000000]) {
    assert.equal(compact(h.ctx.fmtEnergy(kwh)), kwh + "kWh");
  }
  for (const bad of [null, undefined, true, false, -1, Infinity, NaN, "", "bad"]) {
    assert.match(h.ctx.fmtPower(bad), /^[-–—]$/);
    assert.match(h.ctx.fmtEnergy(bad), /^[-–—]$/);
  }
  const row = {...observed, worker:"A", site_id:"a", site_name:"A", sites:1,
    power_kwh:54080, power_observed_hours:24, power_coverage_pct:100};
  if (isCloud) {
    h.ctx.renderOverview({ts:1800000000, totals:{sites:2, sites_ok:2, power_kw:10070},
      sites:[{id:"a", name:"A", type:"hydro", state:"ok", total:1330, online:1330, power_kw:7200},
        {id:"b", name:"B", type:"hydro", state:"ok", total:539, online:539, power_kw:2870}]});
    assert.match(compact(h.el("kPower").innerHTML), /10070kW/);
    assert.match(compact(h.el("gridHydro").innerHTML), /7200kW/);
    assert.match(compact(h.el("gridHydro").innerHTML), /2870kW/);
    h.ctx.renderCustomers({rows:[row], by_worker:[row]});
    h.ctx.renderSiteCustomers([row]);
    for (const id of ["tblCustomers", "tblWorkers", "tblSiteCustomers"]) {
      const body=h.el(id).querySelector().innerHTML;
      assert.match(compact(body), /54080kWh/);
      assert.doesNotMatch(body, /MWh|GWh/);
    }
  } else {
    h.respond({scanned:true, scan_ts:1800000000, total_power_kw:10070, known_power_kw:10070, power_complete:true});
    await h.ctx.refreshSummary();
    assert.match(compact(h.el("cPower").innerHTML), /10070/);
    h.respond({miners:[{ip:"10.0.0.1", status:"online", power:5355}], agg:{total_power:5355}});
    await h.ctx.refreshMiners();
    assert.match(h.el("minerBody").innerHTML, /<td>5.355 kW<\/td>/);
    h.respond({current:{power:5355}, history:[]});
    await h.ctx.openMiner("10.0.0.1");
    assert.match(h.el("mInfo").innerHTML, /当前功率：5.355 kW/);
    assert.doesNotMatch(h.el("mInfo").innerHTML, /kW W/);
    h.respond({customers:[row]});
    await h.ctx.refreshWorkers();
    assert.match(compact(h.el("workerList").innerHTML), /54080kWh/);
    assert.doesNotMatch(h.el("workerList").innerHTML, /MWh|GWh/);
  }
  for (const lang of Object.keys(dicts)) {
    assert.match(dicts[lang][isCloud ? "th.kwh" : "worker.kwh"], /kWh/);
    assert.match(dicts[lang][isCloud ? "kpi.power" : "card.power"], /kW/);
    assert.match(dicts[lang]["power.units_help"], /1000 kW/);
  }
});

test("actual dashboard handlers handle incomplete APIs without null crashes or invented totals",async()=>{
  const h=setup();
  if(isCloud){
    h.ctx.renderOverview({ts:1800000000,totals:{sites:2,sites_ok:1,machines_online:null,machines_total:null,
      hashrate_ths:null,power_kw:null,active_alerts:null,containers:null,containers_faulty:null,containers_offline:null,customers:1},
      observed_totals:{machines_online:1,machines_total:1,hashrate_ths:293,power_kw:3},
      metrics_complete:{hashrate_ths:false,power_kw:false,machines_total:false},metric_known_sites:{hashrate_ths:1,power_kw:1,machines_total:1},
      sites:[{id:"a",name:"<旧场地>",type:"air",state:"stalled",data_quality:"stale",online:100,total:100,hashrate_ths:29300}]});
    assert.equal(h.el("kMachines").textContent,"–/–");
    assert.match(h.el("kMachinesNote").textContent,/1\/1.*1\/2/);
    assert.match(h.el("kHashrate").innerHTML,/rate-value">—/);
    assert.match(h.el("kHashrate").innerHTML,/已知部分：293 TH\/s/);
    assert.match(h.el("gridAir").innerHTML,/最后记录已过期/);
    assert.doesNotMatch(h.el("gridAir").innerHTML,/<旧场地>/);
    h.ctx.renderCustomers({rows:[{worker:"A",site_id:"a",...observed,power_kwh:null,uptime_pct:null}],by_worker:[]});
    assert.doesNotMatch(h.el("tblCustomers").querySelector().innerHTML,/null%|0 kWh/);
    h.respond({sites:[{name:"A",points:[[1,293],[2,null],[3,0]],legacy_points:[[1,null],[2,300],[3,null]]}]});
    await h.ctx.loadTrend();
    assert.equal(h.configs.trendChart.data.datasets[0].data[1].y,null);
    assert.equal(h.configs.trendChart.data.datasets[0].data[2].y,0);
    assert.equal(h.configs.trendChart.data.datasets[1].data[1].y,300);
    assert.ok(h.configs.trendChart.data.datasets[1].borderDash);
  }else{
    h.respond({scanned:true,scan_ts:1800000000,scan_kind:"inventory",online:1,total:2,unknown:1,offline:0,
      total_hashrate_th:null,known_hashrate_ths:293,hashrate_complete:false,avg_hashrate_th:null,
      total_power_kw:null,known_power_kw:3,power_complete:false,avg_efficiency:null});
    await h.ctx.refreshSummary();
    assert.equal(h.el("cOnline").textContent,"1 / 2");
    assert.match(h.el("cTotalHr").innerHTML,/已知部分：293 TH\/s/);
    assert.match(h.el("cPower").innerHTML,/rate-value">—/);
    assert.equal(h.el("cEff").textContent,"—");
    assert.match(h.el("summaryQuality").textContent,/未知\/过期状态 1 台/);
    assert.equal(h.ctx.boxPower({power1:1000,power2:null}),null);
    assert.equal(h.ctx.boxPower({power1:1000,power2:500}),1500);
    assert.equal(h.ctx.rejCell({accepted:100,rejected:null}),"<td>-</td>");
    h.respond({customers:[{worker:"A",...observed,power_kwh:null,uptime_pct:null}]});
    await h.ctx.refreshWorkers();
    assert.doesNotMatch(h.el("workerList").innerHTML,/null%|0 kWh/);
    h.respond({trend:[{ts:1,total_hr:293},{ts:2,total_hr:null,legacy_total_hr:300},{ts:3,total_hr:0}]});
    await h.ctx.refreshTrend();
    assert.equal(h.configs.trendChart.data.datasets[0].data[1].y,null);
    assert.equal(h.configs.trendChart.data.datasets[0].data[2].y,0);
    assert.ok(h.configs.trendChart.data.datasets[1].borderDash);
    assert.equal(h.configs.trendChart.options.scales.x.type,"linear");
    h.respond({current:{online:1,data_status:"unknown",data_complete:0,faults:[]},stale_after:60,
      history:[{ts:100,supply_temp:-1},{ts:110,supply_temp:0},{ts:3700,supply_temp:3}]});
    await h.ctx.openContainer("10.0.0.1");
    const temperatures=h.configs.mChart.data.datasets[0].data;
    assert.equal(h.configs.mChart.options.scales.x.type,"linear");
    assert.equal(temperatures[0].y,-1);
    assert.equal(temperatures[1].y,0);
    assert.equal(temperatures[2].y,null);
    assert.equal(temperatures[3].x,3700);
    assert.match(h.el("mInfo").innerHTML,/数据不完整/);
    assert.doesNotMatch(h.el("mInfo").innerHTML,/无故障/);
    assert.match(h.ctx.containerFaultBadge({flag:"leakage_fault",sev:"crit",observed:false,measured_ts:100}),/尚未确认恢复/);

  }
});
