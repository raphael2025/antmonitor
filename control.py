#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""矿机远程命令层（写操作）：重启 / 定位灯 / 换矿池。

安全约束：
- 受 config.control.enabled 总开关控制，关闭则一律拒绝。
- 重启/换矿池为破坏性操作，前端强制二次确认；本层只负责执行。
- 所有命令写入 command_log 审计表。
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.auth import HTTPDigestAuth

import miner_core

ACTIONS = {"reboot", "locate", "set_pools"}


def _stock(ip, action, params, passwords, timeout):
    s = requests.Session(); s.trust_env = False

    def post(path, **kw):
        last = None
        for u, p in passwords:
            try:
                r = s.post(f"http://{ip}/cgi-bin/{path}", auth=HTTPDigestAuth(u, p),
                           timeout=timeout, **kw)
            except requests.RequestException as e:
                return None, str(e)
            if r.status_code != 401:
                return r, ""
            last = r
        return last, "密码无效(401)"

    def get_conf():
        for u, p in passwords:
            try:
                r = s.get(f"http://{ip}/cgi-bin/get_miner_conf.cgi",
                          auth=HTTPDigestAuth(u, p), timeout=timeout)
                if r.status_code == 200:
                    return r.json()
            except (requests.RequestException, ValueError):
                pass
        return None

    if action == "reboot":
        r, msg = post("reboot.cgi")
        return (r is not None and r.status_code == 200), msg or "reboot ok"
    if action == "locate":
        on = bool(params.get("on"))
        r, msg = post("blink.cgi", json={"blink": "true" if on else "false"})
        return (r is not None and r.status_code == 200), msg or f"locate={on}"
    if action == "set_pools":
        conf = get_conf()
        if conf is None:
            return False, "读取现有配置失败"
        conf["pools"] = params["pools"]
        r, msg = post("set_miner_conf.cgi", json=conf)
        return (r is not None and r.status_code == 200), msg or "pools updated"
    return False, "不支持的命令"


def _uniplus(ip, action, params, uni_pw, timeout):
    s = requests.Session(); s.trust_env = False
    base = f"http://{ip}/api/v1"
    try:
        if action == "locate":
            on = bool(params.get("on"))
            r = s.post(f"{base}/find-miner", json={"find": on}, timeout=timeout)
            return r.status_code == 200, f"locate={on}"
        # reboot / set_pools 需要解锁
        if uni_pw:
            s.post(f"{base}/unlock", json={"pw": uni_pw}, timeout=timeout)
        if action == "reboot":
            r = s.post(f"{base}/system/reboot", timeout=timeout)
            if r.status_code == 401:
                return False, "需要 uniplus 解锁密码"
            return r.status_code in (200, 204), "reboot ok"
        if action == "set_pools":
            r = s.post(f"{base}/settings", json={"pools": params["pools"]}, timeout=timeout)
            if r.status_code == 401:
                return False, "需要 uniplus 解锁密码"
            return r.status_code in (200, 204), "pools updated"
    except requests.RequestException as e:
        return False, str(e)
    return False, "不支持的命令"


def run_one(ip, firmware, action, params, cfg):
    ctl = cfg.get("control", {})
    if not ctl.get("enabled", False):
        return {"ip": ip, "ok": False, "msg": "控制功能未启用"}
    timeout = ctl.get("timeout", 8)
    passwords = [tuple(p) for p in cfg["scan"].get("passwords", [["root", "root"]])]
    uni_pw = ctl.get("uniplus_password", "")

    # 固件未知则现场探测（会话不入全局池，避免被扫描线程的 close_sessions() 跨线程误关正在用的连接）
    if firmware not in ("stock", "uniplus"):
        miner_core._local.no_register = True
        try:
            rec = miner_core.probe(ip, cfg["scan"])
        finally:
            miner_core._local.no_register = False
        firmware = rec.get("firmware")

    try:
        if firmware == "stock":
            ok, msg = _stock(ip, action, params, passwords, timeout)
        elif firmware == "uniplus":
            ok, msg = _uniplus(ip, action, params, uni_pw, timeout)
        else:
            ok, msg = False, "离线或未知固件"
    except Exception as e:  # noqa: BLE001 - 单机失败不影响批量
        ok, msg = False, f"异常:{e}"
    return {"ip": ip, "ok": ok, "msg": msg}


def _run_group(group, action, params, cfg):
    out = []
    with ThreadPoolExecutor(max_workers=min(64, max(4, len(group)))) as ex:
        futs = [ex.submit(run_one, ip, fw, action, params, cfg) for ip, fw in group]
        for f in futs:
            out.append(f.result())
    return out


def run_batch(targets, action, params, cfg, progress=None):
    """targets: [(ip, firmware), ...]；并发执行，返回结果列表。
    reboot 时按 control.reboot_* 打乱+分批+延迟，避免同变压器机器同时重启的浪涌跳闸。
    progress(done, total) 可选回调(后台异步执行时上报进度)。"""
    if action not in ACTIONS:
        return [], "不支持的命令"
    ctl = cfg.get("control", {})
    targets = list(targets)
    batch = int(ctl.get("reboot_concurrency", 0) or 0) if action == "reboot" else 0
    delay = float(ctl.get("reboot_delay_sec", 0) or 0)

    out = []
    if batch and batch > 0 and len(targets) > batch:
        if ctl.get("reboot_shuffle", True):
            random.shuffle(targets)   # 打乱：把同变压器的连号机器分散到不同批次/时间
        groups = [targets[i:i + batch] for i in range(0, len(targets), batch)]
        for gi, grp in enumerate(groups):
            out.extend(_run_group(grp, action, params, cfg))
            if progress:
                progress(len(out), len(targets))
            if delay and gi < len(groups) - 1:
                time.sleep(delay)
    else:
        out = _run_group(targets, action, params, cfg)
        if progress:
            progress(len(out), len(targets))
    out.sort(key=lambda r: tuple(int(x) for x in r["ip"].split(".")) if r["ip"].count(".") == 3 else (0,))
    return out, ""
