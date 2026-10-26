#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端总览上报线程：本场地 → 云端(E:\\main 那套, 部署在云服务器)。

每 cloud.interval(默认60s) 推一次场地摘要、每 cloud.customer_interval(默认600s)
推一次客户报表。**方向是本地主动推**，场地在 NAT 后面不需要任何端口映射。

场地身份 = 稳定唯一 site_id（云端按它识别场地，改名不会串数据）：
  优先 config.cloud.site_id（显式指定，迁移/恢复用）；
  否则读/自动生成 cloud_site_id.txt（与本文件同目录，一次生成永不变）。
⚠ 把整个目录克隆部署到新场地时，必须删除 cloud_site_id.txt（会自动重新生成新ID），
  否则两个场地同一个ID，云端数据互相覆盖！

config.yaml 增加：
cloud:
  enabled: true
  url: "https://overview.example.com"   # 云端总览地址
  token: "云端 config.yaml > ingest.token(或分配给本场地的专属token)"
  site_name: "XX一场"                    # 云端显示名(可改，改名不影响数据归属)
  site_type: air                         # air=风冷 | hydro=水冷 | mixed=混合(只影响云端分组显示)
  site_id: ""                            # 一般留空自动生成；迁移机器时填旧ID
  interval: 60
  customer_interval: 600
  customer_hours: 24
  timeout: 10
  tls_fingerprint: ""                    # 云端没有域名、用自签证书(https://IP:端口)时填云端面板
                                         # 「场地接入」里显示的证书指纹(SHA-256)，按指纹认证书

断网期间不缓存重传（总览缺几分钟无妨，云端会显示"未上报"变灰），恢复自动续传。
上报失败只打日志，绝不影响本地扫描/告警。
"""
import os
import re
import secrets
import threading
import time
import warnings

import requests
from requests.adapters import HTTPAdapter

import logs

log = logs.get(__name__)

_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud_site_id.txt")


def site_id(cfg):
    """本场地稳定唯一ID。config.cloud.site_id 覆盖 > cloud_site_id.txt > 自动生成并持久化。"""
    c = cfg.get("cloud") or {}
    if c.get("site_id"):
        return str(c["site_id"]).strip()
    try:
        # errors="replace": 文件被断电写残/误编辑成含中文时不抛 UnicodeDecodeError，
        # 脏内容由下面的 isascii/isprintable 校验拦掉并重新生成；except 再兜一层。
        with open(_ID_FILE, encoding="ascii", errors="replace") as f:
            sid = f.read().strip()
        if sid and sid.isascii() and sid.isprintable():
            return sid
        if sid:
            log.warning("%s 内容异常(%r)，忽略并重新生成场地ID", _ID_FILE, sid[:40])
    except FileNotFoundError:
        pass                      # 首次运行，正常
    except (OSError, UnicodeError, ValueError) as e:
        log.warning("读取场地ID文件失败(%s)，将重新生成", e)
    sid = "st-" + secrets.token_hex(6)
    with open(_ID_FILE, "w", encoding="ascii") as f:
        f.write(sid + "\n")
    return sid


_FP_RE = re.compile(r"^[0-9a-f]{64}$")


def normalize_fingerprint(fp):
    """'AB:CD:…' / 'ab cd …' / 'sha256:…' → 64 位小写十六进制；空串原样返回空；非法返回 None。"""
    raw = str(fp or "").strip().lower()
    if raw.startswith("sha256:"):
        raw = raw[7:]
    raw = re.sub(r"[\s:]", "", raw)
    if not raw:
        return ""
    return raw if _FP_RE.match(raw) else None


class _PinnedAdapter(HTTPAdapter):
    """按证书 SHA-256 指纹认云端(自签证书 / 没有域名时)。

    不走 CA 校验，但握手时把对端证书的指纹和配置里的比对，对不上直接断开——
    比"关掉证书校验"安全得多：中间人拿不到云端私钥就伪造不出同指纹的证书。"""

    def __init__(self, fingerprint, **kw):
        self._fp = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, *a, **kw):
        kw["assert_fingerprint"] = self._fp
        kw["assert_hostname"] = False
        return super().init_poolmanager(*a, **kw)


def _session(c):
    s = requests.Session()
    fp = normalize_fingerprint(c.get("tls_fingerprint"))
    if fp:
        s.mount("https://", _PinnedAdapter(fp))
        s.verify = False   # 证书链不校验，改由上面的指纹校验
    return s


# 上报状态：给本地面板显示"最后一次成功/失败原因"，不用登机器翻日志
_STATUS = {"last_ok": 0, "last_try": 0, "last_err": "", "fails": 0, "last_customers_ok": 0}
_status_lock = threading.Lock()


def _set_status(**kw):
    with _status_lock:
        _STATUS.update(kw)


def status():
    with _status_lock:
        return dict(_STATUS)


def explain(e):
    """把上报异常翻成值班员看得懂的一句话。"""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    if code == 401:
        return "上报 token 不对(401)：到云端面板「场地接入」核对后重新填写"
    if code == 404:
        return "云端上报接口未开启(404)：云端还没生成上报 token，或云端版本过旧"
    if code == 403:
        detail = ""
        try:
            detail = (resp.json() or {}).get("detail") or ""
        except Exception:  # noqa: BLE001
            pass
        return f"云端拒绝(403){'：' + detail if detail else ''}"
    if code == 410:
        return "本场地已在云端被删除(410)：请云端管理员在「已删除场地」里恢复"
    if code == 429:
        return "上报太频繁被云端限速(429)"
    if code:
        return f"云端返回错误 HTTP {code}"
    text = str(e)
    if "fingerprint" in text.lower():
        return "云端证书指纹不匹配：可能连错了服务器或被中间人劫持，请到云端面板核对指纹"
    if isinstance(e, requests.exceptions.SSLError):
        return ("HTTPS 证书校验失败：云端用的是自签证书(按 IP 访问)时，"
                "请填写云端面板「场地接入」里显示的证书指纹")
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return "连接云端超时：检查云端地址/端口、云服务器防火墙(安全组)放行，以及本场地能否上外网"
    if isinstance(e, requests.exceptions.ConnectionError):
        return f"连不上云端：{text[:160]}"
    if isinstance(e, requests.exceptions.Timeout):
        return "云端响应超时"
    return text[:200]


def test(cfg_cloud, sid):
    """给网页「测试连接」用：发一条 ping(只校验 token，不写数据)。返回 (ok, 说明)。"""
    c = dict(cfg_cloud or {})
    if not c.get("url") or not c.get("token"):
        return False, "请先填写云端地址和上报 token"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = _session(c).post(c["url"].rstrip("/") + "/api/ingest/ping",
                                 json={"site_id": sid, "site": c.get("site_name") or sid},
                                 timeout=float(c.get("timeout", 10)),
                                 headers={"Authorization": f"Bearer {c.get('token', '')}"})
        if r.status_code == 404:
            return False, ("云端没有 ping 接口(云端版本过旧，或上报接口未开启)；"
                           "可直接保存并启用，看下方上报状态是否成功")
        r.raise_for_status()
        d = r.json() if r.content else {}
        if d.get("registered"):
            return True, f"连接成功：云端已识别本场地(显示名「{d.get('site_name') or sid}」)"
        return True, "连接成功：token 有效，保存并启用后场地会自动出现在云端面板"
    except Exception as e:  # noqa: BLE001
        return False, explain(e)


def enabled(cfg):
    c = cfg.get("cloud") or {}
    return bool(c.get("enabled") and c.get("url") and c.get("token") and c.get("site_name"))


_GEN = 0   # 代数：每次 start() 递增使旧线程下一轮自行退出——支持网页改配置后热重启
_thread = None   # 当前上报线程引用，供 is_alive() 给看门狗做存活检查


def is_alive():
    """上报线程是否还活着，供外部(server.py 的 guardian 循环)做存活检查+自愈。
    未启用云端上报时线程本就不存在，调用方应先用 enabled(cfg) 判断。"""
    return bool(_thread and _thread.is_alive())


def start(cfg, get_summary, get_customers):
    """启动/热重启上报线程(旧线程自动失效)。未启用时仅停掉旧线程。
    get_summary: () -> /api/public/summary 同构 dict
    get_customers: (hours:int) -> customers 列表(worker/machines/uptime_pct/delivered_th_h/power_kwh)"""
    global _GEN, _thread
    _GEN += 1        # 先递增：即使下面初始化失败，旧线程也必须停掉
    try:
        if not enabled(cfg):
            _thread = None
            return None
        c = dict(cfg.get("cloud") or {})
        sid = site_id(cfg)
        if str(c.get("url") or "").strip().lower().startswith("http://"):
            log.warning("云端上报地址使用了非加密的 http:// 协议，token 将明文传输，建议改用 https://")
        t = threading.Thread(target=_loop, args=(_GEN, c, sid, get_summary, get_customers),
                             daemon=True, name="cloud-report")
        t.start()
        _thread = t
        return t
    except Exception as e:  # noqa: BLE001  上报是附加功能，绝不能拖垮主服务启动
        log.error("云端上报初始化失败，本功能已禁用，原因：%s", e, exc_info=True)
        _thread = None
        return None


def _post(c, path, payload, sess=None):
    with warnings.catch_warnings():
        # 指纹模式下 verify=False 会触发 InsecureRequestWarning；已由指纹校验兜底，不刷日志
        warnings.simplefilter("ignore")
        r = (sess or requests).post(c["url"].rstrip("/") + path, json=payload,
                                    timeout=float(c.get("timeout", 10)),
                                    headers={"Authorization": f"Bearer {c.get('token', '')}"})
    r.raise_for_status()


def _sleep(gen, secs):
    """分片休眠：代数一变(网页改配置热重启)立刻醒来退出，避免旧线程占着整轮 sleep。"""
    end = time.time() + secs
    while gen == _GEN:
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def _loop(gen, c, sid, get_summary, get_customers):
    interval = max(10, int(c.get("interval", 60)))
    cust_iv = max(60, int(c.get("customer_interval", 600)))
    cust_hours = int(c.get("customer_hours", 24))
    last_cust = 0.0
    fails = 0
    sess = _session(c)
    _set_status(last_err="", fails=0)
    while gen == _GEN:   # 配置被网页改过(代数变了)就退出，由新线程接管
        t0 = time.time()
        pushed = False
        try:
            s = get_summary() or {}
            if s.get("ok") and s.get("scanned"):   # 尚未完成首扫时不推零数据
                now = int(time.time())
                scan_ts = s.get("scan_ts") or 0
                if gen != _GEN:   # 取数期间配置被改：这一轮别再用旧token/旧名字推了
                    break
                _set_status(last_try=now)
                _post(c, "/api/ingest/summary", {
                    "site_id": sid, "site": c["site_name"],
                    "type": c.get("site_type", "air"), "ts": now,
                    "online": s.get("online") or 0, "total": s.get("total") or 0,
                    "hashrate_ths": s.get("total_hashrate_ths") or 0.0,
                    "power_kw": s.get("total_power_kw") or 0.0,
                    "active_alerts": s.get("active_alerts") or 0,
                    "containers": s.get("containers") or 0,
                    "containers_faulty": s.get("containers_faulty") or 0,
                    "containers_offline": s.get("containers_offline") or 0,
                    "scan_age_s": (now - scan_ts) if scan_ts else None,
                    "alert_summary": s.get("alert_summary") or [],
                }, sess)
                pushed = True
                if fails:
                    log.info("上报恢复(此前连续失败 %d 次)", fails)
                fails = 0
                _set_status(last_ok=int(time.time()), last_err="", fails=0)
            else:
                _set_status(last_err="本地还没完成首轮扫描，扫完后自动开始上报")
        except Exception as e:  # noqa: BLE001
            fails += 1
            if gen == _GEN:
                _set_status(last_err=explain(e), fails=fails, last_try=int(time.time()))
            if fails <= 3 or fails % 30 == 0:   # 断网时别刷屏
                log.warning("上报失败×%d: %s", fails, e)
        if gen != _GEN:   # 每次发请求前都重新确认代数，缩短热重启时新旧线程重叠窗口
            break
        # 客户报表独立重试：失败不推迟到下个 cust_iv，下一轮(interval)就再试
        if pushed and t0 - last_cust >= cust_iv:
            try:
                rows = get_customers(cust_hours) or []
                if gen != _GEN:
                    break
                _post(c, "/api/ingest/customers",
                      {"site_id": sid, "site": c["site_name"],
                       "hours": cust_hours, "customers": rows}, sess)
                last_cust = t0
                _set_status(last_customers_ok=int(time.time()))
            except Exception as e:  # noqa: BLE001
                log.warning("客户报表上报失败(下轮重试): %s", e)
        _sleep(gen, max(5.0, interval - (time.time() - t0)))
