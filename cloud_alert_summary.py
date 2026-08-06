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
    "cooler_offline": "集装箱离线",
}

_MAX_GROUPS = 20      # 类型数天然有限(≈10个)，封顶防异常数据撑爆上报体
_MAX_WORKERS = 30     # 单类型下最多带出的客户数(按受影响台数降序)


def _category(alert_type):
    """alerts.type → (分组key, 中文标签)。cooler:<flag> 统一归为"集装箱故障"一组。"""
    if alert_type.startswith("cooler:"):
        return "container_fault", "集装箱故障"
    return alert_type, _CATEGORY_LABEL.get(alert_type, alert_type or "其他")


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
