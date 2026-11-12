#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 服务：JSON API + 前端面板。

数据路径约定：面板/Agent 读的"当前状态"一律来自 MonitorService 的**内存快照**
(SVC.latest())，不再每个请求回查 SQLite。一个浏览器每 30 秒刷新会打 3~4 个接口，
每个都拉 5000 行是纯粹的浪费，而且会和扫描落库抢数据库。历史/报表类查询才走 DB。
"""
if __name__ == "__main__":
    # 刚网页/自动更新过：先于其它业务模块 import 记一次启动尝试。新版本若在模块级就崩，
    # 连续几次后在这里自动回滚到上一版(见 updater.boot_guard)
    import updater as _updater
    _updater.select_runtime()
    _updater.boot_guard()
    # 官方 SSH origin → 匿名 HTTPS：启动时先改好，旧更新器下次也能 `git fetch origin`
    try:
        _updater.ensure_public_update_remote()
    except Exception:  # noqa: BLE001
        pass

import asyncio
import contextlib
import html
import hmac as _hmac
import ipaddress
import os
import re
import sys
import threading
import time
import uuid

from fastapi import (Body, Depends, FastAPI, HTTPException, Query, Request,
                     Response, WebSocket)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from web_assets import index_html
from fastapi.middleware.gzip import GZipMiddleware

import alerts
import appconfig
import auth
import cloud_alert_summary
import cloud_connect
import cloud_report
import control
import data_metrics
import db
import logs
import miner_core
import updater
from service import MonitorService

CFG = appconfig.load_config(os.environ.get("MINER_CONFIG", "config.yaml"))
logs.setup(CFG)
log = logs.get(__name__)

_saved = appconfig.load_settings()
_over = appconfig.settings_overrides(CFG, _saved)
if _over:   # 网页存过的项会一直盖住 config.yaml，改配置文件不生效时一眼能看出原因
    log.warning("以下设置以网页保存的 %s 为准，覆盖了 config.yaml(要改请在网页改，或删掉该文件里的对应项): %s",
                os.path.basename(appconfig.SETTINGS_FILE), "; ".join(_over))
appconfig.apply_settings(CFG, _saved)   # 网页保存的参数覆盖
SVC = MonitorService(CFG)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

WS_CLIENTS = set()
WS_MAX_CLIENTS = 500   # 并发 WS 上限：无界集合会被脚本用海量连接耗尽内存/文件句柄
MAIN_LOOP = None


def _start_cloud_report():
    """(重新)启动云端上报线程，供启动、设置热更新、看门狗自愈共用同一套参数。"""
    return cloud_report.start(CFG, _build_cloud_summary,
                              _customer_report_packet)


@contextlib.asynccontextmanager
async def lifespan(app_):
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    recovered_jobs = db.recover_command_jobs(SVC.conn)
    if recovered_jobs:
        log.warning("检测到 %d 个上次运行中断的批量命令任务；已下发目标标记为 UNCERTAIN，未下发目标取消",
                    recovered_jobs)
    SVC.notify = _broadcast_threadsafe   # 扫描线程→WS 推送
    SVC.start_scheduler()
    guardian = MAIN_LOOP.create_task(_guardian_loop())   # 不依赖 daemon 线程的最终兜底
    if _start_cloud_report():
        log.info("云端上报已启动 → %s (场地: %s, ID: %s)", CFG["cloud"]["url"],
                 CFG["cloud"]["site_name"], cloud_report.site_id(CFG))
    try:
        updater.ensure_public_update_remote()
    except Exception as exc:  # noqa: BLE001
        log.warning("启动时自愈更新源失败: %s", exc)
    updater.start_auto(CFG.get("update") or {})
    # 新版本稳定运行 60 秒 → 确认更新成功(清掉回滚标记)
    try:
        _running_sha = updater._git("rev-parse", "HEAD")
    except Exception:
        _running_sha = None
    _h = threading.Timer(updater.HEALTHY_AFTER_SEC, updater.mark_healthy,
                         kwargs={"expected_sha": _running_sha})
    _h.daemon = True
    _h.start()
    _startup_selfcheck()
    try:
        yield
    finally:
        guardian.cancel()
        SVC.stop()
        db.close_readers()


app = FastAPI(title="矿机监控", lifespan=lifespan)
# 矿机列表全量可达 1~2MB JSON，内网千兆也值得压(通常压到 1/10)
app.add_middleware(GZipMiddleware, minimum_size=1024)


def _startup_selfcheck():
    main_sz, wal_sz = db.db_size(SVC.conn)
    log.info("数据库 %.0f MB (WAL %.0f MB)，报表可覆盖约 %d 小时",
             main_sz / 1e6, wal_sz / 1e6, db.coverage_hours(SVC.conn))
    if wal_sz > 256 * 1024 * 1024:
        log.warning("WAL 文件偏大(%.0f MB)，启动后首次 checkpoint 可能耗时较久", wal_sz / 1e6)
    if auth.enabled(CFG):   # 安全自检：仍用默认弱口令则醒目告警
        weak = auth.weak_default_users(CFG)
        if weak:
            log.error("=" * 64)
            log.error("账号配置提醒: 用户 %s 存在默认弱口令、明文口令或尚未设置密码", weak)
            log.error("弱口令登录只提示修改，仍保留该账号的角色权限。请在网页修改密码；"
                      "空口令/示例占位符不能登录，可运行 `python auth.py passwd 用户名` 设置后重启服务")
            log.error("=" * 64)
    else:
        log.warning("auth.enabled=false：任何人可匿名只读访问面板")


def _ensure_cloud_report():
    """云端上报线程存活检查+自愈：这条线程不归 MonitorService 管，单独在这补一份看门狗覆盖。"""
    if cloud_report.enabled(CFG) and not cloud_report.is_alive():
        log.warning("云端上报线程已意外退出，尝试重新拉起")
        _start_cloud_report()


async def _guardian_loop():
    """主事件循环守护：daemon 扫描/看门狗全死时仍能拉起（Web 活着就有人盯）。"""
    while True:
        try:
            await asyncio.sleep(60)
            await asyncio.get_running_loop().run_in_executor(None, SVC.health_tick)
            await asyncio.get_running_loop().run_in_executor(None, _ensure_cloud_report)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("guardian loop error: %s", e)


# ---- 客户端 IP（限流/审计的依据）----
def _trusted_nets():
    out = []
    for s in (CFG.get("server", {}).get("trusted_proxies") or []):
        try:
            out.append(ipaddress.ip_network(str(s), strict=False))
        except ValueError:
            log.warning("server.trusted_proxies 中的 %r 不是合法 IP/CIDR，已忽略", s)
    return out


_TRUSTED = _trusted_nets()


def _client_ip(request: Request):
    """真实客户端 IP。

    只有当直连对端本身在 server.trusted_proxies 里时才采信 X-Forwarded-For——
    否则任何人加一个伪造头就能绕过登录失败锁定和公共 API 限流。
    默认不信任任何代理，直连部署下这是正确且安全的行为。"""
    peer = request.client.host if request.client else ""
    if _TRUSTED and peer:
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if any(ip in n for n in _TRUSTED):
            xff = request.headers.get("x-forwarded-for", "")
            if xff:
                # 取最右侧一个非可信代理的地址(左侧可被客户端伪造)
                for part in reversed([p.strip() for p in xff.split(",") if p.strip()]):
                    try:
                        cand = ipaddress.ip_address(part)
                    except ValueError:
                        continue
                    if not any(cand in n for n in _TRUSTED):
                        return part
    return peer


def _sess(request: Request):
    return auth.current(CFG, request.cookies.get(auth.COOKIE, ""))


def require(min_role):
    def dep(request: Request):
        sess = _sess(request)
        if not sess:
            raise HTTPException(status_code=401, detail="未登录")
        if not auth.has_role(sess, min_role):
            raise HTTPException(status_code=403, detail="权限不足")
        # 弱口令只提示改密，不锁写权限：Ubuntu 服务器/局域网部署时几乎都是远程登录，
        # 以前把 must_change 当成只读会让首次登录直接没法运维。
        return sess
    return dep


require_viewer = require("viewer")
require_ops = require("ops")
require_admin = require("admin")


@app.post("/api/login")
def api_login(request: Request, response: Response, body: dict = Body(...)):
    src = _client_ip(request)
    if auth.locked(src):
        return JSONResponse({"ok": False, "error": "失败次数过多，请稍后再试"}, status_code=429)
    token, why = auth.login_ex(CFG, body.get("username", ""), body.get("password", ""), src=src,
                               local=_is_console(request, src))
    if not token:
        log.warning("登录失败: user=%r from=%s (%s)", str(body.get("username", ""))[:32], src, why)
        why = (why or "用户名或密码错误").replace("端口", str(CFG["server"]["port"]))
        return JSONResponse({"ok": False, "error": why}, status_code=401)
    s = auth.session(token)
    # secure_cookie: 走 HTTPS 时设为 true；纯内网 HTTP 保持 false 否则 cookie 不发
    response.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                        secure=bool(CFG.get("auth", {}).get("secure_cookie", False)
                                    or _tls_files()),
                        max_age=auth.TTL)
    log.info("登录成功: %s (%s) from %s%s", s["user"], s["role"], src,
             " [弱口令，须改密码]" if s.get("must_change") else "")
    return {"ok": True, "user": s["user"], "role": s["role"],
            "must_change": bool(s.get("must_change")), "weak_remote": bool(s.get("weak_remote")),
            "port": CFG["server"]["port"], "max_batch": CFG.get("control", {}).get("max_batch", 1000)}


@app.post("/api/password")
def api_password(request: Request, body: dict = Body(...), sess: dict = Depends(require_viewer)):
    """改自己的密码 {old, new}：写哈希进 config.yaml(原地、保留注释)，立即生效，踢掉该账号其它会话。"""
    if not auth.enabled(CFG):
        return JSONResponse({"ok": False, "error": "未启用登录"}, status_code=400)
    old, new = body.get("old"), body.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        return JSONResponse({"ok": False, "error": "参数错误"}, status_code=400)
    err = auth.change_password(CFG, sess["user"], old, new,
                               keep_token=request.cookies.get(auth.COOKIE, ""))
    src = _client_ip(request)
    db.log_commands(SVC.conn, f'{sess["user"]}@{src}', "passwd",
                    [{"ip": "-", "ok": not err, "msg": err or "已修改密码"}])
    if err:
        log.warning("修改密码失败: %s from %s (%s)", sess["user"], src, err)
        err = err.replace("端口", str(CFG["server"]["port"]))
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    log.info("已修改密码: %s from %s", sess["user"], src)
    return {"ok": True}


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
            "auth_enabled": auth.enabled(CFG), "must_change": bool(s.get("must_change")),
            "weak_remote": bool(s.get("weak_remote")), "port": CFG["server"]["port"],
            "max_batch": CFG.get("control", {}).get("max_batch", 1000)}


def _broadcast_threadsafe(payload):
    """从后台扫描线程安全地向所有 WS 客户端推送。"""
    if MAIN_LOOP is None:
        return
    for ws in list(WS_CLIENTS):
        try:
            fut = asyncio.run_coroutine_threadsafe(ws.send_json(payload), MAIN_LOOP)
            fut.add_done_callback(
                lambda f, w=ws: WS_CLIENTS.discard(w) if f.exception() else None)
        except Exception:  # noqa: BLE001
            WS_CLIENTS.discard(ws)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    host = websocket.headers.get("host", "")
    origin = websocket.headers.get("origin")
    proxied = _via_trusted_proxy(websocket)   # 与 HTTP 的 _request_guard 同口径：受信代理免检
    bad_origin = not proxied and (not _host_ok(host) or (origin and not _same_origin(origin, host)))
    sess = auth.current(CFG, websocket.cookies.get(auth.COOKIE, ""))
    # 先 accept 再 close：accept 之前 close 浏览器只看到 1006，前端分不清原因。
    # 4403 = 访问地址/来源不被允许(前端不弹登录、不重连)；1008 = 会话失效(前端弹登录)
    if bad_origin or not sess or len(WS_CLIENTS) >= WS_MAX_CLIENTS:
        await websocket.accept()
        if bad_origin:              # 防跨站 WebSocket 劫持
            await websocket.close(code=4403)
        elif not sess:
            await websocket.close(code=1008)
        else:                       # 1013 = try again later
            log.warning("WS 连接数已达上限 %d，拒绝新连接", WS_MAX_CLIENTS)
            await websocket.close(code=1013)
        return
    await websocket.accept()
    WS_CLIENTS.add(websocket)
    try:
        await websocket.send_json({"type": "hello"})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=60)   # 仅保活
            except asyncio.TimeoutError:
                pass
            # 周期性复检会话：登出/令牌过期后主动断开，避免失效会话仍持续接收推送
            if not auth.current(CFG, websocket.cookies.get(auth.COOKIE, "")):
                await websocket.close(code=1008)
                break
    except Exception:  # noqa: BLE001
        pass
    finally:
        WS_CLIENTS.discard(websocket)


def _latest_records():
    """Show the last completed round until its replacement is fully committed."""
    return SVC.latest_display()


# ---- 通用短缓存 + 每来源限流 ----
_cache = {}
_cache_lock = threading.Lock()
_rate = {}
_rate_lock = threading.Lock()
_RATE_MAX = 120                 # 每分钟每来源请求上限
_RATE_PER_SEC = _RATE_MAX / 60.0


def _cached(key, ttl, builder, want_hit=False):
    """短TTL缓存：同 key 在 ttl 秒内复用上次结果，避免重查询/重算。
    want_hit=True 时返回 (data, 是否命中缓存)，供响应里如实标 cached 字段。"""
    now = time.time()
    with _cache_lock:
        ent = _cache.get(key)
        if ent and (now - ent[0]) < ttl:
            return (ent[1], True) if want_hit else ent[1]
    data = builder()
    with _cache_lock:
        _cache[key] = (now, data, ttl)
        if len(_cache) > _CACHE_MAX:
            # 先按各自 ttl 清过期项；还超就按时间丢最旧的。键里带 limit/q 等任意参数，
            # 以前只清"超过 300 秒"的，一个换参数狂刷的脚本 5 分钟能攒上百份全场快照撑爆内存
            for k in [k for k, v in _cache.items() if now - v[0] >= v[2]]:
                _cache.pop(k, None)
            if len(_cache) > _CACHE_MAX:
                for k, _v in sorted(_cache.items(), key=lambda kv: kv[1][0])[:len(_cache) - _CACHE_MAX // 2]:
                    _cache.pop(k, None)
    return (data, False) if want_hit else data


_CACHE_MAX = 64


def _rate_ok(src):
    """令牌桶限流，每来源 _RATE_MAX/分钟。"""
    now = time.time()
    with _rate_lock:
        toks, last = _rate.get(src, (float(_RATE_MAX), now))
        toks = min(float(_RATE_MAX), toks + (now - last) * _RATE_PER_SEC)
        if toks < 1:
            _rate[src] = [toks, now]
            return False
        _rate[src] = [toks - 1, now]
        if len(_rate) > 1000:   # 清理陈旧来源
            for k in [k for k, v in list(_rate.items()) if now - v[1] > 300]:
                _rate.pop(k, None)
        return True


@app.get("/api/summary")
def api_summary(_: dict = Depends(require_viewer)):
    ls, recs = _latest_records()
    # stale_after：前端"数据过期"横幅用服务端看门狗同一阈值(以前写死 900 秒，巡检间隔调大后每轮都误报)
    # 下限 2 个巡检间隔+2 分钟：watchdog_minutes 显式配得比巡检间隔还小时，横幅别每轮都闪
    sch = CFG["schedule"]
    # 扫完才开始等间隔：一轮周期 = 耗时 + 间隔
    stale_after = max(SVC._stale_threshold(),
                      (sch.get("scan_interval", 300) + SVC.last_took) * 2 + 120)
    extra = {"progress": SVC.progress, "stale_after": int(stale_after)}
    if not ls:
        return {"scanned": False, **extra}
    return {"scanned": True, **_summary_stats(ls, recs), **extra}


def _container_stale_after():
    return max(60, float(CFG.get("schedule", {}).get("container_interval", 10) or 10) * 3)


def _current_containers():
    """Keep raw/last-known evidence, but never call a stalled reader current."""
    now = time.time()
    lifetime = _container_stale_after()
    out = []
    for original in db.get_containers(SVC.conn):
        c = dict(original)
        stamp = data_metrics.measurement(c.get("measured_ts"))
        if not stamp or stamp > now + 60 or now - stamp > lifetime:
            c.update(data_status="unknown", data_complete=0, data_stale=True)
        out.append(c)
    return out


def _container_unknown(c):
    return bool(c.get("data_stale") or c.get("data_status") == "unknown" or
                (c.get("online") and c.get("data_complete") != 1))


def _container_faulty(c, ignore):
    """该集装箱是否有"未被忽略"的真实故障——口径必须与 alerts.evaluate_containers 一致，
    否则用户明明已经把某个故障位加进忽略列表(不报警)，总览/列表的"故障"计数却还在算它。"""
    if not c.get("online") or c.get("data_stale"):
        return False
    if any(f["flag"] not in ignore and f.get("observed") is not False for f in (c.get("faults") or [])):
        return True
    # 供/回液压力低也是告警条件(alerts 里按阈值判)：以前卡片标红、告警也报了，"故障 N"却不算它
    a = CFG.get("alerts", {})
    for key, lim in (("supply_pressure", a.get("container_supply_pressure_min", 0) or 0),
                     ("return_pressure", a.get("container_return_pressure_min", 0) or 0)):
        v = data_metrics.measurement(c.get(key))
        if lim and v is not None and v < lim:
            return True
    return False


def _summary_stats(ls, recs):
    """/api/summary 与 /api/public/summary 共用的统计口径，保证两边永远一致。"""
    stats = data_metrics.aggregate(recs)
    stamps = [db.record_measured_ts(r, ls.get("ts")) for r in recs]
    measured = min(stamps) if stamps and all(ts is not None for ts in stamps) else None
    if not recs:  # A completed empty-inventory scan is known zero at its scan time.
        measured = ls.get("ts")
    if stats["unknown"]:
        measured = None
    cs = _current_containers()
    ignore = set(CFG.get("alerts", {}).get("container_faults_ignore", []))
    return {
        **stats,
        "scan_id": ls.get("scan_id"), "scan_ts": ls.get("ts"), "scan_kind": ls.get("kind"),
        "scan_started_ts": ls.get("started_ts"),
        "measurement_ts": measured,
        "active_alerts": db.count_active(SVC.conn),
        "containers": len(cs),
        "containers_faulty": sum(1 for c in cs if _container_faulty(c, ignore)),
        "containers_offline": sum(1 for c in cs if not c.get("online") and not _container_unknown(c)),
        "containers_unknown": sum(_container_unknown(c) for c in cs),
    }


# ---- 外部 Agent 只读 API（统一 token 鉴权，稳定 snake_case schema，单位带后缀）----
def _check_public_token(request: Request):
    """校验共享 Bearer Token。未配置 token 时整组端点返回 404（fail-closed，不泄露存在性）。

    限流必须在比对 token **之前**：否则错误 token 直接 401 返回，永远走不到限流，
    等于给爆破/探测留了一条不限速的通道。正确 token 的调用方限流行为不变(同样每次
    消耗一个令牌，120 次/分钟)。
    """
    cfg_token = CFG.get("public_api", {}).get("token", "")
    if not cfg_token:
        raise HTTPException(status_code=404, detail="not found")
    if not _rate_ok(_client_ip(request)):   # 防高频轮询打爆 DB 拖慢扫描 / 防 token 爆破
        raise HTTPException(status_code=429, detail="rate limit", headers={"Retry-After": "5"})
    supplied = ""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        supplied = auth_header[7:].strip()
    # compare_digest 对含非 ASCII 的 str 会抛 TypeError(→500)，统一按 UTF-8 字节比较
    if not supplied or not _hmac.compare_digest(supplied.encode("utf-8", "surrogatepass"),
                                                str(cfg_token).encode("utf-8", "surrogatepass")):
        raise HTTPException(status_code=401, detail="bad token")


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """参数校验(422)发生在路由函数体之前，早于函数体内的鉴权调用。

    对 /api/public/*，越界/非法参数因此能在完全不提供 token 的情况下换来 422，
    等于确认了端点存在，击穿"未配置 token 就 404"的 fail-closed 设计。这里让这组
    路径先过一遍鉴权：未配置 token → 404，token 错 → 401，都通过了才谈参数合法性。
    """
    if request.url.path.startswith("/api/public/"):
        try:
            _check_public_token(request)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code,
                                headers=getattr(e, "headers", None))
    return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=422)


def _build_public_summary():
    """与 api_summary 同一数据路径，另加一份精简全量矿机列表。包异常，绝不返回 500。"""
    try:
        ls, recs = _latest_records()
        progress = dict(SVC.progress)
        base = {"ok": True, "ts": int(time.time()), "progress": progress,
                "scanning": bool(progress.get("running")),
                "scan_progress_ts": progress.get("progress_ts") if progress.get("running") else None}
        if not ls:
            return {**base, "scanned": False,
                    "online": None, "offline": None, "total": None, "unknown": None,
                    "online_count": None, "offline_count": None, "total_count": None,
                    "total_hashrate_ths": None, "total_power_kw": None,
                    "hashrate_complete": False, "power_complete": False,
                    "measurement_ts": None, "active_alerts": None, "miners": []}
        s = _summary_stats(ls, recs)
        # 在线矿机有时能读到算力但读不到矿池名；只补已知身份，不能把客户误清成 0。
        # 正常快照不查名册，保持每轮云端客户实时摘要轻量。
        customer_identity = None
        if any(not r.get("worker") for r in recs):
            customer_identity = {ip: a.get("worker") or "(未知)"
                                 for ip, a in db.known_identity(SVC.conn).items()}
        return {
            **base, "scanned": True,
            "scan_id": s["scan_id"], "scan_ts": s["scan_ts"], "scan_kind": s["scan_kind"],
            "scan_started_ts": s["scan_started_ts"],
            "measurement_ts": s["measurement_ts"],
            "total": s["total"], "online": s["online"], "offline": s["offline"],
            "unknown": s["unknown"],
            "online_count": s["online"], "offline_count": s["offline"],
            "total_count": s["total"],
            "with_hashrate": s["with_hashrate"], "no_hashrate": s["no_hashrate"],
            "total_hashrate_ths": s["total_hashrate_th"],
            "known_hashrate_ths": s["known_hashrate_ths"],
            "hashrate_complete": s["hashrate_complete"],
            "hashrate_known_machines": s["hashrate_known_machines"],
            "avg_hashrate_ths": s["avg_hashrate_th"],
            "total_power_kw": s["total_power_kw"],
            "known_power_kw": s["known_power_kw"],
            "power_complete": s["power_complete"],
            "power_known_machines": s["power_known_machines"],
            "active_alerts": s["active_alerts"],
            "containers": s["containers"], "containers_faulty": s["containers_faulty"],
            "containers_offline": s["containers_offline"],
            "containers_unknown": s["containers_unknown"],
            "customer_hashrates": db.customer_hashrates(ls, recs, identities=customer_identity),
            "miners": [_public_miner_row(r) for r in recs],
        }
    except Exception as e:  # noqa: BLE001
        log.exception("build_public_summary error")
        return {"ok": False, "ts": int(time.time()), "error": str(e)}


def _build_cloud_summary():
    """云端上报专用：在本地摘要基础上加一份"活跃告警摘要"(按类型+客户聚合，不含单机IP)。
    只喂给 cloud_report 上报线程，不影响 /api/public/summary、/api/summary 等本地接口。"""
    s = _build_public_summary()
    try:
        s["alert_summary"] = cloud_alert_summary.build(SVC.conn)
    except Exception as e:  # noqa: BLE001  聚合失败不能拖累上报本体(在线/算力/功耗)
        log.warning("告警摘要聚合失败(不影响上报本体): %s", e)
        s["alert_summary"] = []
    return s


def _public_miner_row(r):
    """单台矿机的 Agent 友好视图（稳定字段名 + 显式单位）。"""
    r = data_metrics.live_record(r)
    return {
        "ip": r["ip"], "status": r.get("status") or "unknown",
        "firmware": r.get("firmware") or "", "model": r.get("model") or "",
        "sn": r.get("sn") or "", "mac": r.get("mac") or "",
        "worker": r.get("worker") or "",
        "hashrate_ths": r["hr_rt"], "hashrate_avg_ths": r["hr_avg"],
        "power_w": r["power"], "temp_c": r["temp"],
        "measured_ts": r.get("measured_ts"), "data_stale": bool(r.get("data_stale")),
        "efficiency_jth": r.get("eff"), "uptime_s": r.get("uptime"),
        "accepted": r.get("accepted"), "rejected": r.get("rejected"),
    }


@app.get("/api/public/summary")
def api_public_summary(request: Request, lite: int = 0):
    """公共只读总览。lite=1 省略 miners[] 明细（给云端总览等每分钟轮询方省带宽）。"""
    _check_public_token(request)
    data, hit = _cached(("public_summary",), 10, _build_public_summary, want_hit=True)
    out = dict(data)
    out["cached"] = hit
    if lite:
        out.pop("miners", None)
    return out


@app.get("/api/public/health")
def api_public_health(request: Request):
    """轻量心跳：监控进程是否活着、上次扫描多久前、是否在扫、活跃告警数。供 Agent 探活。"""
    _check_public_token(request)
    p = SVC.progress
    last = p.get("last_finished", 0)
    now = int(time.time())
    try:
        n_alerts = db.count_active(SVC.conn)
    except Exception:  # noqa: BLE001
        n_alerts = None
    return {"ok": True, "ts": now, "scanning": bool(p.get("running")),
            "last_scan_ts": last or None,
            "last_scan_age_s": (now - last) if last else None,
            "stalled": SVC.is_stale(now),
            "active_alerts": n_alerts}


def _filter_recs(recs, status="", fw="", seg="", q="", repair=None):
    out = recs
    if seg:
        out = [r for r in out if r["ip"].rsplit(".", 1)[0] == seg]
    if status == "repair":
        out = [r for r in out if repair and r["ip"] in repair]
    elif status:
        out = [r for r in out if r["status"] == status]
    if fw:
        out = [r for r in out if r["firmware"] == fw]
    if q:
        ql = q.lower()
        out = [r for r in out if ql in r["ip"].lower() or ql in (r.get("sn") or "").lower()
               or ql in (r.get("mac") or "").lower()
               or ql in (r.get("worker") or "").lower()]
    return out


@app.get("/api/public/miners")
def api_public_miners(request: Request, status: str = "", fw: str = "", seg: str = "",
                      q: str = "", limit: int = 50000):
    """矿机列表（Agent 友好）。filters: status/fw/seg/q。"""
    # limit 不能用 Query(ge=,le=) 声明：那层校验由 FastAPI 在进入函数体前执行，
    # 越界值会在鉴权之前就返回 422，等于告诉未鉴权的人"这个端点存在"。
    # 一律先鉴权，再在函数体里手工收敛范围。
    _check_public_token(request)
    limit = max(1, min(50000, limit))

    def build():
        _, recs = _latest_records()
        out = _filter_recs(recs, status, fw, seg, q)
        stats = data_metrics.aggregate(out)
        return {"ok": True, "ts": int(time.time()), "count": len(out), "online": stats["online"],
                "unknown": stats["unknown"],
                "total_hashrate_ths": stats["total_hashrate_th"],
                "known_hashrate_ths": stats["known_hashrate_ths"],
                "hashrate_complete": stats["hashrate_complete"], "power_complete": stats["power_complete"],
                "total_power_w": None if stats["total_power_kw"] is None else stats["total_power_kw"] * 1000,
                "miners": [_public_miner_row(r) for r in out[:max(1, limit)]]}
    return _cached(("miners", status, fw, seg, q, limit), 8, build)


@app.get("/api/public/miner/{ip}")
def api_public_miner(ip: str, request: Request):
    """单台矿机当前状态 + 近期算力/温度/功耗历史。"""
    _check_public_token(request)
    _, recs = _latest_records()
    cur = next((r for r in recs if r["ip"] == ip), None)
    if not cur:
        return {"ok": True, "ts": int(time.time()), "found": False, "ip": ip}
    hist = []
    for h in db.ip_history(SVC.conn, ip, 200):
        point = data_metrics.live_record(h, db.record_freshness(h, h["ts"], h["ts"]))
        hist.append({"ts": h["ts"], "measured_ts": h.get("measured_ts"), "status": point["status"],
                     "hashrate_ths": point["hr_rt"], "temp_c": point["temp"], "power_w": point["power"]})
    return {"ok": True, "ts": int(time.time()), "found": True,
            "miner": _public_miner_row(cur), "history": hist}


@app.get("/api/public/alerts")
def api_public_alerts(request: Request, active: bool = True):
    """告警列表（默认仅活跃）。"""
    _check_public_token(request)

    def build():
        al = db.list_alerts(SVC.conn, active_only=active, limit=2000)
        return {"ok": True, "ts": int(time.time()), "count": len(al),
                "counts": db.count_active_by_type(SVC.conn),
                "alerts": [{"id": a["id"], "ts": a["ts"], "ip": a["ip"], "type": a["type"],
                            "severity": a["severity"], "detail": a["detail"],
                            "resolved": bool(a["resolved"]), "resolved_ts": a.get("resolved_ts"),
                            "ack_by": a.get("ack_by")} for a in al]}
    return _cached(("alerts", active), 8, build)


@app.get("/api/public/containers")
def api_public_containers(request: Request):
    """水冷集装箱（AntBox）列表。"""
    _check_public_token(request)

    def build():
        cs = _current_containers()
        ignore = set(CFG.get("alerts", {}).get("container_faults_ignore", []))
        return {"ok": True, "ts": int(time.time()), "count": len(cs),
                "faulty": sum(1 for c in cs if _container_faulty(c, ignore)),
                "offline": sum(1 for c in cs if not c.get("online") and not _container_unknown(c)),
                "unknown": sum(_container_unknown(c) for c in cs),
                "containers": cs}
    return _cached(("containers",), 8, build)


def _report_window(hours):
    """兼容的历史跨度提示，不能用这个取整数替换实际查询窗口。"""
    cov = db.coverage_hours(SVC.conn) or 1
    eff = min(max(1, int(hours)), cov)
    return eff, eff < hours


def _report_end(until_ts=None):
    """共同的 UTC 十分钟边界；各端必须真的查询同一时间窗才能合计。"""
    now = int(time.time())
    if until_ts is None:
        return now // 600 * 600
    end = data_metrics.measurement(until_ts)
    if end is None or end <= 0 or end > now:
        raise HTTPException(400, "统计截止时间必须是已经过去的有效 Unix 秒数")
    return int(end)


def _customer_report_packet(hours=24, until_ts=None):
    """One query and its exact window, including a verifiably empty customer list."""
    hours = max(1, min(8760, int(hours)))
    end = _report_end(until_ts)
    rows = _cached(("customers", hours, end), 45,
                   lambda: db.customer_report(SVC.conn, hours, until_ts=end))
    # A first query may create hourly archives; report the resulting retained span.
    eff, truncated = _report_window(hours)
    return {"hours": hours, "covered_hours": eff, "truncated": truncated,
            "period_start_ts": end - hours * 3600, "period_end_ts": end,
            "billing_period_start_ts": end - hours * 3600, "billing_period_end_ts": end,
            "customers": rows}


@app.get("/api/public/customers")
def api_public_customers(request: Request, hours: int = 24, until_ts: float = None):
    """客户实时/时段平均算力、有效观测覆盖，以及兼容的计费报表字段。"""
    _check_public_token(request)
    hours = max(1, min(8760, hours))   # 顺手封顶，避免任意 hours 撑爆缓存键空间
    end = _report_end(until_ts)

    def build():   # 最重的查询, 缓存 45 秒(报表变化慢)
        return {"ok": True, "ts": int(time.time()), **_customer_report_packet(hours, end)}
    # 缓存槽位必须按**原始入参 hours** 区分：响应里带了 hours/truncated 两个随原始入参
    # 变化的字段，若按收敛后的 eff 做键，hours=20 与 hours=21(都收敛到 eff=20)会互相
    # 拿到对方的元数据。另外键名与 /api/reports/customers 的 ("customers", eff) 区分开
    # ——那边缓存的是裸行列表，同键会让两个端点互相返回对方的数据结构。
    return _cached(("public_customers", hours, end), 45, build)


# ---- 会话 API ----
_SORT_FIELDS = {"ip", "status", "firmware", "model", "hr_rt", "hr_avg", "power",
                "eff", "temp", "uptime", "worker", "sn"}


@app.get("/api/miners")
def api_miners(status: str = "", fw: str = "", q: str = "", seg: str = "",
               sort: str = "hr_rt", order: str = "desc",
               limit: int = Query(50000, ge=1, le=50000),
               _: dict = Depends(require_viewer)):
    # 默认上限须 ≥ 全场真机数，否则前端会把第 N 名之后(恰是离线/零算力=维修/下架对象)
    # 的机器静默移出选择集 → 漏命令
    _, recs = _latest_records()
    repair = db.repair_ips(SVC.conn)
    out = _filter_recs(recs, status, fw, seg, q, repair=repair)
    stats = data_metrics.aggregate(out)
    agg = {"count": len(out), "online": stats["online"], "offline": stats["offline"],
           "unknown": stats["unknown"], "total_hr": stats["total_hashrate_th"],
           "known_hashrate_ths": stats["known_hashrate_ths"],
           "hashrate_complete": stats["hashrate_complete"], "power_complete": stats["power_complete"],
           "total_power": None if stats["total_power_kw"] is None else stats["total_power_kw"] * 1000,
           "known_power": None if stats["known_power_kw"] is None else stats["known_power_kw"] * 1000,
           "power_known_machines": stats["power_known_machines"],
           "avg_efficiency": stats["avg_efficiency"], "known_efficiency": stats["known_efficiency"],
           "efficiency_known_machines": stats["efficiency_known_machines"],
           "efficiency_complete": stats["efficiency_complete"]}
    if sort not in _SORT_FIELDS:   # 防任意字段排序产生空/错误结果
        sort = "hr_rt"
    rev = order == "desc"
    # 空值(离线/无算力)始终排在最后，不受升降序影响
    nn = [r for r in out if r.get(sort) not in (None, "")]
    nul = [r for r in out if r.get(sort) in (None, "")]
    if sort == "ip":   # 按数值排：字符串序会排成 .1 .10 .100 .2，巡架时对不上机位
        nn.sort(key=miner_core._ip_sort_key, reverse=rev)
    else:
        nn.sort(key=lambda r: r.get(sort), reverse=rev)
    page = (nn + nul)[:limit]
    # 拷贝后再挂 mstate：recs 是所有请求共享的内存快照，绝不能原地改
    return {"count": len(out), "agg": agg,
            "miners": [dict(r, mstate=("repair" if r["ip"] in repair else "active"))
                       for r in page]}


@app.get("/api/workers")
def api_workers(_: dict = Depends(require_viewer)):
    """同一完整快照内的已知客户机器；离线/未知不从客户名下消失。"""
    _, recs = _latest_records()
    identities = {ip: r.get("worker") for ip, r in db.known_identity(SVC.conn).items()}
    agg = {}
    for r in recs:
        w = r.get("worker") or identities.get(r["ip"]) or "(未知)"
        model = r.get("model") or "Unknown"
        a = agg.setdefault(w, {"worker": w, "models": {}, "_records": []})
        a["_records"].append(r)
        a["models"][model] = a["models"].get(model, 0) + 1
    for a in agg.values():
        stats = data_metrics.aggregate(a.pop("_records"))
        a.update(total=stats["total"], online=stats["online"], unknown=stats["unknown"],
                 hashrate=stats["total_hashrate_th"], known_hashrate_ths=stats["known_hashrate_ths"],
                 hashrate_complete=stats["hashrate_complete"])
    out = sorted(agg.values(), key=lambda x: x["total"], reverse=True)
    return {"workers": out, "count": len(out)}


@app.get("/api/reports/customers")
def api_report_customers(hours: int = 24, format: str = "", until_ts: float = None,
                         _: dict = Depends(require_viewer)):
    hours = max(1, min(8760, hours))
    packet = _customer_report_packet(hours, until_ts)
    end, rows = packet["period_end_ts"], packet["customers"]
    if format == "csv":   # 对客户出账：UTF-8 BOM 的 CSV(Excel 直接打开不乱码)
        import csv as _csv
        import io
        buf = io.StringIO()
        buf.write("﻿")
        w = _csv.writer(buf)
        w.writerow([f"客户报表：近 {hours} 小时，截止 Unix 秒 {end}"])
        w.writerow(["客户(矿工名)", "台数", "可用率%", "交付算力(TH·h)", "耗电(kWh)",
                    "当前机器数", "实时算力(TH/s)", "平均算力(TH/s)",
                    "统计开始(Unix秒)", "统计截止(Unix秒)"])
        def cell(v):   # 矿工名来自矿机(可被篡改)：= + - @ 开头会被 Excel 当公式执行
            s = str(v or "")
            return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

        for r in rows:
            w.writerow([cell(r["worker"]), r["machines"], r["uptime_pct"],
                        r["delivered_th_h"], r["power_kwh"], r.get("current_machines"),
                        r.get("hashrate_ths"), r.get("avg_hashrate_ths"),
                        r.get("period_start_ts"), r.get("period_end_ts")])
        return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="customers_{hours}h.csv"'})
    return packet


@app.get("/api/containers")
def api_containers(_: dict = Depends(require_viewer)):
    cs = _current_containers()
    acfg = CFG.get("alerts", {})
    ignore = set(acfg.get("container_faults_ignore", []))
    return {"count": len(cs),
            "faulty": sum(1 for c in cs if _container_faulty(c, ignore)),
            "offline": sum(1 for c in cs if not c.get("online") and not _container_unknown(c)),
            "unknown": sum(_container_unknown(c) for c in cs),
            "containers": cs,
            "faults_ignore": acfg.get("container_faults_ignore", []),
            "supply_pressure_min": acfg.get("container_supply_pressure_min", 0),
            "return_pressure_min": acfg.get("container_return_pressure_min", 0)}


@app.post("/api/containers/scan")
def api_containers_scan(_: dict = Depends(require_ops)):
    """手动立即刷新集装箱（后台执行，秒回）。"""
    threading.Thread(target=SVC.scan_containers, daemon=True).start()
    return {"ok": True}


@app.get("/api/container/{ip}")
def api_container(ip: str, _: dict = Depends(require_viewer)):
    cur = next((c for c in _current_containers() if c["ip"] == ip), None)
    return {"current": cur, "history": db.container_history(SVC.conn, ip),
            "stale_after": _container_stale_after()}


@app.get("/api/racks")
def api_racks(_: dict = Depends(require_viewer)):
    """按 /24 网段聚合成货架，每段固定显示完整机位，缺位=空机位(灰)。"""
    ls, recs = _latest_records()
    if not ls:
        return {"racks": []}
    _, hs, he = appconfig.load_segments(CFG)   # 机位范围与扫描范围一致

    def state(r):
        if r is None:
            return "empty"
        if r["status"] == "unknown":   # 本轮扫描超时没来得及探测，既非在线也非离线
            return "unknown"
        if r["status"] == "offline":
            return "offline"
        hr = r.get("hr_rt")
        # 只有明确读到 0 才算零算力；hr_rt=None(接口抖动没读到)不能当零算力画黄，
        # 否则一次接口超时就把正常在线机误显示成"零算力"故障
        return "zero" if hr == 0 else "ok"

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
        slots, cnt, rack_records = [], {}, []
        for h in range(hs, he + 1):
            r = hosts.get(h)
            st = state(r)
            cnt[st] = cnt.get(st, 0) + 1
            if r:
                rack_records.append(r)
            slots.append({"h": h, "ip": f"{seg}.{h}", "st": st,
                          "hr": (r.get("hr_rt") if r else None),
                          "temp": (r.get("temp") if r else None)})
        stats = data_metrics.aggregate(rack_records)
        out.append({"name": seg, "slots": slots,
                    "online": cnt.get("ok", 0) + cnt.get("zero", 0),
                    "abnormal": cnt.get("zero", 0), "offline": cnt.get("offline", 0),
                    "empty": cnt.get("empty", 0), "unknown": cnt.get("unknown", 0),
                    "hashrate": stats["total_hashrate_th"],
                    "known_hashrate_ths": stats["known_hashrate_ths"],
                    "hashrate_complete": stats["hashrate_complete"]})
    return {"racks": out}


@app.get("/api/miner/{ip}")
def api_miner(ip: str, _: dict = Depends(require_viewer)):
    _, recs = _latest_records()
    cur = next((r for r in recs if r["ip"] == ip), None)
    history = [data_metrics.live_record(h, db.record_freshness(h, h["ts"], h["ts"]))
               for h in db.ip_history(SVC.conn, ip)]
    return {"current": cur, "history": history}


@app.get("/api/miner/{ip}/console")
def api_miner_console(ip: str, live: bool = True, _: dict = Depends(require_viewer)):
    """返回打开矿机官方/第三方网页的方案（不代替浏览器跨域登录限制）。"""
    import miner_web
    try:
        ip = miner_web.validate_miner_ip(ip)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    _, recs = _latest_records()
    cur = next((r for r in recs if r["ip"] == ip), None) or {}
    cfg = SVC.cfg if hasattr(SVC, "cfg") else {}
    passwords = cfg.get("passwords") or [["root", "root"]]
    stock_user, stock_pwd = (passwords[0] + ["", ""])[:2] if passwords else ("root", "root")
    control = cfg.get("control") or {}
    uni_pwd = control.get("uniplus_password") or "admin"
    try:
        plan = miner_web.console_plan(
            ip,
            firmware_hint=str(cur.get("firmware") or ""),
            stock_user=str(stock_user or "root"),
            stock_password=str(stock_pwd or "root"),
            uniplus_password=str(uni_pwd or "admin"),
            live=bool(live),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    plan["ok"] = True
    plan["firmware_hint"] = cur.get("firmware")
    if plan.get("kind") == "uniplus":
        plan["bridge_path"] = f"/api/miner/{ip}/console/bridge"
    return plan


@app.get("/api/miner/{ip}/console/bridge")
def api_miner_console_bridge(ip: str, _: dict = Depends(require_viewer)):
    """UniPlus：新窗口 POST pwd 写入 cookie 后跳转 /dashboard。"""
    import miner_web
    try:
        ip = miner_web.validate_miner_ip(ip)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    cfg = SVC.cfg if hasattr(SVC, "cfg") else {}
    uni_pwd = (cfg.get("control") or {}).get("uniplus_password") or "admin"
    return HTMLResponse(miner_web.uniplus_bridge_html(ip, str(uni_pwd or "admin")))


@app.post("/api/alerts/ack")
def api_alert_ack(body: dict = Body(...), sess: dict = Depends(require_ops)):
    """确认矿机告警（集装箱告警不允许确认，修复后自动消失）。"""
    try:
        aid = int(body.get("id"))
    except (TypeError, ValueError, OverflowError):   # 请求体里的 Infinity 会被 JSON 解析成 inf
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
    # counts/total 走精确计数(不受列表 limit 截断)：大面积事件下台数才不会被卡在 limit
    limit = max(1, min(5000, limit))   # 与 /api/trend、/api/commands 保持同一收敛口径
    counts = db.count_active_by_type(SVC.conn)
    return {"alerts": db.list_alerts(SVC.conn, active_only=active, limit=limit),
            "counts": counts, "total": sum(counts.values())}


@app.get("/api/trend")
def api_trend(points: int = 288, _: dict = Depends(require_viewer)):
    return {"trend": db.hashrate_trend(SVC.conn, max(2, min(2000, points)))}


@app.post("/api/scan")
def api_scan(kind: str = Query("manual"), _: dict = Depends(require_ops)):
    """kind=quick 只巡检名册(快)；full/manual 展开全部网段做发现(慢)。"""
    ok = SVC.scan_now_async("quick" if kind == "quick" else "manual")
    return JSONResponse({"started": ok, "progress": SVC.progress})


@app.get("/api/progress")
def api_progress(_: dict = Depends(require_viewer)):
    return SVC.progress


@app.get("/api/segments")
def api_segments(_: dict = Depends(require_viewer)):
    segs, hs, he = appconfig.load_segments(CFG)
    return {"segments": segs, "host_start": hs, "host_end": he}


@app.get("/api/settings")
def api_settings_get(sess: dict = Depends(require_viewer)):
    ctl = CFG.get("control") or {}
    out = {"scan_interval": CFG["schedule"].get("scan_interval", 300),
           "full_interval": CFG["schedule"].get("full_interval", 3600),
           "max_pps": CFG["scan"].get("max_pps", 100),
           "container_interval": CFG["schedule"].get("container_interval", 10),
           "discovery_workers": CFG["scan"].get("discovery_workers", 300),
           # 重启设置：viewer/ops 只读可见；admin 可 POST 改。开关只挡自动重启。
           "reboot_enabled": bool(ctl.get("reboot_enabled")),
           "reboot_concurrency": int(ctl.get("reboot_concurrency", 30) or 30),
           "reboot_delay_sec": int(ctl.get("reboot_delay_sec", 8)),
           "reboot_max_per_day": int(ctl.get("reboot_max_per_day", 4) or 4),
           "reboot_min_interval_sec": int(ctl.get("reboot_min_interval_sec", 900))}
    if auth.has_role(sess, "admin"):   # 云端上报配置只给 admin
        c = CFG.get("cloud") or {}
        tok = str(c.get("token") or "")
        # token 不回传明文：只告诉前端"已填写"和末 4 位，留空保存=沿用原值。
        # 浏览器缓存/截屏/肩窥都拿不到完整 token
        out["cloud"] = {"enabled": bool(c.get("enabled")), "url": c.get("url") or "",
                        "token_set": bool(tok), "token_hint": tok[-4:] if len(tok) >= 12 else "",
                        "site_name": c.get("site_name") or "",
                        "site_type": c.get("site_type") or "air",
                        "tls_fingerprint": c.get("tls_fingerprint") or ""}
        out["site_id"] = cloud_report.site_id(CFG)
    return out


def _cloud_form(c, keep_token=True):
    """校验网页提交的云端上报配置，返回 (配置, 错误)。token 留空 = 沿用已保存的。"""
    cur = CFG.get("cloud") or {}
    token = str(c.get("token") or "").strip()
    fp = cloud_report.normalize_fingerprint(
        c["tls_fingerprint"] if "tls_fingerprint" in c else cur.get("tls_fingerprint"))
    if fp is None:
        return None, "证书指纹格式不对：应为 64 位十六进制(可带冒号)，从云端面板「场地接入」复制"
    url = str(c.get("url") or "").strip().rstrip("/")
    if url:
        try:
            url = cloud_connect.validate_url(url)
        except cloud_connect.SetupError as error:
            return None, str(error)
    if not token and keep_token and cur.get("token"):
        # 沿用已保存的 token 只限地址和指纹都没变：否则改个地址就能让本服务把 token
        # 明文发到任意服务器——浏览器拿不到 token 的保护就白做了
        saved_fp = cloud_report.normalize_fingerprint(cur.get("tls_fingerprint")) or ""
        if url != str(cur.get("url") or "").rstrip("/") or fp != saved_fp:
            return None, "云端地址或证书指纹改了，请重新填写上报 token(或粘贴新的接入码)"
        token = str(cur.get("token") or "")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
        return None, "上报 token 格式不对，请重新复制完整接入信息"
    cl = {"enabled": c.get("enabled", False),
          "url": url,
          "token": token,
          "site_name": str(c.get("site_name") or "").strip()[:64],
          "site_type": c.get("site_type") if c.get("site_type") in ("air", "hydro", "mixed")
          else "air",
          "tls_fingerprint": fp}
    # settings.json replaces the cloud object as a whole. Preserve reporting
    # intervals/timeouts that are configured outside this connection form.
    for key in ("interval", "timeout", "customer_interval", "customer_hours"):
        if key in cur:
            cl[key] = cur[key]
    # 场地 ID 一旦定下就别丢：表单没带时沿用当前值(否则重启后回落到 cloud_site_id.txt，云端变成新场地)
    raw_sid = str(c.get("site_id") or cur.get("site_id") or "")
    if raw_sid:
        # 与云端绑定 token 的目标校验一致：旧码可用中文或带空格的名称作 ID。
        # 清洗后必须原样，禁止静默改写身份，否则可能指向另一个场地。
        sid = "".join(ch for ch in raw_sid if ch >= " " and ch != "\x7f").strip()
        if sid != raw_sid or not 1 <= len(sid) <= 64:
            return None, "场地 ID 须为 1~64 个字符，不能含控制符或首尾空白"
        cl["site_id"] = sid
    if "enabled" in c and not isinstance(c["enabled"], bool):
        return None, "cloud.enabled 须为布尔值"
    if cl["enabled"] and not (cl["url"] and cl["token"] and cl["site_name"]):
        return None, "启用上报需填写 云端地址/上报token/场地名"
    return cl, ""


@app.get("/api/cloud/status")
def api_cloud_status(_: dict = Depends(require_admin)):
    """云端上报状态：给设置面板显示，不用登机器翻日志。"""
    c = CFG.get("cloud") or {}
    st = cloud_report.status()
    return {"ok": True, "enabled": cloud_report.enabled(CFG), "running": cloud_report.is_alive(),
            "url": c.get("url") or "", "site_id": cloud_report.site_id(CFG),
            "pinned": bool(c.get("tls_fingerprint")), **st}


@app.post("/api/cloud/test")
def api_cloud_test(body: dict = Body(default={}), _: dict = Depends(require_admin)):
    """用表单里(还没保存)的地址/token/指纹试连云端一次，只校验不写数据。"""
    c = body.get("cloud") if isinstance(body.get("cloud"), dict) else {}
    cl, err = _cloud_form({**c, "enabled": False})
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    ok, msg = cloud_report.test(cl, cl.get("site_id") or cloud_report.site_id(CFG))
    return {"ok": ok, "message": msg}


@app.post("/api/settings")
def api_settings_post(body: dict = Body(...), _: dict = Depends(require_admin)):
    s = {}
    if isinstance(body.get("cloud"), dict):   # 云端上报(网页配置, 存 settings.json, 即时生效)
        cl, err = _cloud_form(body["cloud"])
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        s["cloud"] = cl
    # 与 appconfig._validate 的上下限保持一致(max_pps/discovery_workers 硬上限 500 = 三层 CoPP)。
    # 超范围直接拒绝而不是悄悄夹紧：以前网页清空 ARP 限速会存成 0、回显 0，实际被夹成 1 pps，
    # 巡检一轮要一个多小时，监控形同停摆却看不出来
    ranges = {"scan_interval": (30, 86400, "巡检间隔(秒)"), "full_interval": (60, 604800, "全网发现间隔(秒)"),
              "container_interval": (5, 3600, "集装箱刷新间隔(秒)"), "max_pps": (1, 500, "ARP限速 max_pps"),
              "discovery_workers": (1, 500, "发现并发"),
              "reboot_concurrency": (1, 1000, "重启并发"),
              "reboot_delay_sec": (0, 3600, "重启批间隔(秒)"),
              "reboot_max_per_day": (1, 100, "24h重启上限"),
              "reboot_min_interval_sec": (0, 86400, "重启最小间隔(秒)")}
    for k, (lo, hi, name) in ranges.items():
        if k not in body:
            continue
        try:
            v = int(body[k])
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"ok": False, "error": f"{name} 须为数字"}, status_code=400)
        if not lo <= v <= hi:
            return JSONResponse({"ok": False, "error": f"{name} 须在 {lo}~{hi} 之间"},
                                status_code=400)
        s[k] = v
    if "reboot_enabled" in body:
        if not isinstance(body["reboot_enabled"], bool):
            return JSONResponse({"ok": False, "error": "reboot_enabled 须为布尔值"},
                                status_code=400)
        s["reboot_enabled"] = body["reboot_enabled"]
    effective_scan = s.get("scan_interval", CFG["schedule"].get("scan_interval", 300))
    effective_full = s.get("full_interval", CFG["schedule"].get("full_interval", 3600))
    if effective_full < effective_scan:
        return JSONResponse({"ok": False, "error": "全网发现间隔不能小于巡检间隔"},
                            status_code=400)
    try:   # 读-合并-写在同一把锁内：两个 admin 同时保存不互相覆盖
        appconfig.update_settings(s)
    except appconfig.SettingsError as e:   # 原文件读不了：不覆盖，原因告诉管理员
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    appconfig.apply_settings(CFG, s)   # 即时生效，无需重启
    SVC.wake()                         # 唤醒调度循环立即按新间隔重排
    out = {"ok": True, **s}
    if "cloud" in out:                 # 回显也不能带 token 明文
        tok = out["cloud"].get("token") or ""
        out["cloud"] = {k: v for k, v in out["cloud"].items() if k != "token"}
        out["cloud"].update(token_set=bool(tok), token_hint=tok[-4:] if len(tok) >= 12 else "")
    for k in ranges:                   # 回显实际生效的值
        if k not in s:
            continue
        if k in ("scan_interval", "full_interval", "container_interval"):
            out[k] = CFG["schedule"].get(k)
        elif k in ("max_pps", "discovery_workers"):
            out[k] = CFG["scan"].get(k)
        else:   # reboot_*
            out[k] = CFG["control"].get(k)
    if "reboot_enabled" in s:
        out["reboot_enabled"] = bool(CFG["control"].get("reboot_enabled"))
    if "cloud" in s:                   # 上报线程热重启(旧线程自动失效)
        _start_cloud_report()
        out["site_id"] = cloud_report.site_id(CFG)
        log.info("网页更新上报配置: enabled=%s → %s (场地: %s)", s["cloud"]["enabled"],
                 s["cloud"].get("url") or "(未填)", s["cloud"].get("site_name") or "-")
    return out


@app.post("/api/segments")
def api_segments_save(body: dict = Body(...), _: dict = Depends(require_admin)):
    norm, bad = [], []
    for s in (body.get("segments") or []):
        parts = str(s).strip().split(".")
        if len(parts) < 3:
            continue
        # 只收 ASCII 数字并规范化(172.016.005 → 172.16.5)：前导 0 会被系统当八进制解析，
        # 扫的就不是你以为的网段；全角/其它数字字符 int() 也认，得挡掉
        if not all(x.isascii() and x.isdigit() and int(x) <= 255 for x in parts[:3]):
            bad.append(str(s)[:32])
            continue
        base = ".".join(str(int(x)) for x in parts[:3])
        # 只允许内网网段：扫描/命令会带着矿机口令去连这些地址，加进公网段等于把口令送出去
        net = ipaddress.ip_address(base + ".1")
        if not (net.is_private or net in ipaddress.ip_network("100.64.0.0/10")) \
                or net.is_loopback or net.is_link_local or net.is_multicast:
            bad.append(str(s)[:32])
            continue
        if base not in norm:
            norm.append(base)
    if bad:
        return JSONResponse({"ok": False, "error": "以下网段无效或不是内网地址: " + "、".join(bad[:5])},
                            status_code=400)
    try:
        hs = max(1, min(254, int(body.get("host_start", 1))))
        he = max(hs, min(254, int(body.get("host_end", 254))))
    except (TypeError, ValueError, OverflowError):
        return JSONResponse({"ok": False, "error": "host_start/host_end 须为数字"},
                            status_code=400)
    appconfig.save_segments(norm, hs, he)
    log.info("网段已更新: %d 段, 主机号 %d-%d", len(norm), hs, he)
    return {"ok": True, "segments": norm, "host_start": hs, "host_end": he, "count": len(norm)}


# 破坏性动作：后端强制要求请求体显式带 confirm:true。
# control.py 的注释历来写"前端强制二次确认"，但前端确认只是个弹窗——任何持有 ops 会话
# 的脚本/curl/被注入的同源 JS 都能直接 POST 触发全场重启或换矿池。确认必须落到后端。
DESTRUCTIVE_ACTIONS = {"reboot", "set_pools"}


def _local_known_ip_filter(ips):
    """control.py 未提供过滤函数时的等价兜底：按 segments.json/config 的网段+主机号判定。"""
    segs, hs, he = appconfig.load_segments(CFG)
    known = {str(s).strip() for s in (segs or []) if str(s).strip()}
    allowed, rejected = [], []
    for ip in ips:
        parts = str(ip).split(".")
        ok = False
        if len(parts) == 4 and ".".join(parts[:3]) in known:
            try:
                ok = hs <= int(parts[3]) <= he
            except ValueError:
                ok = False
        (allowed if ok else rejected).append(ip)
    return allowed, rejected


def _filter_known_ips(ips):
    """把不属于本矿场已配置网段的目标剔除，返回 (allowed, rejected)。

    为什么必须有：/api/command 会带着矿机管理口令(HTTP Digest)去连目标地址。只校验
    IP 格式不校验归属，等于把本服务变成一台内网/公网探测器，还会把口令送到任意地址。
    优先复用 control.filter_known_ips()(控制层统一口径)，拿不到或形状不符则本地兜底，
    保证这道校验不会因为依赖没到位而被跳过。
    """
    fn = getattr(control, "filter_known_ips", None)
    if callable(fn):
        try:
            res = fn(list(ips), CFG)
            allowed_raw = res[0] if isinstance(res, tuple) else res
            if isinstance(allowed_raw, (list, tuple, set)):
                ok = {str(x) for x in allowed_raw}
                allowed = [ip for ip in ips if ip in ok]
                return allowed, [ip for ip in ips if ip not in ok]
            log.warning("control.filter_known_ips 返回结构不符预期(%r)，回退本地校验",
                        type(allowed_raw).__name__)
        except Exception:  # noqa: BLE001
            log.exception("control.filter_known_ips 调用失败，回退本地网段校验")
    return _local_known_ip_filter(ips)


def _audit_reject(user, action, reason):
    try:
        db.log_commands(SVC.conn, user, f"reject:{action}",
                        [{"ip": "-", "ok": False, "msg": reason}])
    except Exception:  # noqa: BLE001
        pass


# 批量重启后台进度(分批+延迟会耗时，异步执行，前端轮询)
CMD_PROGRESS = {"running": False, "action": "", "done": 0, "total": 0,
                "success": 0, "failed": 0, "fail_ips": [], "user": "", "error": "",
                "job_id": ""}
# 重启互斥锁挂在 SVC 上(手动 API 与零算力自动重启共用)，见 MonitorService._reboot_lock


def _update_busy():
    """更新已占用更新锁；再检查设备锁，避免更新和设备命令互相穿过闸门。"""
    if CMD_PROGRESS.get("running") or not SVC._reboot_lock.acquire(blocking=False):
        return "正在执行设备命令"
    try:
        control.flush_reboot_audit(SVC)
    except Exception:
        log.exception("设备命令审计尚未保存，推迟更新")
        return "设备命令执行记录尚未保存，请检查数据库或磁盘后重试"
    finally:
        SVC._reboot_lock.release()
    return ""


updater.BUSY_CHECK = _update_busy


def _pools_desc(params):
    return "; ".join(f'{p.get("url")} {p.get("user")}' for p in
                     (params or {}).get("pools") or [] if isinstance(p, dict))[:300]


def _audit_command(user, action, params, results):
    """写审计。换矿池把完整矿池地址/矿工名写进每条审计(以前只记 "pools updated"，
    事后查不出被换到了哪个池)。"""
    if action == "set_pools":
        desc = _pools_desc(params)
        for r in results:
            r["msg"] = f'{r["msg"]} | {desc}'[:500]
    if action == "reboot":
        control.audit_reboot_results(SVC, user, results)
    else:
        if not results:
            return
        if not hasattr(SVC, "_pending_command_audit"):
            SVC._pending_command_audit = []
        SVC._pending_command_audit.append(
            (int(time.time()), user, action, [dict(r) for r in results]))
        control.flush_command_audit(SVC)


def _record_command(user, action, params, results, audited=False):
    """审计 + 推送：重启/换矿池都推一条 Telegram，值班群里有人知道谁干了什么。
    audited=True：结果已逐批写过审计(异步分批)，这里只推送。"""
    if not audited:
        _audit_command(user, action, params, results)
    if action in DESTRUCTIVE_ACTIONS and results:
        ok_n = sum(1 for r in results if r["ok"])
        name = {"reboot": "重启", "set_pools": "换矿池"}[action]
        text = f"⚙ {html.escape(str(user))} {name} {len(results)} 台(成功 {ok_n})"
        if action == "set_pools":
            text += f"\n矿池: {html.escape(_pools_desc(params))}"
        try:
            alerts.push_text(CFG, text)
        except Exception:  # noqa: BLE001
            log.exception("命令推送失败")


def _run_command_bg(targets, action, params, user, skipped=None, job_id=None):
    # 逐批保留执行结果：异常中断时已完成的机器不能变回“0 成功 / 0 失败”。
    results_by_ip = {r["ip"]: r for r in (skipped or [])}
    attempted = set()
    error = ""

    def update_progress():
        rows = list(results_by_ip.values())
        CMD_PROGRESS.update({
            "done": len(rows), "total": len(targets) + len(skipped or []),
            "success": sum(1 for r in rows if r["ok"]),
            "failed": sum(1 for r in rows if not r["ok"]),
            "fail_ips": [r["ip"] for r in rows if not r["ok"]][:100],
            "error": error,
        })

    try:
        def before_group(ips):
            if job_id:
                db.update_command_job_targets(SVC.conn, job_id, ips, "DISPATCHING")
                db.update_command_job(SVC.conn, job_id, "DISPATCHING")
            attempted.update(ips)
            SVC.mark_rebooting(ips)

        def after_group(_grp, group_results):
            # 逐批写审计：分批重启要跑好几分钟，等全部跑完才记的话，中途进程退出(更新重启/
            # 断电)会丢掉已重启机器的记录，24h 次数上限和 15 分钟间隔对它们就失效了
            results_by_ip.update((r["ip"], r) for r in group_results)
            if job_id:
                durable = []
                for result in group_results:
                    state = ("UNCERTAIN" if result.get("uncertain") else
                             "SUCCEEDED" if result.get("ok") else "FAILED")
                    durable.append({**result, "state": state})
                db.update_command_job_targets(SVC.conn, job_id, durable, "FAILED")
                db.update_command_job(SVC.conn, job_id, "DISPATCHING")
            SVC.unmark_rebooting([r["ip"] for r in group_results if not r["ok"]])
            _audit_command(user, action, params, group_results)
            update_progress()

        if skipped:
            _audit_command(user, action, params, skipped)
        update_progress()
        results, err = control.run_batch(targets, action, params, CFG,
                                         before_group=before_group, after_group=after_group)
        results_by_ip.update((r["ip"], r) for r in results)
        if err:
            raise RuntimeError(err)
    except Exception as e:  # noqa: BLE001
        error = "批量任务已中断，请检查未完成机器的状态后再操作"
        unresolved = []
        for ip, _fw in targets:
            if ip not in results_by_ip:
                sent = ip in attempted
                unresolved.append({"ip": ip, "ok": False, "uncertain": sent,
                                   "msg": "执行结果未确认，请检查机器状态" if sent
                                   else "任务已中断，未下发"})
        results_by_ip.update((r["ip"], r) for r in unresolved)
        if job_id:
            durable = [{**r, "state": "UNCERTAIN" if r.get("uncertain") else "CANCELLED"}
                       for r in unresolved]
            try:
                db.update_command_job_targets(SVC.conn, job_id, durable, "CANCELLED")
            except Exception:
                log.exception("批量任务台账更新失败")
        # 未下发的目标不能继续静默；已经开始请求但没有结果的目标保留重启宽限期。
        SVC.unmark_rebooting([r["ip"] for r in unresolved if not r["uncertain"]])
        try:
            _audit_command(user, action, params, unresolved)
        except Exception:
            log.exception("批量中断结果审计写入失败")
        log.exception("async command error: %s", e)
    finally:
        update_progress()
        success, failed = CMD_PROGRESS["success"], CMD_PROGRESS["failed"]
        CMD_PROGRESS["running"] = False
        if job_id:
            try:
                db.update_command_job(SVC.conn, job_id, "INTERRUPTED" if error else "COMPLETED", error)
            except Exception:
                log.exception("批量任务最终状态保存失败")
        SVC._reboot_lock.release()
    _record_command(user, action, params, list(results_by_ip.values()), audited=True)
    log.info("批量 %s %s: 成功 %d / 未成功 %d (发起人 %s)",
             action, "中断" if error else "完成", success, failed, user)


@app.get("/api/command/progress")
def api_command_progress(_: dict = Depends(require_ops)):
    out = dict(CMD_PROGRESS)
    job_id = out.get("job_id")
    if job_id:
        job = db.get_command_job(SVC.conn, job_id)
        if job:
            job.pop("params_json", None)
            out["job"] = job
    return out


@app.get("/api/command/jobs")
def api_command_jobs(limit: int = 50, _: dict = Depends(require_ops)):
    return {"jobs": db.list_command_jobs(SVC.conn, max(1, min(200, limit)))}


@app.get("/api/command/job/{job_id}")
def api_command_job(job_id: str, _: dict = Depends(require_ops)):
    if not re.fullmatch(r"[0-9a-f]{32}", str(job_id)):
        return JSONResponse({"ok": False, "error": "任务 ID 非法"}, status_code=400)
    job = db.get_command_job(SVC.conn, job_id)
    if not job:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    job.pop("params_json", None)
    return {"ok": True, "job": job}


@app.post("/api/command")
def api_command(request: Request, body: dict = Body(...), sess: dict = Depends(require_ops)):
    """远程命令：{ips:[...], action:"reboot|locate|set_pools", params:{...}, confirm:true}

    reboot/set_pools 属破坏性操作，请求体必须显式带 confirm:true（前端"确认执行"弹窗
    负责补上），否则 400。locate 等只读/无害动作不需要。
    """
    user = f'{sess.get("user", "?")}@{_client_ip(request)}'   # 审计带来源 IP：共用账号时也能追到哪台电脑
    action = body.get("action")
    if not CFG.get("control", {}).get("enabled", False):
        _audit_reject(user, action, "控制功能未启用")
        return JSONResponse({"ok": False, "error": "控制功能未启用"}, status_code=403)
    # 非字符串 action(list/dict) 直接参与集合成员判断会抛 TypeError → 500
    if not isinstance(action, str) or action not in control.ACTIONS:
        _audit_reject(user, str(action)[:64], "不支持的命令")
        return JSONResponse({"ok": False, "error": "不支持的命令"}, status_code=400)
    if action in DESTRUCTIVE_ACTIONS and body.get("confirm") is not True:
        _audit_reject(user, action, "缺少 confirm 二次确认")
        return JSONResponse({"ok": False, "need_confirm": True,
                             "error": "该操作具破坏性，请在请求体中显式传入 confirm:true 确认"},
                            status_code=400)
    max_batch = CFG.get("control", {}).get("max_batch", 1000)
    ips, ip_error = control.normalize_ips(body.get("ips"), max_batch)
    if ip_error:
        _audit_reject(user, action, ip_error)
        return JSONResponse({"ok": False, "error": ip_error}, status_code=400)
    # 只允许对本矿场已配置网段内的地址下发（防被当成内网/公网探测器泄露矿机口令）
    ips, out_of_scope = _filter_known_ips(ips)
    if out_of_scope:
        _audit_reject(user, action, f"目标不在已配置网段: {','.join(out_of_scope[:10])}")
        log.warning("命令 %s 被拒: %d 个目标不在已配置网段 (发起人 %s, 示例 %s)",
                    action, len(out_of_scope), user, out_of_scope[:5])
        shown = "、".join(out_of_scope[:5]) + ("…" if len(out_of_scope) > 5 else "")
        return JSONResponse(
            {"ok": False,
             "error": f"以下 {len(out_of_scope)} 个 IP 不在已配置网段内，已拒绝下发: {shown}",
             "rejected": out_of_scope[:100], "rejected_count": len(out_of_scope)},
            status_code=400)
    params = body.get("params") or {}
    if not isinstance(params, dict):
        _audit_reject(user, action, "params 结构非法")
        return JSONResponse({"ok": False, "error": "params 须为对象"}, status_code=400)
    if action == "set_pools":   # 换矿池: 校验 pools 结构与 URL，避免下发非法/恶意配置
        pools = params.get("pools")
        if not isinstance(pools, list) or not pools or len(pools) > 8:
            _audit_reject(user, action, "pools 结构非法")
            return JSONResponse({"ok": False, "error": "pools 需为 1-8 项的列表"}, status_code=400)
        for p in pools:
            if not isinstance(p, dict) or not str(p.get("url", "")).startswith(
                    ("stratum+tcp://", "stratum+ssl://")):
                _audit_reject(user, action, "矿池 url 非法")
                return JSONResponse(
                    {"ok": False, "error": "每个矿池需含 stratum+tcp:// 开头的 url"},
                    status_code=400)
            if not str(p.get("user", "")).strip():   # 缺矿工名会被原样下发→份额无归属
                _audit_reject(user, action, "矿池缺 user")
                return JSONResponse({"ok": False, "error": "每个矿池需填矿工名(user)"},
                                    status_code=400)
        # 矿池白名单：换池 = 把算力送走，账号被盗/内鬼一次就能偷全场。白名单只能在监控电脑的
        # config.yaml 里改，网页(包括 admin)改不了
        allow = CFG.get("control", {}).get("pool_allowlist") or []
        if not allow:
            _audit_reject(user, action, f"未配置矿池白名单: {_pools_desc(params)}")
            return JSONResponse({"ok": False, "error":
                                 "未配置矿池白名单，网页换矿池已禁用。请在监控电脑的 config.yaml 里"
                                 "配置 control.pool_allowlist(如 [\"f2pool.com\"])后重启服务"},
                                status_code=403)
        malformed = [p["url"] for p in pools if not control.pool_host(p["url"])]
        if malformed:
            _audit_reject(user, action, f"矿池地址格式不合格: {', '.join(malformed)[:300]}")
            return JSONResponse({"ok": False, "error":
                                 "矿池地址格式不对，只接受 stratum+tcp://主机名:端口 这种写法: "
                                 + ", ".join(malformed)[:200]}, status_code=400)
        bad = [p["url"] for p in pools if not control.pool_allowed(p["url"], allow)]
        if bad:
            _audit_reject(user, action, f"矿池不在白名单: {', '.join(bad)[:300]}")
            log.warning("换矿池被拒(不在白名单): %s 发起人 %s", bad, user)
            try:
                alerts.push_text(CFG, f"🔴 {html.escape(str(user))} 试图把矿机换到白名单外的矿池，已拒绝: "
                                      f"{html.escape(', '.join(bad)[:300])}")
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse({"ok": False, "error":
                                 f"矿池不在白名单，已拒绝: {', '.join(bad)[:200]}"},
                                status_code=403)
    # 从最近快照取每台固件类型，避免重复探测
    _, recs = _latest_records()
    fw_map = {r["ip"]: r["firmware"] for r in recs}
    targets = [(ip, fw_map.get(ip, "")) for ip in ips]
    if action == "reboot":
        # 限流查询、下发、成功审计必须在同一个共享锁内。
        # 在锁外先查限流时，并发请求能拿着旧配额依次抢锁，重复重启同一台。
        if not SVC._reboot_lock.acquire(blocking=False):
            _audit_reject(user, action, "已有批量命令在执行")
            return JSONResponse({"ok": False, "error": "已有批量重启在执行，请稍候"},
                                status_code=409)
        background_owns_lock = False
        results, skipped = [], []
        try:
            if updater.is_applying():
                return JSONResponse({"ok": False, "error": "程序正在更新或等待重启，请更新完成后再执行矿机重启"},
                                    status_code=409)
            # 上轮命令已下发但审计失败时，先按原执行时刻补记；失败则继续阻止新命令。
            control.flush_reboot_audit(SVC)
            targets, skipped = control.filter_reboot_targets(SVC.conn, targets, CFG)
            ips = [ip for ip, _fw in targets]
            ctl = CFG.get("control", {})
            rb = int(ctl.get("reboot_concurrency", 0) or 0)
            log.info("命令 %s: %d 台(限流跳过 %d), 发起人 %s",
                     action, len(targets), len(skipped), user)
            if rb > 0 and len(targets) > rb:
                job_id = uuid.uuid4().hex
                # Persist the full target ledger before the first device request.
                db.create_command_job(SVC.conn, job_id, user, action, params, targets, skipped)
                CMD_PROGRESS.update({"running": True, "action": action, "done": len(skipped),
                                     "total": len(targets) + len(skipped), "success": 0,
                                     "failed": len(skipped), "fail_ips": [r["ip"] for r in skipped][:100],
                                     "user": user, "error": "", "job_id": job_id})
                try:
                    threading.Thread(target=_run_command_bg,
                                     args=(targets, action, params, user, skipped, job_id),
                                     daemon=True).start()
                except Exception:
                    try:
                        db.update_command_job_targets(SVC.conn, job_id, ips, "CANCELLED")
                        db.update_command_job(SVC.conn, job_id, "INTERRUPTED",
                                              "后台任务线程未能启动；没有下发重启命令")
                    except Exception:
                        log.exception("后台任务启动失败后的台账更新失败")
                    CMD_PROGRESS.update({"running": False, "done": len(targets) + len(skipped),
                                         "failed": len(targets) + len(skipped),
                                         "fail_ips": (ips + [r["ip"] for r in skipped])[:100],
                                         "error": "任务未能启动，没有下发重启命令"})
                    log.exception("启动批量重启线程失败")
                    return JSONResponse({"ok": False, "error": CMD_PROGRESS["error"]},
                                        status_code=503)
                background_owns_lock = True
                return {"ok": True, "async": True, "action": action, "count": len(targets),
                        "skipped": len(skipped), "batch": rb,
                        "delay": ctl.get("reboot_delay_sec", 0), "job_id": job_id}
            SVC.mark_rebooting(ips)
            if targets:
                results, err = control.run_batch(targets, action, params, CFG,
                                                 before_group=SVC.mark_rebooting)
                SVC.unmark_rebooting([r["ip"] for r in results if not r["ok"]] if not err else ips)
                if err:
                    return JSONResponse({"ok": False, "error": err}, status_code=400)
            # 审计落库后才放开锁；Telegram 推送在锁外进行。
            _audit_command(user, action, params, skipped + results)
        except Exception:
            ok_n = sum(1 for r in results if r["ok"])
            log.exception("重启任务或审计失败，已确认下发 %d 台", ok_n)
            if results:
                message = (f"已确认下发 {ok_n} 台，但执行记录保存失败；"
                           "已暂停新的重启，记录恢复后自动解除，请勿重复下发")
            elif getattr(SVC, "_pending_reboot_audit", None):
                message = "上次重启记录尚未保存，暂不下发新命令；请检查数据库或磁盘状态后重试"
            else:
                message = "重启执行结果未确认，请检查机器状态后再操作"
            return JSONResponse({"ok": False, "error": message, "success": ok_n,
                                 "results": results}, status_code=503)
        finally:
            if not background_owns_lock:
                SVC._reboot_lock.release()
        all_results = skipped + results
        _record_command(user, action, params, all_results, audited=True)
        ok_n = sum(1 for r in results if r["ok"])
        return {"ok": True, "action": action, "success": ok_n,
                "failed": len(all_results) - ok_n, "results": all_results,
                "skipped": len(skipped)}
    # 换池/定位同样会改变设备状态，共用重启和更新的闸门。逐批审计落库后才继续下发。
    if not SVC._reboot_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "已有设备命令在执行，请稍候"}, status_code=409)
    results = []
    try:
        if updater.is_applying():
            return JSONResponse({"ok": False, "error": "程序正在更新或等待重启，请更新完成后再执行设备命令"},
                                status_code=409)
        control.flush_reboot_audit(SVC)
        log.info("命令 %s: %d 台, 发起人 %s", action, len(targets), user)

        def after_group(_group, group_results):
            results.extend(group_results)
            _audit_command(user, action, params, group_results)

        returned, err = control.run_batch(targets, action, params, CFG, after_group=after_group)
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        # run_batch 的标准回调已逐批审计；保留完整排序结果供响应/通知使用。
        results = returned
    except Exception:
        ok_n = sum(1 for r in results if r["ok"])
        log.exception("设备命令或审计失败，已确认成功 %d 台", ok_n)
        return JSONResponse({"ok": False, "error": "设备命令已中断，执行记录未保存时暂不下发新命令；请检查机器及数据库状态",
                             "success": ok_n, "results": results}, status_code=503)
    finally:
        SVC._reboot_lock.release()
    _record_command(user, action, params, results, audited=True)
    ok_n = sum(1 for r in results if r["ok"])
    return {"ok": True, "action": action, "success": ok_n,
            "failed": len(results) - ok_n, "results": results}


# 检查结果缓存：每个 admin 页面都会定时查，git fetch 要走外网，别每次都真去拉
_UPDATE_CACHE = {"ts": 0, "data": None}
_UPDATE_CACHE_SEC = 600


@app.get("/api/update/check")
def api_update_check(force: bool = False, _: dict = Depends(require_admin)):
    """对比 git 远端有没有新版本(git 部署时可用)。force=true 跳过 10 分钟缓存。"""
    now = time.time()
    if force or not _UPDATE_CACHE["data"] or now - _UPDATE_CACHE["ts"] > _UPDATE_CACHE_SEC:
        _UPDATE_CACHE["data"] = updater.check((CFG.get("update") or {}).get("branch") or None)
        _UPDATE_CACHE["ts"] = now
    return dict(_UPDATE_CACHE["data"], restart_mode=updater.restart_mode())


@app.post("/api/update/apply")
def api_update_apply(body: dict = Body(default={}), sess: dict = Depends(require_admin)):
    """拉取新代码(ff-only+编译自检+失败回滚)并重启(有守护交给守护，没有就自己拉起)。"""
    log.info("网页触发版本更新，操作人 %s", sess.get("user", "?"))
    r = updater.apply((CFG.get("update") or {}).get("branch") or None,
                      force=bool((body or {}).get("force")))   # 网页上点"仍然重试"
    _UPDATE_CACHE["data"] = None
    log.info("版本更新结果: %s", r.get("msg") or f"{r.get('from')} → {r.get('to')}")
    return r


@app.get("/api/commands")
def api_commands(limit: int = 100, _: dict = Depends(require_admin)):
    return {"commands": db.list_commands(SVC.conn, max(1, min(5000, limit)))}


@app.post("/api/machine-state")
def api_machine_state(request: Request, body: dict = Body(...), sess: dict = Depends(require_ops)):
    """标记维修/取消维修/下架移除：{ips:[...], action:"repair"|"active"|"remove"}"""
    action = body.get("action")
    if action not in ("repair", "active", "remove"):
        return JSONResponse({"ok": False, "error": "参数错误"}, status_code=400)
    ips, ip_error = control.normalize_ips(
        body.get("ips"), CFG.get("control", {}).get("max_batch", 1000))
    if ip_error:
        return JSONResponse({"ok": False, "error": ip_error}, status_code=400)
    if action == "remove":
        db.remove_miners(SVC.conn, ips)
        SVC.drop_from_snapshot(ips)   # 内存快照同步剔除，否则要等下轮扫描才消失
    else:
        db.set_machine_state(SVC.conn, ips, action)
    db.log_commands(SVC.conn, f'{sess.get("user", "?")}@{_client_ip(request)}', "state:" + action,
                    [{"ip": ip, "ok": True, "msg": action} for ip in ips])
    log.info("机器状态 %s: %d 台, 操作人 %s", action, len(ips), sess.get("user", "?"))
    return {"ok": True, "action": action, "count": len(ips)}


@app.get("/")
@app.api_route("/web/index.html", methods=["GET", "HEAD"], include_in_schema=False)
def index():
    return HTMLResponse(index_html(WEB_DIR))


def _host_name(host_header):
    h = str(host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


_MY_NAMES = None


def _my_names():
    """本机名只取一次：getfqdn() 在 Windows 上可能做反向 DNS，每个请求都查会卡几秒。"""
    global _MY_NAMES
    if _MY_NAMES is None:
        import socket
        names = {"localhost"}
        for f in (socket.gethostname, socket.getfqdn):
            try:
                names.add(f().lower().rstrip("."))
            except OSError:
                pass
        _MY_NAMES = names
    return _MY_NAMES


def _via_trusted_proxy(request):
    peer = request.client.host if request.client else ""
    if not (_TRUSTED and peer):
        return False
    try:
        return any(ipaddress.ip_address(peer) in n for n in _TRUSTED)
    except ValueError:
        return False


_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host")


def _is_console(request, src):
    """是不是坐在监控电脑前的人：来源是回环地址 + 没有代理头 + 用 127.0.0.1/localhost 打开。
    同机跑着 nginx/frp(http) 转发时，外部请求也来自 127.0.0.1，但会带代理头/外部 Host，
    不能当本机(否则"弱口令只准本机登录"和"本机不锁"都能被远程绕过)。"""
    if not auth.is_local(src) or any(h in request.headers for h in _PROXY_HEADERS):
        return False
    if _via_trusted_proxy(request):   # 同机 nginx 默认不加 X-Forwarded-*、Host 也可能是 127.0.0.1
        return False
    name = _host_name(request.headers.get("host", ""))
    return name in ("localhost", "::1") or name.startswith("127.")


def _host_ok(host_header):
    """防 DNS 重绑定：恶意网页把自己的域名解析到 127.0.0.1/本机 IP 后就成了"同源"，
    能在运维浏览器里调接口。只放行 IP 字面量、localhost、本机名和 server.allowed_hosts。"""
    name = _host_name(host_header).rstrip(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    allowed = set(_my_names())
    allowed |= {str(x).strip().lower().rstrip(".")
                for x in CFG["server"].get("allowed_hosts") or []}
    return "*" in allowed or name in allowed


def _same_origin(origin, host_header):
    o = str(origin or "").strip().lower()
    if "://" not in o:
        return False
    return o.split("://", 1)[1].split("/", 1)[0] == str(host_header or "").strip().lower()


@app.middleware("http")
async def _request_guard(request: Request, call_next):
    """所有请求先过这道：Host 白名单(防 DNS 重绑定) + 写请求防跨站(CSRF)。
    正常用 IP/本机名打开面板的运维完全无感。"""
    host = request.headers.get("host", "")
    proxied = _via_trusted_proxy(request)   # 经 trusted_proxies 里的 nginx/frp 进来：Host 是对外域名
    if not proxied and not _host_ok(host):
        return JSONResponse({"ok": False, "error":
                             f"不允许用 {_host_name(host)[:64]} 访问：请用 IP 打开面板，或把该域名加到 "
                             "config.yaml 的 server.allowed_hosts"}, status_code=400)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and not proxied and not _same_origin(origin, host):   # 别的网站页面发来的写请求
            log.warning("拒绝跨站写请求: %s %s Origin=%s", request.method, request.url.path,
                        origin[:100])
            return JSONResponse({"ok": False, "error": "跨站请求已拒绝"}, status_code=403)
        # 带请求体的写请求必须是 JSON：表单/text/plain/无类型的 Blob 是跨站伪造请求的
        # 常用手法(不触发预检)，而面板前端只发 application/json
        has_body = request.headers.get("content-length", "0") not in ("", "0") \
            or "transfer-encoding" in request.headers
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if has_body and ctype != "application/json":
            return JSONResponse({"ok": False, "error": "请求体必须是 JSON"}, status_code=415)
    return await call_next(request)


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """前端文件禁强缓存(每次向服务器复核, 未变返回304)——否则更新版本后浏览器
    拿旧 JS 渲染新接口, 要手动 Ctrl+F5 才恢复。文件只有几十KB, 代价可忽略。"""
    resp = await call_next(request)
    if request.url.path in ("/", "/web/index.html"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/web/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


if os.path.isdir(WEB_DIR):
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


def _tls_files():
    """(cert, key)：两个都配了且文件存在才启用 HTTPS，否则 None。"""
    c, k = CFG["server"].get("tls_cert") or "", CFG["server"].get("tls_key") or ""
    if c and k and os.path.isfile(c) and os.path.isfile(k):
        return c, k
    return None


def _local_ipv4s():
    """本机可用的局域网 IPv4(去掉回环/自动私有地址)，默认路由那张网卡排最前。"""
    import socket
    ips = []
    try:   # UDP connect 不发包，只让系统选出走默认路由的网卡地址
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips += [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    out = []
    for ip in ips:
        if ip not in out and not ip.startswith(("127.", "169.254.", "0.")):
            out.append(ip)
    return out


def _announce_and_open_browser(host, port):
    """启动时打印面板访问地址；Windows 下等端口就绪后自动打开本机浏览器。
    run.bat 守护重启(含自动更新)时会带 MINER_NO_BROWSER=1，不会每次重启都弹一个新窗口。"""
    import socket
    import webbrowser
    scheme = "https" if _tls_files() else "http"
    local_url = f"{scheme}://127.0.0.1:{port}" if host in ("0.0.0.0", "", "127.0.0.1") \
        else f"{scheme}://{host}:{port}"
    lan = _local_ipv4s() if host in ("0.0.0.0", "") else []
    log.info("=" * 64)
    log.info("面板地址(本机): %s", local_url)
    for ip in lan:
        log.info("面板地址(局域网其它电脑): %s://%s:%d", scheme, ip, port)
    log.info("=" * 64)
    if not CFG["server"].get("open_browser", True) or os.environ.get("MINER_NO_BROWSER"):
        return
    if os.name != "nt":   # Linux 服务器多半无桌面，webbrowser 可能拉起终端文本浏览器占住控制台
        return
    if not (sys.stdin and sys.stdin.isatty()):   # NSSM 等服务(session 0)没有交互桌面，开了也看不见
        return

    def _wait_then_open():
        target = "127.0.0.1" if "://127." in local_url else host
        for _ in range(120):   # 大库首次 checkpoint 可能让启动慢一些，最多等 60 秒
            try:
                with socket.create_connection((target, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            return
        try:
            webbrowser.open(local_url)
        except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
            log.warning("自动打开浏览器失败，请手动访问 %s", local_url)

    threading.Thread(target=_wait_then_open, daemon=True).start()


def _wait_port_free(host, port, timeout=30):
    """网页更新后自己拉起的新进程：等旧进程退出放开端口再监听，否则绑定失败直接退出。"""
    import socket
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((target, port), timeout=0.5):
                pass
        except OSError:
            return
        time.sleep(0.5)


if __name__ == "__main__":
    import uvicorn
    if os.environ.pop("MINER_WAIT_PORT_FREE", None):
        _wait_port_free(CFG["server"]["host"], CFG["server"]["port"])
    _announce_and_open_browser(CFG["server"]["host"], CFG["server"]["port"])
    tls = _tls_files()
    if (CFG["server"].get("tls_cert") or CFG["server"].get("tls_key")) and not tls:
        log.error("server.tls_cert/tls_key 配了但文件不存在，仍以 HTTP 启动")
    uvicorn.run(app, host=CFG["server"]["host"], port=CFG["server"]["port"],
                ssl_certfile=tls[0] if tls else None, ssl_keyfile=tls[1] if tls else None,
                log_config=None)   # 日志统一交给 logs.py
