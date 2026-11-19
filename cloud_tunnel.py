#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端远程面板转发线程：让云端管理员能在云端点开本场地的本地面板。

方向仍是本地主动连云端（场地在 NAT 后面，无需任何端口映射）：
  本线程长轮询云端 /api/ingest/tunnel/poll 等活；云端有人打开本场地面板时把请求交给本线程，
  本线程在本机 127.0.0.1 上访问本地面板，再把响应回传 /api/ingest/tunnel/respond。

默认关闭，config.yaml：
cloud:
  tunnel: true        # 允许云端管理员通过云端打开本场地面板(默认 false)

安全要点：
- 只访问本机本面板(127.0.0.1:自己的端口)，不会被云端指到别的地址；
- 转发进来的请求带一个只有本进程知道的暗号，面板据此把它当作"远程来源"：
  不享受"坐在监控电脑前"的本机特权，登录失败锁定照常生效；
- 面板自己的登录照常要求：云端只是搬运请求，看不到、也绕不过本地账号密码；
- 一次最多并发处理 8 个请求；回包超过 8MB 拒绝。
"""
import secrets
import threading
import time

import requests

import cloud_report
import logs

log = logs.get(__name__)

_SECRET = secrets.token_hex(16)      # 进程内暗号：server.py 凭它识别转发请求，外部无从得知
MARK_HEADER = "x-am-tunnel"
CLIENT_HEADER = "x-co-client-ip"
MAX_BODY = 8 * 1024 * 1024
MAX_WORKERS = 8
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
# 不原样带给本地面板的头：逐跳头，以及会让面板误判来源的头
_SKIP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
                 "upgrade", "te", "trailer", "origin", "referer", "x-forwarded-for",
                 "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "forwarded",
                 MARK_HEADER, CLIENT_HEADER}

_GEN = 0
_thread = None
_STATUS = {"state": "disabled", "last_poll_ok": 0, "last_err": "", "served": 0}
_slock = threading.Lock()


def secret():
    return _SECRET


def status():
    with _slock:
        return dict(_STATUS)


def _set(**kw):
    with _slock:
        _STATUS.update(kw)


def wanted(cfg):
    c = cfg.get("cloud") or {}
    return bool(cloud_report.enabled(cfg) and c.get("tunnel"))


def is_alive():
    return bool(_thread and _thread.is_alive())


def start(cfg):
    """(重新)启动转发线程；未启用则只停掉旧线程。与 cloud_report.start 同一套热重启语义。"""
    global _GEN, _thread
    _GEN += 1
    gen = _GEN
    _thread = None
    if not wanted(cfg):
        _set(state="disabled", last_err="")
        return None
    srv = cfg.get("server") or {}
    port = int(srv.get("port") or 8800)
    tls = bool(srv.get("tls_cert") and srv.get("tls_key"))
    base = f"{'https' if tls else 'http'}://127.0.0.1:{port}"
    c = dict(cfg.get("cloud") or {})
    sid = cloud_report.site_id(cfg)
    _set(state="connecting", last_err="")
    t = threading.Thread(target=_loop, args=(gen, c, sid, base), daemon=True, name="cloud-tunnel")
    _thread = t
    t.start()
    log.info("云端远程面板转发已启用(本机面板 %s)", base)
    return t


def _sleep(gen, secs):
    end = time.time() + secs
    while gen == _GEN and time.time() < end:
        time.sleep(min(0.5, max(0.0, end - time.time())))


def _loop(gen, c, sid, base):
    sess = cloud_report._session(c)
    url = c["url"].rstrip("/")
    auth = {"Authorization": f"Bearer {c.get('token', '')}"}
    slots = threading.BoundedSemaphore(MAX_WORKERS)
    backoff = 3
    while gen == _GEN:
        if not slots.acquire(timeout=1):
            continue
        handed = False
        try:
            r = sess.post(url + "/api/ingest/tunnel/poll", json={"site_id": sid},
                          headers=auth, timeout=40)
            if r.status_code in (404, 410):
                # 云端不支持转发/场地已删除/未登记：别狂刷，过一会儿再试
                _set(state="unsupported", last_err=f"云端返回 {r.status_code}(云端未升级或场地未登记)")
                _sleep(gen, 60)
                continue
            r.raise_for_status()
            backoff = 3
            _set(state="online", last_poll_ok=int(time.time()), last_err="")
            job = (r.json() or {}).get("job")
            if job:
                handed = True
                threading.Thread(target=_serve, args=(gen, sess, url, auth, sid, base, job, slots),
                                 daemon=True, name="cloud-tunnel-req").start()
        except Exception as e:  # noqa: BLE001 - 转发失败绝不影响本地监控
            _set(state="error", last_err=cloud_report.explain(e))
            _sleep(gen, backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if not handed:
                slots.release()


def _serve(gen, sess, url, auth, sid, base, job, slots):
    try:
        status, headers, body = _call_local(base, job)
        payload = {"site_id": sid, "id": job.get("id"), "status": status,
                   "headers": headers, "body_b64": _b64(body)}
        sess.post(url + "/api/ingest/tunnel/respond", json=payload, headers=auth, timeout=30)
        with _slock:
            _STATUS["served"] += 1
    except Exception as e:  # noqa: BLE001
        log.warning("远程面板请求处理失败: %s", e)
    finally:
        slots.release()


def _b64(data):
    import base64
    return base64.b64encode(data or b"").decode("ascii")


def _call_local(base, job):
    """在本机面板上执行一个转发请求 → (status, [[k,v],...], body)。"""
    import base64
    method = str(job.get("method") or "GET").upper()
    path = str(job.get("path") or "/")
    if method not in _METHODS or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return 400, [["content-type", "text/plain; charset=utf-8"]], b"bad request"
    hdrs = {k: v for k, v in (job.get("headers") or {}).items()
            if isinstance(k, str) and isinstance(v, str) and k.lower() not in _SKIP_HEADERS}
    client_ip = str((job.get("headers") or {}).get(CLIENT_HEADER) or "unknown")[:64]
    hdrs[MARK_HEADER] = _SECRET
    hdrs[CLIENT_HEADER] = client_ip
    hdrs["X-Forwarded-For"] = client_ip         # 面板据此不把请求当作"本机操作员"
    try:
        body = base64.b64decode(job.get("body_b64") or "")
    except Exception:  # noqa: BLE001
        return 400, [["content-type", "text/plain; charset=utf-8"]], b"bad body"
    try:
        r = requests.request(method, base + path, headers=hdrs, data=body or None,
                             timeout=25, allow_redirects=False, verify=False, stream=True)
    except requests.RequestException as e:
        return 502, [["content-type", "text/plain; charset=utf-8"]], f"本地面板无法访问: {e}".encode()
    try:
        raw = r.raw.read(MAX_BODY + 1, decode_content=True)
    finally:
        r.close()
    if len(raw) > MAX_BODY:
        return 502, [["content-type", "text/plain; charset=utf-8"]], b"response too large"
    out = [[k, v] for k, v in r.headers.items() if k.lower() != "set-cookie"]
    try:
        for v in r.raw.headers.getlist("Set-Cookie"):
            out.append(["set-cookie", v])
    except Exception:  # noqa: BLE001
        pass
    return r.status_code, out, raw
