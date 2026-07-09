#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 存储层：扫描快照 + 告警。"""
import json
import sqlite3
import threading
import time

_lock = threading.RLock()   # 可重入：读也加锁(共享连接并发安全)，且部分读函数会嵌套调用


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    kind    TEXT,                 -- full | quick | manual
    total   INTEGER,
    online  INTEGER,
    offline INTEGER,
    total_hr REAL
);
CREATE INDEX IF NOT EXISTS idx_scans_ts ON scans(ts);
CREATE TABLE IF NOT EXISTS snapshots (
    scan_id  INTEGER NOT NULL,
    ip       TEXT NOT NULL,
    status   TEXT,
    firmware TEXT,
    model    TEXT,
    sn       TEXT,
    hr_rt    REAL,
    hr_avg   REAL,
    power    INTEGER,
    temp     INTEGER,
    eff      REAL,
    uptime   INTEGER,
    worker   TEXT,
    accepted INTEGER,
    rejected INTEGER,
    stale    INTEGER,
    note     TEXT,
    PRIMARY KEY (scan_id, ip)
);
CREATE INDEX IF NOT EXISTS idx_snap_ip ON snapshots(ip);
CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    ip        TEXT,
    type      TEXT,               -- offline | low_hashrate | overheat | no_hashrate | new_miner
    severity  TEXT,               -- info | warn | crit
    detail    TEXT,
    resolved  INTEGER DEFAULT 0,
    resolved_ts INTEGER,
    ack_by    TEXT,
    ack_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alert_active ON alerts(ip, type, resolved);
CREATE TABLE IF NOT EXISTS container_snaps (
    ts       INTEGER NOT NULL,
    ip       TEXT NOT NULL,
    supply_temp REAL, return_temp REAL,
    supply_pressure REAL, return_pressure REAL, flow REAL,
    internal_temp REAL, internal_humidity REAL, tower_inlet_temp REAL, set_temp REAL,
    power1 REAL, power2 REAL, miner_num INTEGER, chip_max_temp INTEGER,
    pumps TEXT, faults TEXT, miner_ips TEXT,
    PRIMARY KEY (ts, ip)
);
CREATE INDEX IF NOT EXISTS idx_csnap_ip ON container_snaps(ip);
CREATE TABLE IF NOT EXISTS known_miners (
    ip          TEXT PRIMARY KEY,
    last_online INTEGER,           -- 最后一次在线时间(名册: 含被限电下线的机器)
    state       TEXT DEFAULT 'active',  -- active | repair(维修中,不报警/不清理/回来自动转active)
    model       TEXT DEFAULT '',   -- 最后已知身份(在线时记住，掉线后回填，便于按客户/型号/SN 搜索)
    sn          TEXT DEFAULT '',
    worker      TEXT DEFAULT '',
    firmware    TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS containers (
    ip          TEXT PRIMARY KEY,
    last_online INTEGER,            -- 最后一次在线时间戳
    online      INTEGER DEFAULT 1,
    supply_temp REAL, return_temp REAL, supply_pressure REAL, return_pressure REAL, flow REAL,
    internal_temp REAL, internal_humidity REAL, tower_inlet_temp REAL, set_temp REAL,
    power1 REAL, power2 REAL, miner_num INTEGER, chip_max_temp INTEGER,
    pumps TEXT, faults TEXT, miner_ips TEXT
);
CREATE TABLE IF NOT EXISTS command_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    user   TEXT,
    action TEXT,
    ip     TEXT,
    ok     INTEGER,
    msg    TEXT
);
"""


def init_db(path):
    conn = connect(path)
    with conn:
        conn.executescript(SCHEMA)
        # 旧库迁移：补列（已存在则忽略）
        for col, typ in (("eff", "REAL"), ("uptime", "INTEGER"), ("worker", "TEXT"),
                         ("accepted", "INTEGER"), ("rejected", "INTEGER"), ("stale", "INTEGER")):
            try:
                conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE known_miners ADD COLUMN state TEXT DEFAULT 'active'")
        except sqlite3.OperationalError:
            pass
        for col in ("model TEXT DEFAULT ''", "sn TEXT DEFAULT ''",
                    "worker TEXT DEFAULT ''", "firmware TEXT DEFAULT ''"):
            try:
                conn.execute(f"ALTER TABLE known_miners ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        for col in ("ack_by TEXT", "ack_at INTEGER"):
            try:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        # 旧版 container_snaps 用 scan_id，迁移到 ts（历史可重建，直接重建）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(container_snaps)").fetchall()]
        if cols and "ts" not in cols:
            conn.execute("DROP TABLE container_snaps")
            conn.executescript(SCHEMA)
    return conn


def save_scan(conn, kind, records, keep_ips=None):
    """落库快照。keep_ips 给定时：只持久化在线机 + 名册内(曾在线/维修)的离线机，
    丢弃大量从未是矿机的死 IP 空记录(全网扫描每轮约1.5万条)，省存储/写放大。
    total/online/offline 按实际落库的"真实机器"统计，使前端在线/总数口径有意义。"""
    if keep_ips is not None:
        kept = [r for r in records if r["status"] == "online" or r["ip"] in keep_ips]
    else:
        kept = records
    online = sum(1 for r in kept if r["status"] == "online")
    total_hr = round(sum(r.get("hr_rt") or 0 for r in kept), 2)
    ts = int(time.time())
    with _lock, conn:
        cur = conn.execute(
            "INSERT INTO scans(ts,kind,total,online,offline,total_hr) VALUES(?,?,?,?,?,?)",
            (ts, kind, len(kept), online, len(kept) - online, total_hr),
        )
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO snapshots(scan_id,ip,status,firmware,model,sn,hr_rt,hr_avg,power,temp,"
            "eff,uptime,worker,accepted,rejected,stale,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, r["ip"], r["status"], r["firmware"], r["model"], r["sn"],
              r.get("hr_rt"), r.get("hr_avg"), r.get("power"), r.get("temp"),
              r.get("eff"), r.get("uptime"), r.get("worker", ""),
              r.get("accepted"), r.get("rejected"), r.get("stale"), r.get("note", "")) for r in kept],
        )
    return sid, ts


CONTAINER_COLS = ("supply_temp", "return_temp", "supply_pressure", "return_pressure", "flow",
                  "internal_temp", "internal_humidity", "tower_inlet_temp", "set_temp",
                  "power1", "power2", "miner_num", "chip_max_temp")


def upsert_containers(conn, containers, ts):
    """持久化在线集装箱最新状态（记住箱子；快巡检刷新已知箱、全量扫描发现新箱）。"""
    if not containers:
        return
    cols = ",".join(CONTAINER_COLS)
    qs = ",".join("?" * len(CONTAINER_COLS))
    with _lock, conn:
        for c in containers:
            vals = [c.get(k) for k in CONTAINER_COLS]
            conn.execute(
                f"INSERT OR REPLACE INTO containers(ip,last_online,online,{cols},pumps,faults,miner_ips) "
                f"VALUES(?,?,1,{qs},?,?,?)",
                (c["ip"], ts, *vals,
                 json.dumps(c.get("pumps") or {}, ensure_ascii=False),
                 json.dumps(c.get("faults") or [], ensure_ascii=False),
                 json.dumps(c.get("miner_ips") or [], ensure_ascii=False)))


def mark_containers_offline(conn, ips):
    """已知箱本轮未采到 → 标记离线（不动 last_online，用于 24h 踢除计时）。"""
    if not ips:
        return
    qm = ",".join("?" * len(ips))
    with _lock, conn:
        conn.execute(f"UPDATE containers SET online=0 WHERE ip IN ({qm})", tuple(ips))


def kick_offline_containers(conn, max_offline_sec=86400):
    """离线超过 max_offline_sec（默认24h）的箱子从记忆中踢除，并清掉其残留冷却告警。返回踢除数。"""
    cutoff = int(time.time()) - max_offline_sec
    ts = int(time.time())
    with _lock, conn:
        victims = [r["ip"] for r in conn.execute(
            "SELECT ip FROM containers WHERE online=0 AND last_online < ?", (cutoff,)).fetchall()]
        if victims:
            qm = ",".join("?" * len(victims))
            conn.execute(f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 "
                         f"AND ip IN ({qm}) AND (type='cooler_offline' OR type LIKE 'cooler:%')",
                         (ts, *victims))
            conn.execute(f"DELETE FROM containers WHERE ip IN ({qm})", victims)
        return len(victims)


def get_containers(conn):
    with _lock:
        rows = conn.execute("SELECT * FROM containers ORDER BY ip").fetchall()
    return [_row_to_container(r) for r in rows]


def known_container_ips(conn):
    with _lock:
        return [r["ip"] for r in conn.execute("SELECT ip FROM containers").fetchall()]


def save_containers(conn, ts, containers):
    if not containers:
        return
    with _lock, conn:
        conn.executemany(
            "INSERT OR REPLACE INTO container_snaps(ts,ip,supply_temp,return_temp,"
            "supply_pressure,return_pressure,flow,internal_temp,internal_humidity,tower_inlet_temp,"
            "set_temp,power1,power2,miner_num,chip_max_temp,pumps,faults,miner_ips)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, c["ip"], c.get("supply_temp"), c.get("return_temp"),
              c.get("supply_pressure"), c.get("return_pressure"), c.get("flow"),
              c.get("internal_temp"), c.get("internal_humidity"), c.get("tower_inlet_temp"),
              c.get("set_temp"), c.get("power1"), c.get("power2"), c.get("miner_num"),
              c.get("chip_max_temp"), json.dumps(c.get("pumps") or {}, ensure_ascii=False),
              json.dumps(c.get("faults") or [], ensure_ascii=False),
              json.dumps(c.get("miner_ips") or [], ensure_ascii=False)) for c in containers],
        )


def _row_to_container(r):
    d = dict(r)
    for k in ("pumps", "faults", "miner_ips"):
        try:
            d[k] = json.loads(d.get(k) or ("[]" if k != "pumps" else "{}"))
        except Exception:
            d[k] = [] if k != "pumps" else {}
    return d


def container_history(conn, ip, limit=300):
    with _lock:
        rows = conn.execute(
            "SELECT ts, supply_temp, return_temp, internal_temp FROM container_snaps "
            "WHERE ip=? ORDER BY ts DESC LIMIT ?", (ip, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def latest_scan(conn):
    with _lock:
        row = conn.execute("SELECT * FROM scans ORDER BY scan_id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def scan_records(conn, scan_id):
    with _lock:
        rows = conn.execute("SELECT * FROM snapshots WHERE scan_id=?", (scan_id,)).fetchall()
    return [dict(r) for r in rows]


def prev_scan_id(conn, before_id, kind=None):
    """上一次扫描的 id；kind 给定时只取同类(避免 quick/full 混排污染对比基准)。"""
    with _lock:
        if kind:
            row = conn.execute("SELECT scan_id FROM scans WHERE scan_id<? AND kind=? "
                               "ORDER BY scan_id DESC LIMIT 1", (before_id, kind)).fetchone()
        else:
            row = conn.execute("SELECT scan_id FROM scans WHERE scan_id<? ORDER BY scan_id DESC LIMIT 1",
                               (before_id,)).fetchone()
    return row["scan_id"] if row else None


def upsert_known_miners(conn, recs, ts):
    """把在线矿机记入名册(last_online=ts) + 记住身份(型号/SN/矿工名/固件)，供掉线后仍可识别/搜索。
    不覆盖已有 state(维修标记保留)；身份字段仅在本次非空时更新(避免偶发读不到把已知身份抹掉)。
    recs 可为 IP 字符串列表(兼容)或在线记录(dict)列表。"""
    if not recs:
        return
    rows = []
    for r in recs:
        if isinstance(r, str):
            rows.append((r, ts, "", "", "", ""))
        else:
            rows.append((r["ip"], ts, r.get("model") or "", r.get("sn") or "",
                         r.get("worker") or "", r.get("firmware") or ""))
    with _lock, conn:
        conn.executemany(
            "INSERT INTO known_miners(ip,last_online,state,model,sn,worker,firmware) "
            "VALUES(?,?,'active',?,?,?,?) "
            "ON CONFLICT(ip) DO UPDATE SET last_online=excluded.last_online, "
            "model=CASE WHEN excluded.model!='' THEN excluded.model ELSE known_miners.model END, "
            "sn=CASE WHEN excluded.sn!='' THEN excluded.sn ELSE known_miners.sn END, "
            "worker=CASE WHEN excluded.worker!='' THEN excluded.worker ELSE known_miners.worker END, "
            "firmware=CASE WHEN excluded.firmware!='' THEN excluded.firmware ELSE known_miners.firmware END",
            rows)


def known_identity(conn):
    """名册里每台机的最后已知身份 {ip: {model,sn,worker,firmware,last_online}}，用于回填离线机。"""
    with _lock:
        rows = conn.execute(
            "SELECT ip,model,sn,worker,firmware,last_online FROM known_miners").fetchall()
    return {r["ip"]: dict(r) for r in rows}


def roster_ips(conn, max_age_days=7):
    """机器名册：max_age_days 内在线过的 + 维修中的全部矿机 IP（快巡检目标）。"""
    cutoff = int(time.time()) - max_age_days * 86400
    with _lock:
        return [r["ip"] for r in conn.execute(
            "SELECT ip FROM known_miners WHERE last_online>=? OR state='repair'", (cutoff,)).fetchall()]


def repair_ips(conn):
    with _lock:
        return set(r["ip"] for r in
                   conn.execute("SELECT ip FROM known_miners WHERE state='repair'").fetchall())


def set_machine_state(conn, ips, state):
    if not ips:
        return
    qm = ",".join("?" * len(ips))
    with _lock, conn:
        conn.execute(f"UPDATE known_miners SET state=? WHERE ip IN ({qm})", (state, *ips))


def remove_miners(conn, ips):
    """下架移除：从名册删除，并清掉其残留 offline/zero/reject 告警（不再探测/告警）。
    删完后，若某网段名册已"无剩余机器"，才顺带清掉该段的 segment_down——
    这样迁移残影全下架后网段告警自动消，但真"大面积掉线"期间(段内还有机器)不会被误消。"""
    if not ips:
        return
    ts = int(time.time())
    bases = sorted({".".join(ip.split(".")[:3]) for ip in ips if ip.count(".") == 3})
    qm = ",".join("?" * len(ips))
    with _lock, conn:
        conn.execute(f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 AND ip IN ({qm}) "
                     f"AND type IN ('offline','zero','reject')", (ts, *ips))
        conn.execute(f"DELETE FROM known_miners WHERE ip IN ({qm})", tuple(ips))
        # 同时从最近一次扫描快照删掉，下架后立刻从矿机列表消失(否则要等下轮扫描列表才更新)
        conn.execute(f"DELETE FROM snapshots WHERE ip IN ({qm}) "
                     f"AND scan_id=(SELECT MAX(scan_id) FROM scans)", tuple(ips))
        for base in bases:   # 仅当该网段名册已空，才消该段 segment_down
            left = conn.execute("SELECT COUNT(*) FROM known_miners WHERE ip LIKE ?",
                                (base + ".%",)).fetchone()[0]
            if left == 0:
                conn.execute("UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 "
                             "AND type='segment_down' AND ip=?", (ts, base + ".x"))


def online_ips(conn):
    """最近一次扫描中在线的 IP 列表（供快巡检使用）。"""
    ls = latest_scan(conn)
    if not ls:
        return []
    with _lock:
        rows = conn.execute("SELECT ip FROM snapshots WHERE scan_id=? AND status='online'",
                            (ls["scan_id"],)).fetchall()
    return [r["ip"] for r in rows]


def ip_history(conn, ip, limit=200):
    with _lock:
        rows = conn.execute(
            "SELECT s.ts, n.status, n.hr_rt, n.temp, n.power FROM snapshots n "
            "JOIN scans s ON s.scan_id=n.scan_id WHERE n.ip=? ORDER BY s.ts DESC LIMIT ?",
            (ip, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def hashrate_trend(conn, limit=288):
    with _lock:
        # 只取定时全网/快巡检，排除 manual 手动扫描(时间点不规则会让趋势出锯齿)
        rows = conn.execute("SELECT ts,total_hr,online FROM scans WHERE kind IN ('full','quick') "
                            "ORDER BY scan_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---- 告警 ----
def active_alert(conn, ip, type_):
    with _lock:
        row = conn.execute("SELECT * FROM alerts WHERE ip=? AND type=? AND resolved=0 "
                           "ORDER BY id DESC LIMIT 1", (ip, type_)).fetchone()
    return dict(row) if row else None


def recent_alert(conn, ip, type_):
    """该 ip+type 最近一条(含已恢复)，用于冷却判断。"""
    with _lock:
        row = conn.execute("SELECT * FROM alerts WHERE ip=? AND type=? ORDER BY id DESC LIMIT 1",
                           (ip, type_)).fetchone()
    return dict(row) if row else None


def raise_alert(conn, ip, type_, severity, detail):
    ts = int(time.time())
    with _lock, conn:
        cur = conn.execute(
            "INSERT INTO alerts(ts,ip,type,severity,detail) VALUES(?,?,?,?,?)",
            (ts, ip, type_, severity, detail))
        return cur.lastrowid


def resolve_alert(conn, ip, type_):
    ts = int(time.time())
    with _lock, conn:
        conn.execute("UPDATE alerts SET resolved=1, resolved_ts=? WHERE ip=? AND type=? AND resolved=0",
                     (ts, ip, type_))


def resolve_types_except(conn, keep_types):
    """把不在 keep_types 内的活跃告警全部置为已恢复（用于告警类型精简后清理旧类型）。"""
    ts = int(time.time())
    qm = ",".join("?" * len(keep_types))
    with _lock, conn:
        conn.execute(
            f"UPDATE alerts SET resolved=1, resolved_ts=? WHERE resolved=0 "
            f"AND type NOT IN ({qm}) AND type NOT LIKE 'cooler%'",
            (ts, *keep_types))


def get_alert(conn, alert_id):
    with _lock:
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    return dict(row) if row else None


def ack_alert(conn, alert_id, user):
    with _lock, conn:
        conn.execute("UPDATE alerts SET ack_by=?, ack_at=? WHERE id=? AND resolved=0",
                     (user, int(time.time()), alert_id))


def list_alerts(conn, active_only=True, limit=200):
    q = "SELECT * FROM alerts"
    if active_only:
        q += " WHERE resolved=0"
    q += " ORDER BY id DESC LIMIT ?"
    with _lock:
        rows = conn.execute(q, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_active_by_type(conn):
    """活跃告警按类型精确计数(无 limit 截断)，供前端真实告警数/语音播报用。
    大面积事件(全场零算力/断电)时活跃告警可达数千，list_alerts 的 200 条上限会让计数失真。"""
    with _lock:
        rows = conn.execute(
            "SELECT type, COUNT(*) c FROM alerts WHERE resolved=0 GROUP BY type").fetchall()
    return {r["type"]: r["c"] for r in rows}


def count_active(conn):
    """活跃告警总数(精确，不截断)。"""
    with _lock:
        return conn.execute("SELECT COUNT(*) FROM alerts WHERE resolved=0").fetchone()[0]


def active_alerts_by_type(conn, type_=None, like=None):
    """按类型取全部活跃告警(无 limit 截断)。供 segment_down/cooler 恢复用，
    避免大面积掉线时数百条告警把早期 fire 的告警挤出 list_alerts 的前 200 名导致永不恢复。"""
    q = "SELECT * FROM alerts WHERE resolved=0"
    args = []
    if type_:
        q += " AND type=?"
        args.append(type_)
    if like:
        q += " AND type LIKE ?"
        args.append(like + "%")
    with _lock:
        rows = conn.execute(q, tuple(args)).fetchall()
    return [dict(r) for r in rows]


def resolve_alerts_for(conn, ips, types=None, like=None):
    """批量恢复指定 ip 列表的活跃告警(下架/踢除时清理残留)。types:精确类型列表; like:类型前缀(如 'cooler')。"""
    if not ips:
        return
    ts = int(time.time())
    qm = ",".join("?" * len(ips))
    with _lock, conn:
        if types:
            tqm = ",".join("?" * len(types))
            conn.execute(f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 "
                         f"AND ip IN ({qm}) AND type IN ({tqm})", (ts, *ips, *types))
        if like:
            conn.execute(f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 "
                         f"AND ip IN ({qm}) AND type LIKE ?", (ts, *ips, like + "%"))


def log_commands(conn, user, action, results):
    ts = int(time.time())
    with _lock, conn:
        conn.executemany(
            "INSERT INTO command_log(ts,user,action,ip,ok,msg) VALUES(?,?,?,?,?,?)",
            [(ts, user, action, r["ip"], 1 if r["ok"] else 0, r["msg"]) for r in results])


def list_commands(conn, limit=100):
    with _lock:
        rows = conn.execute("SELECT * FROM command_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def customer_report(conn, hours=24):
    """按客户(矿工名)统计周期内：机器数、可用率%、交付算力 TH·h、耗电 kWh。"""
    now = int(time.time())
    frm = now - hours * 3600
    with _lock:
        # 排除 manual 手动扫描：否则它插入的不规则时间点会把上一次 full 的时长 delta
        # 从~300s 砍到几十秒，导致交付算力 TH·h / 耗电 kWh / 可用率被错配低估
        scans = conn.execute("SELECT scan_id, ts FROM scans WHERE ts>=? AND kind IN ('full','quick') "
                             "ORDER BY ts", (frm,)).fetchall()
    if not scans:
        return []
    sids = [s["scan_id"] for s in scans]
    # 每次扫描代表的时长(到下一次扫描)；末次用平均间隔
    deltas = {}
    for i, s in enumerate(scans):
        if i + 1 < len(scans):
            d = scans[i + 1]["ts"] - s["ts"]
        else:
            d = (scans[-1]["ts"] - scans[0]["ts"]) // max(1, len(scans) - 1) if len(scans) > 1 else 300
        deltas[s["scan_id"]] = max(1, d)
    qm = ",".join("?" * len(sids))
    with _lock:
        rows = conn.execute(
            f"SELECT scan_id, ip, worker, status, hr_rt, power FROM snapshots WHERE scan_id IN ({qm})",
            sids).fetchall()
    # 先建 ip→矿工名（离线行 worker 为空，需用该机已知矿工名归属，停机才能算到正确客户）
    ip2worker = {}
    for r in rows:
        if r["worker"]:
            ip2worker[r["ip"]] = r["worker"]
    agg = {}
    for r in rows:
        w = ip2worker.get(r["ip"]) or "(未知)"
        a = agg.setdefault(w, {"worker": w, "ips": set(), "samples": 0, "online": 0,
                               "th_h": 0.0, "kwh": 0.0})
        a["ips"].add(r["ip"])
        a["samples"] += 1
        if r["status"] == "online":
            a["online"] += 1
            dt_h = deltas.get(r["scan_id"], 300) / 3600.0
            if r["hr_rt"]:
                a["th_h"] += r["hr_rt"] * dt_h
            if r["power"]:
                a["kwh"] += r["power"] * dt_h / 1000.0
    out = []
    for a in agg.values():
        out.append({"worker": a["worker"], "machines": len(a["ips"]),
                    "uptime_pct": round(a["online"] / a["samples"] * 100, 2) if a["samples"] else 0,
                    "delivered_th_h": round(a["th_h"], 1),
                    "power_kwh": round(a["kwh"], 1)})
    out.sort(key=lambda x: x["machines"], reverse=True)
    return out


def prune(conn, retention_days, roster_days=7):
    cutoff = int(time.time()) - retention_days * 86400
    roster_cut = int(time.time()) - roster_days * 86400
    with _lock, conn:
        old = [r["scan_id"] for r in
               conn.execute("SELECT scan_id FROM scans WHERE ts<?", (cutoff,)).fetchall()]
        if old:
            qm = ",".join("?" * len(old))
            conn.execute(f"DELETE FROM snapshots WHERE scan_id IN ({qm})", old)
            conn.execute(f"DELETE FROM scans WHERE scan_id IN ({qm})", old)
        conn.execute("DELETE FROM container_snaps WHERE ts<?", (cutoff,))
        conn.execute("DELETE FROM alerts WHERE resolved=1 AND resolved_ts<?", (cutoff,))
        conn.execute("DELETE FROM known_miners WHERE last_online<? AND state='active'",  # 维修中不清理
                     (roster_cut,))
        # 命令审计日志只保留最近 N 条，防无限增长
        conn.execute("DELETE FROM command_log WHERE id NOT IN "
                     "(SELECT id FROM command_log ORDER BY id DESC LIMIT 5000)")
    return len(old)
