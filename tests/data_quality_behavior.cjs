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
const visibleText = html => html.replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim();
const observed = {billing_method:"time_weighted_v2",billing_source:"observed",billing_hours:24,
  power_kwh:3, power_observed_hours:1,power_coverage_pct:100/24,
  delivered_th_h:293,delivery_observed_hours:1,delivery_coverage_pct:100/24,
  uptime_pct:100,uptime_observed_hours:1,uptime_coverage_pct:100/24};

test("unknown and measured zero stay different; discarded estimates are not rendered", () => {
  for (const n of [null,undefined,true,false,-1,Infinity,NaN,"", " ", [], {}, "bad"])
    assert.equal(q.number(n),null);
  assert.equal(q.number(0),0);
  assert.equal(q.number("293"),293);
  assert.equal(visibleText(q.billing({},"power",t,format)),"—");
  assert.equal(visibleText(q.billing({...observed,power_kwh:0},"power",t,format)),"0 kWh");
  const old=q.billing({power_kwh:99},"power",t,format);
  assert.equal(visibleText(old),"—");
  assert.doesNotMatch(old,/99|旧口径|完整性未验证|<details/);
});

test("energy and availability keep their own coverage only in hover context", () => {
  const row={...observed,power_observed_hours:6,power_coverage_pct:25,observed_hours:24,coverage_pct:100};
  const cell=q.billing(row,"power",t,format);
  assert.equal(visibleText(cell),"3 kWh");
  assert.match(cell,/title="[^"]*6\/24h/);
  const up=q.billing({...row,uptime_observed_hours:12,uptime_coverage_pct:50},"uptime",t,n=>n+"%");
  assert.equal(visibleText(up),"100%");
  assert.match(up,/title="[^"]*12\/24h/);
  assert.doesNotMatch(up,/6\/24h/);
  assert.doesNotMatch(cell+up,/rate-note|<details/);
});

test("incompatible site windows stay unavailable without extra table rows", () => {
  for (const alignment of ["unaligned","unverified"]){
    const r={...observed,sites:2,billing_window_alignment:alignment,power_unavailable_reason:alignment+"_windows"};
    const cell=q.billing(r,"power",t,format);
    assert.equal(visibleText(cell),"—");
    assert.match(cell,/title="[^"]*时间未对齐/);
    assert.doesNotMatch(cell,/3 kWh/);
    const avg=rates.cell({avg_hashrate_ths:586,hashrate_hours:24,observed_hours:24,coverage_pct:100,sites:2,window_alignment:alignment},"average",t,n=>n+" TH/s",n=>n+"s");
    assert.equal(visibleText(avg),"—");
    assert.doesNotMatch(avg,/586 TH/);
  }
  const stale=q.billing({...observed,report_stale:true},"power",t,format);
  assert.equal(visibleText(stale),"3 kWh");
  assert.match(stale,/title="报表已过期"/);
});

test("daily cells show valid measurements and never fall back to discarded records",()=>{
  const d={method:"time_weighted_v2",elapsed_hours:6,power_kwh:12,
    metric_observed_hours:{power_kw:3},metric_coverage_pct:{power_kw:50},legacy_power_kwh:36};
  const cell=q.daily(d,"power_kwh","power_kw",t,format);
  assert.equal(visibleText(cell),"12 kWh");
  assert.match(cell,/title="[^"]*3\/6h/);
  assert.doesNotMatch(cell,/3\/24h|36 kWh|旧口径|<details/);
  const old=q.daily({method:"legacy_sample_mean",power_kwh:36},"power_kwh","power_kw",t,format);
  assert.equal(visibleText(old),"—");
  assert.doesNotMatch(old,/36|完整性未验证/);
});

test("partial totals and plotting never manufacture measured zero",()=>{
  const cell=q.total({totals:{sites:2,power_kw:null},observed_totals:{power_kw:3},
    metric_known_sites:{power_kw:1},metrics_complete:{power_kw:false}},"power_kw",t,format);
  assert.equal(visibleText(cell),isCloud ? "3 kWh" : "—");
  assert.match(cell,/title="已知部分：3 kWh（1\/2 个场地）"/);
  assert.deepEqual(q.points([[1,null],[2,0],[3,293],[4,true]],0,1),[
    {x:1,y:null},{x:2,y:0},{x:3,y:293},{x:4,y:null}]);
});

test("all six locales cover quality and unit statements and preserve placeholders",()=>{
  const powerKeys = ["card.power", "th.power", "worker.kwh", "worker.hint", "miner.detail_hr",
    "seg.stat", "cbox.power_detail", "cbox.detail_power", "kpi.power", "chart.power", "th.kwh", "th.kwh24"];
  const keys=Object.keys(dicts.zh).filter(k=>k.startsWith("quality.") || k.startsWith("power.") || k.startsWith("trend.") ||
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
    vm.runInContext(app.slice(app.indexOf("let _workerSeq = 0"),app.indexOf('let view = "list"')),ctx);
    vm.runInContext("let rackMode=false;",ctx);
    vm.runInContext(app.slice(app.indexOf("function setView(v)"),app.indexOf("async function openMiner")),ctx);
  }
  return {ctx,el,configs,respond:r=>{response=r;}};
}

function deferredRequests(h, method) {
  const pending = [];
  h.ctx[method] = url => new Promise((resolve, reject) => pending.push({url, resolve, reject}));
  return pending;
}

test("customer periods keep the newest selection when responses arrive out of order", async()=>{
  const h=setup(), pending=deferredRequests(h,"jget");
  const old=h.ctx.refreshWorkers();
  h.el("workerPeriod").value="72";
  const current=h.ctx.refreshWorkers();
  assert.match(pending[0].url,/hours=24$/);
  assert.match(pending[1].url,/hours=72$/);
  pending[1].resolve({customers:[{...observed,worker:"NEW_72_HOURS"}]});
  await current;
  pending[0].resolve({customers:[{...observed,worker:"OLD_24_HOURS"}]});
  await old;
  assert.match(h.el("workerList").innerHTML,/NEW_72_HOURS/);
  assert.doesNotMatch(h.el("workerList").innerHTML,/OLD_24_HOURS/);
});

test("same-period refresh and real-time switch cannot revive an older customer report", async()=>{
  const h=setup(), pending=deferredRequests(h,"jget");
  const oldest=h.ctx.refreshWorkers();
  h.el("workerPeriod").value="0";
  const realtime=h.ctx.refreshWorkers();
  h.el("workerPeriod").value="24";
  const current=h.ctx.refreshWorkers();
  pending[2].resolve({customers:[{...observed,worker:"CURRENT_REPORT"}]});
  await current;
  pending[0].resolve({customers:[{...observed,worker:"STALE_SAME_PERIOD"}]});
  pending[1].resolve({workers:[{worker:"STALE_REALTIME",models:{},total:1,hashrate:1}]});
  await Promise.all([oldest,realtime]);
  assert.match(h.el("workerList").innerHTML,/CURRENT_REPORT/);
  assert.doesNotMatch(h.el("workerList").innerHTML,/STALE_/);
});

test("leaving the customer view prevents an in-flight report from rendering", async()=>{
  const h=setup(), pending=deferredRequests(h,"jget");
  h.el("workerList").innerHTML="KEEP_CURRENT_VIEW";
  const request=h.ctx.refreshWorkers();
  vm.runInContext("view='list'",h.ctx);
  pending[0].resolve({customers:[{...observed,worker:"OLD_VIEW"}]});
  await request;
  assert.equal(h.el("workerList").innerHTML,"KEEP_CURRENT_VIEW");
});

test("closing and reopening the customer view at the same period invalidates its old request", async()=>{
  const h=setup(), pending=deferredRequests(h,"jget");
  h.ctx.refreshMiners=()=>{};
  const old=h.ctx.refreshWorkers();
  h.ctx.setView("list");
  assert.equal(h.el("workerView").classList.contains("hidden"),true);
  const refreshWorkers=h.ctx.refreshWorkers;
  let current;
  h.ctx.refreshWorkers=()=>current=refreshWorkers();
  h.ctx.setView("worker");
  assert.equal(h.el("workerView").classList.contains("hidden"),false);
  assert.equal(pending[0].url,pending[1].url);
  pending[1].resolve({customers:[{...observed,worker:"REOPENED_CUSTOMER_REPORT"}]});
  await current;
  pending[0].resolve({customers:[{...observed,worker:"CLOSED_CUSTOMER_REPORT"}]});
  await old;
  assert.match(h.el("workerList").innerHTML,/REOPENED_CUSTOMER_REPORT/);
  assert.doesNotMatch(h.el("workerList").innerHTML,/CLOSED_CUSTOMER_REPORT/);
});

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
    assert.match(compact(h.el("cPower").textContent), /10070/);
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
    assert.match(h.el("kMachines").title,/1\/1.*1\/2/);
    assert.match(h.el("kHashrate").innerHTML,/rate-value"[^>]*>—/);
    assert.match(h.el("kHashrate").innerHTML,/已知部分：293 TH\/s/);
    assert.match(h.el("gridAir").innerHTML,/site s-stalled/);
    assert.doesNotMatch(h.el("gridAir").innerHTML,/<旧场地>/);
    h.ctx.renderCustomers({rows:[{worker:"A",site_id:"a",...observed,power_kwh:null,uptime_pct:null}],by_worker:[]});
    assert.doesNotMatch(h.el("tblCustomers").querySelector().innerHTML,/null%|0 kWh/);
    h.respond({sites:[{name:"A",points:[[1,293],[2,null],[3,0]],legacy_points:[[1,null],[2,300],[3,null]]}]});
    await h.ctx.loadTrend();
    assert.equal(h.configs.trendChart.data.datasets[0].data[1].y,null);
    assert.equal(h.configs.trendChart.data.datasets[0].data[2].y,0);
    assert.equal(h.configs.trendChart.data.datasets.length,1);
  }else{
    h.respond({scanned:true,scan_ts:1800000000,scan_kind:"inventory",online:1,total:2,unknown:1,offline:0,
      total_hashrate_th:null,known_hashrate_ths:293,hashrate_complete:false,avg_hashrate_th:null,
      total_power_kw:null,known_power_kw:3,power_complete:false,power_known_machines:1,
      avg_efficiency:null,known_efficiency:10.24,efficiency_complete:false,efficiency_known_machines:1});
    await h.ctx.refreshSummary();
    assert.equal(h.el("cOnline").textContent,"1 / 2");
    assert.match(h.el("cTotalHr").innerHTML,/已知部分：293 TH\/s/);
    assert.equal(h.el("cPower").textContent,"3");
    assert.equal(h.el("cEff").textContent,"10.24");
    assert.equal(h.el("cPower").title,"已统计 1/2 台");
    assert.equal(h.el("cEff").title,"已统计 1/2 台");
    assert.equal(h.el("cOnline").title,"未知：1");
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
    assert.equal(h.configs.trendChart.data.datasets.length,1);
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

test("power and efficiency keep their own hover context without extra card lines",async()=>{
  const h=setup();
  const partial={scanned:true,scan_ts:1800000000,total:3,online:3,offline:0,
    total_power_kw:null,known_power_kw:3,power_complete:false,power_known_machines:2,
    avg_efficiency:null,known_efficiency:20,efficiency_complete:false,efficiency_known_machines:1};
  h.respond(partial);
  await h.ctx.refreshSummary();
  assert.equal(h.el("cPower").textContent,"3");
  assert.equal(h.el("cEff").textContent,"20");
  assert.equal(h.el("cPower").title,"已统计 2/3 台");
  assert.equal(h.el("cEff").title,"已统计 1/3 台");
  assert.equal(ids.has("cPowerNote"),false);
  assert.equal(ids.has("cEffNote"),false);
  assert.equal(ids.has("summaryQuality"),false);

  h.respond({miners:[],count:3,agg:{total_power:null,known_power:3000,
    power_complete:false,power_known_machines:2}});
  await h.ctx.refreshMiners();
  assert.match(h.el("segStat").textContent,/3 kW/);
  assert.doesNotMatch(h.el("segStat").textContent,/已统计/);
  assert.equal(h.el("segStat").title,"已统计 2/3 台");

  h.respond({...partial,total_power_kw:3,avg_efficiency:20,power_complete:true,efficiency_complete:true});
  await h.ctx.refreshSummary();
  for (const id of ["cPower","cEff"]) assert.equal(h.el(id).title,"");
  h.respond({...partial,known_power_kw:0,known_efficiency:0});
  await h.ctx.refreshSummary();
  assert.equal(h.el("cPower").textContent,"0");
  assert.equal(h.el("cEff").textContent,"0");
  h.respond({...partial,known_power_kw:null,known_efficiency:null});
  await h.ctx.refreshSummary();
  assert.equal(h.el("cPower").textContent,"—");
  assert.equal(h.el("cEff").textContent,"—");
  for (const id of ["cPower","cEff"]) assert.equal(h.el(id).title,"");

  h.respond(partial);
  await h.ctx.refreshSummary();
  h.respond({scanned:false});
  await h.ctx.refreshSummary();
  for (const id of ["cPower","cEff"]){
    assert.equal(h.el(id).textContent,"—");
    assert.equal(h.el(id).title,"");
  }
});

const completeTrend = (ts, rate = 161700) => ({ts, total:539, total_hr:rate,
  known_total_hr:rate, hashrate_known_machines:539, data_quality:"complete"});
const partialTrend = (ts, rate = 161400) => ({...completeTrend(ts), total_hr:null,
  known_total_hr:rate, hashrate_known_machines:538, data_quality:"incomplete"});

// Use the shipped Chart.js parser/controllers/segment resolver. Canvas methods
// are inert here; visual checks use a real canvas separately, not this adapter.
function realTrendChart(config) {
  const ActualChart = require("../web/vendor/chart.umd.min.js");
  // The UI harness runs in a VM. Chart.js expects plain option objects from
  // its own realm, as in the real browser; keep callbacks while normalizing them.
  const plain = value => Array.isArray(value) ? Array.from(value, plain) :
    value && typeof value === "object" ? Object.fromEntries(
      Object.entries(value).map(([key,item])=>[key,plain(item)])) : value;
  config=plain(config);
  const canvas = {width:800, height:300};
  const context = new Proxy({canvas, measureText:s=>({width:String(s).length*7}), getLineDash:()=>[]}, {
    get:(target,key)=>key in target ? target[key] : ()=>{},
    set:(target,key,value)=>(target[key]=value,true)
  });
  canvas.getContext=()=>context;
  return new ActualChart(canvas, {...config, options:{...config.options, responsive:false,
    plugins:{...config.options.plugins, legend:{display:false}, tooltip:{enabled:false}}}});
}

test("partial trend readings are visible and cannot masquerade as complete totals",async()=>{
  const h=setup();
  h.respond({trend:[completeTrend(1000),partialTrend(1300),completeTrend(1600),completeTrend(1900)]});
  await h.ctx.refreshTrend();
  const c=h.configs.trendChart, full=c.data.datasets[0], partial=c.data.datasets[1];
  assert.equal(full.data[1].y,null);
  assert.equal(partial.trendPartial,true);
  assert.equal(partial.data[1].y,161.4);
  assert.equal(partial.fill,false);
  assert.equal(c.options.spanGaps,false);
  assert.equal(c.options.plugins.legend.display,true);
  const tooltip=c.options.plugins.tooltip;
  const item={dataset:partial,raw:partial.data[1],parsed:{y:161.4}};
  assert.equal(tooltip.filter(item),true);
  assert.match(tooltip.callbacks.label(item).join("\n"),/部分实测: 161.4 PH\/s\n已统计 538\/539 台/);
  assert.match(tooltip.callbacks.label(item).join("\n"),/不是完整场地总量/);
  assert.equal(tooltip.filter({dataset:partial,raw:partial.data[0]}),false);
  assert.equal(tooltip.filter({dataset:full,raw:full.data[0]}),true);



  const chart=realTrendChart(c);
  try {
    const blue=chart.getDatasetMeta(0), orange=chart.getDatasetMeta(1);
    assert.equal(blue.data[1].skip,true);
    assert.equal(orange.data[1].skip,false);
    assert.equal(orange.data[1].options.radius,2);
    assert.equal(orange.data[0].options.radius,0);
    const painted=orange.dataset.segments.filter(s=>s.style.borderColor!=="transparent");
    assert.deepEqual(painted.map(s=>[s.start,s.end]),[[0,2]]);
    assert.deepEqual([...orange.dataset.options.borderDash],[5,4]);
    assert.equal(painted[0].style.borderColor,"#d97706");
    assert.ok(orange.dataset.segments.some(s=>s.start===2&&s.end===3&&s.style.borderColor==="transparent"));
  } finally {chart.destroy();}

  // A later complete response removes the partial legend and explanatory note.
  h.respond({trend:[completeTrend(2000),completeTrend(2300)]});
  await h.ctx.refreshTrend();
  assert.equal(h.configs.trendChart.data.datasets.length,1);
  assert.equal(h.configs.trendChart.options.plugins.legend.display,false);


});

test("real missing scans and long gaps break both complete and partial trend lines",async()=>{
  const h=setup();
  h.respond({trend:[completeTrend(1000),partialTrend(1300),completeTrend(2601),completeTrend(2901)]});
  await h.ctx.refreshTrend();
  const c=h.configs.trendChart;
  for(const dataset of c.data.datasets){
    assert.equal(dataset.data[2].x,2200);
    assert.equal(dataset.data[2].y,null);
  }

  const chart=realTrendChart(c);
  try {
    for(let i=0;i<2;i++){
      const meta=chart.getDatasetMeta(i);
      assert.equal(meta.data[2].skip,true);
      assert.ok(meta.dataset.segments.every(s=>s.end<2||s.start>2));
    }
  } finally {chart.destroy();}

  h.respond({trend:[completeTrend(3000),{ts:3300,total:539,total_hr:null,
    known_total_hr:null,hashrate_known_machines:0,data_quality:"incomplete"},completeTrend(3600)]});
  await h.ctx.refreshTrend();
  assert.equal(h.configs.trendChart.data.datasets.length,1);
  assert.equal(h.configs.trendChart.data.datasets[0].data[1].y,null);

});

test("confirmed zero, partial zero and isolated samples remain visible on the real chart",async()=>{
  const h=setup();
  h.respond({trend:[completeTrend(1000,0),completeTrend(1300,0)]});
  await h.ctx.refreshTrend();
  const c=h.configs.trendChart;
  assert.equal(c.options.scales.y.min,0);
  assert.ok(c.options.scales.y.max>0);
  const chart=realTrendChart(c);
  try {
    const meta=chart.getDatasetMeta(0);
    assert.equal(meta.dataset.segments.length,1);
    assert.ok(meta.data.every(p=>!p.skip&&p.y>=chart.chartArea.top&&p.y<=chart.chartArea.bottom));
  } finally {chart.destroy();}

  h.respond({trend:[partialTrend(2000,0)]});
  await h.ctx.refreshTrend();
  const partial=h.configs.trendChart.data.datasets[1];
  assert.equal(partial.data[0].y,0);
  const zeroChart=realTrendChart(h.configs.trendChart);
  try {
    const point=zeroChart.getDatasetMeta(1).data[0];
    assert.equal(point.skip,false);
    assert.equal(point.options.radius,2);
    assert.ok(point.y<=zeroChart.chartArea.bottom);
  } finally {zeroChart.destroy();}

  h.respond({trend:[completeTrend(3000)]});
  await h.ctx.refreshTrend();
  const single=realTrendChart(h.configs.trendChart);
  try {assert.equal(single.getDatasetMeta(0).data[0].options.radius,2);}
  finally {single.destroy();}
});

test("legacy, invalid and unverified partial readings are not promoted to complete trend data",async()=>{
  const h=setup();
  h.respond({trend:[{...partialTrend(1000),data_quality:"legacy",legacy_total_hr:161700},
    {...completeTrend(1300),total_hr:-1,known_total_hr:-1},
    {...partialTrend(1600),hashrate_known_machines:null},
    {...partialTrend(1900),hashrate_known_machines:0}]});
  await h.ctx.refreshTrend();
  const c=h.configs.trendChart;
  assert.equal(c.data.datasets.length,1);
  assert.ok(c.data.datasets[0].data.every(p=>p.y===null));
  assert.equal(ids.has("trendQuality"),false);


});



test("normal report cells stay concise and exclude discarded legacy statistics", () => {
  const row={...observed,hashrate_ths:293,avg_hashrate_ths:292,hashrate_hours:24,
    observed_hours:23,coverage_pct:95.8,legacy_power_kwh:9999,current_machines:1,
    hashrate_age_s:10,report_age_s:30,period_end_ts:1800000000};
  assert.equal(visibleText(q.billing(row,"power",t,format)),"3 kWh");
  assert.equal(visibleText(q.billing(row,"uptime",t,n=>n+"%")),"100%");
  assert.equal(visibleText(rates.cell(row,"current",t,n=>n+" TH/s",n=>n+"s")),"293 TH/s");
  assert.equal(visibleText(rates.cell(row,"average",t,n=>n+" TH/s",n=>n+"s")),"292 TH/s");
  assert.equal(visibleText(q.daily({method:"legacy_sample_mean",power_kwh:9999},"power_kwh","power_kw",t,format)),"—");
  assert.doesNotMatch(q.billing(row,"power",t,format),/9999|旧口径|完整性未验证|<details/);
});


test("active long scans keep the last complete summary; only completed results change the numbers",async()=>{
  const h=setup();
  h.ctx.Date=class extends Date { static now(){ return 1800000000*1000; } };
  h.ctx._wsStop=false;
  vm.runInContext("lastOkMs=Date.now();",h.ctx);
  const scan={scanned:true,scan_ts:1799998000,scan_kind:"full",stale_after:900,
    online:538,total:538,offline:0,unknown:0,total_hashrate_th:161700,known_hashrate_ths:161700,
    hashrate_complete:true,avg_hashrate_th:300.56,total_power_kw:2870,power_complete:true,
    avg_efficiency:17.75,efficiency_complete:true,active_alerts:0,
    by_firmware:{stock:538},containers:4};
  h.respond({...scan,progress:{running:true,kind:"manual",done:2938,total:8200,progress_ts:1799999995}});
  await h.ctx.refreshSummary();
  assert.equal(h.el("cOnline").textContent,"538 / 538");
  assert.match(visibleText(h.el("cTotalHr").innerHTML),/^161\.7 PH\/s$/);
  assert.equal(h.el("cPower").textContent,"2,870");
  assert.equal(h.el("progFill").style.width,"35%");
  assert.equal(h.el("progressBar").classList.contains("hidden"),false);
  h.ctx.checkStale();
  assert.equal(h.el("staleBanner").className,"stale-banner hidden");
  // A fresh page needs no browser-side cache to render that same snapshot midscan.
  const reopened=setup();
  reopened.respond({...scan,progress:{running:true,done:6000,total:8200,kind:"manual",progress_ts:1799999998}});
  await reopened.ctx.refreshSummary();
  assert.equal(reopened.el("cOnline").textContent,"538 / 538");
  assert.equal(reopened.el("cPower").textContent,"2,870");
  assert.equal(reopened.el("progFill").style.width,"73%");
  // A completed real zero remains zero and an actual offline machine is visible.
  h.respond({...scan,scan_ts:1800000000,online:537,offline:1,total_hashrate_th:0,
    known_hashrate_ths:0,avg_hashrate_th:0,total_power_kw:0,avg_efficiency:null,
    progress:{running:false,last_finished:1800000000}});
  await h.ctx.refreshSummary();
  assert.equal(h.el("cOnline").textContent,"537 / 538");
  assert.equal(h.el("cOffline").textContent,1);
  assert.equal(visibleText(h.el("cTotalHr").innerHTML),"0 TH/s");
  assert.equal(h.el("cPower").textContent,"0");
  assert.equal(h.el("progressBar").classList.contains("hidden"),true);
});

test("recent scan progress never suppresses real lost contact or frozen progress",()=>{
  const h=setup();
  h.ctx.Date=class extends Date { static now(){ return 1800000000*1000; } };
  h.ctx._wsStop=false;
  vm.runInContext("lastOkMs=Date.now();lastScanTs=1799998000;staleAfter=900;",h.ctx);
  h.ctx.updateProgress({running:true,done:100,total:8200,progress_ts:1799998001});
  h.ctx.checkStale();
  assert.equal(h.el("staleBanner").className,"stale-banner stale");
  h.ctx.updateProgress({running:true,done:200,total:8200,progress_ts:1799999999});
  vm.runInContext("lastOkMs=Date.now()-91000;",h.ctx);
  h.ctx.checkStale();
  assert.equal(h.el("staleBanner").className,"stale-banner lost");
});

test("an older summary response cannot replace a newly completed scan",async()=>{
  const h=setup(),pending=deferredRequests(h,"jget");
  const old=h.ctx.refreshSummary(), current=h.ctx.refreshSummary();
  pending[1].resolve({scanned:true,scan_ts:1800000000,online:537,total:538,offline:1,
    total_hashrate_th:160000,progress:{running:false,last_finished:1800000000}});
  await current;
  pending[0].resolve({scanned:true,scan_ts:1799999000,online:538,total:538,offline:0,
    total_hashrate_th:161700,progress:{running:true,done:100,total:8200,progress_ts:1799999990}});
  await old;
  assert.equal(h.el("cOnline").textContent,"537 / 538");
  assert.equal(visibleText(h.el("cTotalHr").innerHTML),"160 PH/s");
  assert.equal(h.el("progressBar").classList.contains("hidden"),true);
});
