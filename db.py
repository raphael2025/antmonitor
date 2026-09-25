#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 存储层：扫描快照 + 告警 + 计费聚合。

并发模型（重要）：
- **写**走唯一一条写连接，由 _wlock 串行化（SQLite 本来就只允许一个写者）。
- **读**走各线程自己的只读连接（PRAGMA query_only），WAL 模式下读不阻塞写、写不阻塞读。
  之前读写共用一把全局锁，等于把 WAL 的并发优势全部抵消：每轮扫描落库(5000+行)
  和告警评估期间，所有 API 请求都在排队。
- 批量写一律用一个事务（executemany），而不是每行一次 commit——逐行提交是 WAL
  暴涨到几百 MB 的主因（每 commit 一条 WAL 帧 + fsync）。
"""
import json
import os
import sqlite3
import sys
import threading
import time

import logs

log = logs.get(__name__)

_wlock = threading.RLock()   # 只保护写连接；读连接每线程独立，无需加锁
_DB_PATH = None
_local = threading.local()
_readers = []                # 所有读连接(仅用于进程退出时统一关闭)
_readers_lock = threading.Lock()
_reader_gen = 0              # 代数：close_readers() 后 +1，使各线程缓存的旧句柄失效

HOUR = 3600

WRITE_BUSY_TIMEOUT_MS = 30000
# 只读连接等得更久：VACUUM 会对整个库加排他锁，大库耗时可达数分钟，
# 30s 超时会让期间所有 API 读请求直接抛 "database is locked"（server.py 无兜底）。
# 3 分钟覆盖绝大多数 VACUUM 场景，读请求宁可慢也不要报错。
READ_BUSY_TIMEOUT_MS = 180000


def connect(path, query_only=False):
    busy_ms = READ_BUSY_TIMEOUT_MS if query_only else WRITE_BUSY_TIMEOUT_MS
    conn = sqlite3.connect(path, check_same_thread=False, timeout=busy_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL：WAL 下崩溃仍不会损坏数据库（最坏丢最后几个事务），但省掉每次提交的 fsync，
    # 对每轮上万行写入的落库路径是数量级的提速。
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    conn.execute("PRAGMA foreign_keys=ON")
    if query_only:
        conn.execute("PRAGMA query_only=1")
    return conn


def _r(conn):
    """本线程只读连接；测试用内存库(或未 init_db)时回退到传入的写连接。

    缓存键带上库路径和"代数"：close_readers() 之后线程本地那份句柄已经失效，
    若还照旧复用就会抛 "Cannot operate on a closed database"。换库(测试)同理。"""
    if not _DB_PATH or _DB_PATH == ":memory:":
        return conn
    st = getattr(_local, "reader", None)
    if st is not None and st[0] == _DB_PATH and st[1] == _reader_gen:
        return st[2]
    c = connect(_DB_PATH, query_only=True)
    _local.reader = (_DB_PATH, _reader_gen, c)
    with _readers_lock:
        _readers.append(c)
    return c


def close_readers():
    """关闭所有只读连接并作废缓存(各线程下次访问时会自动重建)。返回关闭的连接数。"""
    global _reader_gen
    with _readers_lock:
        _reader_gen += 1
        n = len(_readers)
        for c in _readers:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        _readers.clear()
        return n


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
CREATE INDEX IF NOT EXISTS idx_scans_kind_ts ON scans(kind, ts);
CREATE TABLE IF NOT EXISTS snapshots (
    scan_id  INTEGER NOT NULL,
    ip       TEXT NOT NULL,
    status   TEXT,
    firmware TEXT,
    model    TEXT,
    sn       TEXT,
    mac      TEXT,
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
    type      TEXT,
    severity  TEXT,
    detail    TEXT,
    resolved  INTEGER DEFAULT 0,
    resolved_ts INTEGER,
    ack_by    TEXT,
    ack_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alert_active ON alerts(ip, type, resolved);
CREATE INDEX IF NOT EXISTS idx_alert_resolved ON alerts(resolved, type);
CREATE INDEX IF NOT EXISTS idx_alert_recent ON alerts(resolved, resolved_ts);
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
    last_online INTEGER,
    state       TEXT DEFAULT 'active',
    model       TEXT DEFAULT '',
    sn          TEXT DEFAULT '',
    mac         TEXT DEFAULT '',
    worker      TEXT DEFAULT '',
    firmware    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_known_last ON known_miners(last_online);
CREATE TABLE IF NOT EXISTS containers (
    ip          TEXT PRIMARY KEY,
    last_online INTEGER,
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
-- 计费聚合：每小时每客户一行。明细快照按 retention_days(默认3天)清理，
-- 但结算依据必须长期可查(月度对账)，故单独聚合保留 rollup_retention_days(默认400天)。
CREATE TABLE IF NOT EXISTS worker_hourly (
    hour     INTEGER NOT NULL,      -- 对齐到整点的 unix 秒
    worker   TEXT NOT NULL,
    machines INTEGER,               -- 该小时出现过的机器数
    samples  INTEGER,               -- 样本数(该小时所有扫描×该客户机器)
    online   INTEGER,               -- 其中在线样本数 → 可用率
    th_h     REAL,                  -- 交付算力 TH·h
    kwh      REAL,                  -- 耗电 kWh
    PRIMARY KEY (hour, worker)
);
CREATE INDEX IF NOT EXISTS idx_wh_hour ON worker_hourly(hour);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def init_db(path):
    global _DB_PATH
    if _DB_PATH and _DB_PATH != path:   # 换库(测试/多实例)：旧读连接指向旧文件，必须作废
        close_readers()
    _DB_PATH = path
    conn = connect(path)
    with _wlock, conn:
        conn.executescript(SCHEMA)
        # 旧库迁移：补列（已存在则忽略）
        for col, typ in (("eff", "REAL"), ("uptime", "INTEGER"), ("worker", "TEXT"),
                         ("accepted", "INTEGER"), ("rejected", "INTEGER"), ("stale", "INTEGER")):
            _try(conn, f"ALTER TABLE snapshots ADD COLUMN {col} {typ}")
        _try(conn, "ALTER TABLE snapshots ADD COLUMN mac TEXT")
        _try(conn, "ALTER TABLE known_miners ADD COLUMN state TEXT DEFAULT 'active'")
        _try(conn, "ALTER TABLE known_miners ADD COLUMN mac TEXT DEFAULT ''")
        for col in ("model TEXT DEFAULT ''", "sn TEXT DEFAULT ''",
                    "worker TEXT DEFAULT ''", "firmware TEXT DEFAULT ''"):
            _try(conn, f"ALTER TABLE known_miners ADD COLUMN {col}")
        for col in ("ack_by TEXT", "ack_at INTEGER"):
            _try(conn, f"ALTER TABLE alerts ADD COLUMN {col}")
        # 旧版 container_snaps 用 scan_id，迁移到 ts（历史可重建，直接重建）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(container_snaps)").fetchall()]
        if cols and "ts" not in cols:
            conn.execute("DROP TABLE container_snaps")
            conn.executescript(SCHEMA)
    return conn


def _try(conn, sql):
    try:
        conn.execute(sql)
    except sqlite3.OperationalError:
        pass


# ---- meta ----
def meta_get(conn, k, default=None):
    row = _r(conn).execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, k, v):
    with _wlock, conn:
        conn.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


SNAP_COLS = ("ip", "status", "firmware", "model", "sn", "mac", "hr_rt", "hr_avg", "power", "temp",
             "eff", "uptime", "worker", "accepted", "rejected", "stale", "note")


def save_scan(conn, kind, records, keep_ips=None):
    """落库快照，返回 (scan_id, ts, kept)。

    keep_ips 给定时：只持久化在线机 + 名册内(曾在线/维修)的离线机，丢弃大量从未是
    矿机的死 IP 空记录(全网扫描每轮约1.5万条)，省存储/写放大。

    返回的 kept 是**与数据库行同构**的干净记录(只含 SNAP_COLS + scan_id)，
    调用方直接拿去做内存快照，保证 API 无论走内存还是回落查库，字段完全一致。"""
    if keep_ips is not None:
        kept_src = [r for r in records if r["status"] == "online" or r["ip"] in keep_ips]
    else:
        kept_src = list(records)
    online = sum(1 for r in kept_src if r["status"] == "online")
    # status="unknown"(本轮扫描超时没来得及探测的机器)不计入 total/online/offline——
    # 它既不是确认在线也不是确认离线，算进 offline 会在总览上凭空多出一堆假离线。
    offline = sum(1 for r in kept_src if r["status"] == "offline")
    total_hr = round(sum(r.get("hr_rt") or 0 for r in kept_src), 2)
    ts = int(time.time())
    with _wlock, conn:
        cur = conn.execute(
            "INSERT INTO scans(ts,kind,total,online,offline,total_hr) VALUES(?,?,?,?,?,?)",
            (ts, kind, online + offline, online, offline, total_hr),
        )
        sid = cur.lastrowid
        kept = [{"scan_id": sid, "ip": r["ip"], "status": r["status"],
                 "firmware": r.get("firmware") or "", "model": r.get("model") or "",
                 "sn": r.get("sn") or "", "mac": r.get("mac") or "",
                 "hr_rt": r.get("hr_rt"), "hr_avg": r.get("hr_avg"),
                 "power": r.get("power"), "temp": r.get("temp"), "eff": r.get("eff"),
                 "uptime": r.get("uptime"), "worker": r.get("worker") or "",
                 "accepted": r.get("accepted"), "rejected": r.get("rejected"),
                 "stale": r.get("stale"), "note": r.get("note") or ""} for r in kept_src]
        conn.executemany(
            "INSERT INTO snapshots(scan_id,ip,status,firmware,model,sn,mac,hr_rt,hr_avg,power,temp,"
            "eff,uptime,worker,accepted,rejected,stale,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [tuple([sid] + [r[c] for c in SNAP_COLS]) for r in kept],
        )
    return sid, ts, kept


CONTAINER_COLS = ("supply_temp", "return_temp", "supply_pressure", "return_pressure", "flow",
                  "internal_temp", "internal_humidity", "tower_inlet_temp", "set_temp",
                  "power1", "power2", "miner_num", "chip_max_temp")


def upsert_containers(conn, containers, ts):
    """持久化在线集装箱最新状态（记住箱子）。"""
    if not containers:
        return
    cols = ",".join(CONTAINER_COLS)
    qs = ",".join("?" * len(CONTAINER_COLS))
    rows = [(c["ip"], ts, *[c.get(k) for k in CONTAINER_COLS],
             json.dumps(c.get("pumps") or {}, ensure_ascii=False),
             json.dumps(c.get("faults") or [], ensure_ascii=False),
             json.dumps(c.get("miner_ips") or [], ensure_ascii=False))
            for c in containers]
    with _wlock, conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO containers(ip,last_online,online,{cols},pumps,faults,miner_ips) "
            f"VALUES(?,?,1,{qs},?,?,?)", rows)


def mark_containers_offline(conn, ips):
    """已知箱本轮未采到 → 标记离线（不动 last_online，用于 24h 踢除计时）。"""
    if not ips:
        return
    qm = ",".join("?" * len(ips))
    with _wlock, conn:
        conn.execute(f"UPDATE containers SET online=0 WHERE ip IN ({qm})", tuple(ips))


def kick_offline_containers(conn, max_offline_sec=86400):
    """离线超过 max_offline_sec 的箱子从记忆中踢除，并清掉其残留冷却告警。返回踢除数。"""
    cutoff = int(time.time()) - max_offline_sec
    ts = int(time.time())
    with _wlock, conn:
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
    rows = _r(conn).execute("SELECT * FROM containers ORDER BY ip").fetchall()
    return [_row_to_container(r) for r in rows]


def known_container_ips(conn):
    return [r["ip"] for r in _r(conn).execute("SELECT ip FROM containers").fetchall()]


def save_containers(conn, ts, containers):
    if not containers:
        return
    with _wlock, conn:
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
        except (ValueError, TypeError):
            d[k] = [] if k != "pumps" else {}
    return d


def container_history(conn, ip, limit=300):
    rows = _r(conn).execute(
        "SELECT ts, supply_temp, return_temp, internal_temp FROM container_snaps "
        "WHERE ip=? ORDER BY ts DESC LIMIT ?", (ip, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def latest_scan(conn):
    row = _r(conn).execute("SELECT * FROM scans ORDER BY scan_id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def scan_records(conn, scan_id):
    rows = _r(conn).execute("SELECT * FROM snapshots WHERE scan_id=?", (scan_id,)).fetchall()
    return [dict(r) for r in rows]


def prev_scan_id(conn, before_id, kind=None):
    """上一次扫描的 id；kind 给定时只取同类(避免 quick/full 混排污染对比基准)。"""
    if kind:
        row = _r(conn).execute("SELECT scan_id FROM scans WHERE scan_id<? AND kind=? "
                               "ORDER BY scan_id DESC LIMIT 1", (before_id, kind)).fetchone()
    else:
        row = _r(conn).execute("SELECT scan_id FROM scans WHERE scan_id<? ORDER BY scan_id DESC "
                               "LIMIT 1", (before_id,)).fetchone()
    return row["scan_id"] if row else None


def upsert_known_miners(conn, recs, ts):
    """在线矿机记入名册 + 记住身份(型号/SN/矿工名/固件)，供掉线后仍可识别/搜索。
    不覆盖已有 state(维修标记保留)；身份字段仅在本次非空时更新。"""
    if not recs:
        return
    from miner_core import sn_valid   # 延迟导入：db 不该在模块级依赖扫描层
    rows = []
    for r in recs:
        if isinstance(r, str):
            rows.append((r, ts, "", "", "", "", ""))
        else:
            # N/A/error/占位串不算 SN：不能覆盖名册里已知的真 SN(否则掉线后回填出 N/A，搜不到)
            sn = r.get("sn") or ""
            rows.append((r["ip"], ts, r.get("model") or "", sn if sn_valid(sn) else "",
                         r.get("mac") or "",
                         r.get("worker") or "", r.get("firmware") or ""))
    with _wlock, conn:
        conn.executemany(
            "INSERT INTO known_miners(ip,last_online,state,model,sn,mac,worker,firmware) "
            "VALUES(?,?,'active',?,?,?,?,?) "
            "ON CONFLICT(ip) DO UPDATE SET last_online=excluded.last_online, "
            "state=CASE WHEN known_miners.state='migrated' THEN 'active' "
            "ELSE known_miners.state END, "
            "model=CASE WHEN excluded.model!='' THEN excluded.model ELSE known_miners.model END, "
            "sn=CASE WHEN excluded.sn!='' THEN excluded.sn ELSE known_miners.sn END, "
            # 同一 IP 换了一台机(SN 变了)：旧 MAC 属于上一台，作废，否则残影判定会张冠李戴
            "mac=CASE WHEN excluded.mac!='' THEN excluded.mac "
            "WHEN excluded.sn!='' AND excluded.sn!=known_miners.sn THEN '' "
            "ELSE known_miners.mac END, "
            "worker=CASE WHEN excluded.worker!='' THEN excluded.worker ELSE known_miners.worker END, "
            "firmware=CASE WHEN excluded.firmware!='' THEN excluded.firmware "
            "ELSE known_miners.firmware END",
            rows)


def known_identity(conn):
    """名册里每台机的最后已知身份 {ip: {...}}，用于回填离线机。"""
    rows = _r(conn).execute(
        "SELECT ip,model,sn,mac,worker,firmware,last_online,state FROM known_miners").fetchall()
    return {r["ip"]: dict(r) for r in rows}


def roster_ips(conn, max_age_days=7):
    """机器名册：max_age_days 内在线过的 + 维修中的全部矿机 IP（快巡检目标）。"""
    cutoff = int(time.time()) - max_age_days * 86400
    return [r["ip"] for r in _r(conn).execute(
        "SELECT ip FROM known_miners WHERE (state='active' AND last_online>=?) "
        "OR state='repair'", (cutoff,)).fetchall()]


def last_online_map(conn):
    """{ip: 最后一次在线的扫描时间}，掉线告警按"这次掉线报过没有"判断用。"""
    return {r["ip"]: r["last_online"] or 0 for r in
            _r(conn).execute("SELECT ip,last_online FROM known_miners").fetchall()}


def last_alert_ts(conn, type_, since=0):
    """{ip: 该类告警最近一次发出时间}(含已恢复的)。"""
    return {r["ip"]: r["ts"] for r in _r(conn).execute(
        "SELECT ip, MAX(ts) ts FROM alerts WHERE type=? AND ts>=? GROUP BY ip",
        (type_, since)).fetchall()}


def repair_ips(conn):
    return set(r["ip"] for r in
               _r(conn).execute("SELECT ip FROM known_miners WHERE state='repair'").fetchall())


def set_machine_state(conn, ips, state):
    if not ips:
        return
    with _wlock, conn:
        conn.executemany("UPDATE known_miners SET state=? WHERE ip=?", [(state, ip) for ip in ips])


def mark_miners_migrated(conn, ips):
    """Quarantine superseded IPs without deleting their inventory/history."""
    if not ips:
        return
    ts = int(time.time())
    ips = list(dict.fromkeys(ips))
    types = ("offline", "zero", "reject", "low_hashrate", "overheat")
    tqm = ",".join("?" * len(types))
    with _wlock, conn:
        conn.executemany("UPDATE known_miners SET state='migrated' WHERE ip=?",
                         [(ip,) for ip in ips])
        conn.executemany(
            f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 AND ip=? "
            f"AND type IN ({tqm})", [(ts, ip, *types) for ip in ips])


def remove_miners(conn, ips):
    """下架移除：从名册删除，并清掉其残留 offline/zero/reject/low_hr/overheat 告警。
    删完后，若某网段名册已"无剩余机器"，才顺带清掉该段的 segment_down——
    这样迁移残影全下架后网段告警自动消，但真"大面积掉线"期间不会被误消。"""
    if not ips:
        return
    ts = int(time.time())
    ips = list(dict.fromkeys(ips))
    bases = sorted({".".join(ip.split(".")[:3]) for ip in ips if ip.count(".") == 3})
    types = ("offline", "zero", "reject", "low_hashrate", "overheat")
    tqm = ",".join("?" * len(types))
    with _wlock, conn:
        conn.executemany(
            f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 AND ip=? "
            f"AND type IN ({tqm})", [(ts, ip, *types) for ip in ips])
        conn.executemany("DELETE FROM known_miners WHERE ip=?", [(ip,) for ip in ips])
        # 同时从最近一次扫描快照删掉，下架后立刻从矿机列表消失
        last = conn.execute("SELECT MAX(scan_id) FROM scans").fetchone()[0]
        if last:
            conn.executemany("DELETE FROM snapshots WHERE ip=? AND scan_id=?",
                             [(ip, last) for ip in ips])
        for base in bases:   # 仅当该网段名册已空，才消该段 segment_down
            left = conn.execute("SELECT COUNT(*) FROM known_miners WHERE ip LIKE ?",
                                (base + ".%",)).fetchone()[0]
            if left == 0:
                conn.execute("UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 "
                             "AND type='segment_down' AND ip=?", (ts, base + ".x"))


def online_ips(conn):
    """最近一次扫描中在线的 IP 列表。"""
    ls = latest_scan(conn)
    if not ls:
        return []
    rows = _r(conn).execute("SELECT ip FROM snapshots WHERE scan_id=? AND status='online'",
                            (ls["scan_id"],)).fetchall()
    return [r["ip"] for r in rows]


def ip_history(conn, ip, limit=200):
    rows = _r(conn).execute(
        "SELECT s.ts, n.status, n.hr_rt, n.temp, n.power FROM snapshots n "
        "JOIN scans s ON s.scan_id=n.scan_id WHERE n.ip=? ORDER BY s.ts DESC LIMIT ?",
        (ip, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def hashrate_trend(conn, limit=288):
    # 只取定时扫描，排除 manual(时间点不规则会让趋势出锯齿)
    rows = _r(conn).execute("SELECT ts,total_hr,online FROM scans WHERE kind IN ('full','quick') "
                            "ORDER BY scan_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---- 告警 ----
def active_alert(conn, ip, type_):
    row = _r(conn).execute("SELECT * FROM alerts WHERE ip=? AND type=? AND resolved=0 "
                           "ORDER BY id DESC LIMIT 1", (ip, type_)).fetchone()
    return dict(row) if row else None


def recent_alert(conn, ip, type_):
    """该 ip+type 最近一条(含已恢复)，用于冷却判断。"""
    row = _r(conn).execute("SELECT * FROM alerts WHERE ip=? AND type=? ORDER BY id DESC LIMIT 1",
                           (ip, type_)).fetchone()
    return dict(row) if row else None


def alert_state(conn, cooldown_since=0):
    """一次性预加载告警状态，替代"每台机两次点查"。

    返回 (active, cooled)：
      active: {(ip,type)} 当前活跃告警键集合
      cooled: {(ip,type): resolved_ts} 在 cooldown_since 之后恢复过的(仍在冷却期内)
    5000 台机器每轮评估原本要做上万次点查，现在两条 SQL 搞定。
    """
    rc = _r(conn)
    active = {(r["ip"], r["type"]) for r in
              rc.execute("SELECT ip,type FROM alerts WHERE resolved=0").fetchall()}
    cooled = {}
    if cooldown_since:
        for r in rc.execute(
                "SELECT ip,type,MAX(resolved_ts) rt FROM alerts "
                "WHERE resolved=1 AND resolved_ts>=? GROUP BY ip,type",
                (cooldown_since,)).fetchall():
            cooled[(r["ip"], r["type"])] = r["rt"]
    return active, cooled


def raise_alert(conn, ip, type_, severity, detail):
    ts = int(time.time())
    with _wlock, conn:
        cur = conn.execute(
            "INSERT INTO alerts(ts,ip,type,severity,detail) VALUES(?,?,?,?,?)",
            (ts, ip, type_, severity, detail))
        return cur.lastrowid


def raise_alerts_bulk(conn, rows):
    """rows: [(ip, type, severity, detail), ...] 单事务批量插入。"""
    if not rows:
        return 0
    ts = int(time.time())
    with _wlock, conn:
        conn.executemany("INSERT INTO alerts(ts,ip,type,severity,detail) VALUES(?,?,?,?,?)",
                         [(ts, ip, t, sev, detail) for ip, t, sev, detail in rows])
    return len(rows)


def resolve_alert(conn, ip, type_):
    ts = int(time.time())
    with _wlock, conn:
        conn.execute("UPDATE alerts SET resolved=1, resolved_ts=? WHERE ip=? AND type=? "
                     "AND resolved=0", (ts, ip, type_))


def resolve_alerts_bulk(conn, pairs):
    """pairs: [(ip, type), ...] 单事务批量消警。调用方应先用 alert_state() 过滤出
    真正活跃的键，避免为 5000 台不存在告警的机器空跑 UPDATE。"""
    pairs = list(dict.fromkeys(pairs))
    if not pairs:
        return 0
    ts = int(time.time())
    with _wlock, conn:
        conn.executemany("UPDATE alerts SET resolved=1, resolved_ts=? "
                         "WHERE ip=? AND type=? AND resolved=0",
                         [(ts, ip, t) for ip, t in pairs])
    return len(pairs)


def resolve_types_except(conn, keep_types):
    """把不在 keep_types 内的活跃告警全部置为已恢复（用于告警类型精简后清理旧类型）。"""
    ts = int(time.time())
    qm = ",".join("?" * len(keep_types))
    with _wlock, conn:
        conn.execute(
            f"UPDATE alerts SET resolved=1, resolved_ts=? WHERE resolved=0 "
            f"AND type NOT IN ({qm}) AND type NOT LIKE 'cooler%'",
            (ts, *keep_types))


def get_alert(conn, alert_id):
    row = _r(conn).execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    return dict(row) if row else None


def ack_alert(conn, alert_id, user):
    with _wlock, conn:
        conn.execute("UPDATE alerts SET ack_by=?, ack_at=? WHERE id=? AND resolved=0",
                     (user, int(time.time()), alert_id))


def list_alerts(conn, active_only=True, limit=200):
    q = "SELECT * FROM alerts"
    if active_only:
        q += " WHERE resolved=0"
    q += " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in _r(conn).execute(q, (limit,)).fetchall()]


def count_active_by_type(conn):
    """活跃告警按类型精确计数(无 limit 截断)。"""
    rows = _r(conn).execute(
        "SELECT type, COUNT(*) c FROM alerts WHERE resolved=0 GROUP BY type").fetchall()
    return {r["type"]: r["c"] for r in rows}


def count_active(conn):
    return _r(conn).execute("SELECT COUNT(*) FROM alerts WHERE resolved=0").fetchone()[0]


def active_alerts_by_type(conn, type_=None, like=None):
    """按类型取全部活跃告警(无 limit 截断)。"""
    q = "SELECT * FROM alerts WHERE resolved=0"
    args = []
    if type_:
        q += " AND type=?"
        args.append(type_)
    if like:
        q += " AND type LIKE ?"
        args.append(like + "%")
    return [dict(r) for r in _r(conn).execute(q, tuple(args)).fetchall()]


def resolve_alerts_for(conn, ips, types=None, like=None):
    """批量恢复指定 ip 列表的活跃告警(下架/踢除时清理残留)。"""
    if not ips:
        return
    ts = int(time.time())
    qm = ",".join("?" * len(ips))
    with _wlock, conn:
        if types:
            tqm = ",".join("?" * len(types))
            conn.execute(f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 "
                         f"AND ip IN ({qm}) AND type IN ({tqm})", (ts, *ips, *types))
        if like:
            conn.execute(f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 "
                         f"AND ip IN ({qm}) AND type LIKE ?", (ts, *ips, like + "%"))


def log_commands(conn, user, action, results):
    ts = int(time.time())
    with _wlock, conn:
        conn.executemany(
            "INSERT INTO command_log(ts,user,action,ip,ok,msg) VALUES(?,?,?,?,?,?)",
            [(ts, user, action, r["ip"], 1 if r["ok"] else 0, r["msg"]) for r in results])


def list_commands(conn, limit=100):
    rows = _r(conn).execute("SELECT * FROM command_log ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---- 计费聚合(rollup) ----
MAX_SAMPLE_GAP = 900   # 单次扫描最多代表 15 分钟；监控停摆期间不把停机时间算成交付


def _hour_rows(conn, hour_start, hour_end):
    """取某小时内所有定时扫描的快照行，按 scan_id 升序(保证 worker 归属确定)。"""
    rc = _r(conn)
    scans = rc.execute(
        "SELECT scan_id, ts FROM scans WHERE ts>=? AND ts<? AND kind IN ('full','quick') "
        "ORDER BY ts, scan_id", (hour_start, hour_end)).fetchall()
    if not scans:
        return [], {}
    sids = [s["scan_id"] for s in scans]
    # 每次扫描代表的时长：覆盖到下一次扫描；本小时首次扫描回补到整点。
    deltas = {}
    for i, s in enumerate(scans):
        start = hour_start if i == 0 else s["ts"]
        end = scans[i + 1]["ts"] if i + 1 < len(scans) else hour_end
        deltas[s["scan_id"]] = min(max(0, end - start), MAX_SAMPLE_GAP)
    qm = ",".join("?" * len(sids))
    rows = rc.execute(
        f"SELECT scan_id, ip, worker, status, hr_rt, power FROM snapshots "
        f"WHERE scan_id IN ({qm}) ORDER BY scan_id", sids).fetchall()
    return rows, deltas


def _aggregate(rows, deltas):
    """把快照行按客户聚合。worker 归属：取该机在本区间内**最后一次**非空矿工名
    (rows 已按 scan_id 升序 → 后写覆盖先写，结果确定)。离线行 worker 为空，
    需靠该机已知矿工名归属，停机时段才能算到正确客户头上。"""
    ip2worker = {}
    for r in rows:
        if r["worker"]:
            ip2worker[r["ip"]] = r["worker"]
    agg = {}
    last_known = {}
    for r in rows:
        w = ip2worker.get(r["ip"]) or "(未知)"
        a = agg.setdefault(w, {"ips": set(), "samples": 0, "online": 0, "th_h": 0.0, "kwh": 0.0})
        a["ips"].add(r["ip"])
        if r["status"] == "unknown":
            # 本轮扫描拥堵没探到 ≠ 客户停机：沿用该机本区间内上一次确认的状态(正在挖的机器
            # 大概率还在挖)；区间内没有可参照的就不计入分母，绝不当成停机
            prev = last_known.get(r["ip"])
            if prev is None:
                continue
            r = dict(prev, scan_id=r["scan_id"])
        else:
            last_known[r["ip"]] = r
        a["samples"] += 1
        if r["status"] == "online":
            a["online"] += 1
            dt_h = deltas.get(r["scan_id"], 300) / 3600.0
            if r["hr_rt"]:
                a["th_h"] += r["hr_rt"] * dt_h
            if r["power"]:
                a["kwh"] += r["power"] * dt_h / 1000.0
    return agg


def rollup_hours(conn, now=None, max_hours=48):
    """把已结束的整点小时聚合进 worker_hourly。幂等：重复跑同一小时结果一致。
    单次最多补 max_hours 个小时，防首次运行/长期停机后一次性跑太久卡住调度。"""
    now = int(now or time.time())
    cur_hour = now - now % HOUR
    done = int(meta_get(conn, "rollup_hour", 0) or 0)
    if not done:
        row = _r(conn).execute("SELECT MIN(ts) t FROM scans").fetchone()
        if not row or not row["t"]:
            return 0
        done = row["t"] - row["t"] % HOUR
    n = 0
    hour = done
    while hour < cur_hour and n < max_hours:
        rows, deltas = _hour_rows(conn, hour, hour + HOUR)
        if rows:
            agg = _aggregate(rows, deltas)
            with _wlock, conn:
                conn.execute("DELETE FROM worker_hourly WHERE hour=?", (hour,))
                conn.executemany(
                    "INSERT INTO worker_hourly(hour,worker,machines,samples,online,th_h,kwh) "
                    "VALUES(?,?,?,?,?,?,?)",
                    [(hour, w, len(a["ips"]), a["samples"], a["online"],
                      round(a["th_h"], 3), round(a["kwh"], 3)) for w, a in agg.items()])
            n += 1
        hour += HOUR
        meta_set(conn, "rollup_hour", hour)
    if n:
        log.info("计费聚合: 已归档 %d 个小时，进度至 %s", n,
                 time.strftime("%Y-%m-%d %H:00", time.localtime(hour)))
    return n


def coverage_hours(conn):
    """当前可查询的报表覆盖小时数(取 rollup 与明细快照里最早的数据)。"""
    rc = _r(conn)
    a = rc.execute("SELECT MIN(hour) h FROM worker_hourly").fetchone()
    b = rc.execute("SELECT MIN(ts) t FROM scans").fetchone()
    earliest = min([x for x in ((a and a["h"]), (b and b["t"])) if x] or [0])
    if not earliest:
        return 0
    return max(1, int((time.time() - earliest) // HOUR))


def customer_report(conn, hours=24):
    """按客户(矿工名)统计周期内：机器数、可用率%、交付算力 TH·h、耗电 kWh。

    数据来源两段拼接：
      - 已归档的整点小时 → worker_hourly（明细快照删了也还在，支持月度对账）
      - 尚未归档的当前小时 → 直接扫 snapshots
    """
    now = int(time.time())
    frm = now - hours * HOUR
    rolled_to = int(meta_get(conn, "rollup_hour", 0) or 0)
    agg = {}

    def slot(w):
        return agg.setdefault(w, {"worker": w, "machines": 0, "samples": 0,
                                  "online": 0, "th_h": 0.0, "kwh": 0.0})

    if rolled_to > frm:
        # 起点所在的整小时只算落在窗口内的那一截(按比例折算)，其余整小时全算。以前整小时
        # 全算，再加上实时部分，整点后 45 分查"近 1 小时"会得到 1.75 小时的交付算力。
        # 不去补查那一截的明细快照：长周期报表的起点早已超出明细保留期
        frm_floor = frm - frm % HOUR
        parts = [(frm_floor + HOUR if frm % HOUR else frm_floor, rolled_to, 1.0)]
        if frm % HOUR and frm_floor < rolled_to:
            parts.append((frm_floor, frm_floor + 1, (HOUR - frm % HOUR) / HOUR))
        for lo, hi, weight in parts:
            if lo >= hi:
                continue
            rows = _r(conn).execute(
                "SELECT worker, MAX(machines) m, SUM(samples) s, SUM(online) o, "
                "SUM(th_h) th, SUM(kwh) kw FROM worker_hourly WHERE hour>=? AND hour<? "
                "GROUP BY worker", (lo, hi)).fetchall()
            for r in rows:
                a = slot(r["worker"])
                # 机器数取各小时峰值：跨小时求和会把同一台机重复计数
                a["machines"] = max(a["machines"], r["m"] or 0)
                a["samples"] += (r["s"] or 0) * weight
                a["online"] += (r["o"] or 0) * weight
                a["th_h"] += (r["th"] or 0.0) * weight
                a["kwh"] += (r["kw"] or 0.0) * weight

    live_from = max(frm, rolled_to)
    if live_from < now:
        rows, deltas = _hour_rows(conn, live_from, now + 1)
        for w, a0 in _aggregate(rows, deltas).items():
            a = slot(w)
            a["machines"] = max(a["machines"], len(a0["ips"]))
            a["samples"] += a0["samples"]
            a["online"] += a0["online"]
            a["th_h"] += a0["th_h"]
            a["kwh"] += a0["kwh"]

    out = [{"worker": a["worker"], "machines": a["machines"],
            "uptime_pct": round(a["online"] / a["samples"] * 100, 2) if a["samples"] else 0,
            "delivered_th_h": round(a["th_h"], 1),
            "power_kwh": round(a["kwh"], 1)} for a in agg.values()]
    out.sort(key=lambda x: x["machines"], reverse=True)
    return out


# ---- 清理与维护 ----
def prune(conn, retention_days, roster_days=7, rollup_days=400):
    """删过期数据。

    明细快照的删除线**永远不越过计费归档进度**：worker_hourly 里还没有的时段，
    snapshots 是结算的唯一依据，删了就再也算不出来了。rollup 一次都没跑过时
    (rolled_to=0)干脆一条明细都不删——宁可多占几天磁盘，也不能丢客户账。
    集装箱历史/已恢复告警不参与结算，按时间线正常清理。"""
    now = int(time.time())
    time_cut = now - retention_days * 86400
    rolled_to = int(meta_get(conn, "rollup_hour", 0) or 0)
    snap_cut = min(time_cut, rolled_to) if rolled_to else 0
    roster_cut = now - roster_days * 86400
    rollup_cut = now - rollup_days * 86400
    old = []
    with _wlock, conn:
        if snap_cut > 0:
            old = [r["scan_id"] for r in
                   conn.execute("SELECT scan_id FROM scans WHERE ts<?", (snap_cut,)).fetchall()]
            for i in range(0, len(old), 500):       # 分批，避免 SQL 变量数超上限
                chunk = old[i:i + 500]
                qm = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM snapshots WHERE scan_id IN ({qm})", chunk)
                conn.execute(f"DELETE FROM scans WHERE scan_id IN ({qm})", chunk)
        conn.execute("DELETE FROM container_snaps WHERE ts<?", (time_cut,))
        conn.execute("DELETE FROM alerts WHERE resolved=1 AND resolved_ts<?", (time_cut,))
        conn.execute("DELETE FROM known_miners WHERE last_online<? AND state='active'",
                     (roster_cut,))
        conn.execute("DELETE FROM worker_hourly WHERE hour<?", (rollup_cut,))
        # 审计：定位灯/维修标记这类高频低价值记录按条数只留最近 5000；重启/换矿池/下架/
        # 被拒绝的命令按时间长期保留——否则批量点几次"维修中"就能把换矿池记录冲掉
        low = "action IN ('locate','state:repair','state:active')"
        conn.execute(f"DELETE FROM command_log WHERE {low} AND id NOT IN "
                     f"(SELECT id FROM command_log WHERE {low} ORDER BY id DESC LIMIT 5000)")
        conn.execute(f"DELETE FROM command_log WHERE NOT ({low}) AND ts<?", (rollup_cut,))
    return len(old)


def checkpoint(conn):
    """把 WAL 内容合并回主库并截断 WAL 文件。

    不做这件事的后果(本项目真实发生过)：WAL 长到 800MB+，每次读都要扫 WAL 索引，
    越跑越慢，崩溃恢复时间也线性变长。自动 checkpoint 在持续写入 + 并发读的负载下
    会被反复推迟，必须定期显式 TRUNCATE。"""
    with _wlock:
        try:
            conn.commit()
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return tuple(row) if row else None
        except sqlite3.Error as e:
            log.warning("wal_checkpoint 失败: %s", e)
            return None


def vacuum(conn):
    """回收删除留下的空页(SQLite 删行不缩文件)。会持有排他锁，只在低峰期调用。"""
    with _wlock:
        try:
            conn.commit()
            t0 = time.time()
            conn.execute("VACUUM")
            log.info("VACUUM 完成，耗时 %.1fs", time.time() - t0)
            return True
        except sqlite3.Error as e:
            log.warning("VACUUM 失败: %s", e)
            return False


def recent_write_ts(conn):
    """库里最近一次写入的时间戳(秒)，取 scans / command_log 两张表的最大 ts。

    用于 CLI 侧"服务是不是还在跑"的尽力而为判断——不保证准确(服务可能刚好在
    扫描间隔里空转)，只用来给运维一个提醒，不作为任何硬性拦截依据。"""
    newest = 0
    for sql in ("SELECT MAX(ts) FROM scans", "SELECT MAX(ts) FROM command_log"):
        try:
            row = _r(conn).execute(sql).fetchone()
        except sqlite3.Error as e:
            log.debug("recent_write_ts 查询失败 (%s): %s", sql, e)
            continue
        if row and row[0]:
            newest = max(newest, int(row[0]))
    return newest


def service_maybe_running(conn, window=60):
    """最近 window 秒内有新写入 => 服务大概率还在运行。返回 (bool, 距今秒数或 None)。"""
    ts = recent_write_ts(conn)
    if not ts:
        return False, None
    age = int(time.time()) - ts
    return age < window, age


def db_size(conn):
    """(主库字节数, WAL 字节数)，用于判断是否值得 VACUUM。"""
    if not _DB_PATH or _DB_PATH == ":memory:":
        return 0, 0
    def _sz(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    return _sz(_DB_PATH), _sz(_DB_PATH + "-wal")


if __name__ == "__main__":   # 运维小工具: python db.py vacuum|checkpoint|rollup|stats
    import appconfig
    logs.setup(None)
    cfg = appconfig.load_config(os.environ.get("MINER_CONFIG", "config.yaml"))
    c = init_db(cfg["db"]["path"])
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "vacuum":
        running, age = service_maybe_running(c)
        if running:
            print("=" * 60)
            print(f"[WARN] 检测到 {age}s 前还有新的写入记录，服务大概率正在运行中。")
            print("       VACUUM 会对整个数据库加排他锁(大库可能数分钟)，期间")
            print("       服务的写入和网页读请求都会被长时间阻塞甚至超时报错。")
            print("       建议先停止服务再执行 vacuum。")
            print("=" * 60)
            # 只在交互式终端下询问；非交互(计划任务/管道)不拦截，仅警告后继续。
            if sys.stdin is not None and sys.stdin.isatty():
                try:
                    ans = input("仍要继续吗? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = ""
                if ans not in ("y", "yes"):
                    print("已取消。")
                    sys.exit(1)
        print("VACUUM 中(大库可能数分钟，期间数据库被独占)…")
        checkpoint(c)
        vacuum(c)
    elif cmd == "checkpoint":
        print("checkpoint:", checkpoint(c))
    elif cmd == "rollup":
        print("已归档小时数:", rollup_hours(c, max_hours=100000))
    main_sz, wal_sz = db_size(c)
    print(f"主库 {main_sz/1e6:.1f} MB / WAL {wal_sz/1e6:.1f} MB")
    print("报表可覆盖小时数:", coverage_hours(c))
