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
from control import pool_allowed as control_pool_allowed, pool_host as control_pool_host

log = logs.get(__name__)

# 本模块产生的全部告警类型（启动清理旧类型时作为白名单）
MINER_TYPES = ("offline", "zero", "reject", "low_hashrate", "overheat",
               "segment_down", "stalled", "pool_hijack")

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

    def fire(self, ip, type_, sev, detail, force=False):
        key = (ip, type_)
        if key in self.active:          # 已有未恢复的同类告警
            return
        rt = self.cooled.get(key)       # 刚恢复不久的抖动不再报，防刷屏
        if rt and (self.now - rt) < self.cooldown and not force:
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
    rebooting = state.setdefault("rebooting", {})   # ip → 下发重启的时间(MonitorService.mark_rebooting 写入)
    rb_grace = cfg.get("control", {}).get("reboot_grace_sec", 600)

    def rb_expired(ip):   # 有重启标记且静默期已过(按时间戳现判，见下方在线分支注释)
        t = rebooting.get(ip)
        return t is not None and now - t >= rb_grace

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
    pool_allow = [a for a in (cfg.get("control", {}).get("pool_allowlist") or []) if str(a).strip()]
    roster_age = cfg.get("scan", {}).get("roster_retention_days", 7)
    now = int(time.time())

    cur = {r["ip"]: r for r in records}
    repair = db.repair_ips(conn)          # 维修中的机器：不报警
    roster = set(db.roster_ips(conn, roster_age))   # 已知真机(算网段比例的分母)
    # 掉线告警按状态判，不按"上一轮 online → 本轮 offline"的跳变判：跳变那一轮只要没报出来
    # (冷却期/上一轮是 unknown/被网段事件或维修静音)，之后上一轮已是 offline 就永远不再报。
    # 现在：名册内、当前离线、且自最后一次在线以来还没报过掉线 → 报(冷却只会推迟，不会吞掉)。
    last_online = db.last_online_map(conn)
    last_off_alert = db.last_alert_ts(conn, "offline", now - roster_age * 86400)

    b = _Batch(conn, cooldown, now)
    # 静默期内的刚重启机器：掉线是预期内的，不报单机掉线，也不计入网段掉线比例
    # (整段批量重启不能被当成交换机/断电事件)
    rb_quiet = {ip for ip, t in list(rebooting.items()) if now - t < rb_grace}

    # —— 网段级故障(基于当前绝对离线率)：交换机持续挂着会一直维持事件 ——
    # unknown(本轮未探测)不计入分子/分母——理由跟单机判断一致：拥堵时"没问过"
    # 不该被当成"问了没事"，否则真出大面积故障时反而因为分母被冲淡而更难触发。
    # 但同时要记 seg_unknown：用于下面"回落判定"时识别"这轮数据不完整、不可信"。
    seg_total, seg_off, seg_unknown = {}, {}, {}
    for ip, r in cur.items():
        if ip not in roster or ip in rb_quiet or ip in repair:   # 送修拔电的不算"网段掉线"
            continue
        seg = ".".join(ip.split(".")[:3])
        if r["status"] == "unknown":
            seg_unknown[seg] = seg_unknown.get(seg, 0) + 1
            continue
        seg_total[seg] = seg_total.get(seg, 0) + 1
        if r["status"] == "offline":
            seg_off[seg] = seg_off.get(seg, 0) + 1
    down_segments = set()
    for seg, off in seg_off.items():
        tot = seg_total.get(seg, 0)
        if off >= seg_min and tot > 0 and off / tot >= seg_ratio:
            down_segments.add(seg)
            # force：整段事件不受冷却限制。下面会用 down_segments 静音段内单机告警，
            # 若这条被冷却拦下，同一栋冷却期内第二次断电就既无网段告警也无单机告警
            b.fire(f"{seg}.x", "segment_down", "crit",
                   f"网段 {seg}.x 大面积掉线 {off}/{tot}（疑似交换机/断电）", force=True)
    for a in db.active_alerts_by_type(conn, "segment_down"):   # 离线率回落 → 恢复
        seg = a["ip"][:-2] if a["ip"].endswith(".x") else a["ip"]
        tot = seg_total.get(seg, 0)
        unk = seg_unknown.get(seg, 0)
        # 只对"本轮有足够样本、数据可信"的段做回落判定：
        # 1) 整段掉出名册/已拆除(无样本)时保持告警不动，否则长期整段断电会因分母消失
        #    被误判为已恢复而永久消音(漏报真故障)
        # 2) 扫描拥堵导致该段大量机器本轮变成 unknown 时，剩下的样本已经不能代表
        #    真实情况——unknown 数量追上甚至超过确认样本数，说明这轮数据不可信，
        #    暂不消警，等下一轮拿到更完整的数据再判断(防止拥堵期间误判"已恢复")
        if tot > 0 and unk <= tot and seg not in down_segments:
            b.resolve(a["ip"], "segment_down")

    # —— 掉算力基线：同机型在线机的实时算力中位数 ——
    baselines = miner_core.model_baselines(records)
    peers = {}
    for r in records:
        if r["status"] == "online" and (r.get("hr_rt") or 0) > 0:
            peers[r.get("model") or "?"] = peers.get(r.get("model") or "?", 0) + 1

    for ip, r in cur.items():
        if r["status"] == "unknown":
            # 本轮扫描超时没来得及探测(miner_core.scan 的 overall_to 兜底)：
            # 既不是确认在线也不是确认离线，原样跳过——保留上一轮的告警状态不动，
            # 既不新报也不误清，等下一轮真正探测到再判断。
            continue
        seg = ".".join(ip.split(".")[:3])
        if r["status"] == "offline":
            for t in ("zero", "reject", "low_hashrate", "overheat"):
                b.resolve(ip, t)
            streak.pop(ip, None)
            if ip in repair or seg in down_segments:
                b.resolve(ip, "offline")   # 维修中/被网段事件覆盖 → 清掉历史单条
                rebooting.pop(ip, None)
                continue
            if ip in rb_quiet:             # 刚下发重启，还在静默期
                continue
            if rb_expired(ip):   # 静默期已过仍不在线：重启没起来
                rebooting.pop(ip, None)
                b.fire(ip, "offline", "crit",
                       f"{ip} 重启后 {max(1, rb_grace // 60)} 分钟仍未上线")
                continue
            lo = last_online.get(ip)
            if ip in roster and lo is not None and last_off_alert.get(ip, -1) <= lo:
                b.fire(ip, "offline", "crit", f"{ip} 掉线")
            continue
        b.resolve(ip, "offline")
        # 静默期结束时在线 → 重启完成。按时间戳现判，不用 rb_quiet：评估途中请求线程
        # 可能刚写入新标记，rb_quiet 里还没有它，会被误当成"已过期"清掉
        if rb_expired(ip):
            rebooting.pop(ip, None)

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

        # 矿池防篡改：矿机上配置了白名单外的矿池(含备用池) → 严重告警，不受维修/网段静音。
        # 覆盖"绕过面板直接用 root/root 登矿机改池"这条面板管不到的路径
        pools = r.get("pools")
        if pool_allow and pools is not None:
            bad = sorted({control_pool_host(u) or u for u in pools
                          if not control_pool_allowed(u, pool_allow)})
            if bad:
                b.fire(ip, "pool_hijack", "crit",
                       f"{ip} 矿池不在白名单(疑似被篡改偷算力): {', '.join(bad)[:200]}")
            else:
                b.resolve(ip, "pool_hijack")

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
    for ip in list(rebooting):  # 同理：静默期过了还没出现在样本里的(下架/换IP)
        if ip not in cur and rb_expired(ip):
            rebooting.pop(ip, None)

    fired = b.commit()
    if fired:
        _push_batch(cfg.get("telegram", {}), fired)
    return fired


# 集装箱故障连续这么多轮读到"正常"才消警(10 秒刷新 ≈ 30 秒)。一次抖动就消警的话，
# 下一轮故障还在却被冷却拦下，漏液最长 30 分钟没有活跃告警
COOLER_CLEAR_ROUNDS = 3
_COOLER_STATE = {}


def evaluate_containers(conn, containers, known_before, cfg, state=None):
    """集装箱冷却告警：故障位 → 告警；箱体掉线 → 告警；连续 COOLER_CLEAR_ROUNDS 轮正常才消警。
    known_before: 本轮扫描前已记住的箱体 IP 集合（用于判定掉线）。
    state: 跨轮次状态(由 MonitorService 持有)；省略时用模块级默认。"""
    acfg = cfg.get("alerts", {})
    if not acfg.get("enabled", True):
        return []
    clean = (state if state is not None else _COOLER_STATE).setdefault("cooler_clean", {})
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
            clean.pop((ip, type_), None)
            # crit(漏液/断流/冻结…)不受冷却限制：真恢复后又复发必须立刻再报
            b.fire(ip, type_, f["sev"], f"集装箱 {ip} {f['label']}", force=f["sev"] == "crit")
        for a in existing:      # 消除已恢复的该箱冷却告警
            if a["ip"] != ip or not a["type"].startswith("cooler:") or a["type"] in active:
                continue
            # 压力阈值开着但这轮读不到压力：不是"恢复"的证据，保持原状
            if (a["type"] == "cooler:supply_pressure_low" and sp_min and sp is None) or \
                    (a["type"] == "cooler:return_pressure_low_th" and rp_min and rp is None):
                continue
            k = (ip, a["type"])
            clean[k] = clean.get(k, 0) + 1
            if clean[k] >= COOLER_CLEAR_ROUNDS:
                clean.pop(k, None)
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
