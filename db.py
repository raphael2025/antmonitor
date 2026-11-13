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
import math
import os
import sqlite3
import sys
import tempfile
import threading
import time
import weakref

import logs

log = logs.get(__name__)

_wlock = threading.RLock()   # 只保护写连接；读查询不取这把锁
_DB_PATH = None
_local = threading.local()
_readers = weakref.WeakSet()  # 线程结束即可释放，维护任务不会延长连接寿命
_readers_lock = threading.Lock()

HOUR = 3600
MAX_SAMPLE_GAP = 900   # 最后一次实际采样最多代表 15 分钟。


def _measurement(value):
    """未知/非法读数不能被当作实测零值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def record_measured_ts(record, scan_ts):
    """旧记录没有逐机时间时，只能兼容使用轮次时间，不能伪造更精确的采样。"""
    value = record.get("measured_ts")
    value = scan_ts if value is None else value
    stamp = _measurement(value)
    return stamp if stamp else None


def record_freshness(record, scan_ts, now=None):
    now = time.time() if now is None else now
    stamp = record_measured_ts(record, scan_ts)
    completed = _measurement(scan_ts)
    return stamp is not None and completed is not None and stamp <= completed + 60 \
        and -60 <= now - stamp <= MAX_SAMPLE_GAP


def record_time_valid(record, scan_ts):
    """Validate the recorded clock without imposing a display-age countdown."""
    stamp = record_measured_ts(record, scan_ts)
    completed = _measurement(scan_ts)
    return stamp is not None and completed is not None and stamp <= completed + 60

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


class _ReadResult:
    """已读取的查询结果，不再占用 SQLite 游标或读事务。"""

    def __init__(self, rows):
        self._rows = rows
        self._index = 0

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self):
        rows = self._rows if self._index == 0 else self._rows[self._index:]
        self._rows, self._index = [], 0
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class _Reader:
    """线程自己的只读连接；维护仅关闭空闲连接，忙时延后到查询完成。

    本模块的查询均一次性读取结果。execute 在连接锁内取完行，避免清理任务
    关闭仍在 fetch 的游标；锁只保护本线程连接，不会串行化其他线程的读取。
    调用方保留的句柄在清理后仍可使用，会按原路径重新打开只读连接。
    """

    def __init__(self, path):
        self.path = path
        self._conn = None
        self._lock = threading.Lock()
        self._retire = threading.Event()

    def _close(self):
        if self._conn is None:
            return 0
        self._conn.close()
        self._conn = None
        return 1

    def execute(self, sql, parameters=()):
        with self._lock:
            try:
                if self._conn is None:
                    self._conn = connect(self.path, query_only=True)
                cursor = self._conn.execute(sql, parameters)
                try:
                    return _ReadResult(cursor.fetchall())
                finally:
                    cursor.close()
            finally:
                if self._retire.is_set():
                    self._close()
                    self._retire.clear()

    def retire(self):
        self._retire.set()
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            closed = self._close()
            self._retire.clear()
            return closed
        finally:
            self._lock.release()

    def __del__(self):
        try:
            self._close()
        except Exception:  # 解释器退出时 SQLite 模块可能已释放。
            pass


def _r(conn):
    """本线程只读句柄；测试用内存库(或未 init_db)时回退到写连接。"""
    if not _DB_PATH or _DB_PATH == ":memory:":
        return conn
    reader = getattr(_local, "reader", None)
    if reader is None or reader.path != _DB_PATH:
        reader = _Reader(_DB_PATH)
        _local.reader = reader
        with _readers_lock:
            _readers.add(reader)
    return reader


def close_readers():
    """回收空闲只读连接，活跃查询完成后再关闭；返回立即关闭的连接数。"""
    with _readers_lock:
        readers = list(_readers)
    return sum(reader.retire() for reader in readers)


SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    started_ts INTEGER,
    source_scan_ts INTEGER,
    kind    TEXT,                 -- full | quick | manual
    total   INTEGER,
    online  INTEGER,
    offline INTEGER,
    unknown INTEGER,
    total_hr REAL,
    known_total_hr REAL,
    hashrate_known_machines INTEGER
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
    measured_ts REAL,
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
    measured_ts REAL, data_status TEXT, observed_fault_flags TEXT,
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
-- SN 锚定客户台账：矿工名会漂，序列号相对稳定；对账优先用台账归属。
CREATE TABLE IF NOT EXISTS asset_ledger (
    sn         TEXT PRIMARY KEY,
    worker     TEXT NOT NULL,
    updated_ts INTEGER NOT NULL,
    source     TEXT DEFAULT 'scan'
);
CREATE INDEX IF NOT EXISTS idx_ledger_worker ON asset_ledger(worker);
-- 客户日汇总（UTC+8 切日），月底对账不必再扫整月小时明细。
CREATE TABLE IF NOT EXISTS customer_daily (
    day               TEXT NOT NULL,   -- YYYY-MM-DD (UTC+8)
    worker            TEXT NOT NULL,
    machines          INTEGER DEFAULT 0,
    delivered_th_h    REAL,
    power_kwh         REAL,
    online_machine_s  REAL,
    known_machine_s   REAL,
    observed_s        REAL,
    PRIMARY KEY (day, worker)
);
CREATE INDEX IF NOT EXISTS idx_cust_daily_day ON customer_daily(day);
CREATE TABLE IF NOT EXISTS containers (
    ip          TEXT PRIMARY KEY,
    last_online INTEGER,
    online      INTEGER DEFAULT 1,
    supply_temp REAL, return_temp REAL, supply_pressure REAL, return_pressure REAL, flow REAL,
    internal_temp REAL, internal_humidity REAL, tower_inlet_temp REAL, set_temp REAL,
    power1 REAL, power2 REAL, miner_num INTEGER, chip_max_temp INTEGER,
    pumps TEXT, faults TEXT, miner_ips TEXT,
    measured_ts REAL, data_status TEXT, data_complete INTEGER,
    last_observed_ts REAL, observed_fault_flags TEXT, known_fault_flags TEXT
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
-- Durable batch-command ledger. Per-target states distinguish not sent from uncertain.
CREATE TABLE IF NOT EXISTS command_jobs (
    id TEXT PRIMARY KEY,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    user TEXT,
    action TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_command_jobs_updated ON command_jobs(updated_ts DESC);
CREATE TABLE IF NOT EXISTS command_job_targets (
    job_id TEXT NOT NULL,
    ip TEXT NOT NULL,
    firmware TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'CREATED',
    ok INTEGER,
    uncertain INTEGER NOT NULL DEFAULT 0,
    msg TEXT NOT NULL DEFAULT '',
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY(job_id, ip),
    FOREIGN KEY(job_id) REFERENCES command_jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_command_job_targets_state ON command_job_targets(job_id, state);
CREATE TABLE IF NOT EXISTS zero_reboot_state (
    ip              TEXT PRIMARY KEY,
    started_at      INTEGER NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at INTEGER
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
-- 算力速率单独归档：不能把旧计费 TH·h 直接当作已完整观测的 24h 均值。
-- observed_s 是场地已观测的墙钟时间；unknown_s 是该客户无法完整读数的时段。
CREATE TABLE IF NOT EXISTS hashrate_hourly (
    hour INTEGER PRIMARY KEY,
    observed_s REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_hashrate_hourly (
    hour INTEGER NOT NULL,
    worker TEXT NOT NULL,
    th_h REAL NOT NULL,
    unknown_s REAL NOT NULL,
    machines INTEGER NOT NULL,
    PRIMARY KEY (hour, worker)
);
-- v2 观测统计独立保存，不覆写旧 worker_hourly 账本。
-- 三种指标各自记录缺测墙钟时长；在线率的分子/分母使用机器秒。
CREATE TABLE IF NOT EXISTS customer_metrics_hourly (
    hour INTEGER PRIMARY KEY,
    observed_s REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_metrics_hourly (
    hour INTEGER NOT NULL,
    worker TEXT NOT NULL,
    th_h REAL NOT NULL,
    unknown_s REAL NOT NULL,
    power_kwh REAL NOT NULL,
    power_unknown_s REAL NOT NULL,
    status_unknown_s REAL NOT NULL,
    online_machine_s REAL NOT NULL,
    known_machine_s REAL NOT NULL,
    machines INTEGER NOT NULL,
    PRIMARY KEY (hour, worker)
);
-- 重启限流按 action+ok+ip+ts 批量查 24h 成功次数；缺索引时勾选上千台会扫全表
CREATE INDEX IF NOT EXISTS idx_cmd_reboot ON command_log(action, ok, ip, ts);
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
        _try(conn, "ALTER TABLE snapshots ADD COLUMN measured_ts REAL")
        _try(conn, "ALTER TABLE scans ADD COLUMN unknown INTEGER")
        _try(conn, "ALTER TABLE scans ADD COLUMN started_ts INTEGER")
        _try(conn, "ALTER TABLE scans ADD COLUMN source_scan_ts INTEGER")
        _try(conn, "ALTER TABLE scans ADD COLUMN known_total_hr REAL")
        _try(conn, "ALTER TABLE scans ADD COLUMN hashrate_known_machines INTEGER")
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
        for table in ("containers", "container_snaps"):
            for col, typ in (("measured_ts", "REAL"), ("data_status", "TEXT"),
                             ("observed_fault_flags", "TEXT")):
                _try(conn, f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        for col, typ in (("data_complete", "INTEGER"), ("last_observed_ts", "REAL"),
                         ("known_fault_flags", "TEXT")):
            _try(conn, f"ALTER TABLE containers ADD COLUMN {col} {typ}")
        # 旧库补重启限流索引(CREATE INDEX IF NOT EXISTS，重复执行无害)
        _try(conn, "CREATE INDEX IF NOT EXISTS idx_cmd_reboot ON command_log(action, ok, ip, ts)")
    try:
        _cleanup_legacy_statistics(conn, path)
    except Exception:
        conn.close()
        raise
    return conn


def _cleanup_legacy_statistics(conn, path):
    """Retire incompatible statistics once, preserving all operational state.

    The old hourly estimate is no longer generated or joined into reports. A
    consistent backup precedes the first destructive migration on a file DB.
    Configuration files, the roster, alerts, controls and v2 archives are untouched.
    """
    key = "statistics_cleanup_v1"
    with _wlock:
        if conn.execute("SELECT 1 FROM meta WHERE k=?", (key,)).fetchone():
            return
        old_scans = conn.execute("SELECT COUNT(*) FROM scans WHERE unknown IS NULL").fetchone()[0]
        old_hours = conn.execute("SELECT COUNT(*) FROM worker_hourly").fetchone()[0]
        if (old_scans or old_hours) and path != ":memory:":
            directory = os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
            os.makedirs(directory, mode=0o700, exist_ok=True)
            target = os.path.join(directory, os.path.basename(path) + ".before-statistics-cleanup-v1.db")
            fd, temporary = tempfile.mkstemp(prefix="statistics-", suffix=".db", dir=directory)
            os.close(fd)
            try:
                backup = sqlite3.connect(temporary)
                try:
                    conn.backup(backup)
                finally:
                    backup.close()
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        with conn:
            conn.execute("DELETE FROM snapshots WHERE scan_id IN (SELECT scan_id FROM scans WHERE unknown IS NULL)")
            conn.execute("DELETE FROM scans WHERE unknown IS NULL")
            conn.execute("DELETE FROM worker_hourly")
            conn.execute("INSERT INTO meta(k,v) VALUES(?,?)", (key, str(int(time.time()))))
        if old_scans or old_hours:
            log.info("已清理不兼容统计: 扫描 %d 轮、小时汇总 %d 条；配置、名册与操作记录保留", old_scans, old_hours)


def _try(conn, sql):
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as exc:
        # Only an already-applied ADD COLUMN is harmless. Full disks, locked DBs
        # and invalid SQL must abort startup instead of leaving a partial schema.
        if not str(exc).lower().startswith("duplicate column name:"):
            raise


# ---- meta ----
def meta_get(conn, k, default=None):
    row = _r(conn).execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, k, v):
    with _wlock, conn:
        conn.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


SNAP_COLS = ("ip", "status", "firmware", "model", "sn", "mac", "hr_rt", "hr_avg", "power", "temp",
             "eff", "uptime", "worker", "accepted", "rejected", "stale", "note", "measured_ts")


def _scan_totals(records, ts):
    # A completed scan is a result, not a live probe. A lengthy discovery phase
    # must not erase devices successfully read earlier in that same round.
    # Command/alert callers still use record_freshness against the raw timestamps.
    states = [r.get("status") if record_time_valid(r, ts) else "unknown" for r in records]
    online, offline = states.count("online"), states.count("offline")
    unknown = len(records) - online - offline
    rates = [0.0 if state == "offline" else _measurement(r.get("hr_rt"))
             if state == "online" else None for r, state in zip(records, states)]
    known_rates = [v for v in rates if v is not None]
    known_total_hr = round(sum(known_rates), 2) if known_rates or not records else None
    total_hr = known_total_hr if len(known_rates) == len(records) else None
    return len(records), online, offline, unknown, total_hr, known_total_hr, len(known_rates)


def save_scan(conn, kind, records, keep_ips=None, started_ts=None):
    """落库快照，返回 (scan_id, ts, kept)。

    keep_ips 给定时：只持久化在线机 + 名册内(曾在线/维修)的离线机，丢弃大量从未是
    矿机的死 IP 空记录(全网扫描每轮约1.5万条)，省存储/写放大。

    返回的 kept 是**与数据库行同构**的干净记录(只含 SNAP_COLS + scan_id)，
    调用方直接拿去做内存快照，保证 API 无论走内存还是回落查库，字段完全一致。"""
    if keep_ips is not None:
        kept_src = [r for r in records if r["status"] == "online" or r["ip"] in keep_ips]
    else:
        kept_src = list(records)
    ts = int(time.time())
    if started_ts is not None:
        started = _measurement(started_ts)
        if started is None or not 0 < started <= ts:
            raise ValueError("scan start must be a valid timestamp no later than completion")
        started_ts = int(started)
    totals = _scan_totals(kept_src, ts)
    with _wlock, conn:
        cur = conn.execute(
            "INSERT INTO scans(ts,started_ts,source_scan_ts,kind,total,online,offline,unknown,total_hr,known_total_hr,"
            "hashrate_known_machines) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, started_ts, ts, kind, *totals),
        )
        sid = cur.lastrowid
        kept = [{"scan_id": sid, "ip": r["ip"], "status": r["status"],
                 "firmware": r.get("firmware") or "", "model": r.get("model") or "",
                 "sn": r.get("sn") or "", "mac": r.get("mac") or "",
                 "hr_rt": r.get("hr_rt"), "hr_avg": r.get("hr_avg"),
                 "power": r.get("power"), "temp": r.get("temp"), "eff": r.get("eff"),
                 "uptime": r.get("uptime"), "worker": r.get("worker") or "",
                 "accepted": r.get("accepted"), "rejected": r.get("rejected"),
                 "stale": r.get("stale"), "note": r.get("note") or "",
                 # Preserve invalid time as an invalid numeric sentinel. SQLite
                 # REAL affinity must not turn an untrusted string/bool into a
                 # valid clock after restart; None is reserved for old missing clocks.
                 "measured_ts": record_measured_ts(r, ts) if record_time_valid(r, ts) else 0}
                for r in kept_src]
        conn.executemany(
            "INSERT INTO snapshots(scan_id,ip,status,firmware,model,sn,mac,hr_rt,hr_avg,power,temp,"
            "eff,uptime,worker,accepted,rejected,stale,note,measured_ts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [tuple([sid] + [r[c] for c in SNAP_COLS]) for r in kept],
        )
    return sid, ts, kept


CONTAINER_COLS = ("supply_temp", "return_temp", "supply_pressure", "return_pressure", "flow",
                  "internal_temp", "internal_humidity", "tower_inlet_temp", "set_temp",
                  "power1", "power2", "miner_num", "chip_max_temp")


def upsert_containers(conn, containers, ts):
    """保存可达性及读数；缺失故障位保留最后证据，不能把未观测当恢复。"""
    if not containers:
        return
    columns = ("ip", "last_online", "online", *CONTAINER_COLS, "pumps", "faults", "miner_ips",
               "measured_ts", "data_status", "data_complete", "last_observed_ts",
               "observed_fault_flags", "known_fault_flags")
    with _wlock, conn:
        for c in containers:
            old = conn.execute("SELECT * FROM containers WHERE ip=?", (c["ip"],)).fetchone()
            old = _row_to_container(old) if old else {}
            measured = record_measured_ts(c, ts)
            if old.get("measured_ts") and measured and measured < old["measured_ts"]:
                continue
            observed = c.get("observed_fault_flags")
            observed = set(observed) if isinstance(observed, list) else None
            known = set(old.get("known_fault_flags") or [])
            previous_faults = {f["flag"]: f for f in old.get("faults", [])}
            known.update(previous_faults)
            faults = {}
            if observed is not None:
                known.update(observed)
                for flag, fault in previous_faults.items():
                    if flag not in observed:
                        faults[flag] = {**fault, "observed": False,
                                        "measured_ts": fault.get("measured_ts") or old.get("last_observed_ts")
                                        or old.get("last_online") or old.get("measured_ts")}
                complete = bool(observed) and known <= observed
            else:
                complete = True  # Legacy/custom drivers expose a complete fault list.
            complete = complete and c.get("status") != "unknown" and record_freshness(c, ts, ts)
            for fault in c.get("faults") or []:
                faults[fault["flag"]] = {**fault, "observed": True, "measured_ts": measured}
            last_observed = measured if complete else old.get("last_observed_ts")
            rows = (c["ip"], max(old.get("last_online") or 0, measured or ts), 1,
                    *(c.get(k) for k in CONTAINER_COLS),
                    json.dumps(c.get("pumps") or {}, ensure_ascii=False),
                    json.dumps(list(faults.values()), ensure_ascii=False),
                    json.dumps(c.get("miner_ips") or [], ensure_ascii=False),
                    measured, "online" if complete else "unknown", int(complete), last_observed,
                    json.dumps(sorted(observed) if observed is not None else None),
                    json.dumps(sorted(known)))
            conn.execute(f"INSERT OR REPLACE INTO containers({','.join(columns)}) VALUES(" +
                         ",".join("?" for _ in columns) + ")", rows)


def mark_containers_offline(conn, ips):
    """已知箱本轮未采到 → 标记离线（不动 last_online，用于 24h 踢除计时）。"""
    if not ips:
        return
    qm = ",".join("?" * len(ips))
    with _wlock, conn:
        conn.execute(f"UPDATE containers SET online=0,data_status='offline',data_complete=0,measured_ts=? "
                     f"WHERE ip IN ({qm})", (time.time(), *ips))


def _resolve_container_alerts(conn, ips, ts):
    """消掉这些箱上的冷却告警（offline / stale / cooler:故障位）。调用方持有 _wlock。"""
    if not ips:
        return
    qm = ",".join("?" * len(ips))
    conn.execute(
        f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 AND ip IN ({qm}) "
        f"AND (type IN ('cooler_offline','cooler_stale') OR type LIKE 'cooler:%')",
        (ts, *ips))


def remove_containers(conn, ips):
    """运维手动删除集装箱：立刻从监控记忆剔除并消冷却告警。历史快照保留。返回实际删除的 IP。"""
    if not ips:
        return []
    ips = list(dict.fromkeys(str(ip).strip() for ip in ips if str(ip or "").strip()))
    if not ips:
        return []
    ts = int(time.time())
    with _wlock, conn:
        qm = ",".join("?" * len(ips))
        victims = [r["ip"] for r in conn.execute(
            f"SELECT ip FROM containers WHERE ip IN ({qm})", ips).fetchall()]
        if not victims:
            return []
        _resolve_container_alerts(conn, victims, ts)
        conn.execute(f"DELETE FROM containers WHERE ip IN ({qm})", victims)
        return victims


def kick_offline_containers(conn, max_offline_sec=86400):
    """离线超过 max_offline_sec 的箱子从记忆中踢除，并清掉其残留冷却告警。返回被踢除的 IP 列表。"""
    cutoff = int(time.time()) - max_offline_sec
    ts = int(time.time())
    with _wlock, conn:
        victims = [r["ip"] for r in conn.execute(
            "SELECT ip FROM containers WHERE online=0 AND last_online < ?", (cutoff,)).fetchall()]
        if victims:
            qm = ",".join("?" * len(victims))
            _resolve_container_alerts(conn, victims, ts)
            conn.execute(f"DELETE FROM containers WHERE ip IN ({qm})", victims)
        return victims


def get_containers(conn):
    rows = _r(conn).execute("SELECT * FROM containers ORDER BY ip").fetchall()
    return [_row_to_container(r) for r in rows]


def known_container_ips(conn):
    return [r["ip"] for r in _r(conn).execute("SELECT ip FROM containers").fetchall()]


def save_containers(conn, ts, containers):
    if not containers:
        return
    columns = ("ts", "ip", *CONTAINER_COLS, "pumps", "faults", "miner_ips",
               "measured_ts", "data_status", "observed_fault_flags")
    with _wlock, conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO container_snaps({','.join(columns)}) VALUES(" +
            ",".join("?" for _ in columns) + ")",
            [(ts, c["ip"], *(c.get(k) for k in CONTAINER_COLS),
              json.dumps(c.get("pumps") or {}, ensure_ascii=False),
              json.dumps(c.get("faults") or [], ensure_ascii=False),
              json.dumps(c.get("miner_ips") or [], ensure_ascii=False), record_measured_ts(c, ts),
              c.get("status", "online"), json.dumps(c.get("observed_fault_flags"))) for c in containers],
        )


def _row_to_container(r):
    d = dict(r)
    for k in ("pumps", "faults", "miner_ips"):
        try:
            d[k] = json.loads(d.get(k) or ("[]" if k != "pumps" else "{}"))
        except (ValueError, TypeError):
            d[k] = [] if k != "pumps" else {}
    for key in ("observed_fault_flags", "known_fault_flags"):
        try:
            d[key] = json.loads(d.get(key) or "null")
        except (TypeError, ValueError):
            d[key] = None
    if not d.get("online"):
        d["faults"] = [{**fault, "observed": False} for fault in d["faults"]]
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
    不覆盖已有 state(维修标记保留)；缺测通常保留身份，可靠 MAC 换机则作废旧 SN。"""
    if not recs:
        return
    from miner_core import normalize_mac, sn_valid   # 延迟导入：db 不该在模块级依赖扫描层
    rows = []
    for r in recs:
        if isinstance(r, str):
            rows.append((r, ts, "", "", "", "", ""))
        else:
            # N/A/error/占位串不算 SN：不能覆盖名册里已知的真 SN(否则掉线后回填出 N/A，搜不到)
            sn = r.get("sn") or ""
            measured = record_measured_ts(r, ts)
            if measured is None or measured > ts + 60:
                continue
            rows.append((r["ip"], int(measured), r.get("model") or "", sn if sn_valid(sn) else "",
                         normalize_mac(r.get("mac")),
                         r.get("worker") or "", r.get("firmware") or ""))
    with _wlock, conn:
        # 与实际写入持同一把锁：新机没有 SN 时，不能让上一台的 SN 在掉线后被回填，
        # 否则旧机换 IP 上线会把这台新机误标为 migrated。MAC 必须先校验、规范化。
        identities = {r["ip"]: (r["sn"], normalize_mac(r["mac"])) for r in
                      conn.execute("SELECT ip,sn,mac FROM known_miners").fetchall()}
        updates = []
        for row in rows:
            ip, _ts, _model, sn, mac, _worker, _firmware = row
            old_sn, old_mac = identities.get(ip, ("", ""))
            new_mac = normalize_mac(mac)
            clear_sn = bool(not sn and old_mac and new_mac and old_mac != new_mac)
            updates.append((*row, clear_sn))
            # 同批次若包含重复 IP，也按上一条更新后的身份继续比较。
            identities[ip] = (sn or ("" if clear_sn else old_sn),
                              new_mac if mac else ("" if sn and sn != old_sn else old_mac))
        conn.executemany(
            "INSERT INTO known_miners(ip,last_online,state,model,sn,mac,worker,firmware) "
            "VALUES(?,?,'active',?,?,?,?,?) "
            "ON CONFLICT(ip) DO UPDATE SET last_online=MAX(COALESCE(known_miners.last_online,0),excluded.last_online), "
            "state=CASE WHEN known_miners.state='migrated' THEN 'active' "
            "ELSE known_miners.state END, "
            "model=CASE WHEN excluded.model!='' THEN excluded.model ELSE known_miners.model END, "
            "sn=CASE WHEN excluded.sn!='' THEN excluded.sn WHEN ? THEN '' ELSE known_miners.sn END, "
            # 同一 IP 换了一台机(SN 变了)：旧 MAC 属于上一台，作废，否则残影判定会张冠李戴
            "mac=CASE WHEN excluded.mac!='' THEN excluded.mac "
            "WHEN excluded.sn!='' AND excluded.sn!=known_miners.sn THEN '' "
            "ELSE known_miners.mac END, "
            "worker=CASE WHEN excluded.worker!='' THEN excluded.worker ELSE known_miners.worker END, "
            "firmware=CASE WHEN excluded.firmware!='' THEN excluded.firmware "
            "ELSE known_miners.firmware END",
            updates)
        # SN→客户台账：有可靠 SN + 矿工名时写入/刷新，换矿工名即更新归属。
        ledger = []
        for _ip, measured, _model, sn, _mac, worker, _fw in rows:
            if sn and worker and worker != "(未知)":
                ledger.append((sn, worker, int(measured)))
        if ledger:
            conn.executemany(
                "INSERT INTO asset_ledger(sn,worker,updated_ts,source) VALUES(?,?,?,'scan') "
                "ON CONFLICT(sn) DO UPDATE SET worker=excluded.worker, "
                "updated_ts=excluded.updated_ts, source='scan'",
                ledger)


def known_identity(conn):
    """名册里每台机的最后已知身份 {ip: {...}}，用于回填离线机。"""
    rows = _r(conn).execute(
        "SELECT ip,model,sn,mac,worker,firmware,last_online,state FROM known_miners").fetchall()
    return {r["ip"]: dict(r) for r in rows}


def asset_ledger_map(conn):
    """{sn: worker} 客户台账；对账归属优先于瞬时矿工名。"""
    return {r["sn"]: r["worker"] for r in _r(conn).execute(
        "SELECT sn,worker FROM asset_ledger").fetchall() if r["sn"] and r["worker"]}


def ledger_worker_for_ip(conn, identities=None):
    """按 SN 台账覆盖 IP→矿工名；无 SN/无台账时保留原名册 worker。"""
    ids = identities if identities is not None else known_identity(conn)
    ledger = asset_ledger_map(conn)
    out = {}
    for ip, info in ids.items():
        sn = (info.get("sn") or "").strip()
        if sn and sn in ledger:
            out[ip] = ledger[sn]
        elif info.get("worker"):
            out[ip] = info["worker"]
    return out


def roster_ips(conn, max_age_days=7):
    """机器名册：max_age_days 内在线过的 + 维修/限电中的全部矿机 IP（快巡检目标）。"""
    cutoff = int(time.time()) - max_age_days * 86400
    return [r["ip"] for r in _r(conn).execute(
        "SELECT ip FROM known_miners WHERE (state='active' AND last_online>=?) "
        "OR state IN ('repair','powercut')", (cutoff,)).fetchall()]


def repair_ips(conn):
    return set(r["ip"] for r in
               _r(conn).execute("SELECT ip FROM known_miners WHERE state='repair'").fetchall())


def powercut_ips(conn):
    """限电/计划停机：掉线与零算力不报警（仍巡检，便于恢复后自动回到正常）。"""
    return set(r["ip"] for r in
               _r(conn).execute("SELECT ip FROM known_miners WHERE state='powercut'").fetchall())


def quiet_ips(conn):
    """告警静音集合：维修中 ∪ 限电/计划停机。"""
    return set(r["ip"] for r in
               _r(conn).execute(
                   "SELECT ip FROM known_miners WHERE state IN ('repair','powercut')"
               ).fetchall())


def set_machine_state(conn, ips, state):
    if not ips:
        return
    if state not in ("active", "repair", "powercut"):
        raise ValueError("invalid machine state")
    with _wlock, conn:
        conn.executemany("UPDATE known_miners SET state=? WHERE ip=?", [(state, ip) for ip in ips])


_MINER_ALERT_TYPES = ("offline", "zero", "reject", "low_hashrate", "overheat",
                      "pool_hijack", "reboot_limit")


def _resolve_gone_miners(conn, ips, ts):
    """机器已不在监控范围(下架/迁移/掉出名册)：消掉它们的单机告警；某网段名册里已没有
    受监控的机器时，顺带消掉该段 segment_down——否则这些告警没人会再评估，永远挂着。
    调用方持有 _wlock 并在事务内。"""
    if not ips:
        return
    tqm = ",".join("?" * len(_MINER_ALERT_TYPES))
    conn.executemany(
        f"UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 AND ip=? "
        f"AND type IN ({tqm})", [(ts, ip, *_MINER_ALERT_TYPES) for ip in ips])
    bases = sorted({".".join(ip.split(".")[:3]) for ip in ips if ip.count(".") == 3})
    for base in bases:
        left = conn.execute("SELECT COUNT(*) FROM known_miners WHERE ip LIKE ? "
                            "AND state!='migrated'", (base + ".%",)).fetchone()[0]
        if left == 0:
            conn.execute("UPDATE alerts SET resolved=1,resolved_ts=? WHERE resolved=0 "
                         "AND type='segment_down' AND ip=?", (ts, base + ".x"))


def mark_miners_migrated(conn, ips):
    """Quarantine superseded IPs without deleting their inventory/history."""
    if not ips:
        return
    ts = int(time.time())
    ips = list(dict.fromkeys(ips))
    with _wlock, conn:
        conn.executemany("UPDATE known_miners SET state='migrated' WHERE ip=?",
                         [(ip,) for ip in ips])
        _resolve_gone_miners(conn, ips, ts)


def remove_miners(conn, ips):
    """下架移除：从名册删除，并清掉其残留 offline/zero/reject/low_hr/overheat 告警。
    删完后，若某网段名册已"无剩余机器"，才顺带清掉该段的 segment_down——
    这样迁移残影全下架后网段告警自动消，但真"大面积掉线"期间不会被误消。"""
    if not ips:
        return
    ts = int(time.time())
    ips = list(dict.fromkeys(ips))
    with _wlock, conn:
        conn.executemany("DELETE FROM known_miners WHERE ip=?", [(ip,) for ip in ips])
        # 下架是此刻的库存事件，不能倒改几分钟前的原始采样或结算依据。
        last = conn.execute("SELECT * FROM scans ORDER BY scan_id DESC LIMIT 1").fetchone()
        if last:
            source = [dict(r) for r in conn.execute("SELECT * FROM snapshots WHERE scan_id=?",
                                                   (last["scan_id"],)).fetchall()]
            source_totals = _scan_totals(source, last["ts"])
            source_complete = isinstance(last["total"], int) and len(source) == last["total"]
            states_match = source_totals[:4] == tuple(last[k] for k in
                                                     ("total", "online", "offline", "unknown"))
            if last["hashrate_known_machines"] is None:
                # 旧完整总量可作为证据；旧 partial/legacy 不能从 REAL 快照重建有效性。
                hash_matches = (last["known_total_hr"] is None and last["total_hr"] is not None
                                and source_totals[4] == last["total_hr"])
            else:
                # 必须在删除前、按原采样轮次核对，避免下架恰好隐藏矛盾的源记录。
                hash_matches = source_totals[4:] == tuple(last[k] for k in
                    ("total_hr", "known_total_hr", "hashrate_known_machines"))
            source_trusted = source_complete and states_match and hash_matches
            removed = set(ips)
            remaining = [r for r in source if r["ip"] not in removed]
            for r in remaining:
                if r.get("measured_ts") is None:
                    r["measured_ts"] = last["ts"]  # 旧记录不能借库存事件刷新采样时间。
                # Inventory edits do not resample the remaining devices or
                # expire their completed result. Raw timestamps remain intact.
            totals = _scan_totals(remaining, ts)
            if not source_complete:
                # 缺快照不能把剩下的行数冒充完整库存；NULL 也阻止后续下架伪造空场。
                totals = (None, *totals[1:4], None, None, None)
            elif remaining and not source_trusted:
                totals = (*totals[:4], None, None, None)
            # 原集合完整且已全部移除，可证明空库存为 0/0，不依赖旧算力值。
            cur = conn.execute("INSERT INTO scans(ts,started_ts,source_scan_ts,kind,total,online,offline,unknown,"
                               "total_hr,known_total_hr,hashrate_known_machines) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (ts, last["started_ts"], last["source_scan_ts"] or last["ts"], "inventory", *totals))
            conn.executemany(
                "INSERT INTO snapshots(scan_id," + ",".join(SNAP_COLS) + ") VALUES(" +
                ",".join("?" for _ in range(len(SNAP_COLS) + 1)) + ")",
                [(cur.lastrowid, *(r.get(key) for key in SNAP_COLS)) for r in remaining])
        _resolve_gone_miners(conn, ips, ts)


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
        "SELECT s.ts, n.status, n.hr_rt, n.temp, n.power, n.measured_ts FROM snapshots n "
        "JOIN scans s ON s.scan_id=n.scan_id WHERE n.ip=? ORDER BY s.ts DESC LIMIT ?",
        (ip, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def hashrate_trend(conn, limit=288):
    # 完整手动扫描与库存事件都是有效状态边界，按真实时间显示而非等间隔样本。
    reader = _r(conn)
    rows = reader.execute(
        "SELECT ts,total,total_hr,online,unknown,known_total_hr,hashrate_known_machines "
        "FROM scans WHERE kind IN ('full','quick','manual','inventory') AND unknown IS NOT NULL "
        "ORDER BY scan_id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["legacy_total_hr"] = d["legacy_online"] = None
        d["known_online"] = d["online"]
        if d["hashrate_known_machines"] is None:
            # 升级前已校验的完整总量可直接复用。旧部分缺测没有覆盖元数据，
            # 且 REAL 快照已丢失源类型，不能证明数字串/bool 当时是有效读数。
            # 因此保留空缺，不从快照重算小计，也不回填或改写历史。
            d["known_total_hr"] = d["total_hr"]
            if d["total_hr"] is not None:
                d["hashrate_known_machines"] = d["total"]
        if d["unknown"]:
            d["online"] = None  # 确认在线的部分计数不能冒充完整在线总数。
        d["data_quality"] = "incomplete" if d["total_hr"] is None else "complete"
        out.append(d)
    return out


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
    # Keep the last known worker name beside miner alerts so the UI can identify
    # an abnormal machine even after it has gone offline.
    q = "SELECT alerts.*, known_miners.worker AS worker FROM alerts " \
        "LEFT JOIN known_miners ON known_miners.ip=alerts.ip"
    if active_only:
        q += " WHERE alerts.resolved=0"
    q += " ORDER BY alerts.id DESC LIMIT ?"
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


def create_command_job(conn, job_id, user, action, params, targets, skipped=None):
    """Persist the complete target list before any destructive request is dispatched."""
    now = int(time.time())
    rows = [(job_id, ip, str(fw or ""), "CREATED", None, 0, "", now)
            for ip, fw in targets]
    for result in skipped or []:
        rows.append((job_id, result["ip"], "", "FAILED", 0, 0,
                     str(result.get("msg") or "skipped")[:500], now))
    total = len(rows)
    success = 0
    failed = len(skipped or [])
    with _wlock, conn:
        conn.execute(
            "INSERT INTO command_jobs(id,created_ts,updated_ts,user,action,params_json,state,total,done,success,failed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, now, now, user, action, json.dumps(params or {}, ensure_ascii=False),
             "CREATED", total, failed, success, failed))
        conn.executemany(
            "INSERT INTO command_job_targets(job_id,ip,firmware,state,ok,uncertain,msg,updated_ts) "
            "VALUES(?,?,?,?,?,?,?,?)", rows)
    return job_id


def update_command_job_targets(conn, job_id, targets, state):
    """Update a batch of target rows atomically; state is DISPATCHING or a terminal state."""
    now = int(time.time())
    with _wlock, conn:
        for item in targets:
            if isinstance(item, dict):
                ip = item["ip"]
                terminal = item.get("state") or state
                ok = item.get("ok")
                uncertain = 1 if item.get("uncertain") else 0
                msg = str(item.get("msg") or "")[:500]
            else:
                ip = str(item)
                terminal, ok, uncertain, msg = state, None, 0, ""
            conn.execute(
                "UPDATE command_job_targets SET state=?,ok=?,uncertain=?,msg=?,updated_ts=? "
                "WHERE job_id=? AND ip=?",
                (terminal, None if ok is None else int(bool(ok)), uncertain, msg, now, job_id, ip))


def update_command_job(conn, job_id, state, error=""):
    now = int(time.time())
    with _wlock, conn:
        row = conn.execute(
            "SELECT COUNT(*) total, SUM(state IN ('SUCCEEDED','FAILED','UNCERTAIN','CANCELLED')) done, "
            "SUM(state='SUCCEEDED') success, SUM(state IN ('FAILED','UNCERTAIN','CANCELLED')) failed "
            "FROM command_job_targets WHERE job_id=?", (job_id,)).fetchone()
        conn.execute(
            "UPDATE command_jobs SET updated_ts=?,state=?,done=?,success=?,failed=?,error=? WHERE id=?",
            (now, state, int(row["done"] or 0), int(row["success"] or 0),
             int(row["failed"] or 0), str(error or "")[:500], job_id))


def list_command_jobs(conn, limit=50):
    limit = max(1, min(200, int(limit)))
    rows = _r(conn).execute(
        "SELECT id,created_ts,updated_ts,user,action,state,total,done,success,failed,error "
        "FROM command_jobs ORDER BY created_ts DESC,id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_command_job(conn, job_id):
    row = _r(conn).execute("SELECT * FROM command_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    job = dict(row)
    job["targets"] = [dict(r) for r in _r(conn).execute(
        "SELECT ip,firmware,state,ok,uncertain,msg,updated_ts FROM command_job_targets "
        "WHERE job_id=? ORDER BY ip", (job_id,)).fetchall()]
    return job


def recover_command_jobs(conn):
    """After a process crash, never replay commands: sent targets become UNCERTAIN."""
    now = int(time.time())
    with _wlock, conn:
        active = [r["id"] for r in conn.execute(
            "SELECT id FROM command_jobs WHERE state IN ('CREATED','DISPATCHING')").fetchall()]
        for job_id in active:
            conn.execute(
                "UPDATE command_job_targets SET state='UNCERTAIN',uncertain=1,"
                "msg='服务重启时命令结果未确认；请核实矿机状态，禁止自动重发',updated_ts=? "
                "WHERE job_id=? AND state='DISPATCHING'", (now, job_id))
            conn.execute(
                "UPDATE command_job_targets SET state='CANCELLED',"
                "msg='服务重启前尚未下发',updated_ts=? "
                "WHERE job_id=? AND state='CREATED'", (now, job_id))
            counts = conn.execute(
                "SELECT COUNT(*) total, SUM(state IN ('SUCCEEDED','FAILED','UNCERTAIN','CANCELLED')) done, "
                "SUM(state='SUCCEEDED') success, SUM(state IN ('FAILED','UNCERTAIN','CANCELLED')) failed "
                "FROM command_job_targets WHERE job_id=?", (job_id,)).fetchone()
            conn.execute(
                "UPDATE command_jobs SET state='INTERRUPTED',updated_ts=?,done=?,success=?,failed=?,"
                "error='服务重启导致任务中断；未下发目标已取消，已下发目标需人工核实' WHERE id=?",
                (now, int(counts["done"] or 0), int(counts["success"] or 0),
                 int(counts["failed"] or 0), job_id))
    return len(active)


def log_commands(conn, user, action, results, ts=None):
    ts = int(time.time()) if ts is None else int(ts)
    with _wlock, conn:
        conn.executemany(
            "INSERT INTO command_log(ts,user,action,ip,ok,msg) VALUES(?,?,?,?,?,?)",
            [(ts, user, action, r["ip"], 1 if r["ok"] else 0, r["msg"]) for r in results])


def list_commands(conn, limit=100):
    rows = _r(conn).execute("SELECT * FROM command_log ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_reboot_batches(conn, limit=50, lookback=5000, gap_sec=180):
    """把 command_log 里的 reboot 行按「同操作者 + 时间邻近」聚成批次。

    覆盖手动同步/异步重启与零算力自动重启（后者只写审计、不建 command_jobs）。
    gap_sec：同一操作者相邻两条间隔超过此值则拆成新批次（兼容分批下发的批间隔）。
    """
    limit = max(1, min(200, int(limit or 50)))
    lookback = max(limit, min(20000, int(lookback or 5000)))
    gap_sec = max(30, min(3600, int(gap_sec or 180)))
    rows = _r(conn).execute(
        "SELECT id,ts,user,ip,ok,msg FROM command_log "
        "WHERE action='reboot' ORDER BY ts DESC, id DESC LIMIT ?",
        (lookback,)).fetchall()
    if not rows:
        return []
    chrono = [dict(r) for r in reversed(rows)]
    batches = []
    cur = None
    for r in chrono:
        ts = int(r["ts"] or 0)
        user = str(r.get("user") or "")
        rid = int(r["id"])
        if (cur is None or user != cur["user"]
                or ts - int(cur["ended_ts"]) > gap_sec):
            cur = {
                "user": user,
                "started_ts": ts,
                "ended_ts": ts,
                "total": 0,
                "success": 0,
                "failed": 0,
                "id_from": rid,
                "id_to": rid,
            }
            batches.append(cur)
        else:
            cur["ended_ts"] = ts
            cur["id_from"] = min(int(cur["id_from"]), rid)
            cur["id_to"] = max(int(cur["id_to"]), rid)
        cur["total"] += 1
        if int(r.get("ok") or 0):
            cur["success"] += 1
        else:
            cur["failed"] += 1
    out = []
    for b in reversed(batches):
        b["source"] = "auto" if str(b["user"]).startswith("system@") else "manual"
        out.append(b)
        if len(out) >= limit:
            break
    return out


def list_reboot_batch_detail(conn, user, id_from, id_to, limit=2000):
    """某批次内的单机明细（按 id 闭区间 + user 约束，避免串批）。"""
    user = str(user or "")
    id_from, id_to = int(id_from), int(id_to)
    if id_to < id_from:
        id_from, id_to = id_to, id_from
    limit = max(1, min(5000, int(limit or 2000)))
    rows = _r(conn).execute(
        "SELECT id,ts,user,ip,ok,msg FROM command_log "
        "WHERE action='reboot' AND user=? AND id>=? AND id<=? "
        "ORDER BY ts ASC, id ASC LIMIT ?",
        (user, id_from, id_to, limit)).fetchall()
    return [dict(r) for r in rows]


def recent_reboots(conn, since_ts):
    """since_ts 之后成功下发过重启的 IP → 最近一次时间。服务重启后据此恢复重启静默期标记。"""
    rows = _r(conn).execute(
        "SELECT ip, MAX(ts) AS t FROM command_log WHERE action='reboot' AND ok=1 AND ts>=? "
        "GROUP BY ip", (int(since_ts),)).fetchall()
    return {r["ip"]: int(r["t"]) for r in rows}


def reboot_stats(conn, ips, window_sec=86400):
    """每 IP 在滚动 window_sec 内成功重启次数与最近一次成功时间。

    只计 action='reboot' AND ok=1(手动+自动共用限流口径)。返回 {ip: {count, last_ts}}，
    未出现过的 IP 也有条目(count=0, last_ts=0)，方便调用方直接取值。
    """
    if not ips:
        return {}
    now = int(time.time())
    cut = now - int(window_sec or 86400)
    out = {ip: {"count": 0, "last_ts": 0} for ip in ips}
    # SQLite 变量上限约 999；分块避免大批量勾选时炸参数
    chunk = 400
    rc = _r(conn)
    for i in range(0, len(ips), chunk):
        part = list(ips[i:i + chunk])
        qm = ",".join("?" * len(part))
        rows = rc.execute(
            f"SELECT ip, COUNT(*) AS c, MAX(ts) AS last_ts FROM command_log "
            f"WHERE action='reboot' AND ok=1 AND ts>? AND ip IN ({qm}) GROUP BY ip",
            (cut, *part)).fetchall()
        for r in rows:
            out[r["ip"]] = {"count": int(r["c"] or 0), "last_ts": int(r["last_ts"] or 0)}
    return out


def zero_reboot_states(conn):
    """持续零算力自动重启状态，跨进程重启保存。"""
    rows = _r(conn).execute(
        "SELECT ip,started_at,attempts,last_attempt_at FROM zero_reboot_state").fetchall()
    return {r["ip"]: dict(r) for r in rows}


def start_zero_reboot_states(conn, ips, ts):
    if not ips:
        return
    with _wlock, conn:
        conn.executemany(
            "INSERT OR IGNORE INTO zero_reboot_state(ip,started_at,attempts,last_attempt_at) "
            "VALUES(?,?,0,NULL)", [(ip, int(ts)) for ip in ips])


def record_zero_reboot_attempts(conn, ips, ts):
    if not ips:
        return
    with _wlock, conn:
        conn.executemany(
            "UPDATE zero_reboot_state SET attempts=attempts+1,last_attempt_at=? WHERE ip=?",
            [(int(ts), ip) for ip in ips])


def clear_zero_reboot_states(conn, ips):
    if not ips:
        return
    with _wlock, conn:
        conn.executemany("DELETE FROM zero_reboot_state WHERE ip=?", [(ip,) for ip in ips])


# ---- 计费聚合(rollup) ----
def _customer_metric_sample(records, identities=None, at=None, scan_ts=None, interval=False):
    """每项独立判定完整性；身份只沿时间向前传播，未知不能变成零。"""
    identities = identities if identities is not None else {}
    out = {}
    for r in records:
        ip = r["ip"]
        if r.get("worker"):
            identities[ip] = r["worker"]
        elif r.get("status") == "online":
            identities.setdefault(ip, "(未知)")  # 已确认真机，即使还没有读到客户名。
        worker = r.get("worker") or identities.get(ip)
        if not worker and ip in identities:
            worker = "(未知)"
        if not worker:
            continue  # 历史首扫中的陌生空地址不能成为匿名客户的停机机器。
        a = out.setdefault(worker, {"machines": 0, "hashrate_ths": 0.0, "power_w": 0.0,
                                   "status_known": True, "online": 0, "oldest_ts": None,
                                   "time_known": True})
        a["machines"] += 1
        stamp = record_measured_ts(r, scan_ts)
        if stamp is None:
            a["time_known"] = False
        else:
            a["oldest_ts"] = stamp if a["oldest_ts"] is None else min(a["oldest_ts"], stamp)
        fresh = (scan_ts is None or record_time_valid(r, scan_ts)) if at is None \
            else record_freshness(r, scan_ts, at)
        if interval and stamp is not None and at >= stamp + MAX_SAMPLE_GAP:
            fresh = False
        state = r.get("status") if fresh else "unknown"
        if state == "online":
            a["online"] += 1
        elif state != "offline":
            a["status_known"] = False
        for dest, source in (("hashrate_ths", "hr_rt"), ("power_w", "power")):
            value = 0.0 if state == "offline" else _measurement(r.get(source)) if state == "online" else None
            if value is None:
                a[dest] = None
            elif a[dest] is not None:
                a[dest] += value
    return out


def _customer_rate_sample(records, identities=None):
    return {worker: {"machines": a["machines"], "hashrate_ths": a["hashrate_ths"]}
            for worker, a in _customer_metric_sample(records, identities).items()}


def customer_hashrates(scan, records, now=None, identities=None):
    """最近完成轮次的客户摘要；下次完整结果发布前不随墙钟改变。"""
    if not scan:
        return []
    now = int(time.time() if now is None else now)
    ts = int(scan.get("ts") or 0)
    return [{"worker": worker, "machines": a["machines"],
             "hashrate_ths": round(a["hashrate_ths"], 2)
             if a["hashrate_ths"] is not None else None,
             "hashrate_ts": int(a["oldest_ts"]) if a["time_known"] and a["oldest_ts"] is not None else None}
            for worker, a in sorted(_customer_metric_sample(records, identities, scan_ts=ts).items())]


def _past_customer_identities(conn, scans):
    """仅为窗口初次出现时无客户名的 IP 补过去身份，不能借用未来/当前名册。

    矿池名读取失败可能跨越多个整点；每小时只带入一轮旧读数不足以保留归属。
    查询走现有 snapshots(ip) 索引，正常都有矿工名时不产生额外查询。
    """
    if not scans:
        return {}
    seen, missing = set(), set()
    for _sid, _ts, records in scans:
        for r in records:
            if r["ip"] not in seen:
                seen.add(r["ip"])
                if not r.get("worker"):
                    missing.add(r["ip"])
    if not missing:
        return {}
    first_id, first_ts, _records = scans[0]
    rc = _r(conn)
    out = {}
    for ip in missing:
        row = rc.execute(
            "SELECT p.worker FROM snapshots p JOIN scans s ON s.scan_id=p.scan_id "
            "WHERE p.ip=? AND (p.worker<>'' OR p.status='online') "
            "AND s.kind IN ('full','quick','manual','inventory') "
            "AND (s.ts<? OR (s.ts=? AND s.scan_id<?)) "
            "ORDER BY (p.worker<>'') DESC, s.ts DESC, s.scan_id DESC LIMIT 1",
            (ip, first_ts, first_ts, first_id)).fetchone()
        if row:
            out[ip] = row["worker"] or "(未知)"
    return out


def _empty_metrics():
    return {"th_h": 0.0, "unknown_s": 0.0, "power_kwh": 0.0, "power_unknown_s": 0.0,
            "status_unknown_s": 0.0, "online_machine_s": 0.0, "known_machine_s": 0.0,
            "machines": 0}


def _customer_metric_window(conn, start, end):
    """Integrate completed scan results without turning a running scan into an outage.

    New rounds record their start and completion. When the next round started
    promptly, the displayed completed result covers that scan until it commits.
    A late next start or no next result still gets the bounded 15-minute tail.
    Historical rows without start metadata retain their strict sampling limit.
    """
    kinds = "('full','quick','manual','inventory')"
    rows = _r(conn).execute(
        "SELECT s.scan_id, s.ts, s.started_ts, s.source_scan_ts, s.kind, "
        "p.ip, p.worker, p.status, p.hr_rt, p.power, p.measured_ts "
        "FROM scans s LEFT JOIN snapshots p ON p.scan_id=s.scan_id WHERE s.kind IN " + kinds +
        " AND ((s.ts>=? AND s.ts<?) OR s.scan_id=(SELECT scan_id FROM scans WHERE kind IN " + kinds +
        " AND ts<? ORDER BY ts DESC,scan_id DESC LIMIT 1) OR s.scan_id=(SELECT scan_id FROM scans "
        "WHERE kind IN ('full','quick','manual') AND ts>=? ORDER BY ts,scan_id LIMIT 1)) "
        "ORDER BY s.ts,s.scan_id,p.ip", (start, end, start, end)).fetchall()
    scans, starts, sources, scan_kinds = [], {}, {}, {}
    for row in rows:
        if not scans or scans[-1][0] != row["scan_id"]:
            scans.append((row["scan_id"], row["ts"], []))
            starts[row["scan_id"]] = row["started_ts"]
            sources[row["scan_id"]] = row["source_scan_ts"] or row["ts"]
            scan_kinds[row["scan_id"]] = row["kind"]
        if row["ip"] is not None:
            scans[-1][2].append(dict(row))
    observed_s, workers = 0.0, {}
    identities = _past_customer_identities(conn, scans)
    # SN 台账覆盖瞬时矿工名；维修/限电机不计入客户可用率分母（不算客户停机）。
    ledger = asset_ledger_map(conn)
    sn_by_ip = {ip: (info.get("sn") or "") for ip, info in known_identity(conn).items()}
    for ip, sn in sn_by_ip.items():
        if sn and sn in ledger:
            identities[ip] = ledger[sn]
    quiet = quiet_ips(conn)
    following_real, next_real = {}, None
    for scan in reversed(scans):
        following_real[scan[0]] = next_real
        if scan_kinds[scan[0]] != "inventory":
            next_real = scan
    for i, (sid, ts, records) in enumerate(scans):
        for r in records:
            if r.get("worker"):
                identities[r["ip"]] = r["worker"]
            elif r.get("status") == "online":
                identities.setdefault(r["ip"], "(未知)")
            sn = sn_by_ip.get(r["ip"]) or ""
            if sn and sn in ledger:
                identities[r["ip"]] = ledger[sn]
        # 维修/限电：从计费样本剔除，避免压低客户可用率
        billing_records = [r for r in records if r["ip"] not in quiet] if quiet else records
        started = _measurement(starts[sid])
        source_ts = sources[sid]
        completed_round = started is not None and 0 < started <= source_ts <= ts
        next_scan = scans[i + 1] if i + 1 < len(scans) else None
        next_real = following_real[sid]
        next_start = _measurement(starts[next_real[0]]) if next_real else None
        continuous = (completed_round and next_start is not None and
                      source_ts - 60 <= next_start <= source_ts + MAX_SAMPLE_GAP)
        stop = min(end, next_scan[1] if next_scan else end,
                   next_real[1] if continuous else source_ts + MAX_SAMPLE_GAP)
        begin = max(start, ts)
        if stop <= begin:
            continue
        dt = stop - begin
        if completed_round:
            # Only measurements from this scan may support its completed result.
            # This never changes raw timestamps or relaxes automatic-action checks.
            checked = []
            for r in billing_records:
                stamp = record_measured_ts(r, ts)
                checked.append(r if stamp is not None and started - 60 <= stamp <= source_ts + 60
                               else dict(r, status="unknown"))
            sample = _customer_metric_sample(checked, identities, scan_ts=ts)
        else:
            sample = _customer_metric_sample(billing_records, identities, begin, ts, interval=True)
        observed_s += dt
        for worker, a in sample.items():
            valid_dt = dt if completed_round else (max(0.0, min(stop, a["oldest_ts"] + MAX_SAMPLE_GAP) - begin)
                if a["time_known"] and a["oldest_ts"] is not None else 0.0)
            dst = workers.setdefault(worker, _empty_metrics())
            dst["machines"] = max(dst["machines"], a["machines"])
            rate_dt = valid_dt if a["hashrate_ths"] is not None else 0.0
            dst["unknown_s"] += dt - rate_dt
            if rate_dt:
                dst["th_h"] += a["hashrate_ths"] * rate_dt / HOUR
            power_dt = valid_dt if a["power_w"] is not None else 0.0
            dst["power_unknown_s"] += dt - power_dt
            if power_dt:
                dst["power_kwh"] += a["power_w"] * power_dt / (1000 * HOUR)
            status_dt = valid_dt if a["status_known"] else 0.0
            dst["status_unknown_s"] += dt - status_dt
            dst["online_machine_s"] += a["online"] * status_dt
            dst["known_machine_s"] += a["machines"] * status_dt
    return observed_s, workers


def _customer_hashrate_window(conn, start, end):
    seconds, workers = _customer_metric_window(conn, start, end)
    return seconds, {w: {k: a[k] for k in ("th_h", "unknown_s", "machines")}
                     for w, a in workers.items()}


def _save_hashrate_hour(conn, hour, observed_s, workers):
    with _wlock, conn:
        conn.execute("INSERT OR REPLACE INTO hashrate_hourly(hour,observed_s) VALUES(?,?)",
                     (hour, observed_s))
        conn.execute("DELETE FROM worker_hashrate_hourly WHERE hour=?", (hour,))
        conn.executemany(
            "INSERT INTO worker_hashrate_hourly(hour,worker,th_h,unknown_s,machines) "
            "VALUES(?,?,?,?,?)",
            [(hour, worker, a["th_h"], a["unknown_s"], a["machines"])
             for worker, a in workers.items()])


def _rollup_customer_hashrates(conn, now, max_hours):
    """独立进度允许升级后补算尚保留的旧快照，不改写历史计费数字。"""
    cur_hour = now - now % HOUR
    hour = int(meta_get(conn, "hashrate_rollup_hour", 0) or 0)
    if not hour:
        row = _r(conn).execute("SELECT MIN(ts) t FROM scans WHERE kind IN ('full','quick','manual','inventory')").fetchone()
        if not row or row["t"] is None:
            return
        hour = row["t"] - row["t"] % HOUR
    n = 0
    while hour < cur_hour and n < max_hours:
        if _r(conn).execute("SELECT 1 FROM hashrate_hourly WHERE hour=?", (hour,)).fetchone() is None:
            observed_s, workers = _customer_hashrate_window(conn, hour, hour + HOUR)
            _save_hashrate_hour(conn, hour, observed_s, workers)
        hour += HOUR
        n += 1
        meta_set(conn, "hashrate_rollup_hour", hour)


def _customer_hashrate_stats(conn, hours, now):
    """有观测依据的时长平均；分母是客户有效墙钟时长，绝不是机器小时数。"""
    start = now - hours * HOUR
    first_hour = start - start % HOUR
    rc = _r(conn)
    archived = {r["hour"]: [r["observed_s"], {}] for r in rc.execute(
        "SELECT hour,observed_s FROM hashrate_hourly WHERE hour>=? AND hour<?",
        (first_hour, now)).fetchall()}
    for r in rc.execute("SELECT * FROM worker_hashrate_hourly WHERE hour>=? AND hour<?",
                        (first_hour, now)).fetchall():
        if r["hour"] in archived:
            archived[r["hour"]][1][r["worker"]] = dict(r)
    first_raw = rc.execute("SELECT MIN(ts) t FROM scans WHERE kind IN ('full','quick','manual','inventory')").fetchone()["t"]
    observed_s = 0.0
    workers = {}
    hour = first_hour
    while hour < now:
        lo, hi = max(start, hour), min(now, hour + HOUR)
        whole = lo == hour and hi == hour + HOUR
        raw_available = first_raw is not None and hi > first_raw
        part = archived.get(hour)
        # 非整点窗口优先按仍在的快照精确裁切，不能把整小时均值冒充这一截。
        if (part is None or not whole) and raw_available:
            seconds, rates = _customer_hashrate_window(conn, lo, hi)
            if whole:
                _save_hashrate_hour(conn, hour, seconds, rates)
        elif part is not None and whole:
            seconds, rates = part
        else:
            # 只有整小时归档时，不知道窗口截取的半小时里实际挖了多少。
            # 不能按比例摊后声称完整均值；这一截明确计为缺测，保留其余整小时。
            seconds, rates = 0.0, {}
        observed_s += seconds
        for worker, a in rates.items():
            dst = workers.setdefault(worker, {"th_h": 0.0, "unknown_s": 0.0, "machines": 0})
            dst["th_h"] += a["th_h"]
            dst["unknown_s"] += a["unknown_s"]
            dst["machines"] = max(dst["machines"], a["machines"])
        hour += HOUR
    return observed_s, workers


_METRIC_COLUMNS = ("th_h", "unknown_s", "power_kwh", "power_unknown_s", "status_unknown_s",
                   "online_machine_s", "known_machine_s", "machines")


def _save_metric_hour(conn, hour, seconds, workers):
    with _wlock, conn:
        conn.execute("INSERT OR REPLACE INTO customer_metrics_hourly(hour,observed_s) VALUES(?,?)",
                     (hour, seconds))
        conn.execute("DELETE FROM worker_metrics_hourly WHERE hour=?", (hour,))
        conn.executemany(
            "INSERT INTO worker_metrics_hourly(hour,worker,th_h,unknown_s,power_kwh,power_unknown_s,"
            "status_unknown_s,online_machine_s,known_machine_s,machines) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(hour, worker, *(a[k] for k in _METRIC_COLUMNS)) for worker, a in workers.items()])
        # 保留已有可信算力归档；只补缺项，绝不覆写旧计费账本。
        if conn.execute("SELECT 1 FROM hashrate_hourly WHERE hour=?", (hour,)).fetchone() is None:
            conn.execute("INSERT INTO hashrate_hourly(hour,observed_s) VALUES(?,?)", (hour, seconds))
            conn.executemany(
                "INSERT INTO worker_hashrate_hourly(hour,worker,th_h,unknown_s,machines) VALUES(?,?,?,?,?)",
                [(hour, w, a["th_h"], a["unknown_s"], a["machines"]) for w, a in workers.items()])


def _customer_metric_stats(conn, hours, now):
    """同一时间轴生成算力、耗电和在线率；无明细的旧账本不能冒充新观测。"""
    start = now - hours * HOUR
    first_hour = start - start % HOUR
    rc = _r(conn)
    archived = {r["hour"]: [r["observed_s"], {}] for r in rc.execute(
        "SELECT hour,observed_s FROM customer_metrics_hourly WHERE hour>=? AND hour<?",
        (first_hour, now)).fetchall()}
    for r in rc.execute("SELECT * FROM worker_metrics_hourly WHERE hour>=? AND hour<?",
                        (first_hour, now)).fetchall():
        if r["hour"] in archived:
            archived[r["hour"]][1][r["worker"]] = dict(r)
    prior = {r["hour"]: [r["observed_s"], {}] for r in rc.execute(
        "SELECT hour,observed_s FROM hashrate_hourly WHERE hour>=? AND hour<?",
        (first_hour, now)).fetchall()}
    for r in rc.execute("SELECT * FROM worker_hashrate_hourly WHERE hour>=? AND hour<?",
                        (first_hour, now)).fetchall():
        if r["hour"] in prior:
            prior[r["hour"]][1][r["worker"]] = dict(r)
    first_raw = rc.execute("SELECT MIN(ts) t FROM scans WHERE kind IN ('full','quick','manual','inventory')").fetchone()["t"]
    latest_raw = rc.execute("SELECT ts,started_ts FROM scans WHERE kind IN ('full','quick','manual') ORDER BY ts DESC,scan_id DESC LIMIT 1").fetchone()
    observed_s, workers = 0.0, {}
    hour = first_hour
    while hour < now:
        lo, hi = max(start, hour), min(now, hour + HOUR)
        whole = lo == hour and hi == hour + HOUR
        raw_available = first_raw is not None and hi > first_raw
        part, old = archived.get(hour), prior.get(hour)
        if part is not None and whole:
            seconds, rates = part
        elif raw_available:
            seconds, rates = _customer_metric_window(conn, lo, hi)
            # 清理后只余小时尾部明细时，旧完整算力归档仍可信；它不能恢复功率或状态时间。
            if whole and old is not None and first_raw > lo and old[0] > seconds:
                extra = old[0] - seconds
                merged = {}
                for worker in rates.keys() | old[1].keys():
                    a = dict(rates.get(worker, _empty_metrics()))
                    previous = old[1].get(worker, {})
                    a["th_h"] = previous.get("th_h", 0.0)
                    a["unknown_s"] = previous.get("unknown_s", 0.0)
                    a["machines"] = max(a["machines"], previous.get("machines", 0))
                    if previous.get("machines", 0):
                        a["power_unknown_s"] += extra
                        a["status_unknown_s"] += extra
                    merged[worker] = a
                seconds, rates = old[0], merged
            sealed = not latest_raw or latest_raw["started_ts"] is None or hi <= latest_raw["ts"]
            if whole and sealed:
                _save_metric_hour(conn, hour, seconds, rates)
        elif whole and old is not None:
            seconds, rates = old[0], {}
            for worker, previous in old[1].items():
                a = _empty_metrics()
                a.update({k: previous[k] for k in ("th_h", "unknown_s", "machines")})
                a["power_unknown_s"] = a["status_unknown_s"] = seconds
                rates[worker] = a
        else:
            # 非整小时只有归档时，不知道窗口内与窗口外的实际分布，禁止按比例摊入。
            seconds, rates = 0.0, {}
        observed_s += seconds
        for worker, a in rates.items():
            dst = workers.setdefault(worker, _empty_metrics())
            for key in _METRIC_COLUMNS:
                if key == "machines":
                    dst[key] = max(dst[key], a[key])
                else:
                    dst[key] += a[key]
        hour += HOUR
    return observed_s, workers


def _rollup_customer_metrics(conn, now, max_hours):
    """Archive the single current statistics model; never regenerate the retired ledger."""
    cur_hour = now - now % HOUR
    hour = int(meta_get(conn, "customer_metrics_rollup_hour", 0) or 0)
    if not hour:
        row = _r(conn).execute("SELECT MIN(ts) t FROM scans WHERE kind IN ('full','quick','manual','inventory')").fetchone()
        if not row or row["t"] is None:
            return 0
        hour = row["t"] - row["t"] % HOUR
    n = 0
    while hour < cur_hour and n < max_hours:
        _customer_metric_stats(conn, 1, hour + HOUR)
        if not _r(conn).execute("SELECT 1 FROM customer_metrics_hourly WHERE hour=?", (hour,)).fetchone():
            # The next long scan may still change this interval. Never advance
            # the pruning watermarks beyond an hour that was not archived.
            break
        hour += HOUR
        n += 1
        for key in ("customer_metrics_rollup_hour", "hashrate_rollup_hour", "rollup_hour"):
            meta_set(conn, key, hour)
    return n


def rollup_hours(conn, now=None, max_hours=48):
    """幂等归档已结束小时；限制补算工作量，停止生成旧估算账本。"""
    return _rollup_customer_metrics(conn, int(now or time.time()), max_hours)


def coverage_hours(conn):
    """当前可查询的报表跨度；各指标真实有效覆盖另随报表返回。"""
    rc = _r(conn)
    # 完整 manual 扫描也会产生 v2 归档；不能仅因旧计费表为空、raw 已清理就
    # 宣称没有历史数据。这里是保留跨度提示，不拿它替代任一指标的有效覆盖。
    earliest = rc.execute(
        "SELECT MIN(stamp) t FROM (SELECT MIN(hour) stamp FROM customer_metrics_hourly "
        "UNION ALL SELECT MIN(hour) FROM hashrate_hourly "
        "UNION ALL SELECT MIN(ts) FROM scans)").fetchone()["t"]
    if not earliest:
        return 0
    return max(1, int((time.time() - earliest) // HOUR))


def customer_report(conn, hours=24, hashrate_hours=None, until_ts=None):
    """One statistics model for delivery, consumption and availability.

    Historical averages use persisted hourly observations and raw scan intervals.
    Current values always refer to the latest completed result, as on the dashboard.
    """
    realtime_now = int(time.time())
    if until_ts is None:
        now = realtime_now
    else:
        end = _measurement(until_ts)
        if end is None or end <= 0 or end > realtime_now:
            raise ValueError("until_ts must be a positive Unix timestamp no later than now")
        now = int(end)
    hours = max(1, min(8760, int(hours)))
    rate_hours = hours if hashrate_hours is None else max(1, min(8760, int(hashrate_hours)))
    observed_s, rates = _customer_metric_stats(conn, rate_hours, now)
    billing_s, billing = (observed_s, rates) if hours == rate_hours else _customer_metric_stats(conn, hours, now)
    latest = latest_scan(conn)
    known = known_identity(conn)
    identities = {ip: a.get("worker") or "(未知)" for ip, a in known.items()}
    # SN 台账优先：换矿工名后仍按序列号归属对账
    for ip, worker in ledger_worker_for_ip(conn, known).items():
        identities[ip] = worker
    previous_id = prev_scan_id(conn, latest["scan_id"]) if latest else None
    if previous_id:
        for r in scan_records(conn, previous_id):
            if r.get("worker") or r.get("status") == "online":
                identities.setdefault(r["ip"], r.get("worker") or "(未知)")
    current = {r["worker"]: r for r in customer_hashrates(
        latest, scan_records(conn, latest["scan_id"]) if latest else [], realtime_now, identities)}
    current_ts = int(latest["ts"] or 0) if latest else 0
    out = []
    for worker in sorted(rates.keys() | billing.keys() | current.keys()):
        rate, bill, live = rates.get(worker, {}), billing.get(worker, {}), current.get(worker, {})
        seconds = max(0.0, observed_s - rate.get("unknown_s", 0.0))
        delivery_s = max(0.0, billing_s - bill.get("unknown_s", 0.0))
        power_s = max(0.0, billing_s - bill.get("power_unknown_s", 0.0))
        uptime_s = max(0.0, billing_s - bill.get("status_unknown_s", 0.0))
        stamp = live.get("hashrate_ts", current_ts)
        age = max(0, realtime_now - stamp) if stamp and stamp <= realtime_now + 60 else None
        peak = max(rate.get("machines", 0), bill.get("machines", 0)) \
            if observed_s > 0 or billing_s > 0 else live.get("machines", 0)
        out.append({
            "worker": worker, "machines": peak, "machines_source": "observed",
            "current_machines": live.get("machines", 0) if latest else None,
            "uptime_pct": round(bill.get("online_machine_s", 0.0) / bill["known_machine_s"] * 100, 2)
            if bill.get("known_machine_s", 0) > 0 else None,
            "delivered_th_h": round(bill.get("th_h", 0.0), 3) if delivery_s > 0 else None,
            "power_kwh": round(bill.get("power_kwh", 0.0), 3) if power_s > 0 else None,
            "billing_method": "time_weighted_v2", "billing_hours": hours,
            "billing_period_start_ts": now - hours * HOUR, "billing_period_end_ts": now,
            "billing_source": "observed" if max(delivery_s, power_s, uptime_s) > 0 else "missing",
            "delivery_observed_hours": round(delivery_s / HOUR, 6),
            "delivery_coverage_pct": round(min(100.0, delivery_s / (hours * HOUR) * 100), 4),
            "power_observed_hours": round(power_s / HOUR, 6),
            "power_coverage_pct": round(min(100.0, power_s / (hours * HOUR) * 100), 4),
            "uptime_observed_hours": round(uptime_s / HOUR, 6),
            "uptime_coverage_pct": round(min(100.0, uptime_s / (hours * HOUR) * 100), 4),
            # Keep null compatibility keys for older clients, without old values or rows.
            "legacy_delivered_th_h": None, "legacy_power_kwh": None, "legacy_uptime_pct": None,
            "hashrate_ths": live.get("hashrate_ths", 0.0) if latest else None,
            "hashrate_ts": stamp, "hashrate_age_s": age,
            "hashrate_stale": age is not None and age > MAX_SAMPLE_GAP,
            "avg_hashrate_ths": round(rate.get("th_h", 0.0) * HOUR / seconds, 2) if seconds > 0 else None,
            "hashrate_hours": rate_hours, "observed_hours": round(seconds / HOUR, 6),
            "period_start_ts": now - rate_hours * HOUR, "period_end_ts": now,
            "coverage_pct": round(min(100.0, seconds / (rate_hours * HOUR) * 100), 4),
        })
    out.sort(key=lambda x: x["machines"], reverse=True)
    return out


def _day_key_utc8(ts):
    """UTC+8 切日键 YYYY-MM-DD。"""
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts) + 8 * 3600))


def rollup_customer_daily(conn, day_offsets=(0, 1), now=None):
    """把已归档的小时客户指标幂等汇总进 customer_daily（今天+昨天默认）。"""
    now = int(now or time.time())
    # UTC+8 今天 0 点对应的 unix
    local = now + 8 * 3600
    today0 = (local - local % 86400) - 8 * 3600
    n = 0
    for off in day_offsets:
        day_start = today0 - int(off) * 86400
        day_end = day_start + 86400
        if day_end > now:
            day_end = now
        if day_end <= day_start:
            continue
        hours = max(1, int((day_end - day_start + HOUR - 1) // HOUR))
        observed_s, workers = _customer_metric_stats(conn, hours, day_end)
        # 窗口可能略长于一天：只取落入该日的小时归档再加总更准；这里用截止到 day_end 的 hours 近似。
        day = _day_key_utc8(day_start)
        with _wlock, conn:
            conn.execute("DELETE FROM customer_daily WHERE day=?", (day,))
            conn.executemany(
                "INSERT INTO customer_daily(day,worker,machines,delivered_th_h,power_kwh,"
                "online_machine_s,known_machine_s,observed_s) VALUES(?,?,?,?,?,?,?,?)",
                [(day, w, a.get("machines", 0), round(a.get("th_h", 0.0), 3),
                  round(a.get("power_kwh", 0.0), 3), a.get("online_machine_s", 0.0),
                  a.get("known_machine_s", 0.0), observed_s)
                 for w, a in workers.items()])
        n += 1
    return n


def customer_daily_report(conn, days=30, until_ts=None):
    """近 N 天客户日汇总（优先读 customer_daily；缺日则现场补算）。"""
    now = int(until_ts or time.time())
    days = max(1, min(400, int(days)))
    rollup_customer_daily(conn, day_offsets=tuple(range(days)), now=now)
    local = now + 8 * 3600
    today0 = (local - local % 86400) - 8 * 3600
    start_day = _day_key_utc8(today0 - (days - 1) * 86400)
    rows = _r(conn).execute(
        "SELECT day,worker,machines,delivered_th_h,power_kwh,online_machine_s,known_machine_s,observed_s "
        "FROM customer_daily WHERE day>=? ORDER BY day DESC, machines DESC",
        (start_day,)).fetchall()
    out = []
    for r in rows:
        uptime = None
        if (r["known_machine_s"] or 0) > 0:
            uptime = round(r["online_machine_s"] / r["known_machine_s"] * 100, 2)
        out.append({
            "day": r["day"], "worker": r["worker"], "machines": r["machines"],
            "delivered_th_h": r["delivered_th_h"], "power_kwh": r["power_kwh"],
            "uptime_pct": uptime, "observed_s": r["observed_s"],
        })
    return {"days": days, "tz": "UTC+8", "customers_daily": out}


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
    rate_rolled_to = int(meta_get(conn, "hashrate_rollup_hour", 0) or 0)
    metrics_rolled_to = int(meta_get(conn, "customer_metrics_rollup_hour", 0) or 0)
    snap_cut = min(time_cut, rolled_to, rate_rolled_to, metrics_rolled_to) \
        if rolled_to and rate_rolled_to and metrics_rolled_to else 0
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
        gone = [r["ip"] for r in conn.execute(
            "SELECT ip FROM known_miners WHERE last_online<? AND state='active'",
            (roster_cut,)).fetchall()]
        conn.execute("DELETE FROM known_miners WHERE last_online<? AND state='active'",
                     (roster_cut,))
        # 掉出名册的机器不再被评估：它们的掉线告警、整段已清空网段的 segment_down 一并消掉
        _resolve_gone_miners(conn, gone, now)
        conn.execute("DELETE FROM worker_hourly WHERE hour<?", (rollup_cut,))
        conn.execute("DELETE FROM worker_hashrate_hourly WHERE hour<?", (rollup_cut,))
        conn.execute("DELETE FROM hashrate_hourly WHERE hour<?", (rollup_cut,))
        conn.execute("DELETE FROM worker_metrics_hourly WHERE hour<?", (rollup_cut,))
        conn.execute("DELETE FROM customer_metrics_hourly WHERE hour<?", (rollup_cut,))
        cut_day = _day_key_utc8(rollup_cut)
        conn.execute("DELETE FROM customer_daily WHERE day<?", (cut_day,))
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
