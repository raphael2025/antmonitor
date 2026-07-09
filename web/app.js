const $ = (id) => document.getElementById(id);
let trendChart, modalChart;
let sortKey = "hr_rt", sortOrder = "desc";
const HOT = 90;

// 转义设备/矿池返回的不可信字符串，防止 innerHTML 注入(XSS)
function esc(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let lastOkMs = Date.now();   // 最近一次成功从服务器拿到数据(失联自检用)
let lastScanTs = 0;          // 服务器最近一次完成扫描的时间戳
let _staleOn = false;
let _staleTimer = null;
async function jget(u) {
  const r = await fetch(u);
  if (r.status === 401) { showLogin(); throw new Error("401"); }
  const j = await r.json();
  lastOkMs = Date.now();     // 拿到数据 → 刷新"最近通信"时间
  return j;
}

// 失联/数据过期自检：网页自己判断，不依赖后台。连不上服务器→红条；服务器在但扫描停滞→橙条
function checkStale() {
  const b = document.getElementById("staleBanner");
  if (!b) return;
  if (_wsStop) { b.className = "stale-banner hidden"; _staleOn = false; return; }  // 登录页不显示
  const now = Date.now();
  const noContact = Math.round((now - lastOkMs) / 1000);
  const scanAge = lastScanTs ? Math.round(now / 1000 - lastScanTs) : 0;
  let cls = "", msg = "";
  if (noContact > 90) {
    cls = "lost"; msg = `⚠ 监控失联：已 ${noContact} 秒连不上服务器，屏幕上的数据可能已停止更新！请立即检查监控程序/网络`;
  } else if (lastScanTs && scanAge > 900) {
    cls = "stale"; msg = `⚠ 数据已约 ${Math.round(scanAge / 60)} 分钟未更新，扫描可能已停滞，请核实监控是否正常`;
  }
  if (cls) {
    b.className = "stale-banner " + cls; b.textContent = msg;
    if (!_staleOn) { _staleOn = true; try { beep(880, 0.4); setTimeout(() => beep(660, 0.4), 200); } catch (e) {} }
  } else {
    b.className = "stale-banner hidden"; _staleOn = false;
  }
}

// 算力千进制：1000 TH = 1 PH，1000 PH = 1 EH
function fmtHash(th, suffix) {
  suffix = suffix || "";
  if (th == null) return "-";
  const v = Number(th), a = Math.abs(v);
  let div = 1, unit = "TH";
  if (a >= 1e6) { div = 1e6; unit = "EH"; }
  else if (a >= 1e3) { div = 1e3; unit = "PH"; }
  return (v / div).toLocaleString(undefined, { maximumFractionDigits: 2 }) + " " + unit + suffix;
}
const fmtHashH = (thh) => fmtHash(thh, "·h");   // 交付算力 TH·h，同千进制
function hashUnit(maxTh) {
  if (maxTh >= 1e6) return { div: 1e6, unit: "EH" };
  if (maxTh >= 1e3) return { div: 1e3, unit: "PH" };
  return { div: 1, unit: "TH" };
}

function fmtTime(ts) {
  if (!ts) return "尚未扫描";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}
function ago(ts) {
  if (!ts) return "";
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return s + "秒前";
  if (s < 3600) return Math.floor(s / 60) + "分钟前";
  return Math.floor(s / 3600) + "小时前";
}
function fmtPower(w) {   // 功率 W → kW/MW
  if (w == null) return "-";
  const v = Number(w);
  return v >= 1e6 ? (v / 1e6).toFixed(2) + " MW" : (v / 1000).toFixed(1) + " kW";
}
function boxPower(c) {   // 集装箱总功耗 = 两路配电之和(W)
  if (c.power1 == null && c.power2 == null) return null;
  return (c.power1 || 0) + (c.power2 || 0);
}
function fmtDT(ts) {   // 告警时间戳: MM-DD HH:MM
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function fmtUptime(sec) {
  if (sec == null) return "-";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}天${h}时`;
  if (h) return `${h}时${m}分`;
  return `${m}分`;
}

async function refreshSummary() {
  const s = await jget("/api/summary");
  updateProgress(s.progress);
  if (s.scan_ts) lastScanTs = s.scan_ts;   // 记录服务器最近完成扫描时间(失联自检用)
  if (!s.scanned) { $("lastScan").textContent = "尚未扫描，等待首轮…"; return; }
  $("cOnline").textContent = `${s.online} / ${s.total}`;
  $("cTotalHr").textContent = fmtHash(s.total_hashrate_th);
  $("cAvgHr").textContent = fmtHash(s.avg_hashrate_th);
  $("cOffline").textContent = s.offline;
  $("cAlerts").textContent = s.active_alerts;
  const fw = s.by_firmware || {};
  $("cFw").textContent = `${fw.stock || 0} / ${fw.uniplus || 0}`;
  $("cPower").textContent = (s.total_power_kw || 0).toLocaleString();
  $("cEff").textContent = s.avg_efficiency || 0;
  $("cContainers").textContent = `${s.containers || 0}`
    + ((s.containers_faulty || s.containers_offline) ? ` (${(s.containers_faulty || 0) + (s.containers_offline || 0)})` : "");
  $("lastScan").textContent = `上次扫描 ${ago(s.scan_ts)} (${s.scan_kind})`;
}

async function ackAlert(id) {
  await fetch("/api/alerts/ack", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
  refreshAlerts();
}
// 告警里直接下架坏机器：从名册移除 + 清掉它的告警(不再探测/告警)
async function removeFromAlert(ip) {
  if (!confirm(`确认下架移除 ${ip}？\n将从名册删除、不再探测/告警。若机器仍通电，下次扫描可能被重新收录。`)) return;
  try {
    const r = await fetch("/api/machine-state", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ips: [ip], action: "remove" }),
    });
    const d = await r.json();
    if (!d.ok) { toast("下架失败：" + (d.error || r.status)); return; }
    toast(`已下架 ${ip}`, "ok");
    refreshAlerts(); refreshMiners();
  } catch (e) { toast("下架出错：" + e); }
}
function alertClick(x) {
  if (x.type && x.type.startsWith("cooler")) return `openContainer('${x.ip}')`;
  if (x.ip && x.ip.split(".").length === 4) return `openMiner('${x.ip}')`;
  return "";
}
async function refreshAlerts() {
  const d = await jget("/api/alerts?active=true");
  const a = d.alerts || [];
  const total = (d.total != null) ? d.total : a.length;   // 真实总数(不被列表200/500条上限卡住)
  $("alertCount").textContent = total;
  $("alertList").innerHTML = (a.length ? a.map(x => {
    const click = alertClick(x);
    const isCooler = x.type && x.type.startsWith("cooler");
    const isMiner = x.ip && x.ip.split(".").length === 4 && !x.ip.endsWith(".x");  // 单台矿机(非网段/非箱)
    let tail = "";
    if (!isCooler) {   // 集装箱告警不给按钮(修好自动消失)
      tail = x.ack_by ? `<span class="acked">✓ ${esc(x.ack_by)} 已确认</span>`
                      : `<button class="ackbtn" onclick="ackAlert(${x.id})">确认</button>`;
      if (isMiner) tail += `<button class="rmbtn" onclick="removeFromAlert('${x.ip}')" title="从名册下架移除该机器">下架</button>`;
    }
    return `<div class="a ${x.severity}${x.ack_by ? " is-ack" : ""}"><span class="atime">${fmtDT(x.ts)} · ${ago(x.ts)}</span>`
      + `<span class="ip"${click ? ` onclick="${click}"` : ""}>${esc(x.ip)}</span>`
      + `<span class="adetail">${esc(x.detail)}</span>${tail}</div>`;
  }).join("") : '<div class="muted">无</div>')
    + (total > a.length ? `<div class="muted" style="text-align:center;padding:6px">…仅显示前 ${a.length} 条，共 <b>${total}</b> 条活跃告警</div>` : "");
  document.title = total ? `(${total}) 矿机监控面板` : "矿机监控面板";
  handleVoice(a, d.counts || {}, total);
}

/* ---------------- 语音告警 ---------------- */
let voiceOn = localStorage.getItem("voiceOn") === "1";
let maxSeenId = 0;             // 已见告警的最大id(自增,永不复用)：O(1)判新，替代无限增长的Set
let prevActiveIds = new Set();
let alertsInit = false;
let audioCtx;

function beep(freq = 880, dur = 0.25) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();   // 浏览器锁声后必须先唤醒，否则没声
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination);
    o.type = "sine"; o.frequency.value = freq;
    const t = audioCtx.currentTime;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.35, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.start(t); o.stop(t + dur);
  } catch (e) {}
}

function speak(text) {
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "zh-CN"; u.rate = 1; u.volume = 1;
    speechSynthesis.speak(u);
  } catch (e) {}
}

function ipSay(ip) { return ip.replace(/\./g, " 点 "); }

function notify(title, body) {
  try {
    if ("Notification" in window && Notification.permission === "granted")
      new Notification(title, { body });
  } catch (e) {}
}

function announceCounts(counts) {
  // 只报当前各类告警「台数/处数」，不念 IP。counts 是后端精确计数(不被列表上限截断)
  const n = (t) => counts[t] || 0;
  const offline = n("offline"), zero = n("zero"), reject = n("reject");
  const seg = n("segment_down"), stalled = n("stalled");
  const cooler = Object.keys(counts).filter(t => t.startsWith("cooler")).reduce((s, t) => s + counts[t], 0);
  const parts = [];
  if (stalled) parts.push("监控停滞");
  if (seg) parts.push(`${seg} 个网段掉线`);
  if (offline) parts.push(`掉线 ${offline} 台`);
  if (zero) parts.push(`零算力 ${zero} 台`);
  if (reject) parts.push(`拒绝率偏高 ${reject} 台`);
  if (cooler) parts.push(`集装箱故障 ${cooler} 处`);
  if (!parts.length) return;
  beep(880, 0.3); beep(660, 0.3);
  speak("告警，" + parts.join("，"));
  notify("⛏ 矿机告警", parts.join(" / "));
}

function handleVoice(alerts, counts, total) {
  const curIds = new Set(alerts.map(x => x.id));
  const maxId = alerts.reduce((m, x) => Math.max(m, x.id), 0);
  if (!alertsInit) {            // 首次加载只记录，不播报历史告警
    maxSeenId = maxId; alertsInit = true; prevActiveIds = curIds; return;
  }
  if (voiceOn) {
    const hasNew = alerts.some(x => x.id > maxSeenId);
    if (maxId > maxSeenId) maxSeenId = maxId;
    if (hasNew) announceCounts(counts || {});   // 有新告警 → 播报各类真实总数(不念IP)
    // 列表被截断(>500)时不做"已恢复"播报：老告警被挤出窗口会被误判成已恢复
    if (!(total > alerts.length)) {
      const recovered = [...prevActiveIds].filter(id => !curIds.has(id));
      if (recovered.length) { beep(523, 0.15); speak(`${recovered.length} 项告警已恢复`); }
    }
  }
  prevActiveIds = curIds;
}

function setVoice(on, gesture) {
  voiceOn = on;
  localStorage.setItem("voiceOn", on ? "1" : "0");
  const b = $("btnVoice");
  b.textContent = (on ? "🔊" : "🔇") + " 语音告警: " + (on ? "开" : "关");
  b.classList.toggle("primary", on);
  if (on && gesture) {                       // 用户手势：解锁音频 + 申请通知权限
    beep(660, 0.15); speak("语音告警已开启");
    if ("Notification" in window && Notification.permission === "default")
      Notification.requestPermission();
  }
}

// 浏览器规则：刷新后声音被锁，必须有用户点击才能出声。这里在首次点击页面任意处时解锁，
// 省得每次刷新都要专门去点语音按钮(语音开着才解锁)。
let _audioUnlocked = false;
document.addEventListener("click", () => {
  if (_audioUnlocked || !voiceOn) return;
  _audioUnlocked = true;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch (e) {}
});

async function refreshTrend() {
  const d = await jget("/api/trend?points=288");
  const t = d.trend || [];
  const labels = t.map(x => new Date(x.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }));
  const raw = t.map(x => x.total_hr);
  const { div, unit } = hashUnit(raw.length ? Math.max(...raw) : 0);
  const data = raw.map(v => v / div);
  const tt = $("trendTitle"); if (tt) tt.textContent = `总算力趋势 (${unit})`;
  if (!trendChart) {
    trendChart = new Chart($("trendChart"), {
      type: "line",
      data: { labels, datasets: [{ data, borderColor: "#1f6feb", backgroundColor: "rgba(31,111,235,.1)", fill: true, tension: .3, pointRadius: 0, borderWidth: 2 }] },
      options: {
        interaction: { mode: "index", intersect: false },   // 鼠标放在图上任意处即显示数值(不必正好压到点)
        plugins: { legend: { display: false }, tooltip: { callbacks: {
          title: (items) => items.length ? items[0].label : "",
          label: (c) => c.parsed.y.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " " + unit } } },
        scales: { x: { ticks: { color: "#6e7681", maxTicksLimit: 8 } }, y: { ticks: { color: "#6e7681" } } }
      }
    });
  } else {
    trendChart.data.labels = labels; trendChart.data.datasets[0].data = data;
    trendChart.options.plugins.tooltip.callbacks.label = (c) => c.parsed.y.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " " + unit;
    trendChart.update("none");
  }
}

function rowClass(r) {
  if (r.status === "offline") return "off";
  return "";
}
function hrCell(r) {
  if (r.hr_rt == null) return `<td class="low">0 TH</td>`;   // 读不到=零算力
  const zero = r.hr_rt === 0;
  return `<td class="${zero ? "low" : ""}">${fmtHash(r.hr_rt)}</td>`;
}
function tempCell(r) {
  if (r.temp == null) return `<td>-</td>`;
  return `<td class="${r.temp >= HOT ? "hot" : ""}">${r.temp}℃</td>`;
}
const REJECT_HI = 5;
function rejCell(r) {
  if (r.accepted == null && r.rejected == null) return `<td>-</td>`;
  const tot = (r.accepted || 0) + (r.rejected || 0);
  if (tot === 0) return `<td>0%</td>`;
  const p = Math.round((r.rejected || 0) / tot * 1000) / 10;
  return `<td class="${p >= REJECT_HI ? "hot" : ""}">${p}%</td>`;
}

let lastMiners = [];           // 当前筛选结果（用于全选）
const selected = new Set();     // 选中的 IP

async function refreshMiners() {
  const q = $("search").value.trim();
  const st = $("fStatus").value, fw = $("fFw").value, seg = $("fSeg").value;
  const u = `/api/miners?sort=${sortKey}&order=${sortOrder}&status=${st}&fw=${fw}&seg=${seg}&q=${encodeURIComponent(q)}`;
  const d = await jget(u);
  lastMiners = d.miners || [];   // 后端异常响应(无miners字段)时兜底为空，别让整个矿机视图崩掉
  // 防误操作：把选择集收敛为当前结果集内的IP，避免切网段/筛选后残留的不可见机器被命令误打
  const visible = new Set(lastMiners.map(m => m.ip));
  for (const ip of [...selected]) if (!visible.has(ip)) selected.delete(ip);
  $("minerCount").textContent = d.count != null ? d.count : lastMiners.length;
  const a = d.agg || {};
  $("segStat").textContent = `${seg ? seg + ".x ｜ " : ""}在线 ${a.online || 0} ｜ 算力 ${fmtHash(a.total_hr)} ｜ 功耗 ${fmtPower(a.total_power)}`;
  $("minerBody").innerHTML = lastMiners.map(r => {
    const ck = selected.has(r.ip) ? "checked" : "";
    return `<tr class="${rowClass(r)} ${selected.has(r.ip) ? "sel" : ""}">`
      + `<td class="cbcol"><input type="checkbox" data-ip="${r.ip}" ${ck}></td>`
      + `<td class="ip-link" onclick="openMiner('${r.ip}')">${r.ip}</td>`
      + `<td>${r.mstate === "repair" ? '<span class="pill repair">维修中</span>' : `<span class="pill ${r.status}">${r.status === "online" ? "在线" : "离线"}</span>`}</td>`
      + `<td>${r.firmware ? `<span class="pill ${r.firmware}">${r.firmware}</span>` : "-"}</td>`
      + `<td>${esc(r.model) || "-"}</td>`
      + hrCell(r) + `<td>${r.hr_avg != null ? fmtHash(r.hr_avg) : "-"}</td>`
      + `<td>${r.power != null ? r.power + " W" : "-"}</td>`
      + `<td>${r.eff != null ? r.eff + "" : "-"}</td>`
      + rejCell(r)
      + tempCell(r)
      + `<td>${fmtUptime(r.uptime)}</td>`
      + `<td>${esc(r.worker) || "-"}</td>`
      + `<td>${esc(r.sn) || "-"}</td><td class="muted">${esc(r.note)}</td></tr>`;
  }).join("");
  updateSelCount();
}

function updateSelCount() {
  $("selCount").textContent = `已选 ${selected.size} 台`;
  const all = $("cbAll");   // 全选框跟随当前结果集同步，避免切筛选后仍显示已勾
  if (all) all.checked = lastMiners.length > 0 && lastMiners.every(m => selected.has(m.ip));
}

/* ---------------- 货架视图 ---------------- */
let rackMode = false;
async function refreshRacks() {
  if (!rackMode) return;
  const d = await jget("/api/racks");
  const legend = `<div class="legend" style="margin-bottom:12px">`
    + `<span><i class="dot slot ok"></i>在线</span><span><i class="dot slot zero"></i>零算力</span>`
    + `<span><i class="dot slot offline"></i>离线</span><span><i class="dot slot empty"></i>空机位</span>`
    + `<span class="muted">（格子里是机位号，空号一眼可见）</span></div>`;
  $("rackList").innerHTML = legend + (d.racks || []).map(rk => {
    const slots = rk.slots.map(s => {
      if (s.st === "empty")   // 空机位：灰、不可点
        return `<div class="slot empty" title="${s.ip} · 空机位">${s.h}</div>`;
      const tip = `${s.ip}${s.hr != null ? " · " + fmtHash(s.hr) : ""}${s.temp != null ? " · " + s.temp + "℃" : ""}`;
      return `<div class="slot ${s.st}" title="${tip}" onclick="openMiner('${s.ip}')">${s.h}</div>`;
    }).join("");
    const bad = rk.abnormal ? `零算力 <b class="bad">${rk.abnormal}</b> · ` : "";
    return `<div class="rack"><div class="rack-head"><span class="name">${rk.name}.x</span>`
      + `<span class="stat">在线 ${rk.online} · ${fmtHash(rk.hashrate)} · ${bad}离线 ${rk.offline} · 空 ${rk.empty}</span></div>`
      + `<div class="slots">${slots}</div></div>`;
  }).join("");
}

async function refreshWorkers() {
  if (view !== "worker") return;
  const period = parseInt($("workerPeriod").value) || 0;
  $("btnExportCsv").classList.toggle("hidden", period === 0);   // 仅周期报表可导出CSV
  if (period === 0) {
    const d = await jget("/api/workers");
    $("workerList").innerHTML = (d.workers || []).map(w => {
      const models = Object.entries(w.models).sort((a, b) => b[1] - a[1])
        .map(([m, c]) => `<div class="wmodel">└─ ${esc(m)}：<b>${c}</b> 台</div>`).join("");
      return `<div class="worker"><div class="worker-head">`
        + `<span class="wname">${esc(w.worker)}</span>`
        + `<span class="wstat">${w.total} 台 · ${fmtHash(w.hashrate)}</span></div>`
        + `<div class="wmodels">${models}</div></div>`;
    }).join("") || '<div class="muted">无数据</div>';
  } else {
    const d = await jget(`/api/reports/customers?hours=${period}`);
    // 周期超过快照保留期被截断时给出提示，避免把"近7天"标签下的实为~3天数据误读为7天
    const note = d.truncated
      ? `<div class="muted" style="margin-bottom:8px">⚠️ 数据实际仅覆盖近 ${(d.covered_hours / 24).toFixed(1)} 天（受保留期限制；更长周期请调大 db.retention_days）</div>`
      : "";
    $("workerList").innerHTML = note + ((d.customers || []).map(c => {
      const up = c.uptime_pct;
      const upCls = up >= 99 ? "res-ok" : up >= 95 ? "" : "res-fail";
      return `<div class="worker"><div class="worker-head">`
        + `<span class="wname">${esc(c.worker)}</span>`
        + `<span class="wstat">${c.machines} 台</span></div>`
        + `<div class="wmodels">`
        + `可用率 <b class="${upCls}">${up}%</b><br>`
        + `交付算力 <b>${fmtHashH(c.delivered_th_h)}</b><br>`
        + `耗电 <b>${c.power_kwh.toLocaleString()}</b> kWh`
        + `</div></div>`;
    }).join("") || '<div class="muted">该周期无数据</div>');
  }
}

let view = "list";   // list | rack | worker
function setView(v) {
  view = v;
  $("listView").classList.toggle("hidden", v !== "list");
  $("rackView").classList.toggle("hidden", v !== "rack");
  $("workerView").classList.toggle("hidden", v !== "worker");
  $("tabList").classList.toggle("active", v === "list");
  $("tabRack").classList.toggle("active", v === "rack");
  $("tabWorker").classList.toggle("active", v === "worker");
  rackMode = v === "rack";
  if (v === "rack") refreshRacks();
  else if (v === "worker") refreshWorkers();
  else refreshMiners();
}

function updateProgress(p) {
  if (!p) return;
  const bar = $("progressBar"), btnF = $("btnFull"), btnQ = $("btnQuick");
  if (p.running) {
    bar.classList.remove("hidden");
    const pct = p.total ? Math.floor(p.done / p.total * 100) : 0;
    $("progFill").style.width = pct + "%";
    $("progText").textContent = `${p.kind} 扫描中 ${p.done}/${p.total} (${pct}%)`;
    btnF.disabled = btnQ.disabled = true;
  } else {
    bar.classList.add("hidden");
    btnF.disabled = btnQ.disabled = false;
  }
}

async function openMiner(ip) {
  const d = await jget(`/api/miner/${ip}`);
  const c = d.current || {};
  $("mTitle").textContent = `${ip}  ${c.model || ""}`;
  const h = d.history || [];
  // 用真实时间戳做 x：关机/掉线的空档按时间比例显示；相邻点间隔>15分钟插断点让线断开(一眼看出停机)
  const hr = [], temp = [];
  let prevTs = null;
  for (const x of h) {
    const ts = x.ts * 1000;
    if (prevTs != null && ts - prevTs > 15 * 60 * 1000) {
      hr.push({ x: prevTs + 1, y: null }); temp.push({ x: prevTs + 1, y: null });
    }
    hr.push({ x: ts, y: x.hr_rt }); temp.push({ x: ts, y: x.temp });
    prevTs = ts;
  }
  if (modalChart) modalChart.destroy();
  modalChart = new Chart($("mChart"), {
    type: "line",
    data: { datasets: [
      { label: "算力 TH", data: hr, borderColor: "#1f6feb", yAxisID: "y", pointRadius: 0, tension: .3, spanGaps: false },
      { label: "芯片温 ℃", data: temp, borderColor: "#f85149", yAxisID: "y1", pointRadius: 0, tension: .3, spanGaps: false } ] },
    options: {
      interaction: { mode: "index", intersect: false },   // 鼠标放上去显示数值
      scales: {
        y: { position: "left", ticks: { color: "#6e7681" } },
        y1: { position: "right", ticks: { color: "#6e7681" }, grid: { drawOnChartArea: false } },
        x: { type: "linear",                              // 按真实时间间隔铺开
             ticks: { color: "#6e7681", maxTicksLimit: 7,
                      callback: (v) => new Date(v).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" }) } } },
      plugins: { legend: { labels: { color: "#adbac7" } },
                 tooltip: { callbacks: { title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleString("zh-CN", { hour12: false }) : "" } } } }
  });
  $("mInfo").innerHTML =
    `固件：${esc(c.firmware) || "-"} ｜ 状态：${esc(c.status) || "-"} ｜ SN：${esc(c.sn) || "-"}<br>`
    + `实时算力：${fmtHash(c.hr_rt)} ｜ 平均：${fmtHash(c.hr_avg)} ｜ 功耗：${c.power ?? "-"} W<br>`
    + `能效：${c.eff ?? "-"} J/TH ｜ 芯片温：${c.temp ?? "-"}℃ ｜ 运行时长：${fmtUptime(c.uptime)}<br>`
    + `矿工名：${esc(c.worker) || "-"} ｜ 接受/拒绝/陈旧：${c.accepted ?? "-"}/${c.rejected ?? "-"}/${c.stale ?? "-"}`
    + (c.note ? `<br>备注：${esc(c.note)}` : "");
  $("modal").classList.remove("hidden");
}

async function triggerScan(kind) {
  await fetch(`/api/scan?kind=${kind}`, { method: "POST" });
  setTimeout(pollProgress, 500);
}
async function pollProgress() {
  const p = await jget("/api/progress");
  updateProgress(p);
  if (p.running) setTimeout(pollProgress, 1000);
  else refreshAll();
}

/* ---------------- 集装箱 (AntBox) ---------------- */
async function refreshContainers() {
  const d = await jget("/api/containers");
  const cs = d.containers || [];
  const ign = new Set(d.faults_ignore || []);
  const spMin = d.supply_pressure_min || 0, rpMin = d.return_pressure_min || 0;
  $("containerPanel").style.display = cs.length ? "" : "none";
  $("containerCount").textContent = `${cs.length}`
    + (d.faulty ? ` · 故障 ${d.faulty}` : "") + (d.offline ? ` · 离线 ${d.offline}` : "");
  $("containerList").innerHTML = cs.map(c => {
    if (!c.online) {
      return `<div class="cbox offline" onclick="openContainer('${c.ip}')">`
        + `<div class="cbox-head"><span class="cbox-ip">${c.ip}</span>`
        + `<span class="fbadge crit">控制器离线</span></div>`
        + `<div class="cbox-meta">最后进水 ${c.supply_temp ?? "-"}℃ / 出水 ${c.return_temp ?? "-"}℃</div></div>`;
    }
    const dt = (c.supply_temp != null && c.return_temp != null) ? (c.return_temp - c.supply_temp).toFixed(1) : "-";
    const pumps = Object.entries(c.pumps || {}).map(([k, v]) => `<span class="pump ${v ? "on" : ""}">${k}</span>`).join("");
    // 忽略列表内的故障置灰(info)，其余按严重级；按阈值标红低压力
    const faults = (c.faults || []).map(f => `<span class="fbadge ${ign.has(f.flag) ? "info" : f.sev}">${esc(f.label)}</span>`).join("");
    const spLow = spMin && c.supply_pressure != null && c.supply_pressure < spMin;
    const rpLow = rpMin && c.return_pressure != null && c.return_pressure < rpMin;
    const realFault = (c.faults || []).some(f => !ign.has(f.flag)) || spLow || rpLow;
    return `<div class="cbox ${realFault ? "fault" : ""}" onclick="openContainer('${c.ip}')">`
      + `<div class="cbox-head"><span class="cbox-ip">${c.ip}</span>`
      + `<span class="cbox-meta">矿机 ${c.miner_num ?? "-"} 台 ｜ ⚡ <b>${fmtPower(boxPower(c))}</b></span></div>`
      + `<div class="cbox-temps"><div>进水 <span class="v in">${c.supply_temp ?? "-"}℃</span></div>`
      + `<div>出水 <span class="v out">${c.return_temp ?? "-"}℃</span></div><div>ΔT <span class="v">${dt}℃</span></div></div>`
      + `<div class="cbox-meta">箱内 ${c.internal_temp ?? "-"}℃ / ${c.internal_humidity ?? "-"}% ｜ 流量 ${c.flow ?? "-"} ｜ 压力 `
      + `<span class="${spLow ? "res-fail" : ""}">${c.supply_pressure ?? "-"}</span>/<span class="${rpLow ? "res-fail" : ""}">${c.return_pressure ?? "-"}</span> ｜ 设定 ${c.set_temp ?? "-"}℃</div>`
      + `<div class="cbox-pumps">${pumps}</div>`
      + (faults ? `<div class="cbox-faults">${faults}</div>` : "")
      + `</div>`;
  }).join("");
}

async function openContainer(ip) {
  const d = await jget(`/api/container/${ip}`);
  const c = d.current || {};
  $("mTitle").textContent = `🧊 集装箱 ${ip}`;
  const h = d.history || [];
  const labels = h.map(x => new Date(x.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }));
  if (modalChart) modalChart.destroy();
  modalChart = new Chart($("mChart"), {
    type: "line",
    data: {
      labels, datasets: [
        { label: "进水℃", data: h.map(x => x.supply_temp), borderColor: "#56d4dd", pointRadius: 0, tension: .3 },
        { label: "出水℃", data: h.map(x => x.return_temp), borderColor: "#f0883e", pointRadius: 0, tension: .3 },
        { label: "箱内℃", data: h.map(x => x.internal_temp), borderColor: "#3fb950", pointRadius: 0, tension: .3 }]
    },
    options: { interaction: { mode: "index", intersect: false },
      scales: { x: { ticks: { color: "#6e7681", maxTicksLimit: 6 } }, y: { ticks: { color: "#6e7681" } } }, plugins: { legend: { labels: { color: "#adbac7" } } } }
  });
  const faults = (c.faults || []).map(f => `<span class="fbadge ${f.sev}">${esc(f.label)}</span>`).join("") || "无";
  const pumps = Object.entries(c.pumps || {}).map(([k, v]) => `${k}:${v ? "开" : "关"}`).join(" ｜ ");
  $("mInfo").innerHTML =
    `进水 ${c.supply_temp ?? "-"}℃ ｜ 出水 ${c.return_temp ?? "-"}℃ ｜ 设定 ${c.set_temp ?? "-"}℃<br>`
    + `供/回压 ${c.supply_pressure ?? "-"}/${c.return_pressure ?? "-"} ｜ 流量 ${c.flow ?? "-"} ｜ 冷却塔进水 ${c.tower_inlet_temp ?? "-"}℃<br>`
    + `箱内 ${c.internal_temp ?? "-"}℃ / ${c.internal_humidity ?? "-"}% ｜ 矿机 ${c.miner_num ?? "-"} 台 ｜ 芯片最高 ${c.chip_max_temp ?? "-"}℃<br>`
    + `总功耗 ${fmtPower(boxPower(c))}（配电1 ${fmtPower(c.power1)} + 配电2 ${fmtPower(c.power2)}）<br>`
    + `泵/风扇：${pumps}<br>故障：${faults}`;
  $("modal").classList.remove("hidden");
}

function refreshAll() {
  refreshSummary(); refreshAlerts(); refreshTrend(); refreshContainers();
  if (view === "rack") refreshRacks();
  else if (view === "worker") refreshWorkers();
  else refreshMiners();
}

// events
/* ---------------- 选择 + 远程命令 ---------------- */
$("minerBody").addEventListener("change", (e) => {
  const cb = e.target;
  if (cb.dataset && cb.dataset.ip) {
    if (cb.checked) selected.add(cb.dataset.ip); else selected.delete(cb.dataset.ip);
    cb.closest("tr").classList.toggle("sel", cb.checked);
    updateSelCount();
  }
});
$("cbAll").onclick = (e) => {
  document.querySelectorAll("#minerBody input[data-ip]").forEach(cb => {
    cb.checked = e.target.checked;
    if (e.target.checked) selected.add(cb.dataset.ip); else selected.delete(cb.dataset.ip);
    cb.closest("tr").classList.toggle("sel", e.target.checked);
  });
  updateSelCount();
};

const CMD_META = {
  "locate-on":  { action: "locate", params: { on: true },  title: "💡 开启定位灯", danger: false },
  "locate-off": { action: "locate", params: { on: false }, title: "关闭定位灯", danger: false },
  "reboot":     { action: "reboot", params: {}, title: "⟳ 重启矿机", danger: true },
  "set-pools":  { action: "set_pools", params: {}, title: "⚙ 修改矿池", danger: true },
};

document.querySelector(".cmdbar").addEventListener("click", (e) => {
  const cmd = e.target.dataset && e.target.dataset.cmd;
  if (!cmd) return;
  if (cmd === "selall") {
    lastMiners.forEach(m => selected.add(m.ip));
    refreshMiners(); return;
  }
  if (cmd === "clear") { selected.clear(); refreshMiners(); return; }
  if (cmd === "repair") return doMachineState("repair");
  if (cmd === "unrepair") return doMachineState("active");
  if (cmd === "remove") return doMachineState("remove");
  openCmdDialog(cmd);
});

async function doMachineState(action) {
  if (!selected.size) { toast("请先选择矿机"); return; }
  const ips = [...selected];
  // 维修/下架影响大(停告警/删名册)，二次确认把后果讲清；大批量再确认一次，防手滑把全场"安静地标维修"
  if (action === "repair" && !confirm(`确认把 ${ips.length} 台标记「维修中」？\n期间这些机器掉线/零算力将不再报警、也不计入客户统计。`)) return;
  if (action === "remove" && !confirm(`确认从名册「下架移除」${ips.length} 台？\n将不再探测/告警；若机器仍通电，下次扫描可能被重新收录。`)) return;
  if (ips.length > 500 && !confirm(`⚠️ 本次将影响 ${ips.length} 台（数量很大），请再确认一次！`)) return;
  const r = await fetch("/api/machine-state", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ips, action }),
  });
  const d = await r.json();
  if (!d.ok) { toast("失败：" + (d.error || r.status)); return; }
  toast(`${ { repair: "标记维修", active: "取消维修", remove: "下架移除" }[action] } ${d.count} 台`, "ok");
  selected.clear(); refreshMiners();
}

let pendingCmd = null;
function openCmdDialog(cmd) {
  if (!selected.size) { toast("请先选择矿机"); return; }
  const meta = CMD_META[cmd];
  pendingCmd = { action: meta.action, params: { ...meta.params } };
  $("cmdTitle").textContent = meta.title;
  const ips = [...selected];
  let html = `对 <b>${ips.length}</b> 台矿机执行：<b>${meta.title}</b>`;
  if (meta.danger)
    html += `<div class="warn-box">⚠️ 这是破坏性操作，会立即影响矿机运行（${meta.action === "reboot" ? "重启会中断挖矿约数分钟" : "改矿池会切换挖矿目标"}）。请确认无误。</div>`;
  if (meta.action === "set_pools") {
    html += `<div>矿池地址<input id="poolUrl" placeholder="stratum+tcp://host:port"></div>`
      + `<div>矿工名<input id="poolUser" placeholder="worker"></div>`
      + `<div>密码<input id="poolPass" value="x"></div>`;
  }
  html += `<div class="muted" style="margin-top:8px">目标 ${ips.length} 台：</div>`
    + `<div style="max-height:120px;overflow:auto;font-size:12px;line-height:1.6;border:1px solid #30363d;border-radius:6px;padding:6px;margin-top:4px">`
    + ips.map(esc).join("、") + `</div>`;
  $("cmdBody").innerHTML = html;
  $("cmdModal").classList.remove("hidden");
}

$("cmdConfirm").onclick = async () => {
  if (!pendingCmd) return;
  const ips = [...selected];
  if (pendingCmd.action === "set_pools") {
    const url = $("poolUrl").value.trim();
    if (!url) { toast("请填写矿池地址"); return; }
    pendingCmd.params.pools = [{ url, user: $("poolUser").value.trim(), pass: $("poolPass").value || "x" }];
  }
  $("cmdConfirm").disabled = true; $("cmdConfirm").textContent = "执行中…";
  try {
    const r = await fetch("/api/command", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ips, action: pendingCmd.action, params: pendingCmd.params }),
    });
    const d = await r.json();
    if (!d.ok) { toast("失败：" + (d.error || r.status)); }
    else if (d.async) {   // 分批重启：后台执行，轮询进度，界面不卡
      toast(`已开始分批重启 ${d.count} 台（打乱顺序，每批 ${d.batch} 台、间隔 ${d.delay}s，防变压器浪涌），后台执行中…`, "ok");
      pollCmdProgress();
      selected.clear();
    } else {
      const fails = d.results.filter(x => !x.ok);
      let msg = `${pendingCmd.action}：成功 ${d.success} / 失败 ${d.failed}`;
      if (fails.length) msg += "\n" + fails.slice(0, 5).map(x => `${x.ip}: ${x.msg}`).join("\n");
      toast(msg, fails.length ? "fail" : "ok");
    }
  } catch (err) { toast("请求出错：" + err); }
  $("cmdConfirm").disabled = false; $("cmdConfirm").textContent = "确认执行";
  $("cmdModal").classList.add("hidden");
  pendingCmd = null;
};
$("cmdCancel").onclick = $("cmdClose").onclick = () => { $("cmdModal").classList.add("hidden"); pendingCmd = null; };

// 分批重启后台进度轮询：界面不卡，跑完弹最终结果
async function pollCmdProgress() {
  if (_wsStop) return;   // 登出/会话失效后停止，别成僵尸轮询
  try {
    const p = await jget("/api/command/progress");
    if (p.running) {
      document.title = `重启 ${p.done}/${p.total} · 矿机监控面板`;
      setTimeout(pollCmdProgress, 3000);
    } else if (p.total) {
      document.title = "矿机监控面板";
      let msg = `分批重启完成：成功 ${p.success} / 失败 ${p.failed}（共 ${p.total} 台）`;
      if (p.fail_ips && p.fail_ips.length) msg += "\n失败示例：" + p.fail_ips.slice(0, 8).join("、");
      toast(msg, p.failed ? "fail" : "ok");
      refreshMiners();
    }
  } catch (e) { if (!_wsStop) setTimeout(pollCmdProgress, 5000); }
}

function toast(text, kind) {
  const t = document.createElement("div");
  t.className = "toast";
  t.innerHTML = `<span class="${kind === "fail" ? "res-fail" : kind === "ok" ? "res-ok" : ""}">${text.replace(/\n/g, "<br>")}</span>`;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 6000);
}

/* ---------------- 网段设置 ---------------- */
async function openSeg() {
  const d = await jget("/api/segments");
  $("segText").value = (d.segments || []).join("\n");
  $("segHs").value = d.host_start; $("segHe").value = d.host_end;
  const s = await jget("/api/settings");
  $("setInterval").value = s.scan_interval; $("setPps").value = s.max_pps;
  $("segModal").classList.remove("hidden");
}
async function saveSeg(scan) {
  const segs = $("segText").value.split(/[\n,\s]+/).map(s => s.trim()).filter(Boolean);
  const body = { segments: segs, host_start: parseInt($("segHs").value) || 1, host_end: parseInt($("segHe").value) || 254 };
  const r = await fetch("/api/segments", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  if (!d.ok) { toast("保存失败：" + (d.error || r.status)); return; }
  await fetch("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scan_interval: parseInt($("setInterval").value) || 300, max_pps: parseInt($("setPps").value) || 0 }),
  });
  $("segModal").classList.add("hidden");
  toast(`已保存 ${d.count} 个网段 + 扫描参数`, "ok");
  if (scan) { await fetch("/api/scan?kind=full", { method: "POST" }); toast("已触发全网扫描", "ok"); setTimeout(pollProgress, 500); }
}
$("btnSeg").onclick = openSeg;
$("segClose").onclick = $("segCancel").onclick = () => $("segModal").classList.add("hidden");
$("segSave").onclick = () => saveSeg(false);
$("segSaveScan").onclick = () => saveSeg(true);

$("btnFull").onclick = () => triggerScan("full");
$("btnQuick").onclick = () => triggerScan("quick");
$("btnVoice").onclick = () => setVoice(!voiceOn, true);
$("btnTestVoice").onclick = () => {
  try {   // 点击是用户手势，趁机唤醒被浏览器锁住的音频
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch (e) {}
  beep(880, 0.3); setTimeout(() => beep(660, 0.3), 350);
  speak("测试，一台矿机掉线，172 点 16 点 119 点 21");
  const hasVoice = ("speechSynthesis" in window);
  toast(hasVoice ? "已播放测试音。没声音的话：查电脑音量/静音、或浏览器是否给本页面静音了" :
        "你的浏览器不支持语音播报，建议用 Chrome/Edge", hasVoice ? "ok" : "fail");
};
setVoice(voiceOn, false);   // 仅刷新按钮文字，不播放（无手势）
$("mClose").onclick = () => $("modal").classList.add("hidden");
$("modal").onclick = (e) => { if (e.target.id === "modal") $("modal").classList.add("hidden"); };
["search", "fStatus", "fFw", "fSeg"].forEach(id => $(id).addEventListener("input", refreshMiners));

async function loadSegOptions() {
  try {
    const d = await jget("/api/segments");
    const sel = $("fSeg");
    (d.segments || []).forEach(s => {
      const o = document.createElement("option"); o.value = s; o.textContent = s + ".x"; sel.appendChild(o);
    });
  } catch (e) {}
}
document.querySelectorAll("#minerTable th[data-k]").forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  if (sortKey === k) sortOrder = sortOrder === "desc" ? "asc" : "desc";
  else { sortKey = k; sortOrder = "desc"; }
  refreshMiners();
});

/* ---------------- 登录 / 权限 ---------------- */
let myRole = "admin";
let _timer = null;

function showLogin() { stopDashboard(); $("loginOverlay").classList.remove("hidden"); }

function applyRole() {
  const canCtl = myRole === "ops" || myRole === "admin";
  document.body.classList.toggle("viewer", !canCtl);
  $("btnSeg").style.display = myRole === "admin" ? "" : "none";  // 网段设置仅 admin
  $("userBadge").textContent = myRole;
}

let _ws = null;
let _wsStop = false;   // 登出/会话失效后置真，停止重连风暴
function connectWS() {
  if (_wsStop) return;
  try {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    _ws = new WebSocket(`${proto}://${location.host}/ws`);
    _ws.onmessage = () => refreshAll();      // 扫描完成/告警 → 即时刷新
    _ws.onclose = (ev) => {                   // 1008=后端判会话失效 → 弹登录并停重连，避免每5秒握手风暴
      _ws = null;
      if (ev && ev.code === 1008) { showLogin(); return; }
      if (!_wsStop) setTimeout(connectWS, 5000);
    };
    _ws.onerror = () => { try { _ws.close(); } catch (e) {} };
  } catch (e) {}
}
function stopDashboard() {   // 会话失效/登出：停轮询与 WS 重连，避免登录页后台空转
  _wsStop = true;
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_ws) { try { _ws.close(); } catch (e) {} _ws = null; }
}

function startDashboard() {
  _wsStop = false;
  lastOkMs = Date.now();   // 登录成功，重置失联计时，避免刚进来误报
  loadSegOptions();
  refreshAll();
  connectWS();
  if (!_timer) _timer = setInterval(refreshAll, 30000);  // WS 推送为主，轮询兜底
  if (!_staleTimer) _staleTimer = setInterval(checkStale, 10000);  // 失联自检，每10秒
}

async function doLogin() {
  const r = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: $("loginUser").value, password: $("loginPass").value }),
  });
  const d = await r.json();
  if (!d.ok) { $("loginErr").textContent = d.error || "登录失败"; return; }
  $("loginOverlay").classList.add("hidden");
  myRole = d.role; applyRole(); startDashboard();
}

async function doLogout() { await fetch("/api/logout", { method: "POST" }); location.reload(); }

$("loginBtn").onclick = doLogin;
$("loginPass").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("btnLogout").onclick = doLogout;
$("btnScanContainers").onclick = async () => {
  await fetch("/api/containers/scan", { method: "POST" });
  toast("已触发集装箱刷新", "ok");
  setTimeout(refreshContainers, 1500);
};
$("tabList").onclick = () => setView("list");
$("tabRack").onclick = () => setView("rack");
$("tabWorker").onclick = () => setView("worker");
$("workerPeriod").addEventListener("change", refreshWorkers);
$("btnExportCsv").onclick = () => {   // 浏览器直接下载(带 Cookie)；对客户出账存档
  const period = parseInt($("workerPeriod").value) || 0;
  if (period) window.open(`/api/reports/customers?hours=${period}&format=csv`, "_blank");
};

async function init() {
  const r = await fetch("/api/me");
  if (r.status === 401) { showLogin(); return; }
  const me = await r.json();
  myRole = me.role; applyRole(); startDashboard();
}
init();
