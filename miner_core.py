#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
矿机扫描核心层（无副作用，只返回结构化数据）。
CLI / 定时服务 / Web API 全部复用本模块。
"""
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.auth import HTTPDigestAuth
from requests.adapters import HTTPAdapter

_local = threading.local()


class RateLimiter:
    """令牌桶：限制每秒发起的探测数（≈给死IP的ARP速率），保护三层 CPU/ARP/会话限制。"""
    def __init__(self, rate):
        import time as _t
        self.rate = float(rate)
        self.allow = float(rate)
        self.last = _t.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        import time as _t
        while True:
            with self.lock:
                now = _t.monotonic()
                self.allow = min(self.rate, self.allow + (now - self.last) * self.rate)
                self.last = now
                if self.allow >= 1:
                    self.allow -= 1
                    return
                wait = (1 - self.allow) / self.rate
            _t.sleep(wait)


_sessions = []            # 所有创建过的会话(每扫描线程一个)，扫完统一关闭防句柄/socket泄漏
_sessions_lock = threading.Lock()


def get_session():
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False  # 忽略系统代理
        s.mount("http://", HTTPAdapter(max_retries=0, pool_connections=4, pool_maxsize=4))
        _local.session = s
        # no_register(控制线程探测时置真)：不登记进全局池，避免被扫描的 close_sessions() 跨线程误关
        if not getattr(_local, "no_register", False):
            with _sessions_lock:
                _sessions.append(s)
    return s


_SN_BAD = frozenset(("", "n/a", "unknown", "none", "no miner sn stored on board"))


def sn_valid(sn):
    """SN 能否作为机器唯一身份：排除空/N/A/unknown/未烧录占位串(no miner sn stored on board)/含error/过短。
    未烧录SN的控制板会返回同一个占位串，若当唯一身份用会把不同机器认成同一台→误删，故必须过滤。"""
    if not sn:
        return False
    s = str(sn).strip().lower()
    if s in _SN_BAD or "error" in s or "no miner sn" in s:
        return False
    return len(s) >= 6


def close_sessions():
    """关闭并清空所有扫描会话，释放连接池/socket。只在扫描线程池已 join(无线程在用)后调用。
    长时间运行(每5分钟建300个会话、每10秒建~30个)若不关，句柄/CLOSE_WAIT 会攒到耗尽资源压垮线程。"""
    with _sessions_lock:
        for s in _sessions:
            try:
                s.close()
            except Exception:
                pass
        _sessions.clear()
    # 线程本地引用一并清掉(旧扫描线程已死；下批用全新线程会重建会话)
    try:
        if getattr(_local, "session", None) is not None:
            _local.session = None
    except Exception:
        pass


def tcp_open(ip, port, timeout):
    """快速判活：能在 timeout 内建立 TCP 连接即视为有设备。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def gh_to_th(v):
    """GH/s -> TH/s"""
    try:
        return round(float(v) / 1000.0, 2)
    except (TypeError, ValueError):
        return None


def _blank(ip, status):
    # 全局统一的记录结构（与 DB 列名、前端字段一致）
    return {
        "device": "miner",
        "ip": ip, "status": status, "firmware": "", "model": "", "sn": "",
        "hr_rt": None, "hr_avg": None, "power": None, "temp": None,
        "eff": None, "uptime": None, "worker": "",
        "accepted": None, "rejected": None, "stale": None, "note": "",
    }


# AntBox 集装箱冷却故障位（flag, 中文, 严重级）
ANTBOX_FAULTS = [
    ("leakage_fault", "漏液", "crit"), ("freezing_alarm", "冻结", "crit"),
    ("power_fault", "电源故障", "crit"), ("power_relay_fault", "电源继电器故障", "crit"),
    ("phasefailure", "断相", "crit"),
    ("supply_liquid_temp_too_high", "供液温过高", "crit"),
    ("liquid_level_low", "液位低", "crit"),
    ("circulating_pump_fault", "循环泵故障", "crit"),
    ("second_pump1_fault", "二级泵1故障", "crit"), ("second_pump2_fault", "二级泵2故障", "crit"),
    ("disconnect", "控制器失联", "crit"),
    ("supply_liquid_temp_high", "供液温偏高", "warn"),
    ("liquid_level_high", "液位高", "warn"),
    ("cooling_tower_liquid_level_low", "冷却塔液位低", "warn"),
    ("supply_liquid_flow_low", "供液流量低", "warn"),
    ("supply_liquid_pressure_high", "供液压力高", "warn"),
    ("return_liquid_pressure_low", "回液压力低", "warn"),
    ("spray_pump_fault", "喷淋泵故障", "warn"), ("fluid_infusion_pump_fault", "补液泵故障", "warn"),
    ("fan1_fault", "风扇1故障", "warn"), ("fan2_fault", "风扇2故障", "warn"), ("fan_fault", "风扇故障", "warn"),
    ("cooling_tower_fan1_fault", "冷却塔风扇1故障", "warn"),
    ("cooling_tower_fan2_fault", "冷却塔风扇2故障", "warn"),
    ("cooling_tower_fan3_fault", "冷却塔风扇3故障", "warn"),
    ("supply_liquid_temp_fault", "供液温传感器故障", "warn"),
    ("return_liquid_temp_fault", "回液温传感器故障", "warn"),
]


def antbox_faults(p):
    return [{"flag": f, "label": lab, "sev": sev} for f, lab, sev in ANTBOX_FAULTS if p.get(f)]


def probe_antbox(ip, online_to, data_to):
    """AntBox 集装箱控制器：GET /cooler?operation=coolerState（免认证）。返回容器记录或 None。"""
    s = get_session()
    try:
        r = s.get(f"http://{ip}/cooler?operation=coolerState", timeout=online_to)
        d = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not (isinstance(d, dict) and d.get("ok") and d.get("method") == "coolerState"):
        return None
    p = d.get("params") or {}
    rec = {
        "device": "container", "ip": ip, "status": "online", "name": ip,
        "supply_temp": p.get("supply_liquid_temp"), "return_temp": p.get("return_liquid_temp"),
        "supply_pressure": p.get("supply_liquid_pressure"), "return_pressure": p.get("return_liquid_pressure"),
        "flow": p.get("supply_liquid_flow"), "internal_temp": p.get("antbox_internal_temp"),
        "internal_humidity": p.get("antbox_internal_humidity"), "tower_inlet_temp": p.get("colding_tower_inlet_temp"),
        "set_temp": p.get("supply_liquid_set_temp"),
        "power1": p.get("distribution_box1_power"), "power2": p.get("distribution_box2_power"),
        "pumps": {"循环泵": p.get("circulating_pump"), "喷淋泵": p.get("spray_pump"),
                  "风扇1": p.get("fan1"), "风扇2": p.get("fan2"),
                  "塔风扇1": p.get("cooling_tower_fan1"), "塔风扇2": p.get("cooling_tower_fan2"),
                  "塔风扇3": p.get("cooling_tower_fan3")},
        "faults": antbox_faults(p),
        "miner_num": None, "chip_max_temp": None, "miner_ips": [],
    }
    try:  # 矿机信息（best-effort，箱内矿机数/芯片温/成员列表）
        mj = s.get(f"http://{ip}/cooler?operation=minerInfo", timeout=data_to).json()
        m = (mj.get("params") or {}) if isinstance(mj, dict) else {}   # 返回非dict(如数组)不崩，防中断本轮扫描
        rec["miner_num"] = m.get("miner_num")
        ct = m.get("chip_max_temp")
        rec["chip_max_temp"] = ct if (isinstance(ct, (int, float)) and ct > -100000) else None
        mi = m.get("miner_info")
        if isinstance(mi, dict):
            rec["miner_ips"] = list(mi.keys())   # 箱内矿机（populated 时）
    except (requests.RequestException, ValueError):
        pass
    return rec


def fetch_pool(ip, timeout):
    """cgminer 4028 端口 pools 命令：取矿工名 + 接受/拒绝/陈旧份额（明文、跨固件）。
    返回 {worker, accepted, rejected, stale} 或 None。"""
    s = None
    try:
        s = socket.create_connection((ip, 4028), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b'{"command":"pools"}\n')
        buf = b""
        d = None
        while True:
            try:
                c = s.recv(65535)
            except socket.timeout:
                break
            if not c:
                break
            buf += c
            if b"\x00" in buf:        # cgminer 显式以 \x00 结尾 → 已收齐
                break
            # 否则尝试解析已收到的 {...}：能完整解析才算收齐。
            # 不能凭末字符是 '}' 就 break——多矿池响应可能恰好分段在某个内层 pool 的 '}' 处，
            # 那样会截断 JSON 导致解析失败、整台 worker/份额丢失。
            txt = buf.decode("utf-8", "ignore")
            i, j = txt.find("{"), txt.rfind("}")
            if i >= 0 and j > i:
                try:
                    d = json.loads(txt[i:j + 1])
                    break
                except ValueError:
                    pass              # 还没收完整，继续读
        if d is None:                 # 超时/EOF 收尾再解析一次
            txt = buf.replace(b"\x00", b"").decode("utf-8", "ignore")
            i, j = txt.find("{"), txt.rfind("}")
            if i < 0 or j <= i:
                return None
            d = json.loads(txt[i:j + 1])
        pools = d.get("POOLS") or []
        # 只统计活跃池(Status=Alive)：备用/失活池的历史拒绝份额会把拒绝率顶过阈值误报；
        # worker 也取活跃池中优先级最高(Priority 最小)的主池，避免归属到备用池的异名 worker。
        active = [p for p in pools
                  if str(p.get("Status") or p.get("status") or "").lower() == "alive"]
        use = active or pools   # 无活跃池(全挂)则回退全部，至少still报点东西

        def _prio(p):
            try:
                return int(p.get("Priority", p.get("priority", 999)))
            except (TypeError, ValueError):
                return 999

        worker, acc, rej, stl = None, 0, 0, 0
        for p in sorted(use, key=_prio):
            u = p.get("User") or p.get("user")
            if u and worker is None:
                worker = u.split(".", 1)[0].strip()   # 去掉 .25x146 后缀
        for p in use:
            acc += int(p.get("Accepted") or p.get("accepted") or 0)
            rej += int(p.get("Rejected") or p.get("rejected") or 0)
            stl += int(p.get("Stale") or p.get("stale") or 0)
        return {"worker": worker, "accepted": acc, "rejected": rej, "stale": stl}
    except Exception:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def reject_pct(accepted, rejected):
    a, r = accepted or 0, rejected or 0
    return round(r / (a + r) * 100, 3) if (a + r) > 0 else 0.0


def probe_stock(ip, online_to, data_to, passwords):
    """原厂：6060/get_sn 判定在线，summary.cgi 取算力。返回 dict 或 None。"""
    s = get_session()
    try:
        r = s.get(f"http://{ip}:6060/get_sn", timeout=online_to)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None   # 非 200(错误页/其它服务) 不当原厂矿机
    txt = (r.text or "").strip()
    sn = txt if (txt and "error" not in txt.lower()) else "N/A"

    rec = _blank(ip, "online")
    rec["firmware"] = "stock"
    rec["sn"] = sn

    for user, pwd in passwords:
        try:
            # 用 stats.cgi（比 summary.cgi 多出功耗/芯片温/能效/风扇/运行时长）
            r = s.get(f"http://{ip}/cgi-bin/stats.cgi",
                      auth=HTTPDigestAuth(user, pwd), timeout=data_to)
            if r.status_code == 401:
                continue
            d = r.json()
            info = d.get("INFO", {})
            st = (d.get("STATS") or [{}])[0]
            rec["model"] = info.get("type", "")
            rec["hr_rt"] = gh_to_th(st.get("rate_5s"))
            rec["hr_avg"] = gh_to_th(st.get("rate_avg"))
            rec["power"] = st.get("watt") or None
            rec["eff"] = st.get("jt")              # J/TH
            rec["uptime"] = st.get("elapsed")      # 秒
            temps = []
            for c in (st.get("chain") or []):
                temps += [t for t in (c.get("temp_chip") or []) if t]
            rec["temp"] = max(temps) if temps else None
            return rec
        except requests.RequestException:
            rec["note"] = "6060在线,stats.cgi超时"
            return rec
        except (ValueError, KeyError):
            rec["note"] = "stats.cgi响应异常"
            return rec
    rec["note"] = "密码无效(401)"
    return rec


def probe_uniplus(ip, online_to):
    """第三方 UniPlusOS：/api/v1/summary 免认证取算力。返回 dict 或 None。"""
    s = get_session()
    try:
        r = s.get(f"http://{ip}/api/v1/summary", timeout=online_to)
        d = r.json()
    except (requests.RequestException, ValueError):
        return None
    m = d.get("miner")
    if not isinstance(m, dict):
        return None
    ct = m.get("chip_temp", {}) or {}
    rec = _blank(ip, "online")
    rec["firmware"] = "uniplus"
    rec["model"] = m.get("miner_type", "")
    rec["hr_rt"] = gh_to_th(m.get("hr_realtime"))
    rec["hr_avg"] = gh_to_th(m.get("hr_average"))
    rec["power"] = m.get("power_consumption")
    rec["temp"] = ct.get("max")
    rec["eff"] = m.get("power_efficiency")          # J/TH
    rec["uptime"] = (m.get("miner_status") or {}).get("miner_state_time")
    return rec


def probe(ip, cfg, limiter=None):
    """单机探测：先原厂(6060最快)，不通再第三方，都不通=离线。"""
    if limiter is not None:
        limiter.acquire()   # 限速：控制每秒新建连接(≈ARP)速率，保护三层
    passwords = [tuple(p) for p in cfg.get("passwords", [["root", "root"]])]
    online_to = cfg.get("online_timeout", 0.5)
    data_to = cfg.get("data_timeout", 2.0)
    # 快速判活：三类设备(原厂/第三方/AntBox)都开 80；连不上直接判离线，死IP不白跑3个探测
    if cfg.get("liveness_gate") and not tcp_open(ip, 80, cfg.get("gate_timeout", 0.4)):
        return _blank(ip, "offline")
    rec = probe_stock(ip, online_to, data_to, passwords)
    if rec is None:
        rec = probe_uniplus(ip, online_to)
    if rec is None:
        # AntBox 控制器响应比矿机慢（要查 PLC），给更宽超时
        cont = probe_antbox(ip, max(online_to, 1.5), data_to)
        if cont is not None:
            return cont
        return _blank(ip, "offline")
    # 在线机补取矿工名 + 矿池份额（4028 明文，跨固件通用）
    if cfg.get("fetch_worker", True):
        pi = fetch_pool(ip, min(data_to, 1.5))
        if pi:
            if pi.get("worker"):
                rec["worker"] = pi["worker"]
            rec["accepted"] = pi.get("accepted")
            rec["rejected"] = pi.get("rejected")
            rec["stale"] = pi.get("stale")
    return rec


def scan(ips, cfg, progress_cb=None, workers=None):
    """并发扫描一批 IP，返回 record 列表。progress_cb(done,total) 可选；workers 可覆盖配置。"""
    ips = list(ips)
    total = len(ips)
    workers = workers or cfg.get("workers", 256)
    max_pps = cfg.get("max_pps", 0)
    limiter = RateLimiter(max_pps) if max_pps and max_pps > 0 else None
    results = []
    done = 0
    seen = set()
    pps = max_pps or 100
    overall_to = max(600.0, total / max(pps, 1) * 3.0)   # 防线程池永久挂死
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(probe, ip, cfg, limiter): ip for ip in ips}
    try:
        for f in as_completed(futs, timeout=overall_to):
            ip = futs[f]
            seen.add(ip)
            try:
                results.append(f.result(timeout=5))
            except Exception:
                results.append(_blank(ip, "offline"))
            done += 1
            if progress_cb and done % 200 == 0:
                progress_cb(done, total)
    except TimeoutError:
        print(f"scan: 整体超时 {int(overall_to)}s，已完成 {len(seen)}/{total}")
    finally:
        for f, ip in futs.items():
            if ip in seen:
                continue
            f.cancel()
            results.append(_blank(ip, "offline"))
            done += 1
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Python < 3.9
            ex.shutdown(wait=False)
    if progress_cb:
        progress_cb(total, total)
    close_sessions()   # 关闭本轮所有会话，防句柄/socket 泄漏
    results.sort(key=lambda r: tuple(int(x) for x in r["ip"].split(".")))
    return results


def scan_containers(ips, cfg, progress_cb=None):
    """只对已知箱体探测 /cooler（不连矿机那套），用于高频箱体刷新。返回容器记录列表。"""
    ips = list(ips)
    online_to = max(cfg.get("online_timeout", 0.5), 1.5)
    data_to = cfg.get("data_timeout", 2.0)
    workers = min(64, max(4, len(ips)))
    out = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(probe_antbox, ip, online_to, data_to) for ip in ips]
        for f in as_completed(futs):
            r = f.result()
            if r:
                out.append(r)
            done += 1
            if progress_cb and done % 20 == 0:
                progress_cb(done, len(ips))
    close_sessions()   # 线程池已 join，关闭本轮所有会话，防句柄/socket 泄漏(容器循环每10秒一次尤其要紧)
    return out


def gen_ips(lo, hi, base="172.16"):
    out = []
    for c in range(lo, hi + 1):
        for h in range(1, 255):
            out.append(f"{base}.{c}.{h}")
    return out


def gen_ips_seg(segments, host_start=1, host_end=254):
    """按网段前缀列表(每项取前三段)展开 IP。"""
    out = []
    for s in segments:
        parts = str(s).strip().split(".")
        if len(parts) < 3:
            continue
        base = ".".join(parts[:3])
        for h in range(host_start, host_end + 1):
            out.append(f"{base}.{h}")
    return out


def rank(records):
    """返回 (有算力降序列表, 在线无算力列表, 离线列表)。"""
    online = [r for r in records if r["status"] == "online"]
    with_hr = [r for r in online if r.get("hr_rt") is not None]
    no_hr = [r for r in online if r.get("hr_rt") is None]
    offline = [r for r in records if r["status"] == "offline"]
    with_hr.sort(key=lambda r: r["hr_rt"], reverse=True)
    return with_hr, no_hr, offline


def model_baselines(records):
    """按机型取实时算力中位数，作为掉算力判定基线。"""
    from statistics import median
    buckets = {}
    for r in records:
        if r["status"] == "online" and r.get("hr_rt") is not None:
            buckets.setdefault(r["model"] or "?", []).append(r["hr_rt"])
    return {m: round(median(v), 2) for m, v in buckets.items() if v}
