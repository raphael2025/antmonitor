const $ = (id) => document.getElementById(id);
let trendChart, modalChart;
let sortKey = "hr_rt", sortOrder = "desc";
const HOT = 90;
const RENDER_CAP = 800;   // 一次最多渲染多少行：5000 行 innerHTML 重建会让低配运维机明显卡顿

function kindLabel(k) {
  const key = "kind." + (k || "");
  const v = t(key);
  return v === key ? (k || "") : v;
}
function statusLabel(s) {
  if (s === "online") return t("status.online");
  if (s === "offline") return t("status.offline");
  if (s === "unknown") return t("status.unknown");
  return s || "";
}
function uiLocale() {
  const lang = window.I18N && I18N.lang;
  return lang === "zh" ? "zh-CN" : (lang || undefined);
}

function shortWorkerName(name) {
  const chars = [...String(name || "")];
  return chars.length > 11
    ? `${chars.slice(0, 5).join("")}…${chars.slice(-5).join("")}`
    : chars.join("");
}

function alertDetail(x) {
  // Alert details are stored as Chinese prose. Rebuild them from the alert type
  // and the diagnostic values so active and historical rows use the UI locale.
  const d = String(x.detail || "");
  const value = (pattern) => d.match(pattern);
  if (x.type === "offline") {
    const m = value(/重启后\s*(\d+)\s*分钟/);
    if (m) return t("alert.offline_reboot", { minutes: m[1] });
    const u = value(/持续无应答\s*(\d+)\s*轮/);
    return u ? t("alert.offline_unresponsive", { rounds: u[1] }) : t("alert.offline");
  }
  if (x.type === "zero") return t("alert.zero");
  if (x.type === "reboot_limit") {
    const m = value(/已成功重启\s*(\d+)\s*次\(上限\s*(\d+)\)/);
    return m ? t("alert.reboot_limit", { count: m[1], max: m[2] })
      : t("alert.reboot_limit_short");
  }
  if (x.type === "reject") {
    const m = value(/拒绝率\s*([\d.]+)%\s*≥\s*([\d.]+)%/);
    return m ? t("alert.reject", { rate: m[1], threshold: m[2] }) : t("alert.reject_short");
  }
  if (x.type === "low_hashrate") {
    const m = value(/算力\s*([\d.]+)\s*TH.*?中位数\s*([\d.]+)\s*TH.*?(\d+)%/);
    return m ? t("alert.low_hashrate", { rate: m[1], median: m[2], ratio: m[3] })
      : t("alert.low_hashrate_short");
  }
  if (x.type === "overheat") {
    const m = value(/芯片温\s*([\d.]+)℃\s*≥\s*([\d.]+)℃/);
    return m ? t("alert.overheat", { temp: m[1], threshold: m[2] }) : t("alert.overheat_short");
  }
  if (x.type === "pool_hijack") {
    const pools = d.match(/偷算力\):\s*(.*)$/);
    return pools ? t("alert.pool_hijack", { pools: pools[1] }) : t("alert.pool_hijack_short");
  }
  if (x.type === "segment_down") {
    const m = value(/大面积掉线\s*(\d+)\/(\d+)/);
    return m ? t("alert.segment_down", { offline: m[1], total: m[2] })
      : t("alert.segment_down_short");
  }
  if (x.type === "site_down") {
    const m = value(/大面积掉线\s*(\d+)\/(\d+)/);
    return m ? t("alert.site_down", { offline: m[1], total: m[2] }) : t("alert.site_down_short");
  }
  if (x.type === "stalled") {
    const m = value(/已\s*(\d+)\s*分钟/);
    return m ? t("alert.stalled", { minutes: m[1] }) : t("alert.stalled_short");
  }
  if (x.type === "cooler_offline") return t("alert.cooler_offline");
  if (x.type && x.type.startsWith("cooler:")) {
    const flag = x.type.slice("cooler:".length);
    const fault = flag.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    if (["supply_pressure_low", "return_pressure_low_th", "return_liquid_pressure_low"].includes(flag)) {
      const m = value(/([\d.]+)\s*<\s*([\d.]+)\s*MPa/i);
      return m ? t("alert.cooler_pressure", { fault, value: m[1], threshold: m[2] })
        : t("alert.cooler_pressure_short", { fault });
    }
    return t("alert.cooler_fault", { fault });
  }
  return x.detail || "";
}

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
let _scanRunning = false, _scanProgressTs = 0;

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
    const why = j.error || j.detail || t("err.forbidden");
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
    cls = "lost"; msg = t("stale.disconnected", { n: noContact });
  } else if (lastScanTs && scanAge > staleAfter &&
      !(_scanRunning && _scanProgressTs > 0 && now / 1000 - _scanProgressTs <= staleAfter)) {
    cls = "stale"; msg = t("stale.scan_old", { n: Math.round(scanAge / 60) });
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

/** 列表展示：基础名.后缀。统计归组仍只用 worker 基础名；与 MAC 列无关。 */
function workerLabel(r) {
  const base = String((r && r.worker) || "").trim();
  if (!base) return "";
  const suffix = String((r && r.worker_suffix) || "").trim();
  return suffix ? `${base}.${suffix}` : base;
}

function fmtTime(ts) {
  if (!ts) return t("header.last_scan_none");
  return new Date(ts * 1000).toLocaleString(uiLocale(), { hour12: false });
}
function ago(ts) {
  if (!ts) return "";
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return t("time.sec_ago", { n: s });
  if (s < 3600) return t("time.min_ago", { n: Math.floor(s / 60) });
  return t("time.hour_ago", { n: Math.floor(s / 3600) });
}
function fmtPower(w) {   // 矿机/配电原始读数为 W；界面固定用 kW 表示当前功率。
  const v = AntDataQuality.number(w);
  return v == null ? "-" : (v / 1000).toLocaleString(undefined, { maximumFractionDigits: 3 }) + " kW";
}
function fmtEnergy(kwh) {   // 累计电量固定用 kWh，避免与当前负荷混淆。
  const v = AntDataQuality.number(kwh);
  return v == null ? "-" : v.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " kWh";
}
function boxPower(c) {   // 集装箱总功耗 = 两路配电之和(W)
  const p1 = AntDataQuality.number(c.power1), p2 = AntDataQuality.number(c.power2);
  return p1 == null || p2 == null ? null : p1 + p2;
}
function fmtDT(ts) {   // 告警时间戳: MM-DD HH:MM
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString(uiLocale(), { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function fmtUptime(sec) {
  if (sec == null) return "-";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return t("time.day_h", { d, h });
  if (h) return t("time.h_m", { h, m });
  return t("time.m_only", { m });
}
const KIND_LABEL = { get full(){return t("kind.full");}, get quick(){return t("kind.quick");}, get manual(){return t("kind.manual");}, get inventory(){return t("kind.inventory");} };

function currentMetric(id, full, known, complete, count, total) {
  const value = AntDataQuality.number(full) ?? AntDataQuality.number(known);
  $(id).textContent = value == null ? "—" : AntDataQuality.short(value);
  const measured = AntDataQuality.number(count), machines = AntDataQuality.number(total);
  $(id).title = complete === false && value != null ? (measured != null && machines != null
    ? t("power.coverage", { known: measured, total: machines }) : t("power.partial")) : "";
}

let _summarySeq = 0;

async function refreshSummary() {
  const seq = ++_summarySeq;
  const s = await jget("/api/summary");
  if (seq !== _summarySeq) return;
  updateProgress(s.progress);
  if (s.scan_ts) lastScanTs = s.scan_ts;   // 记录服务器最近完成扫描时间(失联自检用)
  if (s.stale_after) staleAfter = s.stale_after;
  if (!s.scanned) {
    lastScanTs = 0;
    $("lastScan").textContent = t("scan.none_wait");
    ["cOnline", "cTotalHr", "cAvgHr", "cOffline", "cAlerts", "cFw", "cPower", "cEff", "cContainers"].forEach(id => { $(id).textContent = "—"; });
    ["cOnline", "cPower", "cEff"].forEach(id => { $(id).title = ""; });
    return;
  }
  $("cOnline").textContent = `${s.online} / ${s.total}`;
  $("cOnline").title = s.unknown ? t("quality.unknown_count", { n: s.unknown }) : "";
  $("cTotalHr").innerHTML = AntDataQuality.partial(s.total_hashrate_th, s.known_hashrate_ths, s.hashrate_complete, t, n => fmtHash(n, "/s"));
  $("cAvgHr").textContent = fmtHash(s.avg_hashrate_th);
  $("cAvgHr").title = t("quality.avg_machine_help");
  $("cOffline").textContent = s.offline;
  $("cAlerts").textContent = s.active_alerts;
  const fw = s.by_firmware || {};
  $("cFw").textContent = `${fw.stock || 0} / ${fw.uniplus || 0}`;
  currentMetric("cPower", s.total_power_kw, s.known_power_kw,
    s.power_complete, s.power_known_machines, s.total);
  currentMetric("cEff", s.avg_efficiency, s.known_efficiency,
    s.efficiency_complete, s.efficiency_known_machines, s.total);
  $("cContainers").innerHTML = `${s.containers || 0}`
    + ((s.containers_faulty || s.containers_offline) ? ` (${(s.containers_faulty || 0) + (s.containers_offline || 0)})` : "")
    + (s.containers_unknown ? AntDataQuality.note(t("quality.unknown_count", { n: s.containers_unknown }), true) : "");
  $("lastScan").textContent = t("scan.last", { ago: ago(s.scan_ts), kind: KIND_LABEL[s.scan_kind] || s.scan_kind });
}

async function ackAlert(id) {
  try { await jpost("/api/alerts/ack", { id }); refreshAlerts(); } catch (e) {}
}
// 告警里直接下架坏机器：从名册移除 + 清掉它的告警(不再探测/告警)
async function removeFromAlert(ip) {
  if (!confirm(t("toast.remove_confirm", { ip }))) return;
  try {
    const d = await jpost("/api/machine-state", { ips: [ip], action: "remove" });
    if (!d.ok) { toast(t("toast.remove_fail", { msg: d.error || "" })); return; }
    toast(t("toast.removed", { ip }), "ok");
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
function consoleClick(ip) {
  if (!/^[0-9.]+$/.test(String(ip || "")) || String(ip).split(".").length !== 4) return "";
  return `openMinerConsole('${esc(ip)}'); event.stopPropagation();`;
}
async function openMinerConsole(ip) {
  try {
    const d = await jget(`/api/miner/${encodeURIComponent(ip)}/console`);
    if (!d || d.ok === false) {
      toast(t("toast.console_fail", { msg: (d && d.error) || "" }));
      return;
    }
    if (d.kind === "uniplus" && d.bridge_path) {
      window.open(d.bridge_path, "_blank", "noopener");
      if (d.password) {
        try { await navigator.clipboard.writeText(d.password); } catch (e) {}
        toast(t("toast.console_uniplus", { pwd: d.password }), "ok");
      }
      return;
    }
    if (d.kind === "stock" && d.open_url) {
      window.open(d.open_url, "_blank", "noopener");
      toast(t("toast.console_stock", { user: d.username || "root", pwd: d.password || "root" }), "ok");
      return;
    }
    window.open((d && d.open_url) || `http://${ip}/`, "_blank", "noopener");
    toast(t("toast.console_unknown"), "ok");
  } catch (e) {
    toast(t("toast.console_fail", { msg: "" }));
  }
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
    const worker = isMiner && x.worker
      ? `<span class="aworker" title="${esc(x.worker)}"><b>${esc(t("alert.worker"))}:</b> ${esc(shortWorkerName(x.worker))}</span>`
      : "";
    let tail = "";
    if (!isCooler) {   // 集装箱告警不给按钮(修好自动消失)
      tail = x.ack_by ? `<span class="acked">${esc(t("ack.done", { user: x.ack_by }))}</span>`
                      : `<button class="ackbtn" onclick="ackAlert(${x.id})">${esc(t("ack.confirm"))}</button>`;
      if (isMiner) tail += `<button class="rmbtn" onclick="removeFromAlert('${esc(x.ip)}')" title="${esc(t("alert.remove_title"))}">${esc(t("alert.remove_btn"))}</button>`;
    }
    const cons = isMiner ? consoleClick(x.ip) : "";
    return `<div class="a ${esc(x.severity)}${x.ack_by ? " is-ack" : ""}"><span class="atime">${fmtDT(x.ts)} · ${ago(x.ts)}</span>`
      + `<span class="ip"${click ? ` onclick="${click}"` : ""}>${esc(x.ip)}</span>`
      + (cons ? `<button type="button" class="console-link" onclick="${cons}" title="${esc(t("alert.open_console"))}">↗</button>` : "")
      + worker + `<span class="adetail">${esc(alertDetail(x))}</span>${tail}</div>`;
  }).join("") : `<div class="muted">${esc(t("alerts.none"))}</div>`)
    + (total > a.length ? `<div class="muted" style="text-align:center;padding:6px">${esc(t("alert.more", { shown: a.length, total }))}</div>` : "");
  document.title = total ? t("doc.title_alert", { n: total }) : t("doc.title");
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
    u.lang = (window.I18N && I18N.lang === "zh") ? "zh-CN" : (window.I18N ? I18N.lang : "zh-CN"); u.rate = 1; u.volume = 1;
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
  if (n("pool_hijack")) parts.push(t("voice.pool_hijack", { n: n("pool_hijack") }));
  if (n("stalled")) parts.push(t("voice.stalled"));
  if (n("site_down")) parts.push(t("voice.site_down"));
  if (n("segment_down")) parts.push(t("voice.segment_down", { n: n("segment_down") }));
  if (n("offline")) parts.push(t("voice.offline", { n: n("offline") }));
  if (n("zero")) parts.push(t("voice.zero", { n: n("zero") }));
  if (n("reboot_limit")) parts.push(t("voice.reboot_limit", { n: n("reboot_limit") }));
  if (n("low_hashrate")) parts.push(t("voice.low_hr", { n: n("low_hashrate") }));
  if (n("overheat")) parts.push(t("voice.overheat", { n: n("overheat") }));
  if (n("reject")) parts.push(t("voice.reject", { n: n("reject") }));
  if (cooler) parts.push(t("voice.cooler", { n: cooler }));
  if (!parts.length) return;
  beep(880, 0.3); beep(660, 0.3);
  speak(t("voice.prefix") + parts.join("，"));
  notify(t("voice.notify_title"), parts.join(" / "));
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
      if (recovered.length) { beep(523, 0.15); speak(t("voice.recovered", { n: recovered.length })); }
    }
  }
  prevActiveIds = curIds;
}

function setVoice(on, gesture) {
  voiceOn = on;
  localStorage.setItem("voiceOn", on ? "1" : "0");
  const b = $("btnVoice");
  b.textContent = on ? t("header.voice_on") : t("header.voice_off");
  b.classList.toggle("primary", on);
  if (on && gesture) {                       // 用户手势：解锁音频 + 申请通知权限
    beep(660, 0.15); speak(t("voice.enabled"));
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
  data = data.filter(v => v != null && Number.isFinite(v));
  if (!data.length) return {};
  const lo = Math.min(...data), hi = Math.max(...data);
  const mid = (lo + hi) / 2;
  const span = Math.max(hi - lo, (Math.abs(mid) || 1) * 0.08);
  const pad = span * 0.15;
  return { min: Math.max(0, mid - span / 2 - pad), max: mid + span / 2 + pad };
}

async function refreshTrend() {
  const d = await jget("/api/trend?points=288");
  const pts = d.trend || [];
  const measurements = pts.map(p => {
    const full = p.data_quality === "legacy" ? null : AntDataQuality.number(p.total_hr);
    const known = AntDataQuality.number(p.known_total_hr);
    const count = AntDataQuality.number(p.hashrate_known_machines), total = AntDataQuality.number(p.total);
    const partial = p.data_quality !== "legacy" && full == null && known != null &&
      Number.isInteger(count) && Number.isInteger(total) && count > 0 && count < total;
    return { x: p.ts, y: full ?? (partial ? known : null), partial,
      known: count, total };
  });
  const values = measurements.map(p => p.y).filter(v => v != null);
  const { div, unit } = hashUnit(values.length ? Math.max(...values) : 0);
  const observed = [];
  measurements.forEach((p, i) => {
    if (i && p.x - measurements[i - 1].x > 900) {
      observed.push({ x: measurements[i - 1].x + 900, y: null, partial: false });
    }
    observed.push({ ...p, y: p.y == null ? null : p.y / div });
  });
  const data = observed.map(p => ({ ...p, y: p.partial ? null : p.y }));
  const hasPartial = observed.some(p => p.partial);
  const isolatedRadius = points => c => points[c.dataIndex]?.y != null &&
    points[c.dataIndex - 1]?.y == null && points[c.dataIndex + 1]?.y == null ? 2 : 0;
  const datasets = [{ label: t("card.total_hr"), data, borderColor: "#1f6feb", backgroundColor: "rgba(31,111,235,.1)", fill: true, tension: 0, pointRadius: isolatedRadius(data), borderWidth: 2 }];
  if (hasPartial) datasets.push({ label: t("trend.partial"), data: observed, trendPartial: true,
    borderColor: "#d97706", backgroundColor: "#d97706", borderDash: [5, 4], fill: false,
    tension: 0, borderWidth: 2, pointRadius: c => c.raw?.partial ? 2 : 0,
    pointHoverRadius: c => c.raw?.partial ? 4 : 0,
    // Complete samples anchor adjacent partial segments. Their own segments stay
    // transparent and their tooltip rows are filtered, so totals are drawn once.
    segment: { borderColor: c => observed[c.p0DataIndex]?.partial || observed[c.p1DataIndex]?.partial
      ? "#d97706" : "transparent" } });
  const yScale = trendYScale(observed.map(p => p.y));
  const tt = $("trendTitle"); if (tt) tt.textContent = t("trend.title_u", { unit: unit + "/s" });
  if (!window.Chart || !$("trendChart")) return;
  const tick = ts => new Date(ts * 1000).toLocaleTimeString(uiLocale(), { hour12: false });
  const tooltipFilter = c => !c.dataset.trendPartial || c.raw?.partial === true;
  const tooltip = c => {
    const value = c.parsed.y == null ? "—" : AntDataQuality.short(c.parsed.y) + " " + unit + "/s";
    return c.dataset.trendPartial ? [t("trend.partial") + ": " + value,
      t("power.coverage", { known: c.raw.known, total: c.raw.total }), t("trend.partial_tooltip")] : value;
  };
  if (!trendChart) {
    trendChart = new Chart($("trendChart"), {
      type: "line", data: { datasets },
      options: {
        spanGaps: false, animation: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        plugins: { legend: { display: hasPartial }, tooltip: { filter: tooltipFilter, callbacks: {
          title: items => items.length ? new Date(items[0].parsed.x * 1000).toLocaleString(uiLocale()) : "",
          label: tooltip } } },
        scales: {
          x: { type: "linear", ticks: { color: "#6e7681", maxTicksLimit: 8, callback: tick } },
          y: { ticks: { color: "#6e7681" }, min: yScale.min, max: yScale.max }
        }
      }
    });
  } else {
    trendChart.data.datasets = datasets;
    trendChart.options.plugins.legend.display = hasPartial;
    trendChart.options.plugins.tooltip.filter = tooltipFilter;
    trendChart.options.plugins.tooltip.callbacks.label = tooltip;
    trendChart.options.scales.x.ticks.callback = tick;
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
  if (r.hr_rt == null) return `<td class="muted" title="${esc(t("td.no_hr_title"))}">${esc(t("td.no_hr"))}</td>`;
  return `<td class="${r.hr_rt === 0 ? "low" : ""}">${fmtHash(r.hr_rt)}</td>`;
}
function tempCell(r) {
  if (r.temp == null) return `<td>-</td>`;
  return `<td class="${r.temp >= HOT ? "hot" : ""}">${safeVal(r.temp)}℃</td>`;
}
const REJECT_HI = 5;
function rejCell(r) {
  const accepted = AntDataQuality.number(r.accepted), rejected = AntDataQuality.number(r.rejected);
  if (accepted == null || rejected == null) return `<td>-</td>`;
  const tot = accepted + rejected;
  if (tot === 0) return `<td>0%</td>`;
  const p = Math.round(rejected / tot * 1000) / 10;
  return `<td class="${p >= REJECT_HI ? "hot" : ""}">${p}%</td>`;
}

let lastMiners = [];            // 当前筛选结果全集（选择/命令都基于它，不受渲染上限影响）
const selected = new Set();     // 选中的 IP
let selectionLocked = false;  // 锁定勾选：换筛选也不收敛；自动刷新本就不收敛
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
  // 以前会被悄悄移出选择、实际只下发 270 台且没有任何提示。
  // 「锁定勾选」时换筛选也不收敛——准备大批量时先锁住再核对。
  const filterKey = [st, fw, seg, q].join("|");
  if (filterKey !== _lastFilterKey) {
    if (!selectionLocked) {
      const visible = new Set(lastMiners.map(m => m.ip));
      for (const ip of [...selected]) if (!visible.has(ip)) selected.delete(ip);
    }
    _lastFilterKey = filterKey;
  }
  $("minerCount").textContent = d.count != null ? d.count : lastMiners.length;
  const a = d.agg || {};
  const power = fmtPower(a.total_power ?? a.known_power);
  $("segStat").title = "";
  if (a.power_complete === false && AntDataQuality.number(a.known_power) != null) {
    const known = AntDataQuality.number(a.power_known_machines);
    $("segStat").title = known == null ? t("power.partial")
      : t("power.coverage", { known, total: d.count ?? lastMiners.length });
  }
  $("segStat").textContent = t("seg.stat", { seg: seg ? seg + ".x ｜ " : "", online: a.online || 0, hr: fmtHash(a.total_hr), pw: power });

  const rows = showAllRows ? lastMiners : lastMiners.slice(0, RENDER_CAP);
  $("minerBody").innerHTML = rows.map(r => {
    const ck = selected.has(r.ip) ? "checked" : "";
    return `<tr class="${rowClass(r)} ${selected.has(r.ip) ? "sel" : ""}">`
      + `<td class="cbcol"><input type="checkbox" data-ip="${esc(r.ip)}" ${ck}></td>`
      + `<td class="ip-link"><span onclick="openMiner('${esc(r.ip)}')">${esc(r.ip)}</span>`
      + `<button type="button" class="console-link" onclick="${consoleClick(r.ip)}" title="${esc(t("alert.open_console"))}">↗</button></td>`
      + `<td>${r.mstate === "repair" ? `<span class="pill repair">${esc(t("status.repair"))}</span>`
        : r.mstate === "powercut" ? `<span class="pill powercut">${esc(t("status.powercut"))}</span>`
        : `<span class="pill ${esc(r.status)}" title="${r.status === "unknown" ? esc(t("pill.unknown_title")) : ""}">${esc(statusLabel(r.status))}</span>`}</td>`
      + `<td>${r.firmware ? `<span class="pill ${esc(r.firmware)}">${esc(r.firmware)}</span>` : "-"}</td>`
      + `<td>${esc(r.model) || "-"}</td>`
      + hrCell(r) + `<td>${r.hr_avg != null ? fmtHash(r.hr_avg) : "-"}</td>`
      + `<td>${fmtPower(r.power)}</td>`
      + `<td>${r.eff != null ? esc(r.eff) : "-"}</td>`
      + rejCell(r)
      + tempCell(r)
      + `<td>${fmtUptime(r.uptime)}</td>`
      + `<td title="${esc(r.worker || "")}">${esc(workerLabel(r)) || "-"}</td>`
      + `<td>${esc(r.sn) || "-"}</td>`
      + `<td class="mac-col">${esc(r.mac) || "-"}</td>`
      + `<td class="muted">${esc(r.note)}</td></tr>`;
  }).join("");
  const more = $("renderNote");
  if (more) {
    if (!showAllRows && lastMiners.length > RENDER_CAP) {
      more.innerHTML = `${t("render.cap", { cap: RENDER_CAP, total: lastMiners.length }).replace(String(RENDER_CAP), `<b>${RENDER_CAP}</b>`)}`
          + ` <button id="btnShowAll" class="mini-btn">${esc(t("render.show_all"))}</button>`;
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
  let text = hidden
    ? t("cmd.selected_hidden", { n: selected.size, h: hidden })
    : t("cmd.selected", { n: selected.size });
  if (selectionLocked) text = t("cmd.locked_prefix") + text;
  $("selCount").textContent = text;
  const btn = $("btnLockSel");
  if (btn) {
    btn.classList.toggle("active", selectionLocked);
    btn.textContent = selectionLocked ? t("cmd.unlocksel") : t("cmd.locksel");
  }
  const all = $("cbAll");   // 全选框跟随当前结果集同步
  if (all) all.checked = lastMiners.length > 0 && lastMiners.every(m => selected.has(m.ip));
}

/** 勾选范围摘要：网段数 / 矿工名数，用于确认框顶部防误操作。 */
function selectionScope(ips) {
  const byIp = new Map(lastMiners.map(m => [m.ip, m]));
  const segs = new Set(), workers = new Set();
  let offline = 0, unknownWorker = 0;
  for (const ip of ips) {
    const m = byIp.get(ip);
    const parts = String(ip).split(".");
    if (parts.length === 4) segs.add(parts.slice(0, 3).join("."));
    if (!m) { unknownWorker++; continue; }
    if (m.status && m.status !== "online") offline++;
    const w = (m.worker || "").trim();
    if (w) workers.add(w); else unknownWorker++;
  }
  return { segs: [...segs], workers: [...workers], offline, unknownWorker };
}

function scopeSummaryHtml(scope) {
  let html = `<div class="muted" style="margin:6px 0">${esc(t("cmd.scope_summary", {
    segs: scope.segs.length, workers: scope.workers.length, offline: scope.offline,
  }))}</div>`;
  if (scope.workers.length >= 2)
    html += `<div class="warn-box" style="border-width:2px">${esc(t("cmd.cross_customer", {
      n: scope.workers.length,
      names: scope.workers.slice(0, 8).join(", ") + (scope.workers.length > 8 ? " …" : ""),
    }))}</div>`;
  return html;
}

/* ---------------- 货架视图 ---------------- */
let rackMode = false;
async function refreshRacks() {
  if (!rackMode) return;
  const d = await jget("/api/racks");
  const legend = `<div class="legend" style="margin-bottom:12px">`
    + `<span><i class="dot slot ok"></i>${esc(t("rack.legend_extra"))}</span><span><i class="dot slot zero"></i>${esc(t("rack.legend_zero"))}</span>`
    + `<span><i class="dot slot offline"></i>${esc(t("rack.legend_off"))}</span><span><i class="dot slot empty"></i>${esc(t("rack.legend_empty"))}</span>`
    + `<span><i class="dot slot unknown"></i>${esc(t("rack.legend_unk"))}</span>`
    + `<span class="muted">${esc(t("rack.legend_hint"))}</span></div>`;
  $("rackList").innerHTML = legend + (d.racks || []).map(rk => {
    const slots = rk.slots.map(s => {
      if (s.st === "empty")   // 空机位：灰、不可点
        return `<div class="slot empty" title="${esc(t("rack.empty_title", { ip: s.ip }))}">${s.h}</div>`;
      if (s.st === "unknown")   // 本轮未探测：不是离线，别吓人
        return `<div class="slot unknown" title="${esc(t("rack.unk_title", { ip: s.ip }))}" onclick="openMiner('${esc(s.ip)}')">${s.h}</div>`;
      const tip = `${s.ip}${s.hr != null ? " · " + fmtHash(s.hr) : ""}${s.temp != null ? " · " + s.temp + "℃" : ""}`;
      return `<div class="slot ${s.st}" title="${esc(tip)}" onclick="openMiner('${esc(s.ip)}')">${s.h}</div>`;
    }).join("");
    const bad = rk.abnormal ? t("rack.zero_bad", { n: rk.abnormal }).replace(String(rk.abnormal), `<b class="bad">${rk.abnormal}</b>`) : "";
    const title = rk.label
      ? `<span class="rack-label">${esc(rk.label)}</span><span class="muted rack-seg">(${esc(rk.name)}.x)</span>`
      : `${esc(rk.name)}.x`;
    return `<div class="rack"><div class="rack-head"><span class="name">${title}</span>`
      + `<span class="stat">${t("rack.stat", { online: rk.online, hr: fmtHash(rk.hashrate), bad, offline: rk.offline, empty: rk.empty })}</span></div>`
      + `<div class="slots">${slots}</div></div>`;
  }).join("");
}

let _workerSeq = 0;
async function refreshWorkers() {
  if (view !== "worker") return;
  const seq = ++_workerSeq;
  const period = parseInt($("workerPeriod").value) || 0;
  $("btnExportCsv").classList.toggle("hidden", period === 0);   // 仅周期报表可导出CSV
  const d = await jget(period === 0 ? "/api/workers" : `/api/reports/customers?hours=${period}`);
  if (seq !== _workerSeq || view !== "worker" || period !== (parseInt($("workerPeriod").value) || 0)) return;
  if (period === 0) {
    $("workerList").innerHTML = (d.workers || []).map(w => {
      const models = Object.entries(w.models).sort((a, b) => b[1] - a[1])
        .map(([m, c]) => `<div class="wmodel">${esc(t("worker.models", { m, c }).replace(String(c), "") )}<b>${c}</b></div>`).join("");
      return `<div class="worker"><div class="worker-head">`
        + `<span class="wname">${esc(w.worker)}</span>`
        + `<span class="wstat">${t("worker.stat", { n: w.total, hr: fmtHash(w.hashrate, "/s") })}</span></div>`
        + (w.hashrate_complete === false && w.known_hashrate_ths != null ? `<div class="muted">${esc(t("quality.known_part", { value: fmtHash(w.known_hashrate_ths, "/s") }))}</div>` : "")
        + `<div class="wmodels">${models}</div></div>`;
    }).join("") || `<div class="muted">${esc(t("worker.none"))}</div>`;
  } else {
    // 周期超过可用数据被截断时给出提示
    const note = d.truncated
      ? `<div class="muted" style="margin-bottom:8px">${esc(t("worker.cover_warn", { d: (d.covered_hours / 24).toFixed(1) }))}</div>`
      : "";
    $("workerList").innerHTML = note + ((d.customers || []).map(c => {
      return `<div class="worker"><div class="worker-head">`
        + `<span class="wname">${esc(c.worker)}</span>`
        + `<span class="wstat">${AntCustomerRates.machines(c, t)} ${esc(t("rate.machines_unit"))}</span></div>`
        + `<div class="wmodels">`
        + `<div class="worker-rates"><div><span>${esc(t("rate.current"))}</span>${AntCustomerRates.cell(c, "current", t, fmtHash, rateAgo)}</div>`
        + `<div><span>${esc(AntCustomerRates.header([c], t))}</span>${AntCustomerRates.cell(c, "average", t, fmtHash, rateAgo)}</div></div>`
        + `${esc(t("worker.uptime"))}${AntDataQuality.billing(c, "uptime", t, n => AntDataQuality.short(n) + "%")}`
        + `${esc(t("worker.kwh"))}${AntDataQuality.billing(c, "power", t, fmtEnergy)}`
        + `<details class="worker-billing"><summary>${esc(t("rate.billing"))}</summary>${esc(t("worker.delivered"))}${AntDataQuality.billing(c, "delivery", t, fmtHashH)}</details>`
        + (c.period_end_ts ? `<div class="muted">${esc(t("quality.period_end", { time: fmtTime(c.period_end_ts) }))}</div>` : "")
        + `</div></div>`;
    }).join("") || `<div class="muted">${esc(t("worker.none_period"))}</div>`);
  }
}

function rateAgo(seconds) {
  if (seconds < 60) return t("time.sec_ago", { n: Math.floor(seconds) });
  if (seconds < 3600) return t("time.min_ago", { n: Math.floor(seconds / 60) });
  return t("time.hour_ago", { n: Math.floor(seconds / 3600) });
}

let view = "list";   // list | rack | worker
function setView(v) {
  _workerSeq++;   // 离开视图也作废仍在途的报表请求。
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
  _scanRunning = p.running === true;
  _scanProgressTs = Number(p.progress_ts || p.started) || 0;
  const bar = $("progressBar"), btnF = $("btnFull"), btnQ = $("btnQuick");
  if (p.running) {
    bar.classList.remove("hidden");
    const pct = p.total ? Math.floor(p.done / p.total * 100) : 0;
    $("progFill").style.width = pct + "%";
    $("progText").textContent = t("prog.scanning", { kind: KIND_LABEL[p.kind] || p.kind, done: p.done, total: p.total, pct });
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
      { label: t("chart.hr"), data: hr, borderColor: "#1f6feb", yAxisID: "y", pointRadius: 0, tension: .3, spanGaps: false },
      { label: t("chart.temp"), data: temp, borderColor: "#f85149", yAxisID: "y1", pointRadius: 0, tension: .3, spanGaps: false } ] },
    options: {
      interaction: { mode: "index", intersect: false },
      scales: {
        y: { position: "left", ticks: { color: "#6e7681" } },
        y1: { position: "right", ticks: { color: "#6e7681" }, grid: { drawOnChartArea: false } },
        x: { type: "linear",
             ticks: { color: "#6e7681", maxTicksLimit: 7,
                      callback: (v) => new Date(v).toLocaleTimeString(uiLocale(), { hour12: false, hour: "2-digit", minute: "2-digit" }) } } },
      plugins: { legend: { labels: { color: "#adbac7" } },
                 tooltip: { callbacks: { title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleString(uiLocale(), { hour12: false }) : "" } } } }
  });
  $("mInfo").innerHTML =
    t("miner.detail_fw", { fw: esc(c.firmware) || "-", st: esc(c.status) || "-", sn: esc(c.sn) || "-" }) + "<br>"
    + t("miner.detail_mac", { mac: esc(c.mac) || "-" }) + "<br>"
    + t("miner.detail_hr", { hr: c.hr_rt == null ? t("td.no_hr") : fmtHash(c.hr_rt), avg: fmtHash(c.hr_avg), pw: fmtPower(c.power) }) + "<br>"
    + t("miner.detail_eff", { eff: safeVal(c.eff), temp: safeVal(c.temp), up: fmtUptime(c.uptime) }) + "<br>"
    + t("miner.detail_worker", { w: esc(workerLabel(c)) || "-", a: safeVal(c.accepted), r: safeVal(c.rejected), s: safeVal(c.stale) })
    + (c.note ? "<br>" + t("miner.detail_note", { n: esc(c.note) }) : "");
  const btn = $("mOpenConsole");
  if (btn) {
    btn.classList.remove("hidden");
    btn.textContent = btn.dataset.defaultLabel || "网页";
    btn.title = t("alert.open_console");
    btn.onclick = () => openMinerConsole(ip);
  }
  const rm = $("mRemoveContainer");
  if (rm) { rm.classList.add("hidden"); rm.onclick = null; }
  $("modal").classList.remove("hidden");
}

async function triggerScan(kind) {
  try {
    const d = await jpost(`/api/scan?kind=${kind}`, {});
    if (d && d.started === false) toast(t("toast.scan_busy"));
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
const CBOX_PUMP_KEYS = {
  "循环泵": "cbox.pump_circulating", "喷淋泵": "cbox.pump_spray",
  "风扇1": "cbox.pump_fan1", "风扇2": "cbox.pump_fan2",
  "塔风扇1": "cbox.pump_tower_fan1", "塔风扇2": "cbox.pump_tower_fan2",
  "塔风扇3": "cbox.pump_tower_fan3",
};

function containerPumpLabel(name) {
  const key = CBOX_PUMP_KEYS[name];
  if (key) return t(key);
  return /[\u3400-\u9fff]/.test(name) ? t("cbox.unknown_device") : name;
}

function containerFaultLabel(fault) {
  const key = `cbox.fault.${fault.flag}`;
  const translated = t(key);
  if (translated !== key) return translated;
  const readable = String(fault.flag || "fault").replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return t("cbox.fault_generic", { fault: readable });
}

function containerQuality(c) {
  return c.data_status === "unknown" || c.data_stale || (c.online && c.data_complete !== 1 && c.data_complete !== true);
}

function containerFaultBadge(f, ignored = false) {
  return `<span class="fbadge ${ignored ? "info" : esc(f.sev)}">${esc(containerFaultLabel(f))}`
    + (f.observed === false ? ` · ${esc(t("quality.last_fault", { time: f.measured_ts ? fmtTime(f.measured_ts) : t("rate.time_unknown") }))}` : "") + `</span>`;
}

function containerRemoveBtn(ip) {
  return `<button type="button" class="rmbtn" onclick="event.stopPropagation(); removeContainer('${esc(ip)}')" `
    + `title="${esc(t("cbox.remove_title"))}">${esc(t("cbox.remove_btn"))}</button>`;
}

async function removeContainer(ip) {
  if (!/^[0-9.]+$/.test(String(ip || "")) || String(ip).split(".").length !== 4) return;
  if (!confirm(t("cbox.remove_confirm", { ip }))) return;
  try {
    const d = await jpost("/api/containers/remove", { ips: [ip] });
    if (!d || d.ok === false) {
      toast(t("cbox.remove_fail", { msg: (d && d.error) || "" }));
      return;
    }
    toast(t("cbox.removed", { ip }), "ok");
    $("modal").classList.add("hidden");
    refreshContainers();
    refreshAlerts();
    refreshSummary();
  } catch (e) {}
}

async function refreshContainers() {
  const d = await jget("/api/containers");
  const cs = d.containers || [];
  const ign = new Set(d.faults_ignore || []);
  const spMin = d.supply_pressure_min || 0, rpMin = d.return_pressure_min || 0;
  $("containerPanel").style.display = cs.length ? "" : "none";
  $("containerCount").textContent = `${cs.length}`
    + (d.faulty ? t("cbox.faulty", { n: d.faulty }) : "") + (d.offline ? t("cbox.offline", { n: d.offline }) : "")
    + (d.unknown ? " · " + t("quality.unknown_count", { n: d.unknown }) : "");
  $("containerList").innerHTML = cs.map(c => {
    const headName = c.label
      ? `<span class="cbox-title"><span class="cbox-label">${esc(c.label)}</span>`
        + `<span class="cbox-ip">${esc(c.ip)}</span></span>`
      : `<span class="cbox-ip cbox-ip-solo">${esc(c.ip)}</span>`;
    if (!c.online && !containerQuality(c)) {
      return `<div class="cbox offline" onclick="openContainer('${esc(c.ip)}')">`
        + `<div class="cbox-head">${headName}`
        + `<span class="fbadge crit">${esc(t("cbox.ctrl_off"))}</span>${containerRemoveBtn(c.ip)}</div>`
        + `<div class="cbox-meta">${esc(t("cbox.last_temp", { s: safeVal(c.supply_temp), r: safeVal(c.return_temp) }))}</div></div>`;
    }
    const dt = (c.supply_temp != null && c.return_temp != null) ? (c.return_temp - c.supply_temp).toFixed(1) : "-";
    const pumps = Object.entries(c.pumps || {}).map(([k, v]) => `<span class="pump ${v ? "on" : ""}">${esc(containerPumpLabel(k))}</span>`).join("");
    // 忽略列表内的故障置灰(info)，其余按严重级；按阈值标红低压力
    const faults = (c.faults || []).map(f => containerFaultBadge(f, ign.has(f.flag))).join("");
    const spLow = spMin && c.supply_pressure != null && c.supply_pressure < spMin;
    const rpLow = rpMin && c.return_pressure != null && c.return_pressure < rpMin;
    const realFault = (c.faults || []).some(f => !ign.has(f.flag)) || spLow || rpLow;
    return `<div class="cbox ${realFault ? "fault" : ""}" onclick="openContainer('${esc(c.ip)}')">`
      + `<div class="cbox-head">${headName}`
      + `<span class="cbox-meta">${esc(t("cbox.miners_line", { n: safeVal(c.miner_num) }))} ｜ ⚡ <b>${fmtPower(boxPower(c))}</b></span>`
      + `${containerRemoveBtn(c.ip)}</div>`
      + (containerQuality(c) ? AntDataQuality.note(t(c.data_stale ? "quality.stale" : "quality.partial"), true) : "")
      + `<div class="cbox-temps"><div>${esc(t("cbox.supply"))} <span class="v in">${safeVal(c.supply_temp)}℃</span></div>`
      + `<div>${esc(t("cbox.return"))} <span class="v out">${safeVal(c.return_temp)}℃</span></div><div>ΔT <span class="v">${safeVal(dt)}℃</span></div></div>`
      + `<div class="cbox-meta">${esc(t("cbox.meta_env", { t: safeVal(c.internal_temp), h: safeVal(c.internal_humidity), f: safeVal(c.flow) }))} `
      + `<span class="${spLow ? "res-fail" : ""}">${safeVal(c.supply_pressure)}</span>/<span class="${rpLow ? "res-fail" : ""}">${safeVal(c.return_pressure)}</span> ｜ ${esc(t("cbox.set_temp"))} ${safeVal(c.set_temp)}℃</div>`
      + `<div class="cbox-pumps">${pumps}</div>`
      + (faults ? `<div class="cbox-faults">${faults}</div>` : "")
      + `</div>`;
  }).join("");
}

async function openContainer(ip) {
  const d = await jget(`/api/container/${ip}`);
  const c = d.current || {};
  $("mTitle").textContent = c.label
    ? `${c.label} · ${ip}`
    : t("cbox.title", { ip });
  const consoleBtn = $("mOpenConsole");
  if (consoleBtn) { consoleBtn.classList.add("hidden"); consoleBtn.onclick = null; }
  const rm = $("mRemoveContainer");
  if (rm) {
    rm.classList.remove("hidden");
    rm.textContent = t("cbox.remove_btn");
    rm.title = t("cbox.remove_title");
    rm.onclick = () => removeContainer(ip);
  }
  const h = d.history || [];
  const maxGap = Math.max(60, Number(d.stale_after) || 60);
  const series = key => {
    const out = [];
    h.forEach((r, i) => {
      if (i && r.ts - h[i - 1].ts > maxGap) out.push({ x: h[i - 1].ts + maxGap, y: null });
      const n = typeof r[key] === "number" && Number.isFinite(r[key]) ? r[key] : null;
      out.push({ x: r.ts, y: n }); // Temperatures may be negative; don't use nonnegative power validation.
    });
    return out;
  };
  if (modalChart) modalChart.destroy();
  if (window.Chart) modalChart = new Chart($("mChart"), {
    type: "line",
    data: {
      datasets: [
        { label: t("chart.supply"), data: series("supply_temp"), borderColor: "#56d4dd", pointRadius: 0, tension: 0 },
        { label: t("chart.return"), data: series("return_temp"), borderColor: "#f0883e", pointRadius: 0, tension: 0 },
        { label: t("chart.internal"), data: series("internal_temp"), borderColor: "#3fb950", pointRadius: 0, tension: 0 }]
    },
    options: { spanGaps: false, interaction: { mode: "nearest", axis: "x", intersect: false },
      scales: { x: { type: "linear", ticks: { color: "#6e7681", maxTicksLimit: 6,
        callback: ts => new Date(ts * 1000).toLocaleTimeString(uiLocale(), { hour12: false }) } }, y: { ticks: { color: "#6e7681" } } },
      plugins: { legend: { labels: { color: "#adbac7" } }, tooltip: { callbacks: {
        title: items => items.length ? fmtTime(items[0].parsed.x) : "" } } } }
  });
  const faults = (c.faults || []).map(f => containerFaultBadge(f)).join("") || t(containerQuality(c) ? "quality.partial" : "cbox.none_fault");
  const pumps = Object.entries(c.pumps || {}).map(([k, v]) => `${esc(containerPumpLabel(k))}:${v ? t("cmd.open") : t("cmd.close")}`).join(" ｜ ");
  $("mInfo").innerHTML =
    (containerQuality(c) ? AntDataQuality.note(t(c.data_stale ? "quality.stale" : "quality.partial"), true) : "")
    + `${esc(t("cbox.detail_temps", { s: safeVal(c.supply_temp), r: safeVal(c.return_temp), set: safeVal(c.set_temp) }))}<br>`
    + `${esc(t("cbox.detail_press", { sp: safeVal(c.supply_pressure), rp: safeVal(c.return_pressure), f: safeVal(c.flow), ti: safeVal(c.tower_inlet_temp) }))}<br>`
    + `${esc(t("cbox.detail_box", { t: safeVal(c.internal_temp), h: safeVal(c.internal_humidity), n: safeVal(c.miner_num), chip: safeVal(c.chip_max_temp) }))}<br>`
    + `${esc(t("cbox.detail_power", { total: fmtPower(boxPower(c)), p1: fmtPower(c.power1), p2: fmtPower(c.power2) }))}<br>`
    + `${esc(t("cbox.detail_pumps", { p: pumps }))}<br>${t("cbox.detail_faults", { f: faults })}`;
  $("modal").classList.remove("hidden");
}

function refreshAll() {
  const requests = [refreshSummary(), refreshAlerts(), refreshTrend(), refreshContainers()];
  if (view === "rack") requests.push(refreshRacks());
  else if (view === "worker") requests.push(refreshWorkers());
  else requests.push(refreshMiners());
  // 会话失效时 jget 会弹登录，已发出的并行请求仍需接住各自的 rejection。
  return Promise.allSettled(requests);
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

function CMD_META() {
  return {
    "locate-on":  { action: "locate", params: { on: true },  title: t("meta.locate_on"), short: t("meta.locate_on_s"), danger: false },
    "locate-off": { action: "locate", params: { on: false }, title: t("meta.locate_off"), short: t("meta.locate_off_s"), danger: false },
    "reboot":     { action: "reboot", params: {}, title: t("meta.reboot"), short: t("meta.reboot_s"), danger: true },
  };
}
// 破坏性命令(重启)的「大批量」阈值。比 repair/remove 的 500 更低：重启让全场同时离线，
// 误操作代价远高于标记维修，所以更早开始拦。换矿池面板入口已去掉(API 仍保留)。
const DANGER_BULK = 200;
// 后端 /api/command 对 reboot 强制要求请求体带 confirm:true，否则 400。
const NEED_CONFIRM_FLAG = new Set(["reboot"]);

document.querySelector(".cmdbar").addEventListener("click", (e) => {
  const cmd = e.target.dataset && e.target.dataset.cmd;
  if (!cmd) return;
  if (cmd === "locksel") {
    selectionLocked = !selectionLocked;
    updateSelCount();
    toast(selectionLocked ? t("toast.sel_locked") : t("toast.sel_unlocked"), "ok");
    return;
  }
  if (cmd === "selall") {
    lastMiners.forEach(m => selected.add(m.ip));
    refreshMiners(); return;
  }
  if (cmd === "clear") {
    selected.clear();
    selectionLocked = false;
    refreshMiners();
    return;
  }
  if (cmd === "repair") return doMachineState("repair");
  if (cmd === "powercut") return doMachineState("powercut");
  if (cmd === "unrepair") return doMachineState("active");
  if (cmd === "remove") return doMachineState("remove");
  openCmdDialog(cmd);
});

async function doMachineState(action) {
  if (!selected.size) { toast(t("toast.select_first")); return; }
  const ips = [...selected];
  // 单次上限 control.max_batch 是安全闸：超了直接提示分批，别在确认之后才被后端拒绝，
  // 也别在前端悄悄分批绕过它(下架 5000 台只需确认两次)
  if (ips.length > maxBatch) { toast(t("toast.batch_max", { n: maxBatch })); return; }
  const scope = selectionScope(ips);
  const scopeLine = t("cmd.scope_summary", {
    segs: scope.segs.length, workers: scope.workers.length, offline: scope.offline,
  });
  // 维修/限电/下架影响大(停告警/删名册)，二次确认把后果讲清；大批量再确认一次
  if (action === "repair" && !confirm(scopeLine + "\n\n" + t("confirm.repair", { n: ips.length }))) return;
  if (action === "powercut" && !confirm(scopeLine + "\n\n" + t("confirm.powercut", { n: ips.length }))) return;
  if (action === "remove" && !confirm(scopeLine + "\n\n" + t("confirm.remove", { n: ips.length }))) return;
  if (scope.workers.length >= 2 && (action === "repair" || action === "powercut" || action === "remove")
      && !confirm(t("confirm.cross_customer", {
        n: scope.workers.length,
        names: scope.workers.slice(0, 8).join(", ") + (scope.workers.length > 8 ? " …" : ""),
      }))) return;
  if (ips.length > 200 && (action === "repair" || action === "powercut" || action === "remove")
      && !confirm(t("confirm.bulk_state", { n: ips.length }))) return;
  if (ips.length > 500 && !confirm(t("confirm.bulk", { n: ips.length }))) return;
  try {
    const d = await jpost("/api/machine-state", { ips, action });
    if (!d.ok) { toast(t("toast.fail", { msg: d.error || "" })); return; }
    toast(`${t("action." + action)} ${d.count}`, "ok");
    selected.clear(); refreshMiners();
  } catch (e) { if (!String(e.message).match(/^40[13]$/)) toast(t("req.error")); }
}

let maxBatch = 1000;        // 单次命令/维修/下架上限，登录后取服务端 control.max_batch
let pendingCmd = null;
let _cmdInFlight = false;   // 命令在途：禁止再开新命令弹窗，防两次执行的结果/按钮串台
function openCmdDialog(cmd, includeHidden) {
  if (_cmdInFlight) { toast(t("toast.cmd_busy")); return; }
  if (!selected.size) { toast(t("toast.select_first")); return; }
  const meta = CMD_META()[cmd];
  // 勾选里"已不在当前列表"的机器(自动刷新后状态变了，比如按"离线"勾的已恢复在线在挖矿)：
  // 默认不对它们执行，醒目列出，要执行得自己勾上。以前照样下发且弹窗里毫无提示
  const visible = new Set(lastMiners.map(m => m.ip));
  const hiddenIps = [...selected].filter(ip => !visible.has(ip));
  const ips = includeHidden ? [...selected] : [...selected].filter(ip => visible.has(ip));
  if (ips.length > maxBatch) { toast(t("toast.batch_max", { n: maxBatch })); return; }
  // 目标在打开弹窗这一刻冻结：弹窗里列的就是确认后下发的，期间自动刷新不会改变目标
  pendingCmd = { action: meta.action, params: { ...meta.params }, danger: !!meta.danger,
                 short: meta.short || meta.title, ips };
  $("cmdTitle").textContent = meta.title;
  const scope = selectionScope(ips);
  let html = t("cmd.dialog_run", { n: ips.length, act: esc(meta.title) })
    + scopeSummaryHtml(scope);
  if (hiddenIps.length)
    html += `<div class="warn-box" style="border-width:2px">${t("cmd.hidden_warn", { n: hiddenIps.length, ips: hiddenIps.slice(0, 20).map(esc).join(", ") + (hiddenIps.length > 20 ? " …" : "") })}`
      + `<br><label style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;margin-top:6px">`
      + `<input type="checkbox" id="cmdIncHidden" style="width:auto;margin:0"`
      + `${includeHidden ? " checked" : ""}> ${esc(t("cmd.also_run", { n: hiddenIps.length }))}</label>`
      + `${esc(t("cmd.default_skip"))}</div>`;
  if (meta.danger)
    html += `<div class="warn-box">${esc(t("cmd.danger_note"))}</div>`;
  // 规模分级提示：几台和几千台的后果完全不是一回事，弹窗里必须让人看清影响范围
  if (meta.danger && ips.length > DANGER_BULK)
    html += `<div class="warn-box" style="border-width:2px;font-weight:700;font-size:15px;line-height:1.7">`
      + t("cmd.bulk_danger", { n: ips.length, act: esc(meta.short || meta.title) })
      + `</div>`;
  html += `<div class="muted" style="margin-top:8px">${esc(t("cmd.targets", { n: ips.length }))}</div>`
    + `<div style="max-height:120px;overflow:auto;font-size:12px;line-height:1.6;border:1px solid #30363d;border-radius:6px;padding:6px;margin-top:4px">`
    + ips.map(esc).join(", ") + `</div>`;
  $("cmdBody").innerHTML = html;
  if ($("cmdIncHidden")) $("cmdIncHidden").onchange = (e) => openCmdDialog(cmd, e.target.checked);
  $("cmdConfirm").disabled = ips.length === 0;
  // 确认按钮上写清「几台 + 干什么」，避免用户凭肌肉记忆点掉一个通用的"确认执行"
  $("cmdConfirm").textContent = t("cmd.confirm_n", { n: ips.length, act: meta.short || meta.title });
  $("cmdModal").classList.remove("hidden");
}

$("cmdConfirm").onclick = async () => {
  if (!pendingCmd || _cmdInFlight || !pendingCmd.ips.length) return;
  const cmd = pendingCmd;          // 局部持有：执行中点了取消/×，结果也照样显示，不影响下一个弹窗
  const ips = cmd.ips;
  // 大批量破坏性命令再拦一道原生确认，和 repair/remove 的交互保持一致
  if (cmd.danger && ips.length > DANGER_BULK &&
      !confirm(t("confirm.danger_bulk", { n: ips.length, act: cmd.short }))) return;
  const btnLabel = $("cmdConfirm").textContent;
  _cmdInFlight = true;
  $("cmdConfirm").disabled = true; $("cmdConfirm").textContent = t("cmd.executing");
  $("cmdCancel").textContent = t("cmd.bg_close");
  try {
    const body = { ips, action: cmd.action, params: cmd.params };
    // 后端对 reboot 强制校验 confirm===true，缺了直接 400
    if (NEED_CONFIRM_FLAG.has(cmd.action)) body.confirm = true;
    const d = await jpost("/api/command", body);
    if (!d.ok) { toast(t("toast.fail", { msg: d.error || "" })); }
    else if (d.async) {   // 分批重启：后台执行，轮询进度，界面不卡
      const skipTip = d.skipped ? t("cmd.skipped", { n: d.skipped }) : "";
      toast(t("cmd.batch_started", { n: d.count, skip: skipTip, batch: d.batch, delay: d.delay }), "ok");
      pollCmdProgress();
      selected.clear(); updateSelCount(); refreshMiners();   // 勾选框和"已选 N 台"一起清掉
    } else {
      const fails = (d.results || []).filter(x => !x.ok);
      let msg = t("cmd.result", { act: cmd.action, ok: d.success, fail: d.failed });
      if (d.skipped) msg += t("cmd.skipped", { n: d.skipped });
      if (fails.length) msg += "\n" + fails.slice(0, 5).map(x => `${x.ip}: ${x.msg}`).join("\n");
      toast(msg, fails.length ? "fail" : "ok");
    }
  } catch (err) { /* 401/403 已在 jpost 里处理 */ }
  _cmdInFlight = false;
  $("cmdConfirm").disabled = false; $("cmdConfirm").textContent = btnLabel;
  $("cmdCancel").textContent = t("cmd.cancel");
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
      document.title = t("cmd.reboot_title_prog", { done: p.done, total: p.total });
      _cmdPollTimer = setTimeout(pollCmdProgress, 3000);
    } else if (p.total) {
      document.title = t("doc.title");
      let msg = p.error
        ? `${p.error}\n${t("cmd.result", { act: p.action, ok: p.success, fail: p.failed })}`
        : t("cmd.batch_done", { ok: p.success, fail: p.failed, total: p.total });
      if (p.fail_ips && p.fail_ips.length) msg += t("cmd.fail_ips", { ips: p.fail_ips.slice(0, 8).join(", ") });
      toast(msg, p.failed || p.error ? "fail" : "ok");
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

/* ---------------- 重启记录（批次汇总） ---------------- */
let _rhOpenKey = "";

function closeRebootHist() {
  $("rebootHistModal").classList.add("hidden");
  _rhOpenKey = "";
}

function rhBatchKey(b) {
  return `${b.user}|${b.id_from}|${b.id_to}`;
}

async function loadRebootHist() {
  const body = $("rebootHistBody");
  if (!body) return;
  body.innerHTML = `<div class="muted">${esc(t("rh.loading"))}</div>`;
  try {
    const d = await jget("/api/command/reboot-history?limit=50");
    const batches = d.batches || [];
    if (!batches.length) {
      body.innerHTML = `<div class="muted">${esc(t("rh.empty"))}</div>`;
      return;
    }
    let html = `<table class="rh-table"><thead><tr>`
      + `<th>${esc(t("rh.th_time"))}</th>`
      + `<th>${esc(t("rh.th_source"))}</th>`
      + `<th>${esc(t("rh.th_user"))}</th>`
      + `<th>${esc(t("rh.th_total"))}</th>`
      + `<th>${esc(t("rh.th_ok"))}</th>`
      + `<th>${esc(t("rh.th_fail"))}</th>`
      + `<th>${esc(t("rh.th_act"))}</th>`
      + `</tr></thead><tbody>`;
    for (const b of batches) {
      const key = rhBatchKey(b);
      const src = b.source === "auto" ? "auto" : "manual";
      const srcLabel = src === "auto" ? t("rh.src_auto") : t("rh.src_manual");
      const timeTxt = b.ended_ts && b.ended_ts !== b.started_ts
        ? `${fmtTime(b.started_ts)} → ${fmtTime(b.ended_ts)}`
        : fmtTime(b.started_ts);
      html += `<tr data-rh-key="${esc(key)}">`
        + `<td>${esc(timeTxt)}</td>`
        + `<td><span class="rh-src ${src}">${esc(srcLabel)}</span></td>`
        + `<td>${esc(b.user || "")}</td>`
        + `<td>${esc(String(b.total || 0))}</td>`
        + `<td class="rh-ok">${esc(String(b.success || 0))}</td>`
        + `<td class="${b.failed ? "rh-fail" : ""}">${esc(String(b.failed || 0))}</td>`
        + `<td><button type="button" class="mini-btn rh-toggle" data-user="${esc(b.user || "")}"`
        + ` data-from="${esc(String(b.id_from))}" data-to="${esc(String(b.id_to))}"`
        + ` data-key="${esc(key)}">${esc(_rhOpenKey === key ? t("rh.hide") : t("rh.detail"))}</button></td>`
        + `</tr>`
        + `<tr class="rh-detail-row${_rhOpenKey === key ? "" : " hidden"}" data-rh-detail="${esc(key)}">`
        + `<td colspan="7"><div class="rh-detail">`
        + `${_rhOpenKey === key ? esc(t("rh.loading")) : ""}</div></td></tr>`;
    }
    html += `</tbody></table>`;
    body.innerHTML = html;
    body.querySelectorAll(".rh-toggle").forEach(btn => {
      btn.onclick = () => toggleRebootHistDetail(btn);
    });
    if (_rhOpenKey) {
      const openBtn = body.querySelector(`.rh-toggle[data-key="${CSS.escape(_rhOpenKey)}"]`);
      if (openBtn) fillRebootHistDetail(openBtn);
      else _rhOpenKey = "";
    }
  } catch (e) {
    body.innerHTML = `<div class="muted">${esc(t("rh.detail_fail"))}</div>`;
  }
}

function rhDetailBox(key) {
  const row = $("rebootHistBody").querySelector(`tr[data-rh-detail="${CSS.escape(key)}"]`);
  return row ? row.querySelector(".rh-detail") : null;
}

async function fillRebootHistDetail(btn) {
  const key = btn.dataset.key || "";
  const box = rhDetailBox(key);
  if (!box) return;
  box.textContent = t("rh.loading");
  try {
    const q = new URLSearchParams({
      user: btn.dataset.user || "",
      id_from: btn.dataset.from || "0",
      id_to: btn.dataset.to || "0",
    });
    const d = await jget("/api/command/reboot-history/detail?" + q.toString());
    const items = d.items || [];
    if (!items.length) {
      box.textContent = t("rh.empty");
      return;
    }
    box.innerHTML = items.map(it => {
      const ok = !!it.ok;
      const label = ok ? t("rh.item_ok") : t("rh.item_fail");
      const cls = ok ? "rh-ok" : "rh-fail";
      const msg = it.msg ? ` — ${it.msg}` : "";
      return `<div><span class="ip">${esc(it.ip)}</span>`
        + ` <span class="${cls}">${esc(label)}</span>`
        + `<span class="muted">${esc(msg)}</span></div>`;
    }).join("");
  } catch (e) {
    box.textContent = t("rh.detail_fail");
  }
}

async function toggleRebootHistDetail(btn) {
  const key = btn.dataset.key || "";
  const row = $("rebootHistBody").querySelector(`tr[data-rh-detail="${CSS.escape(key)}"]`);
  if (!row) return;
  if (_rhOpenKey === key) {
    _rhOpenKey = "";
    row.classList.add("hidden");
    btn.textContent = t("rh.detail");
    return;
  }
  // 一次只展开一批，避免明细刷屏
  if (_rhOpenKey) {
    const prev = $("rebootHistBody").querySelector(`tr[data-rh-detail="${CSS.escape(_rhOpenKey)}"]`);
    const prevBtn = $("rebootHistBody").querySelector(`.rh-toggle[data-key="${CSS.escape(_rhOpenKey)}"]`);
    if (prev) prev.classList.add("hidden");
    if (prevBtn) prevBtn.textContent = t("rh.detail");
  }
  _rhOpenKey = key;
  row.classList.remove("hidden");
  btn.textContent = t("rh.hide");
  await fillRebootHistDetail(btn);
}

async function openRebootHist() {
  if (myRole !== "ops" && myRole !== "admin") return;
  $("rebootHistModal").classList.remove("hidden");
  await loadRebootHist();
}

$("btnRebootHist").onclick = openRebootHist;
$("rebootHistClose").onclick = $("rebootHistClose2").onclick = closeRebootHist;
$("rebootHistRefresh").onclick = () => loadRebootHist();

/* ---------------- 网段与扫描设置 ---------------- */
function formatSegLines(segments, labels) {
  labels = labels || {};
  return (segments || []).map(s => {
    const lab = labels[s];
    return lab ? `${s} ${lab}` : s;
  }).join("\n");
}

/** 按行解析「网段 [备注]」；备注可含空格。不再按空白拆碎整段。 */
function parseSegTextarea(text) {
  return String(text || "").split("\n").map(line => line.trim()).filter(Boolean);
}

async function openSeg() {
  const d = await jget("/api/segments");
  $("segText").value = formatSegLines(d.segments, d.labels);
  $("segHs").value = d.host_start; $("segHe").value = d.host_end;
  const s = await jget("/api/settings");
  $("setInterval").value = s.scan_interval;
  if ($("setFullInterval")) $("setFullInterval").value = s.full_interval;
  $("segModal").classList.remove("hidden");
}
async function saveSeg(scan) {
  const segs = parseSegTextarea($("segText").value);
  try {
    const d = await jpost("/api/segments", {
      segments: segs,
      host_start: parseInt($("segHs").value) || 1,
      host_end: parseInt($("segHe").value) || 254,
    });
    if (!d.ok) { toast(t("toast.save_fail", { msg: d.error || "" })); return; }
    // 数字框留空/填错时不提交该项(保留原值)，别悄悄存成 0 或默认值——ARP 限速存成 0 会让扫描几乎停摆
    const num = (id) => { const v = parseInt($(id).value); return Number.isFinite(v) ? v : undefined; };
    const body = {
      scan_interval: num("setInterval"),
    };
    if ($("setFullInterval")) body.full_interval = num("setFullInterval");
    Object.keys(body).forEach(k => body[k] === undefined && delete body[k]);
    const d2 = await jpost("/api/settings", body);
    if (!d2.ok) { toast(t("toast.save_fail", { msg: d2.error || "" })); return; }
    $("segModal").classList.add("hidden");
    toast(t("toast.saved_settings"), "ok");
    if (scan) { await jpost("/api/scan?kind=full", {}); toast(t("toast.full_triggered"), "ok"); setTimeout(pollProgress, 500); }
  } catch (e) {}
}
$("btnSeg").onclick = openSeg;
$("segClose").onclick = $("segCancel").onclick = () => {
  $("segModal").classList.add("hidden");
};
$("segSave").onclick = () => saveSeg(false);
$("segSaveScan").onclick = () => saveSeg(true);

/* ---------------- 独立云端连接：测试成功才保存，仅提交 cloud ---------------- */
let cloudSiteIdOverride = "", _cloudAppliedCode = "";
let _cloudSaved = null, _cloudSnapshot = null, _cloudStatusUnavailable = false;
let _cloudBusy = false, _cloudTimer = null, _cloudPollEpoch = 0, _cloudOpenEpoch = 0;
let _cloudFeedback = null, _cloudCodeHintKey = "cloud.code_hint";
let _cloudReadingClipboard = false, _cloudAwaitingPaste = false;

function cloudFeedback(key, vars = {}, kind = "muted") {
  _cloudFeedback = key ? { key, vars, kind } : null;
  const el = $("cloudFeedback");
  el.className = "cloud-feedback " + (key ? kind : "hidden");
  el.textContent = key ? t(key, vars) : "";
}
function renderCloudLabels() {
  $("cloudCodeHint").textContent = t(_cloudCodeHintKey);
  $("cloudDestination").textContent = $("cloudUrl").value.trim();
  $("cloudDestinationRow").classList.toggle("hidden",
    !_cloudAppliedCode || $("cloudCode").value.trim() !== _cloudAppliedCode);
  $("cloudTokenEye").textContent = t($("cloudToken").type === "password" ? "seg.token_eye" : "seg.token_hide");
  $("cloudToken").placeholder = _cloudSaved && _cloudSaved.token_set
    ? t("seg.token_saved_ph", { hint: _cloudSaved.token_hint || "****" }) : "ingest token";
  $("cloudConnect").textContent = t(_cloudBusy ? "cloud.working" : "header.cloud");
  $("cloudPaste").textContent = t(_cloudReadingClipboard ? "cloud.reading_clipboard" : "cloud.paste_connect");
  $("cloudPaste").disabled = _cloudBusy || _cloudReadingClipboard;
  $("cloudSiteSummary").textContent = $("cloudName").value.trim();
  $("cloudClose").setAttribute("aria-label", t("cloud.close"));
  if (_cloudFeedback) cloudFeedback(_cloudFeedback.key, _cloudFeedback.vars, _cloudFeedback.kind);
}
function fillCloudSettings(c) {
  _cloudSaved = { ...c };
  $("cloudName").value = c.site_name || "";
  $("cloudType").value = c.site_type || "air";
  $("cloudUrl").value = c.url || "";
  $("cloudFp").value = c.tls_fingerprint || "";
  $("cloudSiteId").textContent = c.site_id || "-";
  $("cloudToken").value = "";
  $("cloudToken").type = "password";
  $("cloudCode").value = "";
  $("cloudSiteDetails").open = false;
  _cloudAwaitingPaste = false;
  $("cloudPause").classList.toggle("hidden", !c.enabled);
  cloudSiteIdOverride = "";
  _cloudAppliedCode = "";
  _cloudCodeHintKey = "cloud.code_hint";
  renderCloudLabels();
}
async function openCloud() {
  if (myRole !== "admin") return;
  const epoch = ++_cloudOpenEpoch;
  $("cloudModal").classList.remove("hidden");
  $("cloudClose").focus();
  if (!_cloudBusy) {
    cloudFeedback("cloud.loading");
    $("cloudFields").disabled = true;
    $("cloudConnect").disabled = true;
    try {
      const s = await jget("/api/settings");
      if (epoch !== _cloudOpenEpoch) return;
      if (!s.cloud) throw new Error("settings");
      fillCloudSettings({ ...s.cloud, site_id: s.site_id });
      $("cloudAdvanced").open = false;
      cloudFeedback("");
    } catch (e) {
      if (epoch === _cloudOpenEpoch) cloudFeedback("cloud.load_failed", {}, "res-fail");
    } finally {
      if (epoch === _cloudOpenEpoch) {
        $("cloudFields").disabled = false;
        $("cloudConnect").disabled = false;
        $("cloudCode").focus();
      }
    }
  }
  startCloudPolling();
}
function closeCloud() {
  _cloudOpenEpoch++;
  _cloudAwaitingPaste = false;
  $("cloudModal").classList.add("hidden");
  $("cloudToken").type = "password";
  renderCloudLabels();
  startCloudPolling();
  $("btnCloud").focus();
}
function applyCloudCode(showError = false) {
  const raw = $("cloudCode").value.trim();
  if (!raw) {
    _cloudAppliedCode = "";
    _cloudCodeHintKey = "cloud.code_hint";
    renderCloudLabels();
    return true; // 手动配置和已保存的连接不需要接入码
  }
  const d = AntMonitorCloud.parseCode(raw);
  if (!d) {
    _cloudCodeHintKey = "cloud.code_hint";
    if (showError) cloudFeedback(raw.startsWith("AMC1-") ? "toast.connect_bad" : "cloud.token_only", {}, "res-fail");
    renderCloudLabels();
    return false;
  }
  if (raw !== _cloudAppliedCode) {
    $("cloudUrl").value = d.u;
    $("cloudToken").value = d.t;
    $("cloudFp").value = d.f;
    $("cloudName").value = d.n || (_cloudSaved && _cloudSaved.site_name) || "";
    $("cloudType").value = d.y || (_cloudSaved && _cloudSaved.site_type) || "air";
    cloudSiteIdOverride = d.s;
    $("cloudSiteId").textContent = d.s || (_cloudSaved && _cloudSaved.site_id) || "-";
    _cloudAppliedCode = raw;
  }
  _cloudCodeHintKey = d.n ? "cloud.code_ready" : "cloud.code_needs_name";
  $("cloudSiteDetails").open = !d.n;
  renderCloudLabels();
  return true;
}
function cloudForm() {
  const c = { enabled: true, site_name: $("cloudName").value.trim(),
    site_type: $("cloudType").value, url: $("cloudUrl").value.trim(),
    token: $("cloudToken").value.trim(), tls_fingerprint: $("cloudFp").value.trim() };
  if (cloudSiteIdOverride) c.site_id = cloudSiteIdOverride;
  return c;
}
function cloudHasSavedToken(c) {
  const fp = v => String(v || "").trim().toLowerCase().replace(/^sha256:/, "").replace(/[:\s]/g, "");
  return !!(_cloudSaved && _cloudSaved.token_set
    && c.url.replace(/\/+$/, "") === (_cloudSaved.url || "").replace(/\/+$/, "")
    && fp(c.tls_fingerprint) === fp(_cloudSaved.tls_fingerprint));
}
function setCloudBusy(busy) {
  _cloudBusy = busy;
  $("cloudFields").disabled = busy;
  $("cloudConnect").disabled = busy;
  $("cloudBindingForm").setAttribute("aria-busy", String(busy));
  renderCloudLabels();
}
async function connectCloud(event) {
  event.preventDefault();
  if (_cloudBusy || myRole !== "admin") return;
  cloudFeedback("");
  if (!applyCloudCode(true)) { $("cloudCode").focus(); return; }
  const c = cloudForm(), missing = AntMonitorCloud.validate(c, cloudHasSavedToken(c));
  if (missing) {
    cloudFeedback("cloud.required_" + missing, {}, "res-fail");
    if (missing === "site_name") $("cloudSiteDetails").open = true;
    else $("cloudAdvanced").open = true;
    $({ site_name: "cloudName", url: "cloudUrl", token: "cloudToken" }[missing]).focus();
    return;
  }
  setCloudBusy(true);
  try {
    const saved = await AntMonitorCloud.connect(c, jpost, phase => cloudFeedback("cloud." + phase));
    fillCloudSettings({ ...saved.cloud, site_id: saved.site_id || (saved.cloud || {}).site_id
      || cloudSiteIdOverride || (_cloudSaved && _cloudSaved.site_id) });
    // 丢弃保存前已经发出的状态查询，避免旧连接的成功短暂覆盖新连接。
    _cloudSnapshot = null;
    _cloudStatusUnavailable = false;
    renderCloudStatus();
    cloudFeedback("cloud.saved", {}, "res-ok");
    startCloudPolling();
  } catch (e) {
    const keys = { test: "cloud.test_failed", test_network: "cloud.test_network",
      save: "cloud.save_failed", save_network: "cloud.save_unknown" };
    cloudFeedback(keys[e.step] || "cloud.save_unknown", { error: e.message || "" }, "res-fail");
    startCloudPolling(); // 输入保持不动；同时显示原来已保存连接的真实状态
  } finally { setCloudBusy(false); }
}
async function pasteCloudAndConnect() {
  if (_cloudBusy || _cloudReadingClipboard || myRole !== "admin") return;
  const epoch = _cloudOpenEpoch;
  _cloudAwaitingPaste = false;
  _cloudReadingClipboard = true;
  renderCloudLabels();
  let text;
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.readText !== "function") throw new Error("clipboard");
    text = await navigator.clipboard.readText();
  } catch (e) {
    if (epoch === _cloudOpenEpoch && !$("cloudModal").classList.contains("hidden")) {
      _cloudAwaitingPaste = true;
      cloudFeedback("cloud.paste_manual");
      $("cloudCode").focus();
      $("cloudCode").select();
    }
    return;
  } finally {
    _cloudReadingClipboard = false;
    renderCloudLabels();
  }
  if (epoch !== _cloudOpenEpoch || $("cloudModal").classList.contains("hidden")) return;
  $("cloudCode").value = String(text || "").trim();
  if (!$("cloudCode").value) {
    cloudFeedback("cloud.clipboard_empty", {}, "res-fail");
    $("cloudCode").focus();
    return;
  }
  await connectCloud({ preventDefault() {} });
}
async function pauseCloud() {
  if (_cloudBusy || !_cloudSaved) return;
  setCloudBusy(true);
  cloudFeedback("cloud.saving");
  try {
    const c = { enabled: false, url: _cloudSaved.url, token: "",
      site_name: _cloudSaved.site_name, site_type: _cloudSaved.site_type,
      tls_fingerprint: _cloudSaved.tls_fingerprint, site_id: _cloudSaved.site_id };
    const d = await jpost("/api/settings", { cloud: c });
    if (!d.ok) { cloudFeedback("cloud.save_failed", { error: d.error || "" }, "res-fail"); return; }
    _cloudSaved.enabled = false;
    $("cloudPause").classList.add("hidden");
    cloudFeedback("cloud.paused", {}, "res-ok");
    _cloudSnapshot = null;
  } catch (e) { cloudFeedback("cloud.save_unknown", {}, "res-fail"); }
  finally { setCloudBusy(false); startCloudPolling(); }
}
function agoText(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 90 ? t("cloud.ago_s", { n: s }) : t("cloud.ago_m", { n: Math.round(s / 60) });
}
function renderCloudStatus() {
  const el = $("cloudStatus"), badge = $("cloudHeaderState"), st = _cloudSnapshot;
  el.className = "muted";
  $("btnCloud").classList.toggle("needs-connection", !!(st && !st.enabled));
  if (_cloudStatusUnavailable || !st) {
    el.textContent = t(_cloudStatusUnavailable ? "cloud.status_unavailable" : "cloud.loading");
    badge.textContent = _cloudStatusUnavailable ? t("cloud.badge_unknown") : "…";
    return;
  }
  if (!st.enabled) {
    el.textContent = t("cloud.disconnected");
    badge.textContent = t("cloud.badge_off");
    return;
  }
  if (st.state === "error" || st.fails || st.running === false) {
    el.className = "res-fail";
    el.textContent = t("cloud.retrying", { error: st.last_err || t("cloud.worker_stopped") });
    badge.textContent = t("cloud.badge_retry");
  } else if (st.state === "waiting_scan") {
    el.textContent = t("cloud.waiting_scan");
    badge.textContent = t("cloud.badge_wait");
  } else if (st.last_ok) {
    el.className = "res-ok";
    el.textContent = t("cloud.st_ok", { ago: agoText(st.last_ok) });
    badge.textContent = t("cloud.badge_ok");
  } else {
    el.textContent = t("cloud.waiting_report");
    badge.textContent = t("cloud.badge_wait");
  }
}
function stopCloudPolling() {
  _cloudPollEpoch++;
  if (_cloudTimer) { clearTimeout(_cloudTimer); _cloudTimer = null; }
}
function startCloudPolling() {
  stopCloudPolling();
  if (myRole !== "admin" || _wsStop) return;
  const epoch = _cloudPollEpoch;
  async function poll() {
    try {
      const st = await jget("/api/cloud/status");
      if (epoch !== _cloudPollEpoch) return;
      if (!st.ok) throw new Error("status");
      _cloudSnapshot = st;
      _cloudStatusUnavailable = false;
    } catch (e) {
      if (epoch !== _cloudPollEpoch) return;
      _cloudStatusUnavailable = true;
    }
    renderCloudStatus();
    if (epoch === _cloudPollEpoch) {
      _cloudTimer = setTimeout(poll, $("cloudModal").classList.contains("hidden") ? 30000 : 3000);
    }
  }
  poll();
}
$("btnCloud").onclick = openCloud;
$("cloudClose").onclick = $("cloudCancel").onclick = closeCloud;
$("cloudBindingForm").onsubmit = connectCloud;
$("cloudPaste").onclick = pasteCloudAndConnect;
$("cloudPause").onclick = pauseCloud;
$("cloudName").addEventListener("input", renderCloudLabels);
$("cloudUrl").addEventListener("input", renderCloudLabels);
$("cloudCode").addEventListener("input", () => { cloudFeedback(""); applyCloudCode(); });
$("cloudCode").addEventListener("paste", event => {
  // On a LAN HTTP page browsers may forbid clipboard reads. After the operator
  // requests paste-and-connect, their explicit Ctrl+V gesture completes it.
  if (!_cloudAwaitingPaste || _cloudBusy || !event.clipboardData) return;
  const text = event.clipboardData.getData("text");
  if (!text) return;
  event.preventDefault();
  _cloudAwaitingPaste = false;
  $("cloudCode").value = text.trim();
  return connectCloud({ preventDefault() {} });
});
$("cloudTokenEye").onclick = () => {
  $("cloudToken").type = $("cloudToken").type === "password" ? "text" : "password";
  renderCloudLabels();
};
$("cloudModal").addEventListener("keydown", e => {
  if (e.key === "Escape") { e.preventDefault(); closeCloud(); }
  if (e.key === "Tab") {
    const items = [...$("cloudBindingForm").querySelectorAll("button, input, select, summary, a[href]")]
      .filter(el => !el.matches(":disabled") && el.getClientRects().length);
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});

/* ---------------- 顶部：零算力自动重启设置 ---------------- */
async function loadRebootSettings() {
  if (!$("rebootBar") || myRole === "viewer") return;
  try {
    const s = await jget("/api/settings");
    if ($("rebootEnabled")) $("rebootEnabled").checked = !!s.reboot_enabled;
    if ($("rebootDelay")) $("rebootDelay").value = s.reboot_delay_sec ?? 8;
    if ($("rebootConc")) $("rebootConc").value = s.reboot_concurrency ?? 30;
  } catch (e) {}
}
async function saveRebootSettings() {
  if (myRole !== "admin") { toast(t("toast.admin_only_reboot")); return; }
  const delay = parseInt(($("rebootDelay") || {}).value);
  const conc = parseInt(($("rebootConc") || {}).value);
  if (!Number.isFinite(delay) || !Number.isFinite(conc)) {
    toast(t("toast.need_numbers")); return;
  }
  try {
    const d = await jpost("/api/settings", {
      reboot_enabled: !!($("rebootEnabled") && $("rebootEnabled").checked),
      reboot_delay_sec: delay,
      reboot_concurrency: conc,
    });
    if (!d.ok) { toast(t("toast.save_fail", { msg: d.error || "" })); return; }
    if ($("rebootDelay")) $("rebootDelay").value = d.reboot_delay_sec;
    if ($("rebootConc")) $("rebootConc").value = d.reboot_concurrency;
    if ($("rebootEnabled")) $("rebootEnabled").checked = !!d.reboot_enabled;
    toast(d.reboot_enabled
      ? t("toast.reboot_on", { c: d.reboot_concurrency, d: d.reboot_delay_sec })
      : t("toast.reboot_off", { c: d.reboot_concurrency, d: d.reboot_delay_sec }), "ok");
  } catch (e) {}
}
if ($("btnRebootSave")) $("btnRebootSave").onclick = saveRebootSettings;

$("btnFull").onclick = () => triggerScan("full");
$("btnQuick").onclick = () => triggerScan("quick");
$("btnVoice").onclick = () => setVoice(!voiceOn, true);
$("btnTestVoice").onclick = () => {
  try {   // 点击是用户手势，趁机唤醒被浏览器锁住的音频
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch (e) {}
  beep(880, 0.3); setTimeout(() => beep(660, 0.3), 350);
  speak(t("speak.test"));
  const hasVoice = ("speechSynthesis" in window);
  toast(hasVoice ? t("toast.voice_ok") : t("toast.voice_nosupport"), hasVoice ? "ok" : "fail");
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
    const labels = d.labels || {};
    const sel = $("fSeg");
    [...sel.options].forEach(o => { if (o.value && o.value !== sel.value) o.remove(); });   // 重新登录/改网段后别重复追加
    const have = new Set([...sel.options].map(o => o.value));
    (d.segments || []).filter(s => !have.has(s)).forEach(s => {
      const o = document.createElement("option");
      o.value = s;
      o.textContent = labels[s] ? `${labels[s]} (${s}.x)` : `${s}.x`;
      sel.appendChild(o);
    });
    // 已有选项也刷新显示名（改备注后不必清掉当前筛选）
    [...sel.options].forEach(o => {
      if (!o.value) return;
      o.textContent = labels[o.value] ? `${labels[o.value]} (${o.value}.x)` : `${o.value}.x`;
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
  if (nw !== $("pwdNew2").value) { $("pwdErr").textContent = t("toast.pwd_mismatch"); return; }
  let d;
  try { d = await jpost("/api/password", { old, new: nw }); } catch (e) { return; }
  if (!d.ok) {
    $("pwdErr").textContent = d.error || t("toast.pwd_fail");
    if (String(d.error || "").includes("已退出登录")) setTimeout(() => location.reload(), 1500);
    return;
  }
  closePwd();
  toast(t("toast.pwd_ok"), "ok");
}
$("btnPwd").onclick = () => openPwd(false);
$("pwdClose").onclick = () => { if (!_pwdForced) closePwd(); };
$("pwdLogout").onclick = () => doLogout();
$("pwdSave").onclick = savePwd;
$("pwdNew2").addEventListener("keydown", e => { if (e.key === "Enter") savePwd(); });

function applyRole() {
  const canCtl = myRole === "ops" || myRole === "admin";
  document.body.classList.toggle("viewer", !canCtl);
  $("btnCloud").classList.toggle("hidden", myRole !== "admin");
  $("btnSeg").style.display = myRole === "admin" ? "" : "none";  // 网段设置仅 admin
  $("btnUpdate").style.display = myRole === "admin" ? "" : "none";  // 版本更新仅 admin
  $("btnPwd").style.display = myUser && myUser !== "anonymous" ? "" : "none";
  $("userBadge").textContent = myRole;
  // 重启设置条：ops/admin 可见；保存仅 admin(与 /api/settings POST 一致)
  const rb = $("rebootBar");
  if (rb) {
    const admin = myRole === "admin";
    ["rebootEnabled", "rebootDelay", "rebootConc", "btnRebootSave"].forEach(id => {
      const el = $(id); if (!el) return;
      el.disabled = !admin;
    });
  }
}

// ---- 版本更新（仅 admin）：后台定时查有没有新版本，按钮变绿提示；点开看更新内容，一键更新 ----
let _updTimer = null;
let _updInfo = null;
let _updating = false;
let _updForce = false;   // known_bad 时由用户明确点"仍然重试"

function markUpdateBtn(d) {
  const n = (d && !d.error && !d.known_bad && d.behind) || 0;
  $("btnUpdate").textContent = n ? t("upd.has_new", { n }) : t("header.update");
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
  $("updApply").textContent = t("upd.apply");
  _updForce = false;
  if (!d) { b.innerHTML = `<div class="upd-err">${esc(t("upd.check_fail_net"))}</div>`; return; }
  if (d.git === false) {
    const hint = d.windows_repair ? "upd.windows_repair" :
      (d.reason === "git_missing" ? "upd.install_git" : "upd.need_git");
    b.innerHTML = `<div class="upd-err">${esc(d.error || t("upd.not_git"))}</div>
      <div class="upd-note">${esc(t(hint))}</div>`;
    if (d.windows_repair) {
      b.innerHTML += `<div class="upd-note">${esc(t("upd.repair_docs"))}:
        <a href="https://github.com/raphael2025/antmonitor#windows-web-update-recovery" target="_blank" rel="noopener noreferrer">GitHub</a>
        · <a href="https://gitee.com/raphael01/antmonitor#windows-web-update-recovery" target="_blank" rel="noopener noreferrer">Gitee</a></div>`;
    }
    return;
  }
  if (d.error) {
    b.innerHTML = `<div class="upd-err">${esc(t("upd.check_fail", { msg: d.error }))}</div>
      <div class="upd-note">${esc(t("upd.check_hint"))}</div>`;
    return;
  }
  const when = d.checked_ts ? new Date(d.checked_ts * 1000).toLocaleString() : "-";
  let h = `<div>${t("upd.meta", { b: esc(d.branch), l: esc(d.local), r: esc(d.remote) })}</div>
    <div class="muted">${esc(t("upd.checked_at", { when }))}</div>`;
  if (!d.behind) {
    h += `<div class="upd-ok">${esc(t("upd.latest"))}</div>`;
  } else if (d.known_bad) {
    h += `<div class="upd-err">${t("upd.bad", { r: esc(d.remote) })}</div>`;
    $("updApply").textContent = t("upd.retry");
    $("updApply").disabled = false;
    _updForce = true;
  } else {
    h += `<div>${t("upd.behind", { n: d.behind })}${d.behind > 10 ? t("upd.behind_more") : ""}：</div>
      <ul>${(d.changes || []).map(c => `<li>${esc(c)}</li>`).join("")}</ul>`;
    h += `<div class="upd-note">${esc(t("upd.flow"))}${d.restart_mode === "self" ? t("upd.self_restart") : ""}</div>`;
    $("updApply").disabled = false;
  }
  b.innerHTML = h;
}

async function openUpdate() {
  $("updModal").classList.remove("hidden");
  if (_updating) return;
  $("updBody").textContent = t("upd.checking_long");
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
  $("updBody").innerHTML = `<div class="upd-err">${esc(t("upd.timeout"))}</div>`;
  _updating = false;
  startDashboard();   // 恢复轮询和失联检测：否则关掉弹窗后大屏是一张不更新、也不报警的静止画面
}

async function applyUpdate() {
  if (!_updInfo || !_updInfo.behind) return;
  if (_updForce && !confirm(t("upd.retry_confirm"))) return;
  if (!confirm(t("upd.apply_confirm", { n: _updInfo.behind }))) return;
  _updating = true;
  $("updApply").disabled = true; $("updRecheck").disabled = true;
  $("updBody").textContent = t("upd.applying");
  let r;
  try { r = await jpost("/api/update/apply", { force: _updForce }); } catch (e) { r = null; }
  $("updRecheck").disabled = false;
  if (!r || !r.ok) {
    _updating = false;
    $("updBody").innerHTML = `<div class="upd-err">${esc(t("upd.apply_fail", { msg: (r && (r.msg || r.error)) || t("req.error") }))}</div>
      <div class="upd-note">${esc(t("upd.rollback_note"))}</div>`;
    return;
  }
  $("updBody").innerHTML = `<div class="upd-ok">${esc(t("upd.ok_restarting", { from: r.from, to: r.to }))}</div>
      <div class="muted">${esc(t("upd.refresh_soon"))}</div>`;
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
        toast(t("toast.ws_denied"));
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
  stopCloudPolling();
  _cloudOpenEpoch++;
  $("cloudModal").classList.add("hidden");
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_wsRetryTimer) { clearTimeout(_wsRetryTimer); _wsRetryTimer = null; }
  if (_ws) { const w = _ws; _ws = null; try { w.close(); } catch (e) {} }
}

function startDashboard() {
  _wsStop = false;
  _wsRetry = 0;   // 重新登录 = 全新一轮连接，别背着上一轮的退避时长
  lastOkMs = Date.now();   // 登录成功，重置失联计时，避免刚进来误报
  loadSegOptions();
  loadRebootSettings();
  refreshAll();
  connectWS();
  if (!_timer) _timer = setInterval(refreshAll, 30000);  // WS 推送为主，轮询兜底
  if (!_staleTimer) _staleTimer = setInterval(checkStale, 10000);  // 失联自检，每10秒
  if (myRole === "ops" || myRole === "admin") pollCmdProgress(true);   // 刷新页面后接上在跑的分批重启
  if (myRole === "admin") {   // 新版本提示：进来查一次，之后每 30 分钟(后端有 10 分钟缓存)
    startCloudPolling();
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
  if (!d.ok) { $("loginErr").textContent = d.error || t("toast.login_fail"); return; }
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
    toast(t("toast.container_refresh"), "ok");
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


/* ---- i18n boot ---- */
(function bootI18n() {
  function wireSelect(id) {
    const sel = document.getElementById(id);
    if (!sel || sel._i18nWired) return;
    sel._i18nWired = true;
    sel.addEventListener("change", () => {
      I18N.setLang(sel.value, true);
      const other = document.getElementById(id === "langSelect" ? "langSelectLogin" : "langSelect");
      if (other) other.value = sel.value;
    });
  }
  function syncSelects() {
    const v = I18N.lang;
    ["langSelect", "langSelectLogin"].forEach(id => {
      const sel = document.getElementById(id);
      if (sel) sel.value = v;
    });
  }
  if (window.I18N) {
    I18N.init();
    wireSelect("langSelect");
    wireSelect("langSelectLogin");
    syncSelects();
    renderCloudLabels();
    renderCloudStatus();
    document.addEventListener("i18n:change", () => {
      syncSelects();
      renderCloudLabels();
      renderCloudStatus();
      try { setVoice(voiceOn, false); } catch (e) {}
      try { updateSelCount(); } catch (e) {}
      // 登录页只更新控件语言，不发需要登录的请求。已登录时刷新动态内容。
      if (!_wsStop && myUser && $("loginOverlay").classList.contains("hidden")) {
        refreshAll().catch(() => {});
      }
    });
  }
})();
