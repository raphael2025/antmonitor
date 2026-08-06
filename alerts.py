#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警规则评估 + 推送。

性能约定（关键）：一轮评估对 5000 台机器只做**常数次(O(1))整表读、2 次批量写**，
读写次数与机器数量无关（当前约 6 条 SELECT：告警状态/上轮扫描/上轮记录/维修名单/名册/网段告警）。
之前的实现对每台机逐台调 active_alert/recent_alert 点查、逐台 resolve_alert 单独提交事务，
5000 台每轮上万次事务 → 这是 WAL 膨胀到 800MB 的直接原因。

推送走独立线程队列：Telegram 超时 10s，绝不能卡在扫描落库路径上。
"""
import queue
import threading
import time

import requests

import db
import logs
import miner_core

log = logs.get(__name__)

# 本模块产生的全部告警类型（启动清理旧类型时作为白名单）
MINER_TYPES = ("offline", "zero", "reject", "low_hashrate", "overheat",
               "segment_down", "stalled")

# ---- 推送队列：发送失败/慢不影响扫描 ----
_push_q = queue.Queue(maxsize=200)
_push_thread = None
_push_lock = threading.Lock()


def _push_worker():
    while True:
        cfg, text = _push_q.get()
        try:
            _tg_send(cfg, text)
        except Exception as e:  # noqa: BLE001
            log.warning("告警推送失败: %s", e)
        finally:
            _push_q.task_done()


def _enqueue(tg_cfg, text):
    global _push_thread
    if not tg_cfg.get("enabled"):
        return
    with _push_lock:
        if _push_thread is None or not _push_thread.is_alive():
            _push_thread = threading.Thread(target=_push_worker, daemon=True, name="alert-push")
            _push_thread.start()
    try:
        _push_q.put_nowait((tg_cfg, text))
    except queue.Full:      # 大面积事件时宁可丢推送，也不能阻塞扫描线程
        log.warning("告警推送队列已满，丢弃一条")


def _tg_send(tg_cfg, text):
    token = tg_cfg.get("bot_token", "")
    chat = tg_cfg.get("chat_id", "")
    if not token or not chat:
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=10)
    r.raise_for_status()


class _Batch:
    """一轮评估的告警增量收集器：内存里判重/判冷却，最后两条 SQL 落库。"""

    def __init__(self, conn, cooldown, now):
        self.conn = conn
        self.now = now
        self.cooldown = cooldown
        self.active, self.cooled = db.alert_state(conn, now - cooldown if cooldown else 0)
        self.to_fire = []      # [(ip, type, sev, detail)]
        self.to_resolve = []   # [(ip, type)]

    def fire(self, ip, type_, sev, detail):
        key = (ip, type_)
        if key in self.active:          # 已有未恢复的同类告警
            return
        rt = self.cooled.get(key)       # 刚恢复不久的抖动不再报，防刷屏
        if rt and (self.now - rt) < self.cooldown:
            return
        self.active.add(key)            # 同一轮内不重复
        self.to_fire.append((ip, type_, sev, detail))

    def resolve(self, ip, type_):
        key = (ip, type_)
        if key not in self.active:      # 本来就没有 → 不产生无谓 UPDATE
            return
        self.active.discard(key)
        self.to_resolve.append(key)

    def commit(self):
        if self.to_resolve:
            db.resolve_alerts_bulk(self.conn, self.to_resolve)
        if self.to_fire:
            db.raise_alerts_bulk(self.conn, self.to_fire)
        return self.to_fire


def evaluate(conn, scan_id, records, cfg, kind=None, state=None):
    """对比上一次同类扫描，产生/恢复告警，返回本轮新增告警列表。

    state: 跨轮次的内存状态(掉算力连续计数)。由 MonitorService 持有并传入。
    """
    acfg = cfg.get("alerts", {})
    if not acfg.get("enabled", True):
        return []
    state = state if state is not None else {}
    streak = state.setdefault("low_hr_streak", {})

    cooldown = acfg.get("cooldown", 1800)
    seg_ratio = acfg.get("segment_down_ratio", 0.6)
    seg_min = acfg.get("segment_down_min", 5)
    rej_thresh = acfg.get("reject_pct", 5.0)
    grace = acfg.get("zero_grace_sec", 600)
    lo_ratio = acfg.get("low_hashrate_ratio", 0.7)
    lo_rounds = max(1, int(acfg.get("low_hashrate_rounds", 2)))
    lo_peers = max(1, int(acfg.get("low_hashrate_min_peers", 5)))
    hot_c = acfg.get("overheat_c", 95)
    hot_clear = acfg.get("overheat_clear_c", 90)
    roster_age = cfg.get("scan", {}).get("roster_retention_days", 7)
    now = int(time.time())

    cur = {r["ip"]: r for r in records}
    prev_id = db.prev_scan_id(conn, scan_id, kind)   # 同类扫描的上一次
    prev = {r["ip"]: r for r in db.scan_records(conn, prev_id)} if prev_id else {}
    repair = db.repair_ips(conn)          # 维修中的机器：不报警
    roster = set(db.roster_ips(conn, roster_age))   # 已知真机(算网段比例的分母)

    b = _Batch(conn, cooldown, now)

    # —— 网段级故障(基于当前绝对离线率)：交换机持续挂着会一直维持事件 ——
    seg_total, seg_off = {}, {}
    for ip, r in cur.items():
        if ip in roster:
            seg = ".".join(ip.split(".")[:3])
            seg_total[seg] = seg_total.get(seg, 0) + 1
            if r["status"] == "offline":
                seg_off[seg] = seg_off.get(seg, 0) + 1
    down_segments = set()
    for seg, off in seg_off.items():
        tot = seg_total.get(seg, 0)
        if off >= seg_min and tot > 0 and off / tot >= seg_ratio:
            down_segments.add(seg)
            b.fire(f"{seg}.x", "segment_down", "crit",
                   f"网段 {seg}.x 大面积掉线 {off}/{tot}（疑似交换机/断电）")
    for a in db.active_alerts_by_type(conn, "segment_down"):   # 离线率回落 → 恢复
        seg = a["ip"][:-2] if a["ip"].endswith(".x") else a["ip"]
        # 只对"本轮有样本"的段做回落判定；整段掉出名册/已拆除(无样本)时保持告警不动，
        # 否则长期整段断电会因分母消失被误判为已恢复而永久消音(漏报真故障)
        if seg_total.get(seg, 0) > 0 and seg not in down_segments:
            b.resolve(a["ip"], "segment_down")

    # —— 掉算力基线：同机型在线机的实时算力中位数 ——
    baselines = miner_core.model_baselines(records)
    peers = {}
    for r in records:
        if r["status"] == "online" and (r.get("hr_rt") or 0) > 0:
            peers[r.get("model") or "?"] = peers.get(r.get("model") or "?", 0) + 1

    for ip, r in cur.items():
        seg = ".".join(ip.split(".")[:3])
        if r["status"] == "offline":
            for t in ("zero", "reject", "low_hashrate", "overheat"):
                b.resolve(ip, t)
            streak.pop(ip, None)
            if ip in repair or seg in down_segments:
                b.resolve(ip, "offline")   # 维修中/被网段事件覆盖 → 清掉历史单条
                continue
            if prev.get(ip, {}).get("status") == "online":
                b.fire(ip, "offline", "crit", f"{ip} 掉线")
            continue
        b.resolve(ip, "offline")

        hr = r.get("hr_rt")
        up = r.get("uptime")
        warming = grace and isinstance(up, (int, float)) and up < grace   # 刚开机/升频中
        muted = ip in repair or seg in down_segments   # 维修中 / 整段掉线 → 不单独报

        if hr == 0:                       # 明确为 0 才算零算力
            if muted or warming:
                b.resolve(ip, "zero")
            else:
                b.fire(ip, "zero", "warn", f"{ip} 零算力")
        elif hr is not None:              # 有正算力 → 消零算力警
            b.resolve(ip, "zero")
        # hr 为 None(读不到/密码错/接口未就绪)：不当零算力，既不报也不清

        # 掉算力：低于同机型中位数一定比例，且连续 N 轮成立(防单轮读数抖动)
        base = baselines.get(r.get("model") or "?")
        if hr is None:
            # 本轮读不到算力(接口抖动/未就绪)：不是"恢复"的证据，
            # 连续计数保持不变，既不触发新告警也不消已有告警
            pass
        elif muted or warming:
            streak.pop(ip, None)
            b.resolve(ip, "low_hashrate")
        elif (lo_ratio and hr and base and peers.get(r.get("model") or "?", 0) >= lo_peers
                and hr < base * lo_ratio):
            streak[ip] = streak.get(ip, 0) + 1
            if streak[ip] >= lo_rounds:
                b.fire(ip, "low_hashrate", "warn",
                       f"{ip} 算力 {hr} TH 低于同型号中位数 {base} TH 的 "
                       f"{int(lo_ratio * 100)}%（疑似算力板/风扇故障）")
        else:
            streak.pop(ip, None)
            b.resolve(ip, "low_hashrate")

        # 高温：滞回消警，避免在阈值附近反复触发/恢复刷屏
        temp = r.get("temp")
        if hot_c and isinstance(temp, (int, float)):
            if temp >= hot_c and not muted:
                b.fire(ip, "overheat", "crit", f"{ip} 芯片温 {temp}℃ ≥ {hot_c}℃")
            elif temp < hot_clear or muted:
                # 未静音：滞回消警(中间区间 [hot_clear, hot_c) 维持原状态)
                # 维修中/整段掉线静音：无条件消掉已有高温告警，与 zero/reject 一致
                b.resolve(ip, "overheat")

        # 拒绝率（矿池健康）
        if rej_thresh and rej_thresh > 0 and (r.get("accepted") is not None
                                              or r.get("rejected") is not None):
            rp = miner_core.reject_pct(r.get("accepted"), r.get("rejected"))
            if rp >= rej_thresh and not muted:
                b.fire(ip, "reject", "warn", f"{ip} 拒绝率 {rp}% ≥ {rej_thresh}%")
            else:
                b.resolve(ip, "reject")

    for ip in list(streak):     # 已不在本轮样本内的机器(下架/换IP)：别让计数无限增长
        if ip not in cur:
            streak.pop(ip, None)

    fired = b.commit()
    if fired:
        _push_batch(cfg.get("telegram", {}), fired)
    return fired


def evaluate_containers(conn, containers, known_before, cfg):
    """集装箱冷却告警：故障位 → 告警；箱体掉线 → 告警；恢复自动消警。
    known_before: 本轮扫描前已记住的箱体 IP 集合（用于判定掉线）。"""
    acfg = cfg.get("alerts", {})
    if not acfg.get("enabled", True):
        return []
    now = int(time.time())
    cur = {c["ip"]: c for c in containers}
    b = _Batch(conn, acfg.get("cooldown", 1800), now)

    ignore = set(acfg.get("container_faults_ignore", []))
    sp_min = acfg.get("container_supply_pressure_min", 0) or 0
    rp_min = acfg.get("container_return_pressure_min", 0) or 0
    existing = db.active_alerts_by_type(conn, like="cooler")

    for ip, c in cur.items():
        # 故障位（忽略列表内的不报警，但仍在卡片显示）
        active = {f"cooler:{f['flag']}": f for f in (c.get("faults") or [])
                  if f["flag"] not in ignore}
        sp, rp = c.get("supply_pressure"), c.get("return_pressure")
        if sp_min and isinstance(sp, (int, float)) and sp < sp_min:
            active["cooler:supply_pressure_low"] = {"label": f"供液压力低 {sp}<{sp_min}MPa",
                                                    "sev": "crit"}
        if rp_min and isinstance(rp, (int, float)) and rp < rp_min:
            active["cooler:return_pressure_low_th"] = {"label": f"回液压力低 {rp}<{rp_min}MPa",
                                                       "sev": "crit"}
        for type_, f in active.items():
            b.fire(ip, type_, f["sev"], f"集装箱 {ip} {f['label']}")
        for a in existing:      # 消除已恢复的该箱冷却告警
            if a["ip"] == ip and a["type"].startswith("cooler:") and a["type"] not in active:
                b.resolve(ip, a["type"])
        b.resolve(ip, "cooler_offline")   # 箱体恢复在线

    for ip in known_before:              # 已记住的箱、本次未采到
        if ip not in cur:
            b.fire(ip, "cooler_offline", "crit", f"集装箱 {ip} 控制器离线")

    fired = b.commit()
    if fired:
        _push_batch(cfg.get("telegram", {}), fired)
    return fired


def _push_batch(tg_cfg, fired):
    icons = {"crit": "🔴", "warn": "🟠", "info": "🟢"}
    lines = ["<b>⛏ 矿机告警</b>"]
    for ip, type_, sev, detail in fired[:30]:
        lines.append(f"{icons.get(sev, '•')} {detail}")
    if len(fired) > 30:
        lines.append(f"... 另有 {len(fired) - 30} 条")
    _enqueue(tg_cfg, "\n".join(lines))


def push_text(cfg, text):
    _enqueue(cfg.get("telegram", {}), text)
