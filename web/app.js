const $ = (id) => document.getElementById(id);
let trendChart, modalChart;
let sortKey = "hr_rt", sortOrder = "desc";
const HOT = 90;
const RENDER_CAP = 800;   // 一次最多渲染多少行：5000 行 innerHTML 重建会让低配运维机明显卡顿

// 转义设备/矿池返回的不可信字符串，防止 innerHTML 注入(XSS)
function esc(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const safeVal = (v) => v == null ? "-" : esc(v);

let lastOkMs = Date.now();   // 最近一次成功从服务器拿到数据(失联自检用)
let lastScanTs = 0;          // 服务器最近一次完成扫描的时间戳
let staleAfter = 900;        // 多久没有新扫描算过期：取服务端看门狗阈值(随巡检间隔变)
let _staleOn = false;
let _staleTimer = null;

async function jget(u) {
  const r = await fetch(u);
  if (r.status === 401) { showLogin(); throw new Error("401"); }
  const j = await r.json();
  lastOkMs = Date.now();     // 拿到数据 → 刷新"最近通信"时间
  return j;
}

// 所有写操作统一走这里：会话过期时弹登录框，而不是让用户看到一句 "失败：401" 却不知所措
async function jpost(u, body) {
  const r = await fetch(u, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (r.status === 401) { showLogin(); throw new Error("401"); }
  let j = {};
  try { j = await r.json(); } catch (e) { j = { ok: false, error: "HTTP " + r.status }; }
  if (r.status === 403) {   // 显示后端给的原因(矿池不在白名单/须先改密码…)，不一律说"权限不足"
    const why = j.error || j.detail || "权限不足：该操作需要 ops 或 admin 角色";
    toast(why);
    if (String(why).includes("修改密码")) openPwd(true);
    throw new Error("403");
  }
  lastOkMs = Date.now();
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
  } else if (lastScanTs && scanAge > staleAfter) {
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
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
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
const KIND_LABEL = { full: "全网发现", quick: "巡检", manual: "手动" };

async function refreshSummary() {
  const s = await jget("/api/summary");
  updateProgress(s.progress);
  if (s.scan_ts) lastScanTs = s.scan_ts;   // 记录服务器最近完成扫描时间(失联自检用)
  if (s.stale_after) staleAfter = s.stale_after;
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
  $("lastScan").textContent = `上次扫描 ${ago(s.scan_ts)} (${KIND_LABEL[s.scan_kind] || s.scan_kind})`;
}

async function ackAlert(id) {
  try { await jpost("/api/alerts/ack", { id }); refreshAlerts(); } catch (e) {}
}
// 告警里直接下架坏机器：从名册移除 + 清掉它的告警(不再探测/告警)
async function removeFromAlert(ip) {
  if (!confirm(`确认下架移除 ${ip}？\n将从名册删除、不再探测/告警。若机器仍通电，下次扫描可能被重新收录。`)) return;
  try {
    const d = await jpost("/api/machine-state", { ips: [ip], action: "remove" });
    if (!d.ok) { toast("下架失败：" + (d.error || "")); return; }
    toast(`已下架 ${ip}`, "ok");
    refreshAlerts(); refreshMiners();
  } catch (e) {}
}
function alertClick(x) {
  // 内联 onclick 里的 JS 字符串：esc() 转出的 &#39; 会被 HTML 解码回单引号，挡不住注入。
  // 只给形如 IP/网段的值生成点击，别的一律不可点
  if (!/^[0-9.]+(\.x)?$/.test(String(x.ip || ""))) return "";
  if (x.type && x.type.startsWith("cooler")) return `openContainer('${esc(x.ip)}')`;
  // 网段事件的 ip 是 "172.16.101.x"：以前被当成单台矿机打开一个空详情；改为筛出该段离线机
  if (x.ip && x.ip.endsWith(".x")) return `focusSegment('${esc(x.ip.slice(0, -2))}')`;
  if (x.ip && x.ip.split(".").length === 4) return `openMiner('${esc(x.ip)}')`;
  return "";
}
function focusSegment(seg) {
  const sel = $("fSeg");
  if (![...sel.options].some(o => o.value === seg)) {
    const o = document.createElement("option"); o.value = seg; o.textContent = seg + ".x"; sel.appendChild(o);
  }
  sel.value = seg; $("fStatus").value = "offline";
  $("search").value = ""; $("fFw").value = ""; showAllRows = false;   // 别让原来的搜索/固件筛选把结果筛空
  if (typeof setView === "function") setView("list");
  refreshMiners();
  $("minerTable").scrollIntoView({ behavior: "smooth" });
}
async function refreshAlerts() {
  const d = await jget("/api/alerts?active=true");
  const a = d.alerts || [];
  const total = (d.total != null) ? d.total : a.length;   // 真实总数(不被列表上限卡住)
  $("alertCount").textContent = total;
  $("alertList").innerHTML = (a.length ? a.map(x => {
    const click = alertClick(x);
    const isCooler = x.type && x.type.startsWith("cooler");
    const isMiner = x.ip && x.ip.split(".").length === 4 && !x.ip.endsWith(".x");  // 单台矿机
    let tail = "";
    if (!isCooler) {   // 集装箱告警不给按钮(修好自动消失)
      tail = x.ack_by ? `<span class="acked">✓ ${esc(x.ack_by)} 已确认</span>`
                      : `<button class="ackbtn" onclick="ackAlert(${x.id})">确认</button>`;
      if (isMiner) tail += `<button class="rmbtn" onclick="removeFromAlert('${esc(x.ip)}')" title="从名册下架移除该机器">下架</button>`;
    }
    return `<div class="a ${esc(x.severity)}${x.ack_by ? " is-ack" : ""}"><span class="atime">${fmtDT(x.ts)} · ${ago(x.ts)}</span>`
      + `<span class="ip"${click ? ` onclick="${click}"` : ""}>${esc(x.ip)}</span>`
      + `<span class="adetail">${esc(x.detail)}</span>${tail}</div>`;
  }).join("") : '<div class="muted">无</div>')
    + (total > a.length ? `<div class="muted" style="text-align:center;padding:6px">…仅显示前 ${a.length} 条，共 <b>${total}</b> 条活跃告警</div>` : "");
  document.title = total ? `(${total}) 矿机监控面板` : "矿机监控面板";
  handleVoice(a, d.counts || {}, total);
}

/* ---------------- 语音告警 ---------------- */
let voiceOn = localStorage.getItem("voiceOn") === "1";
let maxSeenId = 0;             // 已见告警的最大id(自增,永不复用)：O(1)判新
let prevActiveIds = new Set();
let alertsInit = false;
let audioCtx;

function beep(freq = 880, dur = 0.25) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();   // 浏览器锁声后必须先唤醒
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

function notify(title, body) {
  try {
    if ("Notification" in window && Notification.permission === "granted")
      new Notification(title, { body });
  } catch (e) {}
}

function announceCounts(counts) {
  // 只报当前各类告警「台数/处数」，不念 IP。counts 是后端精确计数(不被列表上限截断)
  const n = (t) => counts[t] || 0;
  const cooler = Object.keys(counts).filter(t => t.startsWith("cooler")).reduce((s, t) => s + counts[t], 0);
  const parts = [];
  if (n("pool_hijack")) parts.push(`矿池被篡改 ${n("pool_hijack")} 台，请立即核查`);
  if (n("stalled")) parts.push("监控停滞");
  if (n("segment_down")) parts.push(`${n("segment_down")} 个网段掉线`);
  if (n("offline")) parts.push(`掉线 ${n("offline")} 台`);
  if (n("zero")) parts.push(`零算力 ${n("zero")} 台`);
  if (n("low_hashrate")) parts.push(`掉算力 ${n("low_hashrate")} 台`);
  if (n("overheat")) parts.push(`高温 ${n("overheat")} 台`);
  if (n("reject")) parts.push(`拒绝率偏高 ${n("reject")} 台`);
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
    // 列表被截断时不做"已恢复"播报：老告警被挤出窗口会被误判成已恢复
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

// 浏览器规则：刷新后声音被锁，必须有用户点击才能出声。这里在首次点击页面任意处时解锁。
let _audioUnlocked = false;
document.addEventListener("click", () => {
  if (_audioUnlocked || !voiceOn) return;
  _audioUnlocked = true;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch (e) {}
});

function trendYScale(data) {
  // 小幅正常抖动(约±1%)不拉满图高；真实大跌/大涨仍清晰可见。
  // 量程至少覆盖均值的 8%，再在数据上下各留一点边。
  if (!data.length) return {};
  const lo = Math.min(...data), hi = Math.max(...data);
  const mid = (lo + hi) / 2 || 1;
  const span = Math.max(hi - lo, Math.abs(mid) * 0.08);
  const pad = span * 0.15;
  return { min: mid - span / 2 - pad, max: mid + span / 2 + pad };
}

async function refreshTrend() {
  const d = await jget("/api/trend?points=288");
  const t = d.trend || [];
  const labels = t.map(x => new Date(x.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }));
  const raw = t.map(x => x.total_hr);
  const { div, unit } = hashUnit(raw.length ? Math.max(...raw) : 0);
  const data = raw.map(v => v / div);
  const yScale = trendYScale(data);
  const tt = $("trendTitle"); if (tt) tt.textContent = `总算力趋势 (${unit})`;
  if (!window.Chart) return;   // 无网/CDN 不可达时图表库缺失，不能让整个面板挂掉
  if (!trendChart) {
    trendChart = new Chart($("trendChart"), {
      type: "line",
      data: { labels, datasets: [{ data, borderColor: "#1f6feb", backgroundColor: "rgba(31,111,235,.1)", fill: true, tension: .3, pointRadius: 0, borderWidth: 2 }] },
      options: {
        interaction: { mode: "index", intersect: false },   // 鼠标放在图上任意处即显示数值
        plugins: { legend: { display: false }, tooltip: { callbacks: {
          title: (items) => items.length ? items[0].label : "",
          label: (c) => c.parsed.y.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " " + unit } } },
        scales: {
          x: { ticks: { color: "#6e7681", maxTicksLimit: 8 } },
          y: { ticks: { color: "#6e7681" }, min: yScale.min, max: yScale.max }
        }
      }
    });
  } else {
    trendChart.data.labels = labels; trendChart.data.datasets[0].data = data;
    trendChart.options.plugins.tooltip.callbacks.label = (c) => c.parsed.y.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " " + unit;
    trendChart.options.scales.y.min = yScale.min;
    trendChart.options.scales.y.max = yScale.max;
    trendChart.update("none");
  }
}

function rowClass(r) {
  return r.status === "offline" ? "off" : "";
}
// 口径必须与后端告警一致：hr_rt===0 才是"零算力"(会告警)；null 是"这次没读到数"(不告警)。
// 之前前端把 null 也画成红色的 0 TH，运维看到满屏红却一条告警都没有，反过来怀疑监控坏了。
function hrCell(r) {
  if (r.hr_rt == null) return `<td class="muted" title="本次未读到算力(接口超时/密码错)，不计为零算力">无数据</td>`;
  return `<td class="${r.hr_rt === 0 ? "low" : ""}">${fmtHash(r.hr_rt)}</td>`;
}
function tempCell(r) {
  if (r.temp == null) return `<td>-</td>`;
  return `<td class="${r.temp >= HOT ? "hot" : ""}">${safeVal(r.temp)}℃</td>`;
}
const REJECT_HI = 5;
function rejCell(r) {
  if (r.accepted == null && r.rejected == null) return `<td>-</td>`;
  const tot = (r.accepted || 0) + (r.rejected || 0);
  if (tot === 0) return `<td>0%</td>`;
  const p = Math.round((r.rejected || 0) / tot * 1000) / 10;
  return `<td class="${p >= REJECT_HI ? "hot" : ""}">${p}%</td>`;
}

let lastMiners = [];            // 当前筛选结果全集（选择/命令都基于它，不受渲染上限影响）
const selected = new Set();     // 选中的 IP
let showAllRows = false;

let _minerSeq = 0;            // 列表请求序号：慢的旧响应晚到时丢弃，别覆盖新筛选的结果
let _lastFilterKey = null;    // 上次渲染用的筛选条件
async function refreshMiners() {
  const q = $("search").value.trim();
  const st = $("fStatus").value, fw = $("fFw").value, seg = $("fSeg").value;
  const u = `/api/miners?sort=${sortKey}&order=${sortOrder}&status=${st}&fw=${fw}&seg=${seg}&q=${encodeURIComponent(q)}`;
  const seq = ++_minerSeq;
  const d = await jget(u);
  if (seq !== _minerSeq) return;   // 期间又发了新请求(换了筛选/自动刷新)，这个结果已过时
  lastMiners = d.miners || [];   // 后端异常响应(无miners字段)时兜底为空
  // 防误操作：用户自己换了筛选条件时，把选择集收敛到当前结果集，别让看不见的机器被命令误打。
  // 后台自动刷新(同一筛选条件)不动选择集：按"离线"勾了 300 台，期间 30 台恢复在线，
  // 以前会被悄悄移出选择、实际只下发 270 台且没有任何提示
  const filterKey = [st, fw, seg, q].join("|");
  if (filterKey !== _lastFilterKey) {
    const visible = new Set(lastMiners.map(m => m.ip));
    for (const ip of [...selected]) if (!visible.has(ip)) selected.delete(ip);
    _lastFilterKey = filterKey;
  }
  $("minerCount").textContent = d.count != null ? d.count : lastMiners.length;
  const a = d.agg || {};
  $("segStat").textContent = `${seg ? seg + ".x ｜ " : ""}在线 ${a.online || 0} ｜ 算力 ${fmtHash(a.total_hr)} ｜ 功耗 ${fmtPower(a.total_power)}`;

  const rows = showAllRows ? lastMiners : lastMiners.slice(0, RENDER_CAP);
  $("minerBody").innerHTML = rows.map(r => {
    const ck = selected.has(r.ip) ? "checked" : "";
    return `<tr class="${rowClass(r)} ${selected.has(r.ip) ? "sel" : ""}">`
      + `<td class="cbcol"><input type="checkbox" data-ip="${esc(r.ip)}" ${ck}></td>`
      + `<td class="ip-link" onclick="openMiner('${esc(r.ip)}')">${esc(r.ip)}</td>`
      + `<td>${r.mstate === "repair" ? '<span class="pill repair">维修中</span>' : `<span class="pill ${esc(r.status)}" title="${r.status === "unknown" ? "本轮扫描超时没来得及探测，非确认离线" : ""}">${r.status === "online" ? "在线" : r.status === "unknown" ? "未探测" : "离线"}</span>`}</td>`
      + `<td>${r.firmware ? `<span class="pill ${esc(r.firmware)}">${esc(r.firmware)}</span>` : "-"}</td>`
      + `<td>${esc(r.model) || "-"}</td>`
      + hrCell(r) + `<td>${r.hr_avg != null ? fmtHash(r.hr_avg) : "-"}</td>`
      + `<td>${r.power != null ? safeVal(r.power) + " W" : "-"}</td>`
      + `<td>${r.eff != null ? esc(r.eff) : "-"}</td>`
      + rejCell(r)
      + tempCell(r)
      + `<td>${fmtUptime(r.uptime)}</td>`
      + `<td>${esc(r.worker) || "-"}</td>`
      + `<td>${esc(r.sn) || "-"}${r.mac ? `<br><small>${esc(r.mac)}</small>` : ""}</td>`
      + `<td class="muted">${esc(r.note)}</td></tr>`;
  }).join("");
  const more = $("renderNote");
  if (more) {
    if (!showAllRows && lastMiners.length > RENDER_CAP) {
      more.innerHTML = `仅渲染前 <b>${RENDER_CAP}</b> 行（共 ${lastMiners.length} 台）以保证页面流畅。`
        + `批量操作/「选中当前筛选」仍作用于全部 ${lastMiners.length} 台。`
        + ` <button id="btnShowAll" class="mini-btn">仍要渲染全部</button>`;
      more.classList.remove("hidden");
      const b = $("btnShowAll");
      if (b) b.onclick = () => { showAllRows = true; refreshMiners(); };
    } else {
      more.classList.add("hidden"); more.innerHTML = "";
    }
  }
  updateSelCount();
}

function updateSelCount() {
  const visible = new Set(lastMiners.map(m => m.ip));
  const hidden = [...selected].filter(ip => !visible.has(ip)).length;
  $("selCount").textContent = `已选 ${selected.size} 台` + (hidden ? `（其中 ${hidden} 台已不在当前列表）` : "");
  const all = $("cbAll");   // 全选框跟随当前结果集同步
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
    + `<span><i class="dot slot unknown"></i>未探测(本轮扫描超时)</span>`
    + `<span class="muted">（格子里是机位号，空号一眼可见）</span></div>`;
  $("rackList").innerHTML = legend + (d.racks || []).map(rk => {
    const slots = rk.slots.map(s => {
      if (s.st === "empty")   // 空机位：灰、不可点
        return `<div class="slot empty" title="${esc(s.ip)} · 空机位">${s.h}</div>`;
      if (s.st === "unknown")   // 本轮未探测：不是离线，别吓人
        return `<div class="slot unknown" title="${esc(s.ip)} · 本轮扫描超时没来得及探测" onclick="openMiner('${esc(s.ip)}')">${s.h}</div>`;
      const tip = `${s.ip}${s.hr != null ? " · " + fmtHash(s.hr) : ""}${s.temp != null ? " · " + s.temp + "℃" : ""}`;
      return `<div class="slot ${s.st}" title="${esc(tip)}" onclick="openMiner('${esc(s.ip)}')">${s.h}</div>`;
    }).join("");
    const bad = rk.abnormal ? `零算力 <b class="bad">${rk.abnormal}</b> · ` : "";
    return `<div class="rack"><div class="rack-head"><span class="name">${esc(rk.name)}.x</span>`
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
    // 周期超过可用数据被截断时给出提示
    const note = d.truncated
      ? `<div class="muted" style="margin-bottom:8px">⚠️ 数据实际仅覆盖近 ${(d.covered_hours / 24).toFixed(1)} 天（系统投运时间还不够长）</div>`
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
    $("progText").textContent = `${KIND_LABEL[p.kind] || p.kind} 扫描中 ${p.done}/${p.total} (${pct}%)`;
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
  // 用真实时间戳做 x：关机/掉线的空档按时间比例显示；相邻点间隔>15分钟插断点让线断开
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
  if (window.Chart) modalChart = new Chart($("mChart"), {
    type: "line",
    data: { datasets: [
      { label: "算力 TH", data: hr, borderColor: "#1f6feb", yAxisID: "y", pointRadius: 0, tension: .3, spanGaps: false },
      { label: "芯片温 ℃", data: temp, borderColor: "#f85149", yAxisID: "y1", pointRadius: 0, tension: .3, spanGaps: false } ] },
    options: {
      interaction: { mode: "index", intersect: false },
      scales: {
        y: { position: "left", ticks: { color: "#6e7681" } },
        y1: { position: "right", ticks: { color: "#6e7681" }, grid: { drawOnChartArea: false } },
        x: { type: "linear",
             ticks: { color: "#6e7681", maxTicksLimit: 7,
                      callback: (v) => new Date(v).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" }) } } },
      plugins: { legend: { labels: { color: "#adbac7" } },
                 tooltip: { callbacks: { title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleString("zh-CN", { hour12: false }) : "" } } } }
  });
  $("mInfo").innerHTML =
    `固件：${esc(c.firmware) || "-"} ｜ 状态：${esc(c.status) || "-"} ｜ SN：${esc(c.sn) || "-"}<br>`
    + `MAC：${esc(c.mac) || "-"}<br>`
    + `实时算力：${c.hr_rt == null ? "无数据" : fmtHash(c.hr_rt)} ｜ 平均：${fmtHash(c.hr_avg)} ｜ 功耗：${safeVal(c.power)} W<br>`
    + `能效：${safeVal(c.eff)} J/TH ｜ 芯片温：${safeVal(c.temp)}℃ ｜ 运行时长：${fmtUptime(c.uptime)}<br>`
    + `矿工名：${esc(c.worker) || "-"} ｜ 接受/拒绝/陈旧：${safeVal(c.accepted)}/${safeVal(c.rejected)}/${safeVal(c.stale)}`
    + (c.note ? `<br>备注：${esc(c.note)}` : "");
  $("modal").classList.remove("hidden");
}

async function triggerScan(kind) {
  try {
    const d = await jpost(`/api/scan?kind=${kind}`, {});
    if (d && d.started === false) toast("已有扫描在进行中，请稍候");
  } catch (e) { return; }
  setTimeout(pollProgress, 500);
}
async function pollProgress() {
  if (_wsStop) return;
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
      return `<div class="cbox offline" onclick="openContainer('${esc(c.ip)}')">`
        + `<div class="cbox-head"><span class="cbox-ip">${esc(c.ip)}</span>`
        + `<span class="fbadge crit">控制器离线</span></div>`
        + `<div class="cbox-meta">最后进水 ${safeVal(c.supply_temp)}℃ / 出水 ${safeVal(c.return_temp)}℃</div></div>`;
    }
    const dt = (c.supply_temp != null && c.return_temp != null) ? (c.return_temp - c.supply_temp).toFixed(1) : "-";
    const pumps = Object.entries(c.pumps || {}).map(([k, v]) => `<span class="pump ${v ? "on" : ""}">${esc(k)}</span>`).join("");
    // 忽略列表内的故障置灰(info)，其余按严重级；按阈值标红低压力
    const faults = (c.faults || []).map(f => `<span class="fbadge ${ign.has(f.flag) ? "info" : esc(f.sev)}">${esc(f.label)}</span>`).join("");
    const spLow = spMin && c.supply_pressure != null && c.supply_pressure < spMin;
    const rpLow = rpMin && c.return_pressure != null && c.return_pressure < rpMin;
    const realFault = (c.faults || []).some(f => !ign.has(f.flag)) || spLow || rpLow;
    return `<div class="cbox ${realFault ? "fault" : ""}" onclick="openContainer('${esc(c.ip)}')">`
      + `<div class="cbox-head"><span class="cbox-ip">${esc(c.ip)}</span>`
      + `<span class="cbox-meta">矿机 ${safeVal(c.miner_num)} 台 ｜ ⚡ <b>${fmtPower(boxPower(c))}</b></span></div>`
      + `<div class="cbox-temps"><div>进水 <span class="v in">${safeVal(c.supply_temp)}℃</span></div>`
      + `<div>出水 <span class="v out">${safeVal(c.return_temp)}℃</span></div><div>ΔT <span class="v">${safeVal(dt)}℃</span></div></div>`
      + `<div class="cbox-meta">箱内 ${safeVal(c.internal_temp)}℃ / ${safeVal(c.internal_humidity)}% ｜ 流量 ${safeVal(c.flow)} ｜ 压力 `
      + `<span class="${spLow ? "res-fail" : ""}">${safeVal(c.supply_pressure)}</span>/<span class="${rpLow ? "res-fail" : ""}">${safeVal(c.return_pressure)}</span> ｜ 设定 ${safeVal(c.set_temp)}℃</div>`
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
  if (window.Chart) modalChart = new Chart($("mChart"), {
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
  const faults = (c.faults || []).map(f => `<span class="fbadge ${esc(f.sev)}">${esc(f.label)}</span>`).join("") || "无";
  const pumps = Object.entries(c.pumps || {}).map(([k, v]) => `${esc(k)}:${v ? "开" : "关"}`).join(" ｜ ");
  $("mInfo").innerHTML =
    `进水 ${safeVal(c.supply_temp)}℃ ｜ 出水 ${safeVal(c.return_temp)}℃ ｜ 设定 ${safeVal(c.set_temp)}℃<br>`
    + `供/回压 ${safeVal(c.supply_pressure)}/${safeVal(c.return_pressure)} ｜ 流量 ${safeVal(c.flow)} ｜ 冷却塔进水 ${safeVal(c.tower_inlet_temp)}℃<br>`
    + `箱内 ${safeVal(c.internal_temp)}℃ / ${safeVal(c.internal_humidity)}% ｜ 矿机 ${safeVal(c.miner_num)} 台 ｜ 芯片最高 ${safeVal(c.chip_max_temp)}℃<br>`
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

/* ---------------- 选择 + 远程命令 ---------------- */
$("minerBody").addEventListener("change", (e) => {
  const cb = e.target;
  if (cb.dataset && cb.dataset.ip) {
    if (cb.checked) selected.add(cb.dataset.ip); else selected.delete(cb.dataset.ip);
    cb.closest("tr").classList.toggle("sel", cb.checked);
    updateSelCount();
  }
});
// 全选：作用于「当前筛选的全部结果」而不只是渲染出来的那几百行，
// 否则渲染上限会让用户以为全选了、实际漏掉后面的机器。
$("cbAll").onclick = (e) => {
  if (e.target.checked) lastMiners.forEach(m => selected.add(m.ip));
  else selected.clear();
  refreshMiners();
};

const CMD_META = {
  "locate-on":  { action: "locate", params: { on: true },  title: "💡 开启定位灯", short: "开定位灯", danger: false },
  "locate-off": { action: "locate", params: { on: false }, title: "关闭定位灯", short: "关定位灯", danger: false },
  "reboot":     { action: "reboot", params: {}, title: "⟳ 重启矿机", short: "重启", danger: true },
  "set-pools":  { action: "set_pools", params: {}, title: "⚙ 修改矿池", short: "换矿池", danger: true },
};
// 破坏性命令(重启/换矿池)的「大批量」阈值。比 repair/remove 的 500 更低：换矿池直接改全场
// 收益去向、重启让全场同时离线，误操作代价远高于标记维修，所以更早开始拦。
const DANGER_BULK = 200;
// 后端 /api/command 对 reboot/set_pools 强制要求请求体带 confirm:true，否则 400。
// 字段名与 server.py 约定死，改这里必须同步改后端。
const NEED_CONFIRM_FLAG = new Set(["reboot", "set_pools"]);

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
  // 单次上限 control.max_batch 是安全闸：超了直接提示分批，别在确认之后才被后端拒绝，
  // 也别在前端悄悄分批绕过它(下架 5000 台只需确认两次)
  if (ips.length > maxBatch) { toast(`一次最多 ${maxBatch} 台（control.max_batch），请缩小选择范围分批操作`); return; }
  // 维修/下架影响大(停告警/删名册)，二次确认把后果讲清；大批量再确认一次
  if (action === "repair" && !confirm(`确认把 ${ips.length} 台标记「维修中」？\n期间这些机器掉线/零算力将不再报警、也不计入客户统计。`)) return;
  if (action === "remove" && !confirm(`确认从名册「下架移除」${ips.length} 台？\n将不再探测/告警；若机器仍通电，下次扫描可能被重新收录。`)) return;
  if (ips.length > 500 && !confirm(`⚠️ 本次将影响 ${ips.length} 台（数量很大），请再确认一次！`)) return;
  try {
    const d = await jpost("/api/machine-state", { ips, action });
    if (!d.ok) { toast("失败：" + (d.error || "")); return; }
    toast(`${ { repair: "标记维修", active: "取消维修", remove: "下架移除" }[action] } ${d.count} 台`, "ok");
    selected.clear(); refreshMiners();
  } catch (e) { if (!String(e.message).match(/^40[13]$/)) toast("请求失败(网络/服务异常)，请刷新后确认结果"); }
}

let maxBatch = 1000;        // 单次命令/维修/下架上限，登录后取服务端 control.max_batch
let pendingCmd = null;
let _cmdInFlight = false;   // 命令在途：禁止再开新命令弹窗，防两次执行的结果/按钮串台
function openCmdDialog(cmd, includeHidden) {
  if (_cmdInFlight) { toast("上一条命令还在执行，完成后会弹出结果，请稍候"); return; }
  if (!selected.size) { toast("请先选择矿机"); return; }
  const meta = CMD_META[cmd];
  // 勾选里"已不在当前列表"的机器(自动刷新后状态变了，比如按"离线"勾的已恢复在线在挖矿)：
  // 默认不对它们执行，醒目列出，要执行得自己勾上。以前照样下发且弹窗里毫无提示
  const visible = new Set(lastMiners.map(m => m.ip));
  const hiddenIps = [...selected].filter(ip => !visible.has(ip));
  const ips = includeHidden ? [...selected] : [...selected].filter(ip => visible.has(ip));
  if (ips.length > maxBatch) { toast(`一次最多 ${maxBatch} 台（control.max_batch），请缩小选择范围分批操作`); return; }
  const keepPools = [0, 1, 2].map(i => ["poolUrl", "poolUser", "poolPass"].map(k =>
    ($(`${k}${i}`) || {}).value));   // 切换"是否包含"时重画弹窗，别丢已填的矿池
  // 目标在打开弹窗这一刻冻结：弹窗里列的就是确认后下发的，期间自动刷新不会改变目标
  pendingCmd = { action: meta.action, params: { ...meta.params }, danger: !!meta.danger,
                 short: meta.short || meta.title, ips };
  $("cmdTitle").textContent = meta.title;
  let html = `对 <b>${ips.length}</b> 台矿机执行：<b>${esc(meta.title)}</b>`;
  if (hiddenIps.length)
    html += `<div class="warn-box" style="border-width:2px">⚠️ 你勾选的机器里有 <b>${hiddenIps.length}</b> 台`
      + `已不在当前列表（勾选后状态变了，比如已恢复在线）：${hiddenIps.slice(0, 20).map(esc).join("、")}`
      + `${hiddenIps.length > 20 ? " …" : ""}<br><label style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;margin-top:6px">`
      + `<input type="checkbox" id="cmdIncHidden" style="width:auto;margin:0"`
      + `${includeHidden ? " checked" : ""}> 也对这 ${hiddenIps.length} 台执行</label>`
      + `（默认不执行）</div>`;
  if (meta.danger)
    html += `<div class="warn-box">⚠️ 这是破坏性操作，会立即影响矿机运行（${meta.action === "reboot" ? "重启会中断挖矿约数分钟" : "改矿池会切换挖矿目标"}）。请确认无误。</div>`;
  // 规模分级提示：几台和几千台的后果完全不是一回事，弹窗里必须让人看清影响范围
  if (meta.danger && ips.length > DANGER_BULK)
    html += `<div class="warn-box" style="border-width:2px;font-weight:700;font-size:15px;line-height:1.7">`
      + `🚨 即将对 <span style="font-size:19px">${ips.length}</span> 台矿机执行【${esc(meta.short || meta.title)}】<br>`
      + (meta.action === "reboot"
          ? `这会让这 ${ips.length} 台矿机同时离线数分钟（分批下发，仍有整片算力掉坑）。`
          : `这会把这 ${ips.length} 台矿机的挖矿收益去向整体切换到新矿池。`)
      + `<br>这是破坏性操作，请再次确认选中范围无误！</div>`;
  if (meta.action === "set_pools") {
    // 后端支持 1-8 个池，这里给主池 + 两个备用池(矿场标准配置就是一主两备)
    html += `<div class="muted" style="margin:6px 0">主池必填；备用池留空则不下发。矿机会在主池不可用时自动切备用池。</div>`;
    for (let i = 0; i < 3; i++) {
      html += `<div class="poolrow"><b>${i === 0 ? "主池" : "备用池" + i}</b>`
        + `<input id="poolUrl${i}" placeholder="stratum+tcp://host:port">`
        + `<input id="poolUser${i}" placeholder="矿工名 worker">`
        + `<input id="poolPass${i}" value="x" placeholder="密码"></div>`;
    }
  }
  html += `<div class="muted" style="margin-top:8px">目标 ${ips.length} 台：</div>`
    + `<div style="max-height:120px;overflow:auto;font-size:12px;line-height:1.6;border:1px solid #30363d;border-radius:6px;padding:6px;margin-top:4px">`
    + ips.map(esc).join("、") + `</div>`;
  $("cmdBody").innerHTML = html;
  keepPools.forEach((vals, i) => vals.forEach((v, j) => {
    const el = $(`${["poolUrl", "poolUser", "poolPass"][j]}${i}`);
    if (el && v !== undefined) el.value = v;
  }));
  if ($("cmdIncHidden")) $("cmdIncHidden").onchange = (e) => openCmdDialog(cmd, e.target.checked);
  $("cmdConfirm").disabled = ips.length === 0;
  // 确认按钮上写清「几台 + 干什么」，避免用户凭肌肉记忆点掉一个通用的"确认执行"
  $("cmdConfirm").textContent = `确认对 ${ips.length} 台执行【${meta.short || meta.title}】`;
  $("cmdModal").classList.remove("hidden");
}

$("cmdConfirm").onclick = async () => {
  if (!pendingCmd || _cmdInFlight || !pendingCmd.ips.length) return;
  const cmd = pendingCmd;          // 局部持有：执行中点了取消/×，结果也照样显示，不影响下一个弹窗
  const ips = cmd.ips;
  if (cmd.action === "set_pools") {
    const pools = [];
    for (let i = 0; i < 3; i++) {
      const el = $(`poolUrl${i}`);
      const url = el ? el.value.trim() : "";
      if (!url) continue;
      const user = $(`poolUser${i}`).value.trim();
      if (!user) { toast(`第 ${i + 1} 个矿池填了地址但没填矿工名`); return; }
      pools.push({ url, user, pass: $(`poolPass${i}`).value || "x" });
    }
    if (!pools.length) { toast("请至少填写主池地址与矿工名"); return; }
    cmd.params.pools = pools;
  }
  // 大批量破坏性命令再拦一道原生确认，和 repair/remove 的交互保持一致
  if (cmd.danger && ips.length > DANGER_BULK &&
      !confirm(`⚠️ 即将对 ${ips.length} 台矿机执行【${cmd.short}】，数量很大且不可撤销。\n确定继续吗？`)) return;
  const btnLabel = $("cmdConfirm").textContent;
  _cmdInFlight = true;
  $("cmdConfirm").disabled = true; $("cmdConfirm").textContent = "执行中…";
  $("cmdCancel").textContent = "后台执行，关闭窗口";
  try {
    const body = { ips, action: cmd.action, params: cmd.params };
    // 后端对 reboot/set_pools 强制校验 confirm===true，缺了直接 400
    if (NEED_CONFIRM_FLAG.has(cmd.action)) body.confirm = true;
    const d = await jpost("/api/command", body);
    if (!d.ok) { toast("失败：" + (d.error || "")); }
    else if (d.async) {   // 分批重启：后台执行，轮询进度，界面不卡
      toast(`已开始分批重启 ${d.count} 台（打乱顺序，每批 ${d.batch} 台、间隔 ${d.delay}s，防变压器浪涌），后台执行中…`, "ok");
      pollCmdProgress();
      selected.clear(); updateSelCount(); refreshMiners();   // 勾选框和"已选 N 台"一起清掉
    } else {
      const fails = (d.results || []).filter(x => !x.ok);
      let msg = `${cmd.action}：成功 ${d.success} / 失败 ${d.failed}`;
      if (fails.length) msg += "\n" + fails.slice(0, 5).map(x => `${x.ip}: ${x.msg}`).join("\n");
      toast(msg, fails.length ? "fail" : "ok");
    }
  } catch (err) { /* 401/403 已在 jpost 里处理 */ }
  _cmdInFlight = false;
  $("cmdConfirm").disabled = false; $("cmdConfirm").textContent = btnLabel;
  $("cmdCancel").textContent = "取消";
  if (pendingCmd === cmd) { $("cmdModal").classList.add("hidden"); pendingCmd = null; }
};
$("cmdCancel").onclick = $("cmdClose").onclick = () => {
  $("cmdModal").classList.add("hidden");
  if (!_cmdInFlight) pendingCmd = null;   // 执行中关窗只是隐藏，命令照常跑完并提示结果
};

// 分批重启后台进度轮询：界面不卡，跑完弹最终结果
let _cmdPollTimer = null;   // 只保留一条进度轮询链：快速重新登录时别再起一条、结束时弹两次结果
async function pollCmdProgress(onlyIfRunning) {
  if (_wsStop) return;   // 登出/会话失效后停止，别成僵尸轮询
  if (_cmdPollTimer) { clearTimeout(_cmdPollTimer); _cmdPollTimer = null; }
  try {
    const p = await jget("/api/command/progress");
    if (onlyIfRunning && !p.running) return;   // 刷新页面时：没有在跑的就别弹旧结果
    if (p.running) {
      document.title = `重启 ${p.done}/${p.total} · 矿机监控面板`;
      _cmdPollTimer = setTimeout(pollCmdProgress, 3000);
    } else if (p.total) {
      document.title = "矿机监控面板";
      let msg = `分批重启完成：成功 ${p.success} / 失败 ${p.failed}（共 ${p.total} 台）`;
      if (p.fail_ips && p.fail_ips.length) msg += "\n失败示例：" + p.fail_ips.slice(0, 8).join("、");
      toast(msg, p.failed ? "fail" : "ok");
      refreshMiners();
    }
  } catch (e) { if (!_wsStop) _cmdPollTimer = setTimeout(pollCmdProgress, 5000); }
}

// toast 用 textContent 而不是 innerHTML：内容里会拼进矿机/矿池返回的错误串(control.py 把异常
// 原样放进 msg)。内网一台被攻陷的设备返回带 <img onerror=...> 的报错，就能在 ops/admin 的
// 浏览器里执行 JS，进而调用 /api/command 重启全场。换行靠 CSS 的 white-space 处理。
function toast(text, kind) {
  const t = document.createElement("div");
  t.className = "toast";
  const span = document.createElement("span");
  if (kind === "fail") span.className = "res-fail";
  else if (kind === "ok") span.className = "res-ok";
  span.textContent = String(text);
  t.appendChild(span);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 6000);
}

/* ---------------- 设置(网段/扫描/云端上报) ---------------- */
async function openSeg() {
  const d = await jget("/api/segments");
  $("segText").value = (d.segments || []).join("\n");
  $("segHs").value = d.host_start; $("segHe").value = d.host_end;
  const s = await jget("/api/settings");
  $("setInterval").value = s.scan_interval;
  if ($("setFullInterval")) $("setFullInterval").value = s.full_interval;
  $("setPps").value = s.max_pps;
  if (s.cloud) {   // 云端上报配置(仅 admin 下发)
    $("cloudEnabled").checked = !!s.cloud.enabled;
    $("cloudName").value = s.cloud.site_name || "";
    $("cloudType").value = s.cloud.site_type || "air";
    $("cloudUrl").value = s.cloud.url || "";
    $("cloudToken").value = s.cloud.token || "";
    $("cloudSiteId").textContent = s.site_id || "-";
  }
  $("cloudToken").type = "password";   // 每次打开都回到遮蔽态
  if ($("cloudTokenEye")) $("cloudTokenEye").textContent = "👁 显示";
  $("segModal").classList.remove("hidden");
}
async function saveSeg(scan) {
  const segs = $("segText").value.split(/[\n,\s]+/).map(s => s.trim()).filter(Boolean);
  try {
    const d = await jpost("/api/segments", {
      segments: segs,
      host_start: parseInt($("segHs").value) || 1,
      host_end: parseInt($("segHe").value) || 254,
    });
    if (!d.ok) { toast("保存失败：" + (d.error || "")); return; }
    // 数字框留空/填错时不提交该项(保留原值)，别悄悄存成 0 或默认值——ARP 限速存成 0 会让扫描几乎停摆
    const num = (id) => { const v = parseInt($(id).value); return Number.isFinite(v) ? v : undefined; };
    const body = {
      scan_interval: num("setInterval"),
      max_pps: num("setPps"),
      cloud: {
        enabled: $("cloudEnabled").checked,
        site_name: $("cloudName").value.trim(),
        site_type: $("cloudType").value,
        url: $("cloudUrl").value.trim(),
        token: $("cloudToken").value.trim(),
      },
    };
    if ($("setFullInterval")) body.full_interval = num("setFullInterval");
    Object.keys(body).forEach(k => body[k] === undefined && delete body[k]);
    const d2 = await jpost("/api/settings", body);
    if (!d2.ok) { toast("保存失败：" + (d2.error || "")); return; }
    $("segModal").classList.add("hidden");
    toast(`已保存 网段/扫描/云端上报设置` + ($("cloudEnabled").checked ? "（上报已启用，即时生效）" : ""), "ok");
    if (scan) { await jpost("/api/scan?kind=full", {}); toast("已触发全网扫描", "ok"); setTimeout(pollProgress, 500); }
  } catch (e) {}
}
$("btnSeg").onclick = openSeg;
// 上报token 默认按密码框遮住（机房里投屏/旁人围观是常态），需要核对时点👁临时显示
if ($("cloudTokenEye")) $("cloudTokenEye").onclick = () => {
  const el = $("cloudToken"), show = el.type === "password";
  el.type = show ? "text" : "password";
  $("cloudTokenEye").textContent = show ? "🙈 隐藏" : "👁 显示";
};
$("segClose").onclick = $("segCancel").onclick = () => {
  $("segModal").classList.add("hidden");
  const el = $("cloudToken");   // 关窗即复位成遮蔽态，别下次打开还明晃晃亮着
  if (el) el.type = "password";
  if ($("cloudTokenEye")) $("cloudTokenEye").textContent = "👁 显示";
};
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
  speak("测试，一台矿机掉线");
  const hasVoice = ("speechSynthesis" in window);
  toast(hasVoice ? "已播放测试音。没声音的话：查电脑音量/静音、或浏览器是否给本页面静音了" :
        "你的浏览器不支持语音播报，建议用 Chrome/Edge", hasVoice ? "ok" : "fail");
};
setVoice(voiceOn, false);   // 仅刷新按钮文字，不播放（无手势）
$("mClose").onclick = () => $("modal").classList.add("hidden");
$("modal").onclick = (e) => { if (e.target.id === "modal") $("modal").classList.add("hidden"); };
let _searchTimer = null;
["search", "fStatus", "fFw", "fSeg"].forEach(id => $(id).addEventListener("input", () => {
  showAllRows = false;   // 换筛选条件后回到限量渲染，避免一直背着全量
  clearTimeout(_searchTimer);   // 搜索框每敲一个字都拉一次全表太重：停手 250ms 再查
  _searchTimer = setTimeout(refreshMiners, id === "search" ? 250 : 0);
}));

async function loadSegOptions() {
  try {
    const d = await jget("/api/segments");
    const sel = $("fSeg");
    [...sel.options].forEach(o => { if (o.value && o.value !== sel.value) o.remove(); });   // 重新登录/改网段后别重复追加
    const have = new Set([...sel.options].map(o => o.value));
    (d.segments || []).filter(s => !have.has(s)).forEach(s => {
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
let myUser = "";
let _timer = null;

function closePwd() { _pwdForced = false; $("pwdModal").classList.add("hidden"); }
// 会话失效/换人登录：先关掉上一个人的强制改密弹窗，否则它会压在新登录的页面上、× 也被隐藏
function showLogin() { stopDashboard(); closePwd(); $("loginOverlay").classList.remove("hidden"); }

// ---- 改密码：弱口令登录后弹提示，可关掉继续用；不锁权限（服务器部署首次登录多是远程） ----
let _pwdForced = false;
let _weakRemote = false;
let _svcPort = location.port || "8800";
function openPwd(force) {
  // force 仅表示"建议改"：仍可关、可操作；不再因弱口令锁死写权限
  _pwdForced = false;
  $("pwdForce").classList.toggle("hidden", !force);
  $("pwdRemote").classList.add("hidden");
  $("pwdForm").style.display = "";
  $("pwdSave").style.display = "";
  $("pwdClose").style.display = "";
  $("pwdLogout").classList.add("hidden");
  ["pwdOld", "pwdNew", "pwdNew2"].forEach(id => $(id).value = "");
  $("pwdErr").textContent = "";
  $("pwdModal").classList.remove("hidden");
  $("pwdOld").focus();
}
function afterLogin(d) {
  _weakRemote = !!d.weak_remote;
  if (d.port) _svcPort = String(d.port);
  if (d.max_batch) maxBatch = d.max_batch;
  closePwd();
  if (d.must_change) openPwd(true);   // 提示改密，可关
}
async function savePwd() {
  const old = $("pwdOld").value, nw = $("pwdNew").value;
  if (nw !== $("pwdNew2").value) { $("pwdErr").textContent = "两次输入的新密码不一致"; return; }
  let d;
  try { d = await jpost("/api/password", { old, new: nw }); } catch (e) { return; }
  if (!d.ok) {
    $("pwdErr").textContent = d.error || "修改失败";
    if (String(d.error || "").includes("已退出登录")) setTimeout(() => location.reload(), 1500);
    return;
  }
  closePwd();
  toast("密码已修改", "ok");
}
$("btnPwd").onclick = () => openPwd(false);
$("pwdClose").onclick = () => { if (!_pwdForced) closePwd(); };
$("pwdLogout").onclick = () => doLogout();
$("pwdSave").onclick = savePwd;
$("pwdNew2").addEventListener("keydown", e => { if (e.key === "Enter") savePwd(); });

function applyRole() {
  const canCtl = myRole === "ops" || myRole === "admin";
  document.body.classList.toggle("viewer", !canCtl);
  $("btnSeg").style.display = myRole === "admin" ? "" : "none";  // 网段设置仅 admin
  $("btnUpdate").style.display = myRole === "admin" ? "" : "none";  // 版本更新仅 admin
  $("btnPwd").style.display = myUser && myUser !== "anonymous" ? "" : "none";
  $("userBadge").textContent = myRole;
}

// ---- 版本更新（仅 admin）：后台定时查有没有新版本，按钮变绿提示；点开看更新内容，一键更新 ----
let _updTimer = null;
let _updInfo = null;
let _updating = false;
let _updForce = false;   // known_bad 时由用户明确点"仍然重试"

function markUpdateBtn(d) {
  const n = (d && !d.error && !d.known_bad && d.behind) || 0;
  $("btnUpdate").textContent = n ? `⬆ 有新版本(${n})` : "⬆ 版本";
  $("btnUpdate").classList.toggle("has-update", !!n);
}

async function checkUpdate(force) {
  if (myRole !== "admin") return null;
  try {
    const d = await jget("/api/update/check" + (force ? "?force=true" : ""));
    _updInfo = d; markUpdateBtn(d);
    return d;
  } catch (e) { return null; }
}

function renderUpdate(d) {
  const b = $("updBody");
  $("updApply").disabled = true;
  $("updApply").textContent = "立即更新并重启";
  _updForce = false;
  if (!d) { b.innerHTML = `<div class="upd-err">检查失败：连不上服务器</div>`; return; }
  if (d.git === false) {
    b.innerHTML = `<div class="upd-err">${esc(d.error || "本目录不是 git 仓库")}</div>
      <div class="upd-note">网页更新需要用 git clone 方式部署。</div>`;
    return;
  }
  if (d.error) {
    b.innerHTML = `<div class="upd-err">检查失败：${esc(d.error)}</div>
      <div class="upd-note">常见原因：服务器连不上 GitHub（外网/代理）、git 没装或不在 PATH。</div>`;
    return;
  }
  const when = d.checked_ts ? new Date(d.checked_ts * 1000).toLocaleString() : "-";
  let h = `<div>分支 <code>${esc(d.branch)}</code> · 当前版本 <code>${esc(d.local)}</code> · 最新 <code>${esc(d.remote)}</code></div>
    <div class="muted">检查时间 ${esc(when)}</div>`;
  if (!d.behind) {
    h += `<div class="upd-ok">✓ 已是最新版本</div>`;
  } else if (d.known_bad) {
    h += `<div class="upd-err">远端最新版本 <code>${esc(d.remote)}</code> 之前自检未通过（或启动失败）已自动回滚，
      自动更新不会再拉取它。如果原因已排除（比如缺的依赖已经 pip install 好），可以点下面「仍然重试」。</div>`;
    $("updApply").textContent = "仍然重试";
    $("updApply").disabled = false;
    _updForce = true;
  } else {
    h += `<div>有 <b>${d.behind}</b> 个新提交${d.behind > 10 ? "（下面只列最近 10 个）" : ""}：</div>
      <ul>${(d.changes || []).map(c => `<li>${esc(c)}</li>`).join("")}</ul>`;
    h += `<div class="upd-note">更新流程：拉取新代码 → 自检（不通过自动回滚，不会重启）→ 重启服务。
      重启约需十几秒，期间扫描暂停；重启后需要重新登录。配置和数据库不受影响。
      ${d.restart_mode === "self" ? "<br>当前没有守护进程（run.bat / NSSM），会由程序自己重新拉起。" : ""}</div>`;
    $("updApply").disabled = false;
  }
  b.innerHTML = h;
}

async function openUpdate() {
  $("updModal").classList.remove("hidden");
  if (_updating) return;
  $("updBody").textContent = "检查中…（需要连 GitHub，可能要几秒）";
  $("updApply").disabled = true;
  renderUpdate(await checkUpdate(true));
}

// 等服务重启：先等它下线(或最多 20 秒)，再等它回来，回来就刷新页面
async function waitRestart() {
  // 只有本服务自己的响应(200 已登录 / 401 会话已随重启失效)才算回来了：经 nginx/frp 访问时
  // 服务停着代理也会回 502/404，不能当成"已恢复"去刷新页面
  const alive = async () => {
    try { const r = await fetch("/api/me", { cache: "no-store" }); return r.status === 200 || r.status === 401; }
    catch (e) { return false; }
  };
  const t0 = Date.now();
  let wentDown = false;
  while (Date.now() - t0 < 180000) {
    await new Promise(r => setTimeout(r, 2000));
    const up = await alive();
    if (!up) { wentDown = true; continue; }
    if (wentDown || Date.now() - t0 > 20000) { location.reload(); return; }
  }
  $("updBody").innerHTML = `<div class="upd-err">3 分钟了服务还没回来，请到服务器上检查程序窗口 / logs\\miner.log。</div>`;
  _updating = false;
  startDashboard();   // 恢复轮询和失联检测：否则关掉弹窗后大屏是一张不更新、也不报警的静止画面
}

async function applyUpdate() {
  if (!_updInfo || !_updInfo.behind) return;
  if (_updForce && !confirm("这个版本之前自检失败或启动崩溃过。确定原因已经排除、要再试一次吗？\n（再失败会自动回滚）")) return;
  if (!confirm(`确认更新到最新版本（${_updInfo.behind} 个新提交）并重启服务？\n重启期间约十几秒不扫描，所有人需要重新登录。`)) return;
  _updating = true;
  $("updApply").disabled = true; $("updRecheck").disabled = true;
  $("updBody").textContent = "正在拉取新代码并自检，可能需要 1～2 分钟，请勿关闭页面…";
  let r;
  try { r = await jpost("/api/update/apply", { force: _updForce }); } catch (e) { r = null; }
  $("updRecheck").disabled = false;
  if (!r || !r.ok) {
    _updating = false;
    $("updBody").innerHTML = `<div class="upd-err">更新失败：${esc((r && (r.msg || r.error)) || "请求出错")}</div>
      <div class="upd-note">自检不通过会自动回滚，服务仍在旧版本上正常运行。</div>`;
    return;
  }
  $("updBody").innerHTML = `<div class="upd-ok">✓ 已更新 ${esc(r.from)} → ${esc(r.to)}，正在重启服务…</div>
    <div class="muted">服务回来后页面会自动刷新。</div>`;
  stopDashboard();   // 重启期间别弹"监控失联"红条/重连风暴
  waitRestart();
}

$("btnUpdate").onclick = openUpdate;
$("updClose").onclick = () => $("updModal").classList.add("hidden");
$("updRecheck").onclick = openUpdate;
$("updApply").onclick = applyUpdate;

let _ws = null;
let _wsStop = false;   // 登出/会话失效后置真，停止重连风暴
let _wsRetry = 0;      // 连续重连失败次数，连上就归零
// 指数退避 + 随机抖动：固定 5 秒重连会让服务重启后所有在线浏览器在同一秒一起敲门，
// 几十个页面同时握手把刚起来的服务再压一遍。等得越久 + 抖开，避免同步重连风暴。
function wsRetryDelay() {
  return Math.min(30000, 5000 * Math.pow(1.5, _wsRetry)) + Math.random() * 1000;
}
let _wsRetryTimer = null;   // 只允许一条重连链：重新登录前留下的旧定时器会再建一条，推送就刷两遍
function scheduleWS() {
  if (_wsStop || _wsRetryTimer) return;
  const d = wsRetryDelay(); _wsRetry++;
  _wsRetryTimer = setTimeout(() => { _wsRetryTimer = null; connectWS(); }, d);
}
function connectWS() {
  if (_wsStop) return;
  if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) return;
  try {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    _ws = ws;
    ws.onopen = () => { _wsRetry = 0; };     // 连上了，退避计数归零
    ws.onmessage = () => refreshAll();      // 扫描完成/告警 → 即时刷新
    ws.onclose = (ev) => {                   // 1008=后端判会话失效 → 弹登录并停重连
      if (_ws !== ws) return;                // 已被新连接取代的旧连接，别动全局状态
      _ws = null;
      if (ev && ev.code === 1008) { showLogin(); return; }
      if (ev && ev.code === 4403) {          // 访问地址/来源不被允许：别弹登录也别重连，靠 30 秒轮询
        toast("实时推送连接被服务器拒绝(访问地址不在允许列表)，已改为每 30 秒刷新");
        return;
      }
      scheduleWS();
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  } catch (e) {
    scheduleWS();
  }
}
function stopDashboard() {   // 会话失效/登出：停轮询与 WS 重连，避免登录页后台空转
  _wsStop = true;
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_wsRetryTimer) { clearTimeout(_wsRetryTimer); _wsRetryTimer = null; }
  if (_ws) { const w = _ws; _ws = null; try { w.close(); } catch (e) {} }
}

function startDashboard() {
  _wsStop = false;
  _wsRetry = 0;   // 重新登录 = 全新一轮连接，别背着上一轮的退避时长
  lastOkMs = Date.now();   // 登录成功，重置失联计时，避免刚进来误报
  loadSegOptions();
  refreshAll();
  connectWS();
  if (!_timer) _timer = setInterval(refreshAll, 30000);  // WS 推送为主，轮询兜底
  if (!_staleTimer) _staleTimer = setInterval(checkStale, 10000);  // 失联自检，每10秒
  if (myRole === "ops" || myRole === "admin") pollCmdProgress(true);   // 刷新页面后接上在跑的分批重启
  if (myRole === "admin") {   // 新版本提示：进来查一次，之后每 30 分钟(后端有 10 分钟缓存)
    checkUpdate(false);
    if (!_updTimer) _updTimer = setInterval(() => checkUpdate(false), 1800000);
  }
}

async function doLogin() {
  const r = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: $("loginUser").value, password: $("loginPass").value }),
  });
  const d = await r.json();
  if (!d.ok) { $("loginErr").textContent = d.error || "登录失败"; return; }
  $("loginOverlay").classList.add("hidden");
  myRole = d.role; myUser = d.user; applyRole(); startDashboard();
  afterLogin(d);
}

async function doLogout() { await fetch("/api/logout", { method: "POST" }); location.reload(); }

$("loginBtn").onclick = doLogin;
$("loginPass").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("btnLogout").onclick = doLogout;
$("btnScanContainers").onclick = async () => {
  try {
    await jpost("/api/containers/scan", {});
    toast("已触发集装箱刷新", "ok");
    setTimeout(refreshContainers, 1500);
  } catch (e) {}
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
  myRole = me.role; myUser = me.user; applyRole(); startDashboard();
  afterLogin(me);
}
init();
