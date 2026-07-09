#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 服务：JSON API + 前端面板。"""
import os
import asyncio
import threading

from fastapi import FastAPI, Query, Body, Request, Response, Depends, HTTPException, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import db
import miner_core
import control
import auth
import service as service_mod
from service import MonitorService, load_config

CFG = load_config(os.environ.get("MINER_CONFIG", "config.yaml"))
service_mod.apply_settings(CFG, service_mod.load_settings())  # 网页保存的参数覆盖
SVC = MonitorService(CFG)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

app = FastAPI(title="矿机监控")


def _sess(request: Request):
    return auth.current(CFG, request.cookies.get(auth.COOKIE, ""))


def require(min_role):
    def dep(request: Request):
        sess = _sess(request)
        if not sess:
            raise HTTPException(status_code=401, detail="未登录")
        if not auth.has_role(sess, min_role):
            raise HTTPException(status_code=403, detail="权限不足")
        return sess
    return dep


require_viewer = require("viewer")
require_ops = require("ops")
require_admin = require("admin")


@app.post("/api/login")
def api_login(request: Request, response: Response, body: dict = Body(...)):
    src = request.client.host if request.client else None
    if auth.locked(src):
        return JSONResponse({"ok": False, "error": "失败次数过多，请稍后再试"}, status_code=429)
    token = auth.login(CFG, body.get("username", ""), body.get("password", ""), src=src)
    if not token:
        return JSONResponse({"ok": False, "error": "用户名或密码错误"}, status_code=401)
    s = auth.session(token)
    # secure_cookie: 走 HTTPS 时设为 true(令牌只在加密连接传输)；纯内网 HTTP 保持 false 否则 cookie 不发
    response.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                        secure=bool(CFG.get("auth", {}).get("secure_cookie", False)), max_age=auth.TTL)
    return {"ok": True, "user": s["user"], "role": s["role"]}


@app.post("/api/logout")
def api_logout(request: Request, response: Response):
    auth.logout(request.cookies.get(auth.COOKIE, ""))
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/me")
def api_me(request: Request):
    s = _sess(request)
    if not s:
        return JSONResponse({"authenticated": False, "auth_enabled": auth.enabled(CFG)},
                            status_code=401)
    return {"authenticated": True, "user": s["user"], "role": s["role"],
            "auth_enabled": auth.enabled(CFG)}


WS_CLIENTS = set()
MAIN_LOOP = None
_SORT_FIELDS = {"ip", "status", "firmware", "model", "hr_rt", "hr_avg", "power",
                "eff", "temp", "uptime", "worker", "sn"}


@app.on_event("startup")
def _startup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_event_loop()
    SVC.notify = _broadcast_threadsafe   # 扫描线程→WS 推送
    SVC.start_scheduler()
    # 云端总览上报(config.cloud.enabled 才启动)：本地→云端每分钟推摘要，NAT 后无需端口映射
    import cloud_report
    if cloud_report.start(CFG, _build_public_summary,
                          lambda hours: db.customer_report(SVC.conn, hours)):
        print(f"[cloud] 云端上报已启动 → {CFG['cloud']['url']} "
              f"(场地: {CFG['cloud']['site_name']}, ID: {cloud_report.site_id(CFG)})")
    # git 自动更新(config.update.auto 才启用; 需 NSSM/run.bat 守护进程接管重启)
    import updater
    updater.start_auto(CFG.get("update") or {})
    if auth.enabled(CFG):   # 安全自检：仍用默认弱口令则醒目告警(能重启/换池全场，务必改强口令)
        weak = auth.weak_default_users(CFG)
        if weak:
            print("=" * 64)
            print(f"!!! 安全警告: 用户 {weak} 仍在使用默认弱口令(admin888 等) !!!")
            print("!!! 该系统可远程重启/换矿池全场，请立即用 `python auth.py 新强口令` 改掉 !!!")
            print("=" * 64)


def _broadcast_threadsafe(payload):
    """从后台扫描线程安全地向所有 WS 客户端推送。"""
    if MAIN_LOOP is None:
        return
    for ws in list(WS_CLIENTS):
        try:
            fut = asyncio.run_coroutine_threadsafe(ws.send_json(payload), MAIN_LOOP)
            fut.add_done_callback(lambda f, w=ws: WS_CLIENTS.discard(w) if f.exception() else None)
        except Exception:
            WS_CLIENTS.discard(ws)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    sess = auth.current(CFG, websocket.cookies.get(auth.COOKIE, ""))
    if not sess:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    WS_CLIENTS.add(websocket)
    try:
        await websocket.send_json({"type": "hello"})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=60)   # 仅保活，忽略入站
            except asyncio.TimeoutError:
                pass
            # 周期性复检会话：登出/令牌过期后主动断开，避免失效会话仍持续接收推送
            if not auth.current(CFG, websocket.cookies.get(auth.COOKIE, "")):
                await websocket.close(code=1008)
                break
    except Exception:
        pass
    finally:
        WS_CLIENTS.discard(websocket)


def _latest_records():
    ls = db.latest_scan(SVC.conn)
    if not ls:
        return None, []
    return ls, db.scan_records(SVC.conn, ls["scan_id"])


@app.get("/api/summary")
def api_summary(_: dict = Depends(require_viewer)):
    ls, recs = _latest_records()
    if not ls:
        return {"scanned": False, "progress": SVC.progress}
    with_hr, no_hr, offline = miner_core.rank(recs)
    by_fw = {}
    for r in recs:
        if r["status"] == "online":
            by_fw[r["firmware"]] = by_fw.get(r["firmware"], 0) + 1
    total_hr = round(sum(r["hr_rt"] for r in with_hr), 2) if with_hr else 0
    # 总功耗口径与 /api/miners agg 一致：所有在线机的功耗(不限是否有算力)
    total_power = sum(r["power"] for r in recs if r["status"] == "online" and r.get("power"))
    effs = [r["eff"] for r in with_hr if r.get("eff")]
    avg_eff = round(sum(effs) / len(effs), 2) if effs else 0
    _cs = db.get_containers(SVC.conn)
    return {
        "scanned": True,
        "scan_ts": ls["ts"], "scan_kind": ls["kind"],
        "total": ls["total"], "online": ls["online"], "offline": ls["offline"],
        "with_hashrate": len(with_hr), "no_hashrate": len(no_hr),
        "total_hashrate_th": total_hr,
        "avg_hashrate_th": round(total_hr / len(with_hr), 2) if with_hr else 0,
        "total_power_kw": round(total_power / 1000, 1),
        "avg_efficiency": avg_eff,
        "by_firmware": by_fw,
        "active_alerts": db.count_active(SVC.conn),
        "containers": len(_cs),
        "containers_faulty": sum(1 for c in _cs if c.get("online") and c.get("faults")),
        "containers_offline": sum(1 for c in _cs if not c.get("online")),
        "progress": SVC.progress,
    }


# --- Public read-only summary for external agents (Hermes/Cursor) ---
# Added 2026-06-29 by request. Does NOT require auth, but is gated by a
# shared bearer token (config: public_api_token). Token is intentionally
# shared-secret rather than per-user — there are no per-user audit
# requirements for an external polling agent.
# Reuses the same data path as /api/summary to guarantee consistency.
import hmac as _hmac
import time as _time
import threading as _threading

_public_cache = {"ts": 0.0, "data": None}
_public_cache_lock = _threading.Lock()
_PUBLIC_CACHE_TTL = 10.0  # seconds

# 通用短缓存 + 每来源限流：防外部 Agent 高频轮询通过全局DB锁拖慢扫描落库
_pub_cache = {}                # key -> (ts, data)
_pub_cache_lock = _threading.Lock()
_pub_rate = {}                 # src -> [tokens, last]
_pub_rate_lock = _threading.Lock()
_PUB_RATE_MAX = 120            # 每分钟每来源请求上限
_PUB_RATE_PER_SEC = _PUB_RATE_MAX / 60.0


def _cached(key, ttl, builder):
    """短TTL缓存：同 key 在 ttl 秒内复用上次结果，避免重查询/重算。"""
    now = _time.time()
    with _pub_cache_lock:
        ent = _pub_cache.get(key)
        if ent and (now - ent[0]) < ttl:
            return ent[1]
    data = builder()
    with _pub_cache_lock:
        _pub_cache[key] = (now, data)
        if len(_pub_cache) > 300:   # 顺手清过期项防无界增长
            for k in [k for k, v in _pub_cache.items() if now - v[0] > 300]:
                _pub_cache.pop(k, None)
    return data


def _rate_ok(src):
    """令牌桶限流，每来源 _PUB_RATE_MAX/分钟。"""
    now = _time.time()
    with _pub_rate_lock:
        toks, last = _pub_rate.get(src, (float(_PUB_RATE_MAX), now))
        toks = min(float(_PUB_RATE_MAX), toks + (now - last) * _PUB_RATE_PER_SEC)
        if toks < 1:
            _pub_rate[src] = [toks, now]
            return False
        _pub_rate[src] = [toks - 1, now]
        if len(_pub_rate) > 1000:   # 清理陈旧来源
            for k in [k for k, v in list(_pub_rate.items()) if now - v[1] > 300]:
                _pub_rate.pop(k, None)
        return True


def _check_public_token(request: Request):
    """Verify the shared bearer token. If no token is configured, refuse
    all requests (fail-closed). If a token is configured, require it via
    Authorization: Bearer <token> OR ?token=<token> query param."""
    cfg_token = CFG.get("public_api", {}).get("token", "")
    if not cfg_token:
        # No token configured → endpoint disabled. Return 404 to hide its
        # existence rather than 401, so unconfigured installations don't
        # leak.
        raise HTTPException(status_code=404, detail="not found")
    supplied = ""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        supplied = auth_header[7:].strip()
    if not supplied:
        supplied = request.query_params.get("token", "")
    if not supplied or not _hmac.compare_digest(supplied, cfg_token):
        raise HTTPException(status_code=401, detail="bad token")
    src = request.client.host if request.client else "?"   # 限流：防高频轮询打爆DB拖慢扫描
    if not _rate_ok(src):
        raise HTTPException(status_code=429, detail="rate limit", headers={"Retry-After": "5"})


def _build_public_summary():
    """Reuse the exact same data path as api_summary, plus a compact
    per-miner list for the agent caller. Wrapped in try/except so the
    endpoint never returns 500."""
    try:
        ls, recs = _latest_records()
        if not ls:
            return {
                "ok": True,
                "ts": int(_time.time()),
                "scanned": False,
                "progress": SVC.progress,
                "online": 0, "offline": 0, "total": 0,
                "online_count": 0, "offline_count": 0, "total_count": 0,
                "total_hashrate_ths": 0.0,
                "active_alerts": 0,
                "miners": [],
            }
        with_hr, no_hr, offline = miner_core.rank(recs)
        total_hr = round(sum(r["hr_rt"] for r in with_hr), 2) if with_hr else 0.0
        total_power = sum(
            r["power"] for r in recs
            if r["status"] == "online" and r.get("power")
        )
        _cs = db.get_containers(SVC.conn)
        active_alerts_n = db.count_active(SVC.conn)

        # Compact per-miner list. Keep it small (online only by default;
        # include offline flagged with status) for low-overhead polling.
        miners = []
        for r in recs:
            miners.append({
                "ip": r["ip"],
                "model": r.get("model") or "",
                "firmware": r.get("firmware") or "",
                "status": r.get("status") or "unknown",
                "hashrate_ths": round(r.get("hr_rt") or 0.0, 2),
                "temp_c": r.get("temp") or 0,
                "power_w": r.get("power") or 0,
                "worker": r.get("worker") or "",
            })
        return {
            "ok": True,
            "ts": int(_time.time()),
            "scanned": True,
            "scan_id": ls.get("scan_id"),
            "scan_ts": ls.get("ts"),
            "scan_kind": ls.get("kind"),
            "total": ls["total"], "online": ls["online"], "offline": ls["offline"],
            "online_count": ls["online"], "offline_count": ls["offline"], "total_count": ls["total"],
            "with_hashrate": len(with_hr), "no_hashrate": len(no_hr),
            "total_hashrate_ths": total_hr,
            "avg_hashrate_ths": round(total_hr / len(with_hr), 2) if with_hr else 0.0,
            "total_power_kw": round(total_power / 1000, 1),
            "active_alerts": active_alerts_n,
            "containers": len(_cs),
            "containers_faulty": sum(1 for c in _cs if c.get("online") and c.get("faults")),
            "containers_offline": sum(1 for c in _cs if not c.get("online")),
            "progress": SVC.progress,
            "miners": miners,
        }
    except Exception as e:
        return {"ok": False, "ts": int(_time.time()), "error": str(e)}


@app.get("/api/public/summary")
def api_public_summary(request: Request, lite: int = 0):
    """Public, token-gated, read-only summary for external agents.

    Returns the same data as /api/summary but is callable with a shared
    bearer token instead of a per-user login session. Cached in-process
    for 10s to avoid hammering the DB when an agent polls.

    lite=1: 省略 miners[] 全量明细（其余统计字段不变），给云端总览等
    每分钟轮询的调用方省带宽（全场几千台时 ~750KB → 几KB）。
    旧版本服务端会忽略该参数（返回全量），调用方向后兼容。

    Auth: configure `public_api.token` in config.yaml. If unset, returns
    404 (fail-closed). Caller must send either
        Authorization: Bearer <token>
    or pass ?token=<token> as a query parameter."""
    _check_public_token(request)
    now = _time.time()
    with _public_cache_lock:
        if _public_cache["data"] is not None and (now - _public_cache["ts"]) < _PUBLIC_CACHE_TTL:
            out = dict(_public_cache["data"])  # shallow copy
            out["cached"] = True
            if lite:
                out.pop("miners", None)
            return out
    data = _build_public_summary()
    with _public_cache_lock:
        _public_cache["ts"] = now
        _public_cache["data"] = data
    out = dict(data)
    out["cached"] = False
    if lite:
        out.pop("miners", None)
    return out


# ---- 外部 Agent 只读取数 API（统一 token 鉴权，稳定 snake_case schema，单位带后缀） ----
def _public_miner_row(r):
    """单台矿机的 Agent 友好视图（稳定字段名 + 显式单位）。"""
    return {
        "ip": r["ip"],
        "status": r.get("status") or "unknown",
        "firmware": r.get("firmware") or "",
        "model": r.get("model") or "",
        "sn": r.get("sn") or "",
        "worker": r.get("worker") or "",
        "hashrate_ths": round(r.get("hr_rt") or 0.0, 2),
        "hashrate_avg_ths": round(r.get("hr_avg") or 0.0, 2),
        "power_w": r.get("power") or 0,
        "temp_c": r.get("temp") or 0,
        "efficiency_jth": r.get("eff"),
        "uptime_s": r.get("uptime"),
        "accepted": r.get("accepted"),
        "rejected": r.get("rejected"),
    }


@app.get("/api/public/health")
def api_public_health(request: Request):
    """轻量心跳：监控进程是否活着、上次扫描多久前、是否在扫、活跃告警数。供 Agent 探活。"""
    _check_public_token(request)
    p = SVC.progress
    last = p.get("last_finished", 0)
    now = int(_time.time())
    try:
        n_alerts = db.count_active(SVC.conn)
    except Exception:
        n_alerts = None
    return {"ok": True, "ts": now, "scanning": bool(p.get("running")),
            "last_scan_ts": last or None,
            "last_scan_age_s": (now - last) if last else None,
            "active_alerts": n_alerts}


@app.get("/api/public/miners")
def api_public_miners(request: Request, status: str = "", fw: str = "", seg: str = "",
                      q: str = "", limit: int = 50000):
    """矿机列表（Agent 友好）。filters: status(online/offline)、fw(stock/uniplus)、seg(如 172.16.119)、q(IP/SN/worker 模糊)。"""
    _check_public_token(request)

    def build():
        _, recs = _latest_records()
        out = recs
        if seg:
            out = [r for r in out if r["ip"].rsplit(".", 1)[0] == seg]
        if status:
            out = [r for r in out if r["status"] == status]
        if fw:
            out = [r for r in out if r["firmware"] == fw]
        if q:
            ql = q.lower()
            out = [r for r in out if ql in r["ip"].lower() or ql in (r.get("sn") or "").lower()
                   or ql in (r.get("worker") or "").lower()]
        online = [r for r in out if r["status"] == "online"]
        return {"ok": True, "ts": int(_time.time()), "count": len(out),
                "online": len(online),
                "total_hashrate_ths": round(sum(r["hr_rt"] for r in online if r.get("hr_rt")), 2),
                "total_power_w": sum(r["power"] for r in online if r.get("power")),
                "miners": [_public_miner_row(r) for r in out[:max(1, limit)]]}
    return _cached(("miners", status, fw, seg, q, limit), 8, build)


@app.get("/api/public/miner/{ip}")
def api_public_miner(ip: str, request: Request):
    """单台矿机当前状态 + 近期算力/温度/功耗历史。"""
    _check_public_token(request)
    _, recs = _latest_records()
    cur = next((r for r in recs if r["ip"] == ip), None)
    if not cur:
        return {"ok": True, "ts": int(_time.time()), "found": False, "ip": ip}
    hist = [{"ts": h["ts"], "status": h.get("status"),
             "hashrate_ths": round(h.get("hr_rt") or 0.0, 2),
             "temp_c": h.get("temp") or 0, "power_w": h.get("power") or 0}
            for h in db.ip_history(SVC.conn, ip, 200)]
    return {"ok": True, "ts": int(_time.time()), "found": True,
            "miner": _public_miner_row(cur), "history": hist}


@app.get("/api/public/alerts")
def api_public_alerts(request: Request, active: bool = True):
    """告警列表（默认仅活跃）。type: offline/zero/reject/segment_down/stalled/cooler:*/cooler_offline。"""
    _check_public_token(request)

    def build():
        al = db.list_alerts(SVC.conn, active_only=active)
        return {"ok": True, "ts": int(_time.time()), "count": len(al),
                "alerts": [{"id": a["id"], "ts": a["ts"], "ip": a["ip"], "type": a["type"],
                            "severity": a["severity"], "detail": a["detail"],
                            "resolved": bool(a["resolved"]), "resolved_ts": a.get("resolved_ts"),
                            "ack_by": a.get("ack_by")} for a in al]}
    return _cached(("alerts", active), 8, build)


@app.get("/api/public/containers")
def api_public_containers(request: Request):
    """水冷集装箱（AntBox）列表：水温/压力/流量/泵风扇/故障/功耗/箱内矿机数。"""
    _check_public_token(request)

    def build():
        cs = db.get_containers(SVC.conn)
        return {"ok": True, "ts": int(_time.time()), "count": len(cs),
                "faulty": sum(1 for c in cs if c.get("online") and c.get("faults")),
                "offline": sum(1 for c in cs if not c.get("online")),
                "containers": cs}
    return _cached(("containers",), 8, build)


@app.get("/api/public/customers")
def api_public_customers(request: Request, hours: int = 24):
    """按客户(矿工名)报表：机器数 / 可用率% / 交付算力 TH·h / 耗电 kWh。hours 超过保留期自动截断。"""
    _check_public_token(request)
    cap = CFG["db"].get("retention_days", 3) * 24
    eff = min(max(1, hours), cap)

    def build():   # 最重的查询, 缓存 45 秒(报表变化慢)
        return {"ok": True, "ts": int(_time.time()), "hours": hours,
                "covered_hours": eff, "truncated": eff < hours,
                "customers": db.customer_report(SVC.conn, eff)}
    return _cached(("customers", eff), 45, build)


@app.get("/api/miners")
def api_miners(status: str = "", fw: str = "", q: str = "", seg: str = "",
               sort: str = "hr_rt", order: str = "desc", limit: int = 50000,
               _: dict = Depends(require_viewer)):
    # 默认上限须 ≥ 全场真机数：H7 后落库≈真机全集(~5千+)，若仍按 5000 截断，
    # 前端会把第5000名之后(恰是离线/零算力=维修/下架对象)的机器静默移出选择集 → 漏命令
    _, recs = _latest_records()
    repair = db.repair_ips(SVC.conn)
    for r in recs:
        r["mstate"] = "repair" if r["ip"] in repair else "active"
    out = recs
    if seg:
        out = [r for r in out if r["ip"].rsplit(".", 1)[0] == seg]
    if status == "repair":
        out = [r for r in out if r["mstate"] == "repair"]
    elif status:
        out = [r for r in out if r["status"] == status]
    if fw:
        out = [r for r in out if r["firmware"] == fw]
    if q:
        ql = q.lower()
        out = [r for r in out if ql in r["ip"].lower() or ql in (r.get("sn") or "").lower()
               or ql in (r.get("worker") or "").lower()]
    # 当前筛选范围的算力汇总（选网段即看该段算力）
    online = [r for r in out if r["status"] == "online"]
    agg = {"count": len(out),
           "online": len(online),
           "offline": sum(1 for r in out if r["status"] == "offline"),
           "total_hr": round(sum(r["hr_rt"] for r in online if r.get("hr_rt")), 2),
           "total_power": sum(r["power"] for r in online if r.get("power"))}
    if sort not in _SORT_FIELDS:   # 防任意字段排序产生空/错误结果
        sort = "hr_rt"
    rev = order == "desc"
    # 空值(离线/无算力)始终排在最后，不受升降序影响
    nn = [r for r in out if r.get(sort) not in (None, "")]
    nul = [r for r in out if r.get(sort) in (None, "")]
    nn.sort(key=lambda r: r.get(sort), reverse=rev)
    out = nn + nul
    return {"count": len(out), "agg": agg, "miners": out[:limit]}


@app.get("/api/workers")
def api_workers(_: dict = Depends(require_viewer)):
    """按矿工名(矿池User)聚合在线矿机：总台数 / 总算力 / 机型分布。"""
    _, recs = _latest_records()
    agg = {}
    for r in recs:
        if r["status"] != "online":
            continue
        w = r.get("worker") or "(未知)"
        model = r.get("model") or "Unknown"
        a = agg.setdefault(w, {"worker": w, "total": 0, "hashrate": 0.0, "models": {}})
        a["total"] += 1
        if r.get("hr_rt"):
            a["hashrate"] = round(a["hashrate"] + r["hr_rt"], 2)
        a["models"][model] = a["models"].get(model, 0) + 1
    out = sorted(agg.values(), key=lambda x: x["total"], reverse=True)
    return {"workers": out, "count": len(out)}


@app.get("/api/reports/customers")
def api_report_customers(hours: int = 24, format: str = "", _: dict = Depends(require_viewer)):
    # 报表周期不能超过快照保留期(超出部分已被 prune 物理删除)，否则交付算力/耗电按比例少计且无提示
    cap = CFG["db"].get("retention_days", 3) * 24
    eff = min(max(1, hours), cap)
    rows = db.customer_report(SVC.conn, eff)
    if format == "csv":   # 对客户出账：导出 UTF-8 BOM 的 CSV(Excel 直接打开不乱码)，含数据覆盖期
        import io
        import csv as _csv
        buf = io.StringIO()
        buf.write("﻿")
        w = _csv.writer(buf)
        days = round(eff / 24, 1)
        w.writerow([f"客户报表  数据覆盖近 {days} 天" + ("（已按保留期截断）" if eff < hours else "")])
        w.writerow(["客户(矿工名)", "机器数", "可用率%", "交付算力(TH·h)", "耗电(kWh)"])
        for r in rows:
            w.writerow([r["worker"], r["machines"], r["uptime_pct"], r["delivered_th_h"], r["power_kwh"]])
        return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="customers_{eff}h.csv"'})
    return {"hours": hours, "covered_hours": eff, "truncated": eff < hours, "customers": rows}


@app.get("/api/containers")
def api_containers(_: dict = Depends(require_viewer)):
    cs = db.get_containers(SVC.conn)
    faulty = sum(1 for c in cs if c.get("online") and c.get("faults"))
    offline = sum(1 for c in cs if not c.get("online"))
    acfg = CFG.get("alerts", {})
    return {"count": len(cs), "faulty": faulty, "offline": offline, "containers": cs,
            "faults_ignore": acfg.get("container_faults_ignore", []),
            "supply_pressure_min": acfg.get("container_supply_pressure_min", 0),
            "return_pressure_min": acfg.get("container_return_pressure_min", 0)}


@app.post("/api/containers/scan")
def api_containers_scan(_: dict = Depends(require_ops)):
    """手动立即刷新集装箱（后台执行，秒回）。ops+：会真实发包/抢扫描锁，viewer 不可触发。"""
    threading.Thread(target=SVC.scan_containers, daemon=True).start()
    return {"ok": True}


@app.get("/api/container/{ip}")
def api_container(ip: str, _: dict = Depends(require_viewer)):
    cur = next((c for c in db.get_containers(SVC.conn) if c["ip"] == ip), None)
    return {"current": cur, "history": db.container_history(SVC.conn, ip)}


@app.get("/api/racks")
def api_racks(_: dict = Depends(require_viewer)):
    """按 /24 网段聚合成货架，每段固定显示完整机位(host_start..host_end)，缺位=空机位(灰)，
    便于一眼看出空位/缺号。着色：在线绿 / 零算力黄 / 离线红 / 空位灰。"""
    ls, recs = _latest_records()
    if not ls:
        return {"racks": []}
    _, hs, he = service_mod.load_segments(CFG)   # 机位范围与扫描范围一致

    def state(r):
        if r is None:
            return "empty"
        if r["status"] == "offline":
            return "offline"
        hr = r.get("hr_rt")
        return "zero" if (hr is None or hr == 0) else "ok"

    by_seg = {}   # seg -> {host: record}
    for r in recs:
        parts = r["ip"].split(".")
        if len(parts) != 4:
            continue
        try:
            host = int(parts[3])
        except ValueError:
            continue
        by_seg.setdefault(".".join(parts[:3]), {})[host] = r

    out = []
    for seg in sorted(by_seg, key=lambda n: tuple(int(x) for x in n.split("."))):
        hosts = by_seg[seg]
        slots, cnt, hr_sum = [], {}, 0.0
        for h in range(hs, he + 1):
            r = hosts.get(h)
            st = state(r)
            cnt[st] = cnt.get(st, 0) + 1
            if r and r.get("hr_rt"):
                hr_sum += r["hr_rt"]
            slots.append({"h": h, "ip": f"{seg}.{h}", "st": st,
                          "hr": (r.get("hr_rt") if r else None),
                          "temp": (r.get("temp") if r else None)})
        out.append({"name": seg, "slots": slots,
                    "online": cnt.get("ok", 0) + cnt.get("zero", 0),
                    "abnormal": cnt.get("zero", 0),
                    "offline": cnt.get("offline", 0),
                    "empty": cnt.get("empty", 0),
                    "hashrate": round(hr_sum, 2)})
    return {"racks": out}


@app.get("/api/miner/{ip}")
def api_miner(ip: str, _: dict = Depends(require_viewer)):
    _, recs = _latest_records()
    cur = next((r for r in recs if r["ip"] == ip), None)
    return {"current": cur, "history": db.ip_history(SVC.conn, ip)}


@app.post("/api/alerts/ack")
def api_alert_ack(body: dict = Body(...), sess: dict = Depends(require_ops)):
    """确认矿机告警（集装箱告警不允许确认，修复后自动消失）。"""
    try:
        aid = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "id 非法"}, status_code=400)
    a = db.get_alert(SVC.conn, aid)
    if not a:
        return JSONResponse({"ok": False, "error": "告警不存在"}, status_code=404)
    if str(a.get("type", "")).startswith("cooler"):   # 契约对齐：集装箱告警不可确认
        return JSONResponse({"ok": False, "error": "集装箱告警不可确认"}, status_code=400)
    db.ack_alert(SVC.conn, aid, sess.get("user", "?"))
    return {"ok": True}


@app.get("/api/alerts")
def api_alerts(active: bool = True, limit: int = 500, _: dict = Depends(require_viewer)):
    # counts/total 走精确计数(不受列表 limit 截断)：大面积事件下台数才不会被卡在 200
    counts = db.count_active_by_type(SVC.conn)
    return {"alerts": db.list_alerts(SVC.conn, active_only=active, limit=limit),
            "counts": counts, "total": sum(counts.values())}


@app.get("/api/trend")
def api_trend(points: int = 288, _: dict = Depends(require_viewer)):
    return {"trend": db.hashrate_trend(SVC.conn, points)}


@app.post("/api/scan")
def api_scan(kind: str = Query("manual"), _: dict = Depends(require_ops)):
    ok = SVC.scan_now_async("quick" if kind == "quick" else kind)
    return JSONResponse({"started": ok, "progress": SVC.progress})


@app.get("/api/progress")
def api_progress(_: dict = Depends(require_viewer)):
    return SVC.progress


@app.get("/api/segments")
def api_segments(_: dict = Depends(require_viewer)):
    segs, hs, he = service_mod.load_segments(CFG)
    return {"segments": segs, "host_start": hs, "host_end": he}


@app.get("/api/settings")
def api_settings_get(_: dict = Depends(require_viewer)):
    return {"scan_interval": CFG["schedule"].get("scan_interval", 300),
            "max_pps": CFG["scan"].get("max_pps", 100),
            "container_interval": CFG["schedule"].get("container_interval", 10),
            "discovery_workers": CFG["scan"].get("discovery_workers", 300)}


@app.post("/api/settings")
def api_settings_post(body: dict = Body(...), _: dict = Depends(require_admin)):
    s = {}
    try:   # 非数字字段返回 400 而非 500
        if "scan_interval" in body:
            s["scan_interval"] = max(30, min(86400, int(body["scan_interval"])))
        if "max_pps" in body:
            s["max_pps"] = max(0, min(2000, int(body["max_pps"])))
        if "container_interval" in body:
            s["container_interval"] = max(5, min(3600, int(body["container_interval"])))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "参数须为数字"}, status_code=400)
    service_mod.save_settings({**service_mod.load_settings(), **s})
    service_mod.apply_settings(CFG, s)   # 即时生效，无需重启
    SVC.wake()                           # 唤醒调度循环立即按新间隔重排(否则要等当前 sleep 走完)
    return {"ok": True, **s}


@app.post("/api/segments")
def api_segments_save(body: dict = Body(...), _: dict = Depends(require_admin)):
    norm = []
    for s in (body.get("segments") or []):
        parts = str(s).strip().split(".")
        if len(parts) < 3:
            continue
        base = ".".join(parts[:3])
        try:
            if all(0 <= int(x) <= 255 for x in base.split(".")) and base not in norm:
                norm.append(base)
        except ValueError:
            pass
    try:
        hs = max(1, min(254, int(body.get("host_start", 1))))
        he = max(hs, min(254, int(body.get("host_end", 254))))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "host_start/host_end 须为数字"}, status_code=400)
    service_mod.save_segments(norm, hs, he)
    return {"ok": True, "segments": norm, "host_start": hs, "host_end": he, "count": len(norm)}


def _audit_reject(user, action, reason):
    try:
        db.log_commands(SVC.conn, user, f"reject:{action}", [{"ip": "-", "ok": False, "msg": reason}])
    except Exception:
        pass


# 批量重启后台进度(分批+延迟会耗时，异步执行，前端轮询)
CMD_PROGRESS = {"running": False, "action": "", "done": 0, "total": 0,
                "success": 0, "failed": 0, "fail_ips": [], "user": ""}
_cmd_lock = threading.Lock()


def _run_command_bg(targets, action, params, user):
    try:
        def prog(done, total):
            CMD_PROGRESS["done"] = done
            CMD_PROGRESS["total"] = total
        results, _err = control.run_batch(targets, action, params, CFG, progress=prog)
        db.log_commands(SVC.conn, user, action, results)
        ok_n = sum(1 for r in results if r["ok"])
        CMD_PROGRESS["success"] = ok_n
        CMD_PROGRESS["failed"] = len(results) - ok_n
        CMD_PROGRESS["fail_ips"] = [r["ip"] for r in results if not r["ok"]][:100]
    except Exception as e:  # noqa: BLE001
        print(f"async command error: {e}")
    finally:
        CMD_PROGRESS["running"] = False
        _cmd_lock.release()


@app.get("/api/command/progress")
def api_command_progress(_: dict = Depends(require_ops)):
    return dict(CMD_PROGRESS)


@app.post("/api/command")
def api_command(body: dict = Body(...), sess: dict = Depends(require_ops)):
    """远程命令：{ips:[...], action:"reboot|locate|set_pools", params:{...}}"""
    user = sess.get("user", "?")
    action = body.get("action")
    if not CFG.get("control", {}).get("enabled", False):
        _audit_reject(user, action, "控制功能未启用")
        return JSONResponse({"ok": False, "error": "控制功能未启用"}, status_code=403)
    if action not in control.ACTIONS:
        _audit_reject(user, action, "不支持的命令")
        return JSONResponse({"ok": False, "error": "不支持的命令"}, status_code=400)
    ips = body.get("ips") or []
    if not ips:
        _audit_reject(user, action, "未指定目标矿机")
        return JSONResponse({"ok": False, "error": "未指定目标矿机"}, status_code=400)
    max_batch = CFG.get("control", {}).get("max_batch", 1000)
    if len(ips) > max_batch:
        _audit_reject(user, action, f"目标 {len(ips)} 台超上限 {max_batch}")
        return JSONResponse({"ok": False, "error": f"单次目标超上限 {max_batch} 台，请分批"}, status_code=400)
    params = body.get("params") or {}
    if action == "set_pools":   # 换矿池: 校验 pools 结构与 URL，避免下发非法/恶意配置
        pools = params.get("pools")
        if not isinstance(pools, list) or not pools or len(pools) > 8:
            _audit_reject(user, action, "pools 结构非法")
            return JSONResponse({"ok": False, "error": "pools 需为 1-8 项的列表"}, status_code=400)
        for p in pools:
            if not isinstance(p, dict) or not str(p.get("url", "")).startswith(("stratum+tcp://", "stratum+ssl://")):
                _audit_reject(user, action, "矿池 url 非法")
                return JSONResponse({"ok": False, "error": "每个矿池需含 stratum+tcp:// 开头的 url"}, status_code=400)
            if not str(p.get("user", "")).strip():   # 缺矿工名会被原样下发→份额无归属/矿池拒绝
                _audit_reject(user, action, "矿池缺 user")
                return JSONResponse({"ok": False, "error": "每个矿池需填矿工名(user)"}, status_code=400)
    # 从最近快照取每台固件类型，避免重复探测
    _, recs = _latest_records()
    fw_map = {r["ip"]: r["firmware"] for r in recs}
    targets = [(ip, fw_map.get(ip, "")) for ip in ips]
    # 重启且开启分批(打乱+延迟会耗时) → 后台异步执行，前端轮询 /api/command/progress
    ctl = CFG.get("control", {})
    rb = int(ctl.get("reboot_concurrency", 0) or 0)
    if action == "reboot" and rb > 0 and len(targets) > rb:
        if not _cmd_lock.acquire(blocking=False):
            _audit_reject(user, action, "已有批量命令在执行")
            return JSONResponse({"ok": False, "error": "已有批量重启在执行，请稍候"}, status_code=409)
        CMD_PROGRESS.update({"running": True, "action": action, "done": 0, "total": len(targets),
                             "success": 0, "failed": 0, "fail_ips": [], "user": user})
        threading.Thread(target=_run_command_bg, args=(targets, action, params, user), daemon=True).start()
        return {"ok": True, "async": True, "action": action, "count": len(targets),
                "batch": rb, "delay": ctl.get("reboot_delay_sec", 0)}
    results, err = control.run_batch(targets, action, params, CFG)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    db.log_commands(SVC.conn, sess.get("user", "?"), action, results)
    ok_n = sum(1 for r in results if r["ok"])
    return {"ok": True, "action": action, "success": ok_n,
            "failed": len(results) - ok_n, "results": results}


@app.get("/api/update/check")
def api_update_check(_: dict = Depends(require_admin)):
    """对比 git 远端有没有新版本(git 部署时可用)。"""
    import updater
    return updater.check((CFG.get("update") or {}).get("branch") or None)


@app.post("/api/update/apply")
def api_update_apply(_: dict = Depends(require_admin)):
    """拉取新代码(ff-only+编译自检+失败回滚)并重启(需 NSSM/run.bat 守护)。"""
    import updater
    return updater.apply((CFG.get("update") or {}).get("branch") or None)


@app.get("/api/commands")
def api_commands(limit: int = 100, _: dict = Depends(require_admin)):
    return {"commands": db.list_commands(SVC.conn, limit)}


@app.post("/api/machine-state")
def api_machine_state(body: dict = Body(...), sess: dict = Depends(require_ops)):
    """标记维修/取消维修/下架移除：{ips:[...], action:"repair"|"active"|"remove"}"""
    ips = body.get("ips") or []
    action = body.get("action")
    if not ips or action not in ("repair", "active", "remove"):
        return JSONResponse({"ok": False, "error": "参数错误"}, status_code=400)
    if action == "remove":
        db.remove_miners(SVC.conn, ips)
    else:
        db.set_machine_state(SVC.conn, ips, action)
    db.log_commands(SVC.conn, sess.get("user", "?"), "state:" + action,
                    [{"ip": ip, "ok": True, "msg": action} for ip in ips])
    return {"ok": True, "action": action, "count": len(ips)}


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


if os.path.isdir(WEB_DIR):
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CFG["server"]["host"], port=CFG["server"]["port"])
