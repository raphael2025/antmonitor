#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警规则评估 + Telegram 推送。"""
import time
import requests

import db
import miner_core


def _tg_send(tg_cfg, text):
    if not tg_cfg.get("enabled"):
        return
    token = tg_cfg.get("bot_token", "")
    chat = tg_cfg.get("chat_id", "")
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=10)
    except requests.RequestException:
        pass


def evaluate(conn, scan_id, records, cfg, kind=None):
    """对比上一次同类扫描，产生/恢复告警，返回本轮新增告警列表。"""
    acfg = cfg.get("alerts", {})
    if not acfg.get("enabled", True):
        return []
    cooldown = acfg.get("cooldown", 1800)
    seg_ratio = acfg.get("segment_down_ratio", 0.6)
    seg_min = acfg.get("segment_down_min", 5)
    rej_thresh = acfg.get("reject_pct", 5.0)   # 0 = 关闭拒绝率告警
    grace = acfg.get("zero_grace_sec", 600)
    roster_age = cfg.get("scan", {}).get("roster_retention_days", 7)
    now = int(time.time())

    cur = {r["ip"]: r for r in records}
    prev_id = db.prev_scan_id(conn, scan_id, kind)   # 同类扫描的上一次，避免 quick/full 混排
    prev = {r["ip"]: r for r in db.scan_records(conn, prev_id)} if prev_id else {}
    repair = db.repair_ips(conn)   # 维修中的机器：不报离线
    roster = set(db.roster_ips(conn, roster_age))   # 已知真机(算网段比例的分母)

    fired = []

    def fire(ip, type_, sev, detail):
        if db.active_alert(conn, ip, type_):
            return  # 已有未恢复的同类告警
        rec = db.recent_alert(conn, ip, type_)   # 冷却：刚恢复不久的抖动不再报，防刷屏
        if rec and rec.get("resolved") and rec.get("resolved_ts") and (now - rec["resolved_ts"]) < cooldown:
            return
        db.raise_alert(conn, ip, type_, sev, detail)
        fired.append((ip, type_, sev, detail))

    # —— 网段级故障(基于当前绝对离线率，而非"本轮新跌落")：交换机持续挂着会一直维持事件 ——
    seg_total, seg_off = {}, {}
    for ip, r in cur.items():
        if ip in roster:   # 只数已知真机，排除空 IP
            seg = ".".join(ip.split(".")[:3])
            seg_total[seg] = seg_total.get(seg, 0) + 1
            if r["status"] == "offline":
                seg_off[seg] = seg_off.get(seg, 0) + 1
    down_segments = set()
    for seg, off in seg_off.items():
        tot = seg_total.get(seg, 0)
        if off >= seg_min and tot > 0 and off / tot >= seg_ratio:
            down_segments.add(seg)
            fire(f"{seg}.x", "segment_down", "crit",
                 f"网段 {seg}.x 大面积掉线 {off}/{tot}（疑似交换机/断电）")
    for a in db.active_alerts_by_type(conn, "segment_down"):   # 离线率回落 → 恢复网段事件
        seg = a["ip"][:-2] if a["ip"].endswith(".x") else a["ip"]
        # 只对"本轮有样本(seg_total>0)"的段做回落判定；整段掉出7天名册/已拆除(无样本)时
        # 保持告警不动——否则长期整段断电会因分母消失被误判为已恢复而永久消音(漏报真故障)
        if seg_total.get(seg, 0) > 0 and seg not in down_segments:
            db.resolve_alert(conn, a["ip"], "segment_down")

    for ip, r in cur.items():
        seg = ".".join(ip.split(".")[:3])
        if r["status"] == "offline":
            db.resolve_alert(conn, ip, "zero")
            db.resolve_alert(conn, ip, "reject")
            if ip in repair or seg in down_segments:
                db.resolve_alert(conn, ip, "offline")  # 维修中/被网段事件覆盖 → 清掉历史单条
                continue
            if prev.get(ip, {}).get("status") == "online":
                fire(ip, "offline", "crit", f"{ip} 掉线")
            continue
        db.resolve_alert(conn, ip, "offline")

        hr = r.get("hr_rt")
        if hr == 0:                       # 明确为 0 才算零算力
            up = r.get("uptime")
            if ip in repair or seg in down_segments:   # 维修中 / 本段已判整段掉线 → 不单独报零算力
                db.resolve_alert(conn, ip, "zero")     # 一栋断电合并成一条网段事件，段内零算力一起压掉
            elif grace and isinstance(up, (int, float)) and up < grace:
                db.resolve_alert(conn, ip, "zero")   # 刚开机/升频中，暂不报
            else:
                fire(ip, "zero", "warn", f"{ip} 零算力")
        elif hr is not None:              # 有正算力 → 消零算力警
            db.resolve_alert(conn, ip, "zero")
        # hr 为 None(读不到/密码错/接口未就绪)：不当零算力，既不报也不清

        # 拒绝率（矿池健康）
        if rej_thresh and rej_thresh > 0 and (r.get("accepted") is not None or r.get("rejected") is not None):
            rp = miner_core.reject_pct(r.get("accepted"), r.get("rejected"))
            if rp >= rej_thresh:
                fire(ip, "reject", "warn", f"{ip} 拒绝率 {rp}% ≥ {rej_thresh}%")
            else:
                db.resolve_alert(conn, ip, "reject")

    if fired:
        _push_batch(cfg.get("telegram", {}), fired)
    return fired


def evaluate_containers(conn, containers, known_before, cfg):
    """集装箱冷却告警：故障位 → 告警；箱体掉线 → 告警；恢复自动消警。
    known_before: 本轮扫描前已记住的箱体 IP 集合（用于判定掉线）。"""
    acfg = cfg.get("alerts", {})
    if not acfg.get("enabled", True):
        return []
    cooldown = acfg.get("cooldown", 1800)
    now = int(time.time())
    cur = {c["ip"]: c for c in containers}
    fired = []

    def fire(ip, type_, sev, detail):
        if db.active_alert(conn, ip, type_):
            return
        rec = db.recent_alert(conn, ip, type_)   # 冷却防抖动刷屏
        if rec and rec.get("resolved") and rec.get("resolved_ts") and (now - rec["resolved_ts"]) < cooldown:
            return
        db.raise_alert(conn, ip, type_, sev, detail)
        fired.append((ip, type_, sev, detail))

    ignore = set(acfg.get("container_faults_ignore", []))
    sp_min = acfg.get("container_supply_pressure_min", 0) or 0
    rp_min = acfg.get("container_return_pressure_min", 0) or 0
    for ip, c in cur.items():
        # 故障位（忽略列表内的不报警，但仍在卡片显示）
        active = {f"cooler:{f['flag']}": f for f in (c.get("faults") or []) if f["flag"] not in ignore}
        # 压力阈值告警
        sp, rp = c.get("supply_pressure"), c.get("return_pressure")
        if sp_min and isinstance(sp, (int, float)) and sp < sp_min:
            active["cooler:supply_pressure_low"] = {"label": f"供液压力低 {sp}<{sp_min}MPa", "sev": "crit"}
        if rp_min and isinstance(rp, (int, float)) and rp < rp_min:
            active["cooler:return_pressure_low_th"] = {"label": f"回液压力低 {rp}<{rp_min}MPa", "sev": "crit"}
        # 触发新故障
        for type_, f in active.items():
            fire(ip, type_, f["sev"], f"集装箱 {ip} {f['label']}")
        # 消除已恢复的该箱冷却告警(按类型直查，不受 list_alerts 200 条截断影响)
        for a in db.active_alerts_by_type(conn, like="cooler"):
            if a["ip"] == ip and a["type"].startswith("cooler:") and a["type"] not in active:
                db.resolve_alert(conn, ip, a["type"])
        # 箱体恢复在线 → 清除离线告警
        db.resolve_alert(conn, ip, "cooler_offline")

    # 箱体掉线：已记住的箱、本次未采到
    for ip in known_before:
        if ip not in cur:
            fire(ip, "cooler_offline", "crit", f"集装箱 {ip} 控制器离线")

    if fired:
        _push_batch(cfg.get("telegram", {}), fired)
    return fired


def _push_batch(tg_cfg, fired):
    icons = {"crit": "🔴", "warn": "🟠", "info": "🟢"}
    lines = ["<b>⛏ 矿机告警</b>"]
    for ip, type_, sev, detail in fired[:30]:
        lines.append(f"{icons.get(sev,'•')} {detail}")
    if len(fired) > 30:
        lines.append(f"... 另有 {len(fired)-30} 条")
    _tg_send(tg_cfg, "\n".join(lines))


def push_text(cfg, text):
    _tg_send(cfg.get("telegram", {}), text)
