#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""矿机网页控制台跳转：按固件给出打开方式（原厂 Digest / UniPlus 密码框）。

浏览器不能跨域替矿机填登录态，因此：
- stock：用 http://user:pass@ip/ 让浏览器带 Digest
- uniplus：由本服务提供 bridge 页，向矿机 POST pwd=… 写入 auth_token cookie 后再进 /dashboard
"""
from __future__ import annotations

import ipaddress
import re
from html import escape
from urllib.parse import quote

import requests

import logs

log = logs.get(__name__)

_UNIPLUS_META = re.compile(r'content=["\']UniPlusOS["\']', re.I)
_DEFAULT_STOCK = ("root", "root")
_DEFAULT_UNIPLUS_PWD = "admin"


def validate_miner_ip(ip: str) -> str:
    """只允许 IPv4 字面量，防止 SSRF / 奇怪跳转。"""
    text = str(ip or "").strip()
    addr = ipaddress.ip_address(text)
    if addr.version != 4:
        raise ValueError("仅支持 IPv4")
    if addr.is_unspecified or addr.is_multicast or addr.is_reserved:
        raise ValueError("非法 IP")
    return text


def looks_like_uniplus_html(text: str) -> bool:
    if not text:
        return False
    if _UNIPLUS_META.search(text):
        return True
    low = text.lower()
    return "firmware console" in low and "uniplus" in low


def detect_console_kind(ip: str, firmware_hint: str = "", timeout: float = 2.0) -> str:
    """活体分辨 stock / uniplus；失败时回退到扫描记录里的 firmware_hint。"""
    hint = (firmware_hint or "").strip().lower()
    try:
        r = requests.get(f"http://{ip}/", timeout=timeout)
        auth = (r.headers.get("WWW-Authenticate") or "").lower()
        if r.status_code == 401 and "digest" in auth:
            return "stock"
        body = r.text or ""
        if looks_like_uniplus_html(body):
            return "uniplus"
        if r.status_code == 200 and "digest" not in auth:
            # 部分 UniPlus 壳没有 meta 时，再探登录口
            try:
                login = requests.post(
                    f"http://{ip}/cgi-bin/login.cgi",
                    data={"pwd": "__antmonitor_probe__"},
                    timeout=timeout,
                    headers={"Accept": "application/json"},
                )
                if login.status_code in (200, 401) and "json" in (login.headers.get("Content-Type") or ""):
                    return "uniplus"
            except requests.RequestException:
                pass
    except requests.RequestException as exc:
        log.debug("detect_console_kind %s failed: %s", ip, exc)
    if hint == "uniplus":
        return "uniplus"
    if hint == "stock":
        return "stock"
    return hint or "unknown"


def console_plan(ip: str, firmware_hint: str = "",
                 stock_user: str = _DEFAULT_STOCK[0],
                 stock_password: str = _DEFAULT_STOCK[1],
                 uniplus_password: str = _DEFAULT_UNIPLUS_PWD,
                 live: bool = True) -> dict:
    """返回前端/bridge 使用的打开方案。"""
    ip = validate_miner_ip(ip)
    kind = detect_console_kind(ip, firmware_hint) if live else (
        "uniplus" if (firmware_hint or "").lower() == "uniplus" else
        "stock" if (firmware_hint or "").lower() == "stock" else "unknown"
    )
    if kind == "stock":
        user = quote(stock_user or _DEFAULT_STOCK[0], safe="")
        pwd = quote(stock_password or _DEFAULT_STOCK[1], safe="")
        return {
            "ip": ip,
            "kind": "stock",
            "open_url": f"http://{user}:{pwd}@{ip}/",
            "fallback_url": f"http://{ip}/",
            "username": stock_user or _DEFAULT_STOCK[0],
            "password": stock_password or _DEFAULT_STOCK[1],
            "method": "digest_url",
        }
    if kind == "uniplus":
        return {
            "ip": ip,
            "kind": "uniplus",
            "open_url": f"http://{ip}/dashboard",
            "fallback_url": f"http://{ip}/dashboard",
            "username": "",
            "password": uniplus_password or _DEFAULT_UNIPLUS_PWD,
            "login_url": f"http://{ip}/cgi-bin/login.cgi",
            "method": "uniplus_bridge",
        }
    return {
        "ip": ip,
        "kind": "unknown",
        "open_url": f"http://{ip}/",
        "fallback_url": f"http://{ip}/",
        "username": stock_user or _DEFAULT_STOCK[0],
        "password": uniplus_password or _DEFAULT_UNIPLUS_PWD,
        "method": "plain",
    }


def uniplus_login(ip: str, password: str = _DEFAULT_UNIPLUS_PWD, timeout: float = 3.0) -> str:
    """向矿机 POST pwd=…，返回 token；失败返回空串。"""
    ip = validate_miner_ip(ip)
    pwd = password or _DEFAULT_UNIPLUS_PWD
    try:
        r = requests.post(
            f"http://{ip}/cgi-bin/login.cgi",
            data={"pwd": pwd},
            timeout=timeout,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        data = r.json() if "json" in (r.headers.get("Content-Type") or "") else {}
        if not isinstance(data, dict):
            data = {}
        token = str(data.get("token") or "").strip()
        if r.ok and token:
            return token
        log.info("uniplus_login %s failed: status=%s body=%s", ip, r.status_code, str(data)[:120])
    except (requests.RequestException, ValueError) as exc:
        log.info("uniplus_login %s error: %s", ip, exc)
    return ""


def uniplus_autologin_url(ip: str, token: str) -> str:
    """UniPlus SPA 启动时会读 location.search / hash 里的 token|auth_token，写入 sessionStorage。

    用 hash（#?token=）避免 token 进 access log / Referer；SPA 读完会 replaceState 清掉。
    """
    ip = validate_miner_ip(ip)
    tok = quote(str(token or "").strip(), safe="")
    return f"http://{ip}/dashboard#?token={tok}"


def uniplus_bridge_html(ip: str, password: str = _DEFAULT_UNIPLUS_PWD) -> str:
    """服务端代登拿 token，再跳到矿机 dashboard#?token=…（写入 sessionStorage，免输密码）。

    仅靠 cookie / iframe POST 不够：UniPlus 登录门闸只认 sessionStorage.uniplus_auth_token。
    """
    ip = validate_miner_ip(ip)
    pwd = password or _DEFAULT_UNIPLUS_PWD
    dash = f"http://{ip}/dashboard"
    token = uniplus_login(ip, pwd)
    if token:
        target = uniplus_autologin_url(ip, token)
        return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <title>打开矿机网页 {escape(ip)}</title>
  <meta http-equiv="refresh" content="2;url={escape(dash)}">
  <style>
    body {{ font-family: system-ui, sans-serif; background:#0d1117; color:#e6edf3;
           display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
    .box {{ max-width: 28rem; padding: 1.25rem 1.5rem; border:1px solid #30363d; border-radius:10px;
            background:#161b22; line-height:1.5; }}
    a {{ color:#58a6ff; }}
    code {{ background:#21262d; padding:0.1rem 0.35rem; border-radius:4px; }}
  </style>
</head>
<body>
  <div class="box">
    <p>已登录 <code>{escape(ip)}</code>（UniPlus），正在进入看板…</p>
    <p><a href="{escape(target)}">若未跳转，点此进入</a></p>
  </div>
  <script>window.location.replace({target!r});</script>
</body>
</html>
"""
    # 代登失败（密码改过/机子不通）：仍打开看板，并提示默认密码
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <title>打开矿机网页 {escape(ip)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background:#0d1117; color:#e6edf3;
           display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
    .box {{ max-width: 28rem; padding: 1.25rem 1.5rem; border:1px solid #30363d; border-radius:10px;
            background:#161b22; line-height:1.5; }}
    a {{ color:#58a6ff; }}
    code {{ background:#21262d; padding:0.1rem 0.35rem; border-radius:4px; }}
  </style>
</head>
<body>
  <div class="box">
    <p>自动登录 <code>{escape(ip)}</code> 失败（密码可能已改，或机器暂时不可达）。</p>
    <p>默认密码 <code>{escape(pwd)}</code> 已尽量复制；请在看板密码框粘贴。</p>
    <p><a href="{escape(dash)}">打开看板</a></p>
  </div>
  <script>
    (function () {{
      var pwd = {pwd!r}, dash = {dash!r};
      try {{ navigator.clipboard.writeText(pwd); }} catch (e) {{}}
      setTimeout(function () {{ window.location.replace(dash); }}, 600);
    }})();
  </script>
</body>
</html>
"""