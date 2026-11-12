#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
矿机扫描核心层（无副作用，只返回结构化数据）。
CLI / 定时服务 / Web API 全部复用本模块。
"""
import json
import hashlib
import math
import re
import socket
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor, as_completed
# Python 3.8~3.10 上 concurrent.futures.TimeoutError 不是内置 TimeoutError(3.11 起才合并)，
# 只写 except TimeoutError 会让整体超时逃出 scan()，整轮结果全部丢弃
from concurrent.futures import TimeoutError as FutTimeout

import requests
from requests.auth import HTTPDigestAuth
from requests.adapters import HTTPAdapter

import logs

log = logs.get(__name__)

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


class _SessionBag:
    """一次 scan()/scan_containers() 调用私有的会话集合(每扫描线程一个会话)。

    会话按"调用作用域"而非进程全局收集：每轮扫描结束只关闭自己创建的那些，
    绝不会打飞另一轮并发扫描(如看门狗 _force_recover 后重启的新一轮)正在用的连接。
    """
    __slots__ = ("sessions", "closed", "lock", "__weakref__")

    def __init__(self):
        self.sessions = []
        self.closed = False
        self.lock = threading.Lock()

    def add(self, s):
        """登记会话；本袋已收尾(迟到线程)则返回 False，由调用方转记全局兜底袋。"""
        with self.lock:
            if self.closed:
                return False
            self.sessions.append(s)
            return True

    def close(self, final=True):
        with self.lock:
            items, self.sessions = self.sessions, []
            if final:
                self.closed = True
        for s in items:
            try:
                s.close()
            except Exception:
                pass


# 非扫描线程(CLI 直接调 probe、迟到的旧扫描线程)创建的会话兜底袋，由无参 close_sessions() 回收
_global_bag = _SessionBag()
_sessions_lock = threading.Lock()
_bags = weakref.WeakSet()          # 存活中的扫描会话袋，仅供无参 close_sessions() 全量兜底
_bags.add(_global_bag)


def _new_bag():
    bag = _SessionBag()
    with _sessions_lock:
        _bags.add(bag)
    return bag


def _bind_bag(bag):
    """ThreadPoolExecutor initializer：把本轮扫描的会话袋绑到 worker 线程。"""
    _local.session_bag = bag
    _local.session = None          # 线程若被复用也不沿用上一轮的(可能已关闭的)会话


def get_session():
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False  # 忽略系统代理
        s.mount("http://", HTTPAdapter(max_retries=0, pool_connections=4, pool_maxsize=4))
        _local.session = s
        # no_register(控制线程探测时置真)：不登记，避免被扫描的 close_sessions() 跨线程误关
        if not getattr(_local, "no_register", False):
            bag = getattr(_local, "session_bag", None)
            if bag is None or not bag.add(s):
                _global_bag.add(s)
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
    """关闭并清空当前进程里所有还活着的会话，释放连接池/socket。
    这是"全量兜底"清理：只在确认没有扫描在跑(线程池已 join)时调用，否则会打飞在途探测。
    正常每轮扫描结束由 scan()/scan_containers() 只关自己那袋会话，不走这里。
    长时间运行(每5分钟建300个会话、每10秒建~30个)若不关，句柄/CLOSE_WAIT 会攒到耗尽资源压垮线程。"""
    with _sessions_lock:
        bags = list(_bags)
    for bag in bags:
        # 全局兜底袋要继续可用(final=False)，扫描袋按已收尾处理
        bag.close(final=bag is not _global_bag)
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


def _number(v, integer=False):
    """把设备返回值收敛为有限数值；拒绝 HTML、NaN/Infinity 等异常载荷。"""
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(n):
        return None
    if integer:
        # SQLite INTEGER is signed 64-bit.  Device firmware occasionally emits
        # sentinel/exponent values; reject those before they reach a DB write.
        if n < -(2**63) or n > 2**63 - 1:
            return None
        return int(n)
    return n


def _nonnegative(v, integer=False):
    """Rates, consumed power and cumulative counters cannot be negative."""
    n = _number(v)
    if n is None or n < 0:
        return None
    return _number(n, integer=True) if integer else n


def _counter(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 0 <= v <= 2**63 - 1 else None
    n = _nonnegative(v)
    return _number(n, integer=True) if n is not None and n.is_integer() else None


def normalize_mac(value):
    """Return a canonical MAC or an empty string for malformed/placeholder values."""
    raw = str(value or "").strip().upper().replace("-", ":")
    if raw in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}:
        return ""   # 占位值不能用于换机或 IP 迁移判断。
    return raw if re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", raw) else ""


def _truthy(v):
    """兼容 PLC 常见的 bool/0/1/字符串，避免字符串 'false' 被 Python 当真。"""
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


def _flag(v):
    """PLC bits have three states: explicit true, explicit false, or unreadable."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        value = v.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
    return None


def gh_to_th(v):
    """GH/s -> TH/s"""
    try:
        n = _nonnegative(v)
        return round(n / 1000.0, 2) if n is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _blank(ip, status):
    # 全局统一的记录结构（与 DB 列名、前端字段一致）
    return {
        "device": "miner",
        "ip": ip, "status": status, "firmware": "", "model": "", "sn": "",
        "mac": "",
        "hr_rt": None, "hr_avg": None, "power": None, "temp": None,
        "eff": None, "uptime": None, "worker": "",
        "accepted": None, "rejected": None, "stale": None, "note": "",
        "measured_ts": time.time(),
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
    return [{"flag": f, "label": lab, "sev": sev}
            for f, lab, sev in ANTBOX_FAULTS if _flag(p.get(f)) is True]


def _unknown_container(ip):
    return {"device": "container", "ip": ip, "status": "unknown", "name": ip,
            "faults": [], "pumps": {}, "miner_ips": [], "measured_ts": time.time(),
            "observed_fault_flags": [], "data_complete": False,
            "note": "控制器可达，但冷却数据缺失或无效"}


def probe_antbox(ip, online_to, data_to, known=False):
    """AntBox 集装箱控制器：GET /cooler?operation=coolerState（免认证）。返回容器记录或 None。"""
    s = get_session()
    try:
        r = s.get(f"http://{ip}/cooler?operation=coolerState", timeout=online_to)
    except requests.RequestException:
        return None
    try:
        d = r.json()
    except ValueError:
        return _unknown_container(ip) if known else None
    if not (isinstance(d, dict) and _truthy(d.get("ok"))
            and d.get("method") == "coolerState"):
        return _unknown_container(ip) if known else None
    p = d.get("params")
    if not isinstance(p, dict) or not p:
        return _unknown_container(ip)
    observed = [f for f, _label, _severity in ANTBOX_FAULTS if _flag(p.get(f)) is not None]
    rec = {
        "device": "container", "ip": ip, "status": "online", "name": ip,
        "measured_ts": time.time(), "observed_fault_flags": observed,
        "supply_temp": _number(p.get("supply_liquid_temp")),
        "return_temp": _number(p.get("return_liquid_temp")),
        "supply_pressure": _number(p.get("supply_liquid_pressure")),
        "return_pressure": _number(p.get("return_liquid_pressure")),
        "flow": _number(p.get("supply_liquid_flow")),
        "internal_temp": _number(p.get("antbox_internal_temp")),
        "internal_humidity": _number(p.get("antbox_internal_humidity")),
        "tower_inlet_temp": _number(p.get("colding_tower_inlet_temp")),
        "set_temp": _number(p.get("supply_liquid_set_temp")),
        "power1": _nonnegative(p.get("distribution_box1_power")),
        "power2": _nonnegative(p.get("distribution_box2_power")),
        "pumps": {"循环泵": _flag(p.get("circulating_pump")),
                  "喷淋泵": _flag(p.get("spray_pump")),
                  "风扇1": _flag(p.get("fan1")), "风扇2": _flag(p.get("fan2")),
                  "塔风扇1": _flag(p.get("cooling_tower_fan1")),
                  "塔风扇2": _flag(p.get("cooling_tower_fan2")),
                  "塔风扇3": _flag(p.get("cooling_tower_fan3"))},
        "faults": antbox_faults(p),
        "miner_num": None, "chip_max_temp": None, "miner_ips": [],
    }
    if not observed:
        rec["status"] = "unknown"
        rec["note"] = "控制器可达，但未取得有效故障状态位"
    try:  # 矿机信息（best-effort，箱内矿机数/芯片温/成员列表）
        mj = s.get(f"http://{ip}/cooler?operation=minerInfo", timeout=data_to).json()
        m = mj.get("params") if isinstance(mj, dict) else None
        if not isinstance(m, dict):    # 数组/字符串等怪结构：忽略附加信息，冷却数据照常返回
            m = {}
        rec["miner_num"] = _nonnegative(m.get("miner_num"), integer=True)
        ct = _number(m.get("chip_max_temp"))
        rec["chip_max_temp"] = ct if (ct is not None and ct > -100000) else None
        mi = m.get("miner_info")
        if isinstance(mi, dict):
            rec["miner_ips"] = list(mi.keys())   # 箱内矿机（populated 时）
    except Exception:  # noqa: BLE001 - 附加信息出任何问题都不能连累已解析的漏液等故障位
        pass
    return rec


def _cgminer(ip, command, timeout):
    """cgminer 4028 端口发一条命令，返回解析后的 dict；连不上/超时/不是 JSON 返回 None。"""
    s = None
    try:
        s = socket.create_connection((ip, 4028), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(json.dumps({"command": command}).encode() + b"\n")
        buf = b""
        d = None
        # 整体(wall-clock)预算：settimeout 只保证单次 recv 不超时，对端若持续以低于
        # timeout 的间隔吐字节却永远凑不出完整 JSON/\x00，循环会永久悬挂 → worker 线程泄漏。
        deadline = time.monotonic() + max(timeout * 3.0, timeout + 1.0)
        while True:
            if time.monotonic() >= deadline:
                break                 # 走下面的收尾解析，与超时/EOF 路径返回结构一致
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
        return d if isinstance(d, dict) else None
    except Exception:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def fetch_pool(ip, timeout):
    """cgminer 4028 端口 pools 命令：取矿工名 + 接受/拒绝/陈旧份额（明文、跨固件）。
    counter_basis/counter_samples 仅供内存告警比较同一组累计计数，不持久化池地址。"""
    d = _cgminer(ip, "pools", timeout)
    if d is None:
        return None
    try:
        pools = d.get("POOLS") or []
        if not isinstance(pools, list) or not all(isinstance(p, dict) for p in pools):
            return None
        # 份额只统计可用池(Status=Alive)：备用/失活池的历史拒绝份额会误报。
        # 客户归属独立按第一配置矿池读取，不能随主备池连通状态切换。
        active = [p for p in pools
                  if str(p.get("Status") or p.get("status") or "").lower() == "alive"]
        use = active or pools   # 无活跃池(全挂)则回退全部，至少still报点东西

        def _prio(p):
            value = _counter(p.get("Priority", p.get("priority")))
            return value if value is not None else math.inf

        # switchpool 会改变运行 Priority，POOL 配置槽位仍保持不变。
        # 部分槽位缺失时，不能把唯一有编号的备用池误当成第一配置矿池。
        slots = [_counter(p.get("POOL", p.get("pool"))) for p in pools]
        primary = next((p for p, slot in zip(pools, slots) if slot == 0), None)
        if primary is None and slots and all(slot is not None for slot in slots):
            primary = pools[min(range(len(pools)), key=lambda i: slots[i])]
        if primary is None:
            primary = min(pools, key=_prio, default=None)  # 同优先级保持配置返回顺序
        user = primary.get("User", primary.get("user")) if primary is not None else None
        # 主池账户缺失就交给 service 按矿机身份回填，不能跳到备用池认出新客户。
        worker = (user.split(".", 1)[0].strip() or None) if isinstance(user, str) else None
        totals = {key: [] for key in ("Accepted", "Rejected", "Stale")}
        samples = {}
        for p in use:
            for key in totals:
                totals[key].append(_counter(p[key] if key in p else p.get(key.lower())))
            # Counter identities use opaque digests: URLs may include credentials.
            # The separate pools field remains available for the allowlist guard.
            identity = [p.get("POOL", pools.index(p)), p.get("URL") or p.get("url"),
                        p.get("User") or p.get("user")]
            key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            samples[key] = (totals["Accepted"][-1], totals["Rejected"][-1])
        basis = hashlib.sha256(json.dumps(sorted(samples)).encode()).hexdigest()

        def total(key):
            values = totals[key]
            if not values or len(samples) != len(use) or any(v is None for v in values):
                return None
            return sum(values)

        # 全部配置的矿池地址(含备用/失活池)：换池防篡改检查用。攻击者常把自己的池塞在备用位
        urls = [str(p.get("URL") or p.get("url") or "").strip() for p in sorted(pools, key=_prio)]
        return {"worker": worker, "accepted": total("Accepted"),
                "rejected": total("Rejected"), "stale": total("Stale"),
                "counter_basis": "cgminer-pools:" + basis, "counter_samples": samples,
                "pools": [u for u in urls if u]}
    except Exception:
        return None


def fetch_summary(ip, timeout):
    """cgminer 4028 summary：网页接口卡死(lighttpd/CGI 忙)但挖矿程序还在跑时的兜底取数。
    返回 {hr_rt, hr_avg, uptime} 或 None。"""
    d = _cgminer(ip, "summary", timeout)
    try:
        sm = (d or {}).get("SUMMARY") or []
        sm = sm[0] if isinstance(sm, list) and sm else None
        if not isinstance(sm, dict):
            return None

        def rate(key):
            if sm.get(f"GHS {key}") is not None:
                return gh_to_th(sm.get(f"GHS {key}"))
            n = _nonnegative(sm.get(f"MHS {key}"))
            return round(n / 1e6, 2) if n is not None else None

        hr = rate("5s")
        if hr is None:
            return None
        return {"hr_rt": hr, "hr_avg": rate("av"),
                "uptime": _nonnegative(sm.get("Elapsed"), integer=True)}
    except Exception:
        return None


# 芯片温读数的可信区间：0/负数是空槽位，超过上限是传感器坏了或固件哨兵值(如 255)。
# 不过滤的话一颗坏传感器就能让整机每轮报 crit 高温
TEMP_MIN_C, TEMP_MAX_C = 1, 150


def _temp(v):
    n = _number(v)
    return n if n is not None and TEMP_MIN_C <= n <= TEMP_MAX_C else None


def reject_pct(accepted, rejected):
    a, r = _counter(accepted), _counter(rejected)
    if a is None or r is None:
        return None
    return round(r / (a + r) * 100, 3) if (a + r) > 0 else 0.0


def probe_stock(ip, online_to, data_to, passwords, collect_identity=False):
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

    # 多密码重试总预算：每组密码 Digest 认证是两次往返，各自吃满 data_to，
    # 密码表一长单 IP 最坏耗时线性放大 → 拖垮整轮扫描。超预算直接判失败。
    pw_deadline = time.monotonic() + max(6.0, data_to * 4.0)
    for user, pwd in passwords:
        if time.monotonic() >= pw_deadline:
            rec["note"] = "密码尝试超时(预算耗尽)"
            return rec
        try:
            # 用 stats.cgi（比 summary.cgi 多出功耗/芯片温/能效/风扇/运行时长）
            r = s.get(f"http://{ip}/cgi-bin/stats.cgi",
                      auth=HTTPDigestAuth(user, pwd), timeout=data_to)
            if r.status_code == 401:
                continue
            d = r.json()
            if not isinstance(d, dict):
                raise ValueError("stats.cgi response is not an object")
            info = d.get("INFO")
            info = info if isinstance(info, dict) else {}
            rows = d.get("STATS")
            st = rows[0] if isinstance(rows, list) and rows else None
            if not isinstance(st, dict):
                raise ValueError("stats.cgi has no valid STATS record")
            model = info.get("type")
            rec["model"] = model if isinstance(model, str) else ""
            rec["hr_rt"] = gh_to_th(st.get("rate_5s"))
            rec["hr_avg"] = gh_to_th(st.get("rate_avg"))
            rec["power"] = _nonnegative(st.get("watt"), integer=True)
            rec["eff"] = _nonnegative(st.get("jt"))     # J/TH
            rec["uptime"] = _nonnegative(st.get("elapsed"), integer=True)
            rec["measured_ts"] = time.time()
            temps = []
            chains = st.get("chain")
            for c in chains if isinstance(chains, list) else []:
                if not isinstance(c, dict):
                    continue
                chips = c.get("temp_chip")
                if not isinstance(chips, list):
                    continue
                temps += [n for n in (_temp(t) for t in chips)
                          if n is not None]
            rec["temp"] = max(temps) if temps else None
            # 6060 does not expose a MAC.  Only machines without a usable SN
            # need the extra authenticated request, and only on discovery scans.
            if collect_identity and not sn_valid(rec["sn"]):
                try:
                    ir = s.get(f"http://{ip}/cgi-bin/get_system_info.cgi",
                               auth=HTTPDigestAuth(user, pwd), timeout=data_to)
                    if ir.status_code == 200:
                        rec["mac"] = normalize_mac((ir.json() or {}).get("macaddr"))
                except (requests.RequestException, ValueError, AttributeError):
                    pass
            return rec
        except requests.RequestException:
            rec["note"] = "6060在线,stats.cgi超时"
            return rec
        except (ValueError, KeyError, AttributeError, TypeError, IndexError):
            # 结构不对(数组/字符串/字段类型错)：机器明明在线，只是读不到算力。异常若冒到
            # 扫描层会被当成离线，误报掉线
            rec["note"] = "stats.cgi响应异常"
            return rec
    rec["note"] = "密码无效(401)"
    return rec


def probe_uniplus(ip, online_to, data_to=None, collect_identity=False):
    """第三方 UniPlusOS：/api/v1/summary 免认证取算力。返回 dict 或 None。"""
    s = get_session()
    try:
        r = s.get(f"http://{ip}/api/v1/summary", timeout=online_to)
        d = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    m = d.get("miner")
    if not isinstance(m, dict):
        return None
    ct = m.get("chip_temp")
    ct = ct if isinstance(ct, dict) else {}
    state = m.get("miner_status")
    state = state if isinstance(state, dict) else {}
    rec = _blank(ip, "online")
    rec["firmware"] = "uniplus"
    model = m.get("miner_type")
    rec["model"] = model if isinstance(model, str) else ""
    rec["hr_rt"] = gh_to_th(m.get("hr_realtime"))
    rec["hr_avg"] = gh_to_th(m.get("hr_average"))
    rec["power"] = _nonnegative(m.get("power_consumption"), integer=True)
    rec["temp"] = _temp(ct.get("max"))
    rec["eff"] = _nonnegative(m.get("power_efficiency"))  # J/TH
    rec["uptime"] = _nonnegative(state.get("miner_state_time"), integer=True)
    if collect_identity:
        try:
            info = s.get(f"http://{ip}/api/v1/info", timeout=data_to or online_to).json()
            if isinstance(info, dict):
                system = info.get("system") or {}
                network = info.get("network_status") or (
                    system.get("network_status") if isinstance(system, dict) else {}) or {}
                rec["mac"] = normalize_mac(network.get("mac"))
                serial = info.get("serial") or info.get("sn")
                if sn_valid(serial):
                    rec["sn"] = str(serial).strip()
        except (requests.RequestException, ValueError, AttributeError):
            pass
    return rec


def probe(ip, cfg, limiter=None):
    """单机探测：先原厂(6060最快)，不通再第三方，都不通再看端口是否有响应：
    有响应就从 4028 兜底取数(取不到=unknown，带 alive 标记)，完全连不上才是离线。"""
    if limiter is not None:
        limiter.acquire()   # 限速：控制每秒新建连接(≈ARP)速率，保护三层
    passwords = [tuple(p) for p in cfg.get("passwords", [["root", "root"]])]
    online_to = cfg.get("online_timeout", 0.5)
    data_to = cfg.get("data_timeout", 2.0)
    # 快速判活：三类设备(原厂/第三方/AntBox)都开 80；连不上直接判离线，死IP不白跑3个探测
    gate = cfg.get("liveness_gate")
    if gate and not tcp_open(ip, 80, cfg.get("gate_timeout", 0.4)):
        return _blank(ip, "offline")
    collect_identity = bool(cfg.get("collect_identity"))
    # UniPlus 也常开 6060：若先走原厂会被误标 stock。优先看 80 口页面指纹。
    def _uniplus_html(text):
        low = (text or "").lower()
        return 'content="uniplusos"' in low or "content='uniplusos'" in low or (
            "firmware console" in low and "uniplus" in low)

    uniplus_first = False
    page_html = ""
    try:
        s = get_session()
        page = s.get(f"http://{ip}/", timeout=min(online_to, 1.0))
        auth = (page.headers.get("WWW-Authenticate") or "").lower()
        page_html = page.text or ""
        if page.status_code != 401 or "digest" not in auth:
            uniplus_first = _uniplus_html(page_html)
    except Exception:  # noqa: BLE001 - 指纹失败则沿用原顺序
        uniplus_first = False

    rec = None
    if uniplus_first:
        rec = probe_uniplus(ip, online_to, data_to, collect_identity)
        if rec is None:
            rec = probe_stock(ip, online_to, data_to, passwords, collect_identity)
    else:
        rec = probe_stock(ip, online_to, data_to, passwords, collect_identity)
        if rec is None:
            rec = probe_uniplus(ip, online_to, data_to, collect_identity)
        elif rec.get("firmware") == "stock" and page_html and _uniplus_html(page_html):
            alt = probe_uniplus(ip, online_to, data_to, collect_identity)
            if alt is not None:
                rec = alt
    if rec is None:
        # AntBox 控制器响应比矿机慢（要查 PLC），给更宽超时
        cont = probe_antbox(ip, max(online_to, 1.5), data_to)
        if cont is not None:
            return cont
        # 网页接口全部没应答，但设备明明连得上(判活闸门已通过，或二次确认时补测 80/4028)：
        # 大场地最常见的是矿机网页服务(lighttpd/CGI)卡住而挖矿程序照常在跑。以前直接判离线，
        # 一卡就报掉线、过一会儿自己好了又恢复——这是"误报掉线"的主要来源之一
        alive = bool(gate) or (cfg.get("alive_probe") and (
            tcp_open(ip, 80, cfg.get("gate_timeout", 0.4))
            or tcp_open(ip, 4028, cfg.get("gate_timeout", 0.4))))
        if not alive:
            return _blank(ip, "offline")
        sm = fetch_summary(ip, min(data_to, 3.0))
        if sm is None:
            # 连得上但什么都读不到：不是"确认离线"，交给告警层按连续轮数升级
            rec = _blank(ip, "unknown")
            rec["alive"] = True
            rec["note"] = "端口有响应但接口无应答"
            return rec
        rec = _blank(ip, "online")
        rec.update(sm)
        rec["note"] = "网页接口无响应,经4028取数"
    elif rec.get("hr_rt") is None and rec.get("status") == "online":
        # 在线但 stats.cgi 超时/读不出：用 4028 补算力，免得"读不到算力"一直没数据
        sm = fetch_summary(ip, min(data_to, 3.0))
        if sm:
            rec["measured_ts"] = time.time()
            for k, v in sm.items():
                if rec.get(k) is None:
                    rec[k] = v
    # 在线机补取矿工名 + 矿池份额（4028 明文，跨固件通用）
    if cfg.get("fetch_worker", True):
        pi = fetch_pool(ip, min(data_to, 1.5))
        if pi:
            if pi.get("worker"):
                rec["worker"] = pi["worker"]
            rec["accepted"] = pi.get("accepted")
            rec["rejected"] = pi.get("rejected")
            rec["stale"] = pi.get("stale")
            rec["counter_basis"] = pi.get("counter_basis")
            rec["counter_samples"] = pi.get("counter_samples")
            rec["pools"] = pi.get("pools") or []   # 只在内存/本轮使用，不落库
    return rec


def _ip_sort_key(r):
    """按 IPv4 数值排序；非法/非四段 ip 不抛异常(否则整批结果被丢掉)，排到合法 IP 之后。"""
    ip = str((r or {}).get("ip") or "")
    parts = ip.split(".")
    if len(parts) == 4:
        try:
            nums = tuple(int(x) for x in parts)
        except (TypeError, ValueError):
            nums = None
        if nums is not None and all(0 <= n <= 255 for n in nums):
            return (0, nums, "")
    return (1, (0, 0, 0, 0), ip)


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
    # 整体超时：下限 15 分钟。空号多时真实吞吐≈workers/判活超时，远低于 max_pps；
    # 以前用 max(600, 总数/pps×3)，pps 一高就卡在 10 分钟地板，后段网段发现不完。
    overall_to = max(900.0, total / max(pps, 1) * 3.0)
    bag = _new_bag()          # 本次调用私有的会话袋，收尾只关自己创建的会话
    ex = ThreadPoolExecutor(max_workers=workers, initializer=_bind_bag, initargs=(bag,))
    futs = {ex.submit(probe, ip, cfg, limiter): ip for ip in ips}

    def result(f, ip):
        try:
            record = dict(f.result())
            # Legacy/custom probes may omit this field; never renew a timestamp
            # already supplied by the driver when a slow batch finally ends.
            record.setdefault("measured_ts", time.time())
            return record
        except Exception:
            record = _blank(ip, "unknown")
            record["note"] = "探测结果异常(未确认离线)"
            return record

    try:
        for f in as_completed(futs, timeout=overall_to):
            ip = futs[f]
            seen.add(ip)
            results.append(result(f, ip))
            done += 1
            if progress_cb and done % 200 == 0:
                progress_cb(done, total)
    except (FutTimeout, TimeoutError):
        log.warning("scan: 整体超时 %ds，已完成 %d/%d", int(overall_to), len(seen), total)
    finally:
        # 整轮扫描拥堵/超时时，"排不上号、压根没问过"的机器绝不能当成"问了确认离线"——
        # 否则链路一抖动就能把成百上千台在线机批量误报离线。用 f.cancel() 的返回值
        # 区分：线程池里还没排到(never_started) vs. 已经在跑但卡住没回来(started_stuck)，
        # 两者都标记 unknown，交给上层(alerts.evaluate)原样跳过、保留上一轮状态。
        never_started = started_stuck = 0
        for f, ip in futs.items():
            if ip in seen:
                continue
            if f.done():
                # as_completed 的超时检查发生在"取下一个已完成结果"之前：有极小概率
                # 恰好卡在"结果已经算出来、但还没被 as_completed 取走"这个窗口触发
                # TimeoutError，这种 future 其实是真探测完了，绝不能当unknown丢掉。
                results.append(result(f, ip))
                done += 1
                continue
            if f.cancel():
                never_started += 1
            else:
                started_stuck += 1
            rec = _blank(ip, "unknown")
            rec["note"] = "本轮未探测(扫描超时)"
            results.append(rec)
            done += 1
        if never_started or started_stuck:
            log.warning("scan: %d 台本轮未探测，保留原状态不判离线(排队未启动 %d / 已启动但卡住 %d)",
                        never_started + started_stuck, never_started, started_stuck)
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Python < 3.9
            ex.shutdown(wait=False)
        bag.close()    # 只关本次调用创建的会话，防句柄/socket 泄漏(不影响并发的其它扫描)
    if progress_cb:
        progress_cb(total, total)
    try:
        results.sort(key=_ip_sort_key)   # 排序失败绝不能丢掉整批探测结果
    except Exception:
        log.warning("scan: 结果排序失败，按原始顺序返回 %d 条", len(results))
    return results


def scan_containers(ips, cfg, progress_cb=None):
    """只对已知箱体探测 /cooler（不连矿机那套），用于高频箱体刷新。返回容器记录列表。"""
    ips = list(ips)
    online_to = max(cfg.get("online_timeout", 0.5), 1.5)
    data_to = cfg.get("data_timeout", 2.0)
    workers = min(64, max(4, len(ips)))
    overall_to = max(30.0, len(ips) * max(online_to, data_to) * 2)  # 防单箱挂死占住扫描锁
    out = []
    done = 0
    seen = set()
    bag = _new_bag()          # 本次调用私有的会话袋
    ex = ThreadPoolExecutor(max_workers=workers, initializer=_bind_bag, initargs=(bag,))
    futs = {ex.submit(probe_antbox, ip, online_to, data_to, known=True): ip for ip in ips}
    try:
        for f in as_completed(futs, timeout=overall_to):
            seen.add(futs[f])
            try:
                r = f.result(timeout=5)
            except Exception:
                r = None
            if r:
                out.append(r)
            done += 1
            if progress_cb and done % 20 == 0:
                progress_cb(done, len(ips))
    except (FutTimeout, TimeoutError):
        log.warning("scan_containers: 整体超时 %ds，已完成 %d/%d",
                    int(overall_to), len(seen), len(ips))
    finally:
        for f in futs:
            f.cancel()
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)
        bag.close()    # 只关本次调用创建的会话(容器循环每10秒一次，尤其不能误关矿机扫描的会话)
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
    """按机型取实时算力中位数，作为掉算力判定基线。

    只取"有正算力"的在线机：零算力机本身就是故障对象，把它们算进中位数会把基线
    整体拉低，导致真正掉算力的机器反而"达标"而漏报。"""
    from statistics import median
    buckets = {}
    for r in records:
        if r["status"] == "online" and (r.get("hr_rt") or 0) > 0:
            buckets.setdefault(r["model"] or "?", []).append(r["hr_rt"])
    return {m: round(median(v), 2) for m, v in buckets.items() if v}
