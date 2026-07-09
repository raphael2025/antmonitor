"""
AntFleet IP Manager —— 核心引擎
=================================
专注蚂蚁矿机(Antminer S19 XP+ Hyd. 等带 6060 API、固件 FR-1.27 的控制板)。

设计要点(都是真机踩坑换来的):
  - 扫 SN / 在线: GET http://IP:6060/get_sn         (免认证, 最快)
  - 取详情(SN/MAC/机型/网络): GET /cgi-bin/get_system_info.cgi   (digest root/root)
  - 改静态IP: POST /cgi-bin/set_network_conf.cgi   JSON 体  {"ipPro":"2",...}
  - 改DHCP:   同上  {"ipPro":"1","ipHost":"..."}
  - 点灯定位: POST /cgi-bin/blink.cgi   {"blink":true/false}
  - 🔒 铁律: 一台机器一次操作只发【一条】命令, 绝不补发 —— 否则正在切网/重启的
            机器会被写花配置, 网卡起不来, 只能物理 SD 刷回。

依赖: httpx (内置 DigestAuth, 支持 async), openpyxl (读表)
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable, Optional

import httpx

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
PORT_SN = 6060                 # 免认证 SN 接口端口
PORT_WEB = 80                  # Web/CGI 端口
IP_REPORT_PORT = 14235         # IP Report 物理按钮广播端口
DEFAULT_USER = "root"
DEFAULT_PASS = "root"

DEFAULT_MASK = "255.255.255.0"
DEFAULT_HOSTNAME = "antMiner"

SN_NO_READ = "no miner sn stored on board"   # 控制板读不出 SN 时的返回

# ipPro: 2=静态  1=DHCP
IPPRO_STATIC = "2"
IPPRO_DHCP = "1"


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------
@dataclass
class Miner:
    """一台在线矿机的快照。"""
    ip: str
    sn: str = ""                # 设备读出的 SN; 读不出为 "" 或 SN_NO_READ
    mac: str = ""
    model: str = ""
    nettype: str = ""           # DHCP / Static
    firmware: str = ""
    hashrate: float = 0.0       # GH/s (实时)
    temp_max: float = 0.0       # 最高芯片/出水口温度
    elapsed: int = 0            # 运行秒数
    online: bool = True
    detail_ok: bool = False     # 是否成功取到 CGI 详情

    @property
    def sn_readable(self) -> bool:
        return bool(self.sn) and SN_NO_READ not in self.sn.lower() and self.sn.lower() != "unknown"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sn_readable"] = self.sn_readable
        return d


@dataclass
class PlanItem:
    """一条"当前机器 -> 目标IP"的改动计划。"""
    target_ip: str
    identifier: str                 # 表里登记的 SN 或 MAC
    rack: str = ""                  # 机位号 (排-位)
    hostname: str = DEFAULT_HOSTNAME
    # 匹配结果:
    current_ip: str = ""
    device_sn: str = ""
    mac: str = ""
    match_mode: str = ""            # exact / mask7 / first3last6 / mac / none
    status: str = "pending"         # ok / not_found / ambiguous / conflict / skip(already)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------
# SN 归一 / 匹配
# ----------------------------------------------------------------------------
def mask7(sn: str) -> str:
    """把第 7 位(下标6)当通配 —— 设备读出的 SN 第7位常是 'U', 而表里是 'A',
    其余完全相同。屏蔽这一位即可稳定匹配。"""
    s = (sn or "").strip().upper()
    return s[:6] + "*" + s[7:] if len(s) >= 8 else s


def first3last6(sn: str) -> str:
    s = (sn or "").strip().upper()
    return f"{s[:3]}..{s[-6:]}" if len(s) >= 9 else s


def is_mac(text: str) -> bool:
    return bool(re.fullmatch(r"([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}", (text or "").strip()))


def norm_mac(text: str) -> str:
    return (text or "").strip().upper().replace("-", ":")


# ----------------------------------------------------------------------------
# 单机 HTTP 操作 (httpx async)
# ----------------------------------------------------------------------------
async def fetch_sn_6060(client: httpx.AsyncClient, ip: str, timeout: float = 3.0) -> Optional[str]:
    """免认证快速读 SN。返回字符串(可能是 SN_NO_READ),无响应返回 None。"""
    try:
        r = await client.get(f"http://{ip}:{PORT_SN}/get_sn", timeout=timeout)
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        return None
    return None


def _digest_auth(user: str, pwd: str) -> httpx.DigestAuth:
    return httpx.DigestAuth(user, pwd)


async def fetch_system_info(client: httpx.AsyncClient, ip: str, user=DEFAULT_USER,
                            pwd=DEFAULT_PASS, timeout: float = 7.0) -> Optional[dict]:
    """取 get_system_info.cgi (digest)。返回 dict 或 None。"""
    try:
        r = await client.get(f"http://{ip}/cgi-bin/get_system_info.cgi",
                             auth=_digest_auth(user, pwd), timeout=timeout)
        if r.status_code == 200:
            return _loose_json(r.text)
    except Exception:
        return None
    return None


def _loose_json(text: str) -> dict:
    """蚂蚁有些 cgi 前面有空行/杂字符, 用正则兜底解析键值。"""
    import json
    try:
        return json.loads(text)
    except Exception:
        pass
    out = {}
    for k, v in re.findall(r'"([^"]+)"\s*:\s*"([^"]*)"', text):
        out[k] = v
    return out


async def set_network(client: httpx.AsyncClient, ip: str, *, static: bool,
                      target_ip: str = "", mask: str = DEFAULT_MASK, gateway: str = "",
                      dns: str = "", hostname: str = DEFAULT_HOSTNAME,
                      user=DEFAULT_USER, pwd=DEFAULT_PASS, timeout: float = 12.0) -> dict:
    """下发网络配置 —— 一台只调一次!
    static=True 走静态 (需 target_ip/gateway/dns), False 走 DHCP。
    返回 {"ok":bool, "raw":str, "code":str}。"""
    if static:
        body = {"ipPro": IPPRO_STATIC, "ipHost": hostname, "ipAddress": target_ip,
                "ipSub": mask, "ipGateway": gateway, "ipDns": dns}
    else:
        body = {"ipPro": IPPRO_DHCP, "ipHost": hostname}
    try:
        r = await client.post(f"http://{ip}/cgi-bin/set_network_conf.cgi",
                              json=body, auth=_digest_auth(user, pwd), timeout=timeout)
        raw = r.text.strip()
        code = ""
        m = re.search(r'"code"\s*:\s*"([^"]*)"', raw)
        if m:
            code = m.group(1)
        ok = (code == "N000") or ('"success"' in raw)
        return {"ok": ok, "raw": raw, "code": code}
    except Exception as e:
        return {"ok": False, "raw": f"ERR {e}", "code": ""}


async def blink(client: httpx.AsyncClient, ip: str, on: bool,
                user=DEFAULT_USER, pwd=DEFAULT_PASS, timeout: float = 6.0) -> dict:
    """点灯定位: 让面板灯闪烁, 方便在机柜里找机器。"""
    try:
        r = await client.post(f"http://{ip}/cgi-bin/blink.cgi",
                              json={"blink": bool(on)}, auth=_digest_auth(user, pwd), timeout=timeout)
        return {"ok": r.status_code == 200, "raw": r.text.strip()}
    except Exception as e:
        return {"ok": False, "raw": f"ERR {e}"}


# ----------------------------------------------------------------------------
# 扫描
# ----------------------------------------------------------------------------
def expand_targets(subnets: Iterable[str]) -> list[str]:
    """把 '172.16.18.0/24' / '172.16.18' / '172.16.18.1-254' 展开成 IP 列表。"""
    ips: list[str] = []
    for s in subnets:
        s = s.strip()
        if not s:
            continue
        if "/" in s:
            ips += [str(x) for x in ipaddress.ip_network(s, strict=False).hosts()]
        elif re.fullmatch(r"\d+\.\d+\.\d+", s):
            ips += [f"{s}.{i}" for i in range(1, 255)]
        elif "-" in s and s.count(".") == 3:
            base, rng = s.rsplit(".", 1)
            lo, hi = rng.split("-")
            ips += [f"{base}.{i}" for i in range(int(lo), int(hi) + 1)]
        else:
            ips.append(s)
    return ips


async def scan(subnets: Iterable[str], *, concurrency: int = 80, detail: bool = True,
               user=DEFAULT_USER, pwd=DEFAULT_PASS,
               progress: Optional[Callable[[int, int], None]] = None) -> list[Miner]:
    """扫描网段, 返回在线矿机列表。
    先用 6060/get_sn 快速判活+读SN, 命中的再(可选)取 CGI 详情。"""
    targets = expand_targets(subnets)
    total = len(targets)
    done = 0
    sem = asyncio.Semaphore(concurrency)
    miners: list[Miner] = []
    lock = asyncio.Lock()

    async with httpx.AsyncClient() as client:
        async def worker(ip: str):
            nonlocal done
            async with sem:
                sn = await fetch_sn_6060(client, ip)
                async with lock:
                    done += 1
                    if progress:
                        progress(done, total)
                if sn is None:
                    return
                m = Miner(ip=ip, sn=sn if sn != SN_NO_READ else "")
                if detail:
                    info = await fetch_system_info(client, ip, user, pwd)
                    if info:
                        m.detail_ok = True
                        m.sn = info.get("serinum", m.sn) or m.sn
                        m.mac = norm_mac(info.get("macaddr", ""))
                        m.model = info.get("minertype", "")
                        m.nettype = info.get("nettype", "")
                        m.firmware = info.get("system_filesystem_version", "")
                async with lock:
                    miners.append(m)

        await asyncio.gather(*(worker(ip) for ip in targets))

    miners.sort(key=lambda x: tuple(int(p) for p in x.ip.split(".")))
    return miners


# ----------------------------------------------------------------------------
# 智能匹配: 把导入的表 (identifier -> target_ip) 配到在线机器
# ----------------------------------------------------------------------------
def build_plan(rows: list[dict], miners: list[Miner], *,
               skip_empty_slots: bool = False) -> list[PlanItem]:
    """rows: [{"identifier":SN或MAC, "target_ip":..., "rack":..., "hostname":...}]
    返回每条的匹配计划 (含状态/置信)。
    匹配优先级: SN精确 -> 第7位通配 -> 首三后六 -> MAC。"""
    # 建索引
    by_sn_exact, by_sn_mask, by_sn_f3l6, by_mac = {}, {}, {}, {}
    for m in miners:
        if m.sn_readable:
            by_sn_exact.setdefault(m.sn.strip().upper(), []).append(m)
            by_sn_mask.setdefault(mask7(m.sn), []).append(m)
            by_sn_f3l6.setdefault(first3last6(m.sn), []).append(m)
        if m.mac:
            by_mac.setdefault(norm_mac(m.mac), []).append(m)

    used_ips: dict[str, str] = {}      # current_ip -> target_ip (查重复指向)
    plan: list[PlanItem] = []
    for row in rows:
        ident = str(row.get("identifier", "")).strip()
        item = PlanItem(target_ip=str(row.get("target_ip", "")).strip(),
                        identifier=ident, rack=str(row.get("rack", "")),
                        hostname=str(row.get("hostname") or DEFAULT_HOSTNAME))
        if not ident or not item.target_ip:
            item.status = "skip"; item.note = "缺标识或目标IP"; plan.append(item); continue

        cands, mode = [], ""
        if is_mac(ident):
            cands, mode = by_mac.get(norm_mac(ident), []), "mac"
        else:
            key = ident.upper()
            for idx, mode_name in ((by_sn_exact.get(key), "exact"),
                                    (by_sn_mask.get(mask7(ident)), "mask7"),
                                    (by_sn_f3l6.get(first3last6(ident)), "first3last6")):
                if idx:
                    cands, mode = idx, mode_name
                    break

        if len(cands) == 1:
            m = cands[0]
            item.current_ip, item.device_sn, item.mac, item.match_mode = m.ip, m.sn, m.mac, mode
            if m.ip == item.target_ip:
                item.status, item.note = "skip", "已在目标IP"
            else:
                item.status = "ok"
        elif not cands:
            item.status, item.note = "not_found", "现网未扫到"
        else:
            item.status = "ambiguous"
            item.note = "匹配到多台: " + ", ".join(c.ip for c in cands)
            item.match_mode = mode
        plan.append(item)

    # 冲突检测: 多条 ok 指向同一台机器 / 多条目标同一IP
    cur_count, tgt_count = {}, {}
    for it in plan:
        if it.status == "ok":
            cur_count[it.current_ip] = cur_count.get(it.current_ip, 0) + 1
            tgt_count[it.target_ip] = tgt_count.get(it.target_ip, 0) + 1
    occupied = {m.ip for m in miners}
    for it in plan:
        if it.status != "ok":
            continue
        if cur_count.get(it.current_ip, 0) > 1:
            it.status, it.note = "conflict", "多条记录指向同一台机器"
        elif tgt_count.get(it.target_ip, 0) > 1:
            it.status, it.note = "conflict", "多条记录使用同一目标IP"
        elif it.target_ip in occupied and it.target_ip != it.current_ip:
            # 目标IP被别的在线机器占着 (且不是自己)
            it.status, it.note = "conflict", f"目标IP已被在线机器占用"
    return plan


# ----------------------------------------------------------------------------
# 安全应用 (一台一条命令 + 自动验证)
# ----------------------------------------------------------------------------
async def apply_plan(plan: list[PlanItem], *, gateway: str, dns: str,
                     mask: str = DEFAULT_MASK, concurrency: int = 12,
                     user=DEFAULT_USER, pwd=DEFAULT_PASS,
                     on_result: Optional[Callable[[PlanItem, dict], None]] = None
                     ) -> dict:
    """对 status==ok 的条目下发静态IP。每台只发一条命令。返回统计。"""
    todo = [it for it in plan if it.status == "ok"]
    sem = asyncio.Semaphore(concurrency)
    sent, failed = 0, 0
    lock = asyncio.Lock()

    async with httpx.AsyncClient() as client:
        async def worker(it: PlanItem):
            nonlocal sent, failed
            async with sem:
                res = await set_network(client, it.current_ip, static=True,
                                        target_ip=it.target_ip, mask=mask,
                                        gateway=gateway, dns=dns, hostname=it.hostname,
                                        user=user, pwd=pwd)
            async with lock:
                if res["ok"]:
                    sent += 1; it.note = "已下发, 重启中"
                else:
                    failed += 1; it.status = "failed"; it.note = res["raw"][:80]
            if on_result:
                on_result(it, res)
        await asyncio.gather(*(worker(it) for it in todo))
    return {"sent": sent, "failed": failed, "total": len(todo)}


async def verify_plan(plan: list[PlanItem], *, concurrency: int = 80,
                      user=DEFAULT_USER, pwd=DEFAULT_PASS) -> dict:
    """改完后重新扫目标IP, 核对每台是否上线且身份(SN/MAC)正确。"""
    sem = asyncio.Semaphore(concurrency)
    ok, missing, wrong = 0, [], []
    lock = asyncio.Lock()
    async with httpx.AsyncClient() as client:
        async def worker(it: PlanItem):
            nonlocal ok
            if it.status not in ("ok", "failed") or not it.target_ip:
                return
            async with sem:
                sn = await fetch_sn_6060(client, it.target_ip)
                mac = ""
                if sn is None or (it.match_mode == "mac"):
                    info = await fetch_system_info(client, it.target_ip, user, pwd)
                    if info:
                        sn = info.get("serinum", sn); mac = norm_mac(info.get("macaddr", ""))
            async with lock:
                if sn is None and not mac:
                    missing.append(it.target_ip); it.note = "验证: 未上线"
                else:
                    good = True
                    if it.match_mode == "mac" and it.mac:
                        good = (mac == norm_mac(it.mac))
                    elif it.device_sn and sn:
                        good = (mask7(sn) == mask7(it.device_sn))
                    if good:
                        ok += 1; it.status = "done"; it.note = "已验证"
                    else:
                        wrong.append(it.target_ip); it.note = f"验证: 身份不符({sn}/{mac})"
        await asyncio.gather(*(worker(it) for it in plan))
    return {"verified": ok, "missing": missing, "wrong": wrong}


# ----------------------------------------------------------------------------
# IP Report 监听 (模式 A): 收物理按钮的 UDP 广播
# ----------------------------------------------------------------------------
class IpReportListener:
    """监听 UDP 14235。按下矿机 IP Report 按钮时, 它会广播一个包含 MAC/IP 的包。
    用线程跑 socket, 解析出 IP/MAC 后回调。"""

    def __init__(self, on_report: Callable[[dict], None], port: int = IP_REPORT_PORT):
        self.on_report = on_report
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._thread = None

    def start(self):
        import threading
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def _run(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        try:
            s.bind(("", self.port))
        except Exception as e:
            self.on_report({"error": f"无法监听 {self.port}: {e}"})
            return
        self._sock = s
        s.settimeout(1.0)
        while self._running:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception:
                break
            self.on_report(self._parse(data, addr))

    @staticmethod
    def _parse(data: bytes, addr) -> dict:
        """尽力解析: 包格式各固件略有差异, 先抓 IP/MAC, 原始留底备调。"""
        text = data.decode("latin1", "ignore")
        ip = None
        m = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", text)
        if m:
            ip = m.group(1)
        mac = None
        mm = re.search(r"([0-9A-Fa-f]{2}([:\-])[0-9A-Fa-f]{2}(\2[0-9A-Fa-f]{2}){4})", text)
        if mm:
            mac = norm_mac(mm.group(1))
        # 也可能是二进制 6 字节 MAC
        if not mac and len(data) >= 6:
            mac = ":".join(f"{b:02X}" for b in data[:6])
        return {"ip": ip or addr[0], "mac": mac, "from": addr[0], "raw": data.hex()}


# ----------------------------------------------------------------------------
# Excel 导入
# ----------------------------------------------------------------------------
def load_excel(path: str) -> list[dict]:
    """读 Excel, 自动识别列: 标识(SN/MAC) / 目标IP / 机位 / 主机名。
    返回 [{"identifier":..., "target_ip":..., "rack":..., "hostname":...}]。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    def looks_ip(v):
        return bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", str(v).strip())) if v else False

    def looks_id(v):
        v = str(v).strip() if v else ""
        return is_mac(v) or bool(re.fullmatch(r"[A-Za-z0-9]{10,20}", v))

    # 找数据起始行 & 各列下标(扫前若干行投票)
    id_col = ip_col = rack_col = None
    sample = rows[:min(len(rows), 40)]
    ncol = max(len(r) for r in rows)
    for c in range(ncol):
        vals = [r[c] for r in sample if c < len(r)]
        if ip_col is None and sum(looks_ip(v) for v in vals) >= 2:
            ip_col = c
        if id_col is None and sum(looks_id(v) for v in vals) >= 2:
            id_col = c
    # 机位列: 在 id 和 ip 之间、含 '排/-/—' 的列
    if id_col is not None and ip_col is not None:
        for c in range(min(id_col, ip_col) + 1, max(id_col, ip_col)):
            vals = [str(r[c]) for r in sample if c < len(r) and r[c]]
            if any(("排" in v) or ("-" in v) or ("—" in v) for v in vals):
                rack_col = c; break

    out = []
    if id_col is None or ip_col is None:
        return out
    for r in rows:
        if id_col >= len(r) or ip_col >= len(r):
            continue
        ident, tip = r[id_col], r[ip_col]
        if not ident or not looks_ip(tip):
            continue
        if not looks_id(str(ident)):
            continue
        out.append({"identifier": str(ident).strip(), "target_ip": str(tip).strip(),
                    "rack": str(r[rack_col]).strip() if rack_col is not None and rack_col < len(r) and r[rack_col] else "",
                    "hostname": DEFAULT_HOSTNAME})
    return out


# ----------------------------------------------------------------------------
# 本机网段自动发现
# ----------------------------------------------------------------------------
def local_subnets() -> list[str]:
    """猜测本机相关的 /24 网段(给扫描默认值)。"""
    nets = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            a, b, c, _ = ip.split(".")
            nets.add(f"{a}.{b}.{c}.0/24")
    except Exception:
        pass
    return sorted(nets)


# ----------------------------------------------------------------------------
# 自测 (CLI): python core.py scan 172.16.19.0/24
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, json as _json

    async def _main():
        if len(sys.argv) >= 3 and sys.argv[1] == "scan":
            subs = sys.argv[2:]
            seen = {"n": 0}
            def prog(d, t):
                if d - seen["n"] >= 50 or d == t:
                    seen["n"] = d; print(f"\r扫描 {d}/{t}", end="", flush=True)
            ms = await scan(subs, progress=prog)
            print(f"\n在线 {len(ms)} 台:")
            for m in ms[:200]:
                print(f"  {m.ip:<16} {m.sn:<20} {m.mac:<18} {m.nettype:<7} {m.model}")
        else:
            print("用法: python core.py scan <网段...>   例: python core.py scan 172.16.19.0/24")

    asyncio.run(_main())
