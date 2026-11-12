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


def uniplus_bridge_html(ip: str, password: str = _DEFAULT_UNIPLUS_PWD) -> str:
    """在新窗口里 POST pwd 到矿机（写入 auth_token cookie），再跳进 /dashboard。"""
    ip = validate_miner_ip(ip)
    pwd = password or _DEFAULT_UNIPLUS_PWD
    login = f"http://{ip}/cgi-bin/login.cgi"
    dash = f"http://{ip}/dashboard"
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
    <p>正在登录 <code>{escape(ip)}</code>（UniPlus）…</p>
    <p id="msg">写入会话后将进入看板。</p>
    <p><a id="manual" href="{escape(dash)}">若未跳转，点此进入看板</a></p>
  </div>
  <iframe name="antminer_login_frame" style="display:none" title="login"></iframe>
  <form id="f" method="POST" action="{escape(login)}" target="antminer_login_frame"
        enctype="application/x-www-form-urlencoded">
    <input type="hidden" name="pwd" value="{escape(pwd)}">
  </form>
  <script>
    (function () {{
      var dash = {dash!r};
      var msg = document.getElementById("msg");
      try {{ document.getElementById("f").submit(); }}
      catch (e) {{ msg.textContent = "自动登录失败，请手动打开看板并输入密码 admin。"; return; }}
      setTimeout(function () {{ window.location.replace(dash); }}, 700);
      setTimeout(function () {{
        msg.textContent = "若仍停在登录页，密码默认是 admin（可改过）。";
      }}, 2500);
    }})();
  </script>
</body>
</html>
"""
