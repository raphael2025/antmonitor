#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""矿机远程命令层（写操作）：重启 / 定位灯 / 换矿池。

安全约束：
- 受 config.control.enabled 总开关控制，关闭则一律拒绝。
- 重启/换矿池为破坏性操作，前端强制二次确认；本层只负责执行。
- 所有命令写入 command_log 审计表。
"""
import ipaddress
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.auth import HTTPDigestAuth
from urllib3.exceptions import ProtocolError

import appconfig
import miner_core

ACTIONS = {"reboot", "locate", "set_pools"}


# 只认严格的 stratum+tcp|ssl://host[:port][/]：host 只含字母数字.-，端口 1~5 位数字。
# 不能"从一个怪地址里猜主机名"：cgminer/bmminer 按第一个 ':' 截主机、端口缓冲只有 5 位，
# stratum+tcp://evil.com:03333@f2pool.com 在我们这边若按 '@' 取主机会看成 f2pool.com，
# 矿机实际却连 evil.com。任何可能让两边理解不一致的写法(@ ? # % \ 空白…)一律不合格
_POOL_RE = re.compile(r"^stratum\+(?:tcp|ssl)://([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.?)"
                      r"(?::(\d{1,5}))?/?$")


def pool_host(url):
    """合格的矿池地址 → host(小写、去尾点)；不合格返回 ""。"""
    m = _POOL_RE.match(str(url or "").strip())
    if not m:
        return ""
    if m.group(2) is not None and not 1 <= int(m.group(2)) <= 65535:
        return ""
    return m.group(1).lower().rstrip(".")


def pool_allowed(url, allowlist):
    """矿池地址是否在白名单内。白名单项 "f2pool.com" 同时放行其子域名(btc.f2pool.com)，
    但不放行 evilf2pool.com / f2pool.com.evil.io。地址格式不合格、白名单为空 → 一律不放行。"""
    host = pool_host(url)
    if not host:
        return False
    for a in allowlist or []:
        a = str(a).strip().lower().lstrip("*.").rstrip(".")
        if a and (host == a or host.endswith("." + a)):
            return True
    return False


def _seg_prefix(seg):
    """把一条网段配置(如 "172.16.5" / "172.16.5.x")归一成 "172.16.5"；非法返回 None。

    解析规则与 miner_core.gen_ips_seg() 保持一致(取前三段)，只额外做数值归一，
    保证"扫描时会生成的 IP"与"命令校验放行的 IP"是同一个集合。
    """
    parts = str(seg).strip().split(".")
    if len(parts) < 3:
        return None
    try:
        octets = [int(p) for p in parts[:3]]
    except (TypeError, ValueError):
        return None
    if any(o < 0 or o > 255 for o in octets):
        return None
    return ".".join(str(o) for o in octets)


def known_ip_scope(cfg):
    """返回 (网段前缀集合, host_start, host_end)——即本矿场"允许被下发命令"的地址范围。

    直接复用 appconfig.load_segments(cfg)，与扫描线程用的是同一份网段配置。
    """
    segs, hs, he = appconfig.load_segments(cfg)
    prefixes = set()
    for s in segs or []:
        p = _seg_prefix(s)
        if p:
            prefixes.add(p)
    try:
        hs, he = int(hs), int(he)
    except (TypeError, ValueError):
        hs, he = 1, 254
    if he < hs:
        hs, he = he, hs
    return prefixes, hs, he


def is_known_ip(ip, scope):
    """ip 是否落在 scope(known_ip_scope() 的返回值)描述的网段范围内。"""
    prefixes, hs, he = scope
    parts = str(ip).split(".")
    if len(parts) != 4:
        return False
    try:
        host = int(parts[3])
    except (TypeError, ValueError):
        return False
    return ".".join(parts[:3]) in prefixes and hs <= host <= he


def filter_known_ips(ips, cfg):
    """按矿场配置网段过滤命令目标，返回 (allowed, rejected) 两个 IP 字符串列表。

    allowed  = 落在任意一个配置网段(前缀 + host_start~host_end)内的 IP，保持入参顺序；
    rejected = 其余 IP(如 127.0.0.1、公网地址、内网其它业务系统)，调用方应拒绝执行
               并把这些 IP 回显给用户，切勿静默丢弃。

    这是防 SSRF/凭证外泄的关键一层：本模块会对目标发起带 Digest 认证的 HTTP 请求，
    目标一旦超出矿场网段，监控服务器就成了内网扫描跳板，矿机管理密码也会被送到
    攻击者可控的服务上。网段配置为空时一律判为 rejected(失败关闭，不放行)。
    """
    scope = known_ip_scope(cfg)
    allowed, rejected = [], []
    for ip in ips or []:
        (allowed if is_known_ip(ip, scope) else rejected).append(ip)
    return allowed, rejected


def normalize_ips(ips, max_batch, cfg=None):
    """校验 IPv4 目标并按原顺序去重，防止同一台机器被重复执行命令。

    返回 (ip_list, error_msg)；error_msg 非空时 ip_list 为 None。

    传入 cfg 时(推荐，所有对外接口都应传)会额外做网段校验：只要有任何一个目标
    不在配置网段内，整个请求被拒绝(fail closed)，错误信息里列出越界的 IP。
    需要"部分放行"的调用方请改用 filter_known_ips()。
    cfg 省略时行为与旧版完全一致(仅格式校验)，保持向后兼容。
    """
    if not isinstance(ips, list) or not ips:
        return None, "未指定目标矿机"
    out = []
    seen = set()
    for raw in ips:
        if not isinstance(raw, str):
            return None, "目标 IP 必须是字符串"
        try:
            ip = str(ipaddress.IPv4Address(raw.strip()))
        except ipaddress.AddressValueError:
            return None, f"非法 IPv4 地址: {str(raw)[:64]}"
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    if len(out) > int(max_batch):
        return None, f"单次目标超过上限 {max_batch} 台，请分批"
    if cfg is not None:
        out, rejected = filter_known_ips(out, cfg)
        if rejected:
            shown = "、".join(rejected[:5]) + ("…" if len(rejected) > 5 else "")
            return None, f"以下 {len(rejected)} 个 IP 不在矿场配置网段内，已拒绝: {shown}"
    return out, ""


def _sent_then_dropped(e):
    """带认证的请求已送达、矿机在回响应前断开或不再回应——原厂 reboot.cgi 的常见表现
    (系统已开始重启，web 服务先没了)。连不上(拒绝连接/连接超时)不算。
    Digest 第一跳不带凭据，矿机不可能执行；web 卡死的机器正好会在这一跳超时，
    必须算失败，否则"没重启"会被显示成成功还进了告警静默期。"""
    if isinstance(e, requests.ConnectTimeout):
        return False
    req = getattr(e, "request", None)
    if req is None or "Authorization" not in (req.headers or {}):
        return False
    if isinstance(e, (requests.ReadTimeout, requests.exceptions.ChunkedEncodingError)):
        return True
    return isinstance(e, requests.ConnectionError) and bool(e.args) \
        and isinstance(e.args[0], ProtocolError)


def _stock_reboot(s, ip, passwords, timeout):
    """原厂重启：GET /cgi-bin/reboot.cgi(与原厂网页一致)，个别固件回 405 再用 POST。

    矿机收到后常常不回响应就断开(已经在重启)——以前这被当成"失败"，值班员看到失败
    会再点一次，等于对正在重启的机器补发命令。现在按"已下发"处理；真没起来由
    告警侧的"重启后 N 分钟仍未上线"兜底。只有 401(换下一组密码)/405(换 POST)才会
    再发请求，这两种矿机都没执行，不存在重复重启。
    """
    url = f"http://{ip}/cgi-bin/reboot.cgi"
    r = None
    for u, p in passwords:
        auth = HTTPDigestAuth(u, p)
        for method in ("GET", "POST"):
            try:
                r = s.request(method, url, auth=auth, timeout=timeout)
            except requests.RequestException as e:
                if _sent_then_dropped(e):
                    return True, "已下发(矿机未回响应即断开，通常表示已开始重启)"
                return False, f"连不上矿机: {e}"
            if r.status_code != 405:
                break
        if r.status_code == 401:
            continue
        if r.status_code == 200:
            return True, "reboot ok"
        return False, f"矿机拒绝重启: HTTP {r.status_code}"
    return False, "密码无效(401)"


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
        return _stock_reboot(s, ip, passwords, timeout)
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
    if action == "set_pools":   # 纵深防御：无论谁调到这里，白名单外的矿池一律不下发
        pools = (params or {}).get("pools") or []
        if not pools or not all(pool_allowed(p.get("url"), ctl.get("pool_allowlist"))
                                for p in pools if isinstance(p, dict)):
            return {"ip": ip, "ok": False, "msg": "矿池不在白名单，拒绝下发"}
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


def run_batch(targets, action, params, cfg, progress=None, before_group=None):
    """targets: [(ip, firmware), ...]；并发执行，返回结果列表。
    reboot 时按 control.reboot_* 打乱+分批+延迟，避免同变压器机器同时重启的浪涌跳闸。
    progress(done, total) 可选回调(后台异步执行时上报进度)。
    before_group(ips) 可选回调：每组真正下发前调用(重启静默期按实际下发时间起算)。"""
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
            if before_group:
                before_group([ip for ip, _fw in grp])
            out.extend(_run_group(grp, action, params, cfg))
            if progress:
                progress(len(out), len(targets))
            if delay and gi < len(groups) - 1:
                time.sleep(delay)
    else:
        if before_group:
            before_group([ip for ip, _fw in targets])
        out = _run_group(targets, action, params, cfg)
        if progress:
            progress(len(out), len(targets))
    out.sort(key=lambda r: tuple(int(x) for x in r["ip"].split(".")) if r["ip"].count(".") == 3 else (0,))
    return out, ""
