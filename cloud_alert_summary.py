#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端告警摘要聚合：把本地活跃告警(alerts表)按类型分组、标出受影响客户，只给云端上报用。

纯只读聚合，不产生告警、不写库、不改变任何本地接口的返回结构——只被
server.py 里专供云端上报的摘要函数调用，不影响 /api/alerts、/api/summary 等
本地面板已有接口。不下发单机 IP，只给"类型/台数/受影响客户/起始时间"，
云端总览按此渲染"活跃告警"卡片，供老板级用户一眼看出影响范围，不用打电话问场地。
"""
import db

_CATEGORY_LABEL = {
    "offline": "掉线",
    "zero": "零算力",
    "low_hashrate": "算力偏低",
    "overheat": "高温",
    "reject": "矿池拒绝率高",
    "segment_down": "网段整体掉线",
    "site_down": "全场大面积掉线",
    "cooler_offline": "集装箱离线",
    "pool_hijack": "矿池被篡改",
    "stalled": "监控停滞",
}

_MAX_GROUPS = 20      # 类型数天然有限(≈10个)，封顶防异常数据撑爆上报体
_MAX_WORKERS = 30     # 单类型下最多带出的客户数(按受影响台数降序)


def _category(alert_type):
    """alerts.type → (分组key, 中文标签)。cooler:<flag> 统一归为"集装箱故障"一组。"""
    if alert_type.startswith("cooler:"):
        return "container_fault", "集装箱故障"
    return alert_type, _CATEGORY_LABEL.get(alert_type, alert_type or "其他")


def _add_segment_group(conn, seg_alerts, ip2worker, groups):
    segs = {(a.get("ip") or "")[:-2] for a in seg_alerts if (a.get("ip") or "").endswith(".x")}
    since = min((a["ts"] for a in seg_alerts if a.get("ts")), default=0)
    workers = {}
    try:
        ls = db.latest_scan(conn)
        recs = db.scan_records(conn, ls["scan_id"]) if ls else []
    except Exception:  # noqa: BLE001
        recs = []
    n = 0
    for r in recs:
        if r.get("status") == "offline" and ".".join(r["ip"].split(".")[:3]) in segs:
            n += 1
            w = (ip2worker.get(r["ip"]) or {}).get("worker") or "(未知)"
            workers[w] = workers.get(w, 0) + 1
    if not n:                 # 快照里取不到(刚重启等)：退回按网段计，至少别显示 0
        n, workers = len(segs) or len(seg_alerts), {}
    groups["segment_down"] = {"category": "segment_down",
                              "label": f"网段整体掉线({len(segs) or len(seg_alerts)}个网段)",
                              "count": n, "since_ts": since, "_workers": workers}


def build(conn):
    """返回 [{"category","label","count","since_ts","workers":[{"worker","count"}]}]，
    按受影响台数降序。since_ts=该类告警最早触发时间(可算出"已持续多久")。
    用 active_alerts_by_type(无 limit) 取全量——大面积故障(如整场断电)时活跃告警可能
    远超普通分页上限，用带 LIMIT 的查询会把最早触发的那批(id最小)截掉，
    正好是"已持续多久"最需要保留的部分，反而在最需要准的时候算错。
    查询失败/无告警都返回 []，绝不抛异常拖累上报主体。"""
    try:
        alerts = db.active_alerts_by_type(conn)
    except Exception:  # noqa: BLE001  聚合是上报的附加信息，查询异常不能影响摘要本体
        return []
    if not alerts:
        return []
    try:
        ip2worker = db.known_identity(conn)
    except Exception:  # noqa: BLE001
        ip2worker = {}

    groups = {}
    seg_alerts = [a for a in alerts if a.get("type") == "segment_down"]
    if seg_alerts:
        # 网段事件按"段内当前离线的机器"计台数、归客户：段内单机掉线告警被网段事件静音，
        # 若按告警条数算，整段 250 台断电在云端只显示"网段整体掉线 1 台、掉线 0 台"
        alerts = [a for a in alerts if a.get("type") != "segment_down"]
        _add_segment_group(conn, seg_alerts, ip2worker, groups)
    site = [a for a in alerts if a.get("type") == "site_down"]
    if site:
        # 全场事件按"当前离线台数"计，而不是 1 条告警
        alerts = [a for a in alerts if a.get("type") != "site_down"]
        try:
            ls = db.latest_scan(conn)
            n = int(ls.get("offline") or 0) if ls else 0
        except Exception:  # noqa: BLE001
            n = 0
        groups["site_down"] = {"category": "site_down", "label": _CATEGORY_LABEL["site_down"],
                               "count": n or 1, "since_ts": site[0].get("ts") or 0,
                               "_workers": {}}
    for a in alerts:
        cat, label = _category(a.get("type") or "")
        g = groups.setdefault(cat, {"category": cat, "label": label, "count": 0,
                                    "since_ts": a["ts"], "_workers": {}})
        g["count"] += 1
        if a["ts"] and a["ts"] < g["since_ts"]:
            g["since_ts"] = a["ts"]
        worker = (ip2worker.get(a.get("ip") or "") or {}).get("worker") or "(未知)"
        g["_workers"][worker] = g["_workers"].get(worker, 0) + 1

    out = []
    for g in groups.values():
        workers = [{"worker": w, "count": c} for w, c in
                   sorted(g["_workers"].items(), key=lambda kv: -kv[1])][:_MAX_WORKERS]
        out.append({"category": g["category"], "label": g["label"], "count": g["count"],
                    "since_ts": g["since_ts"], "workers": workers})
    out.sort(key=lambda g: -g["count"])
    return out[:_MAX_GROUPS]
