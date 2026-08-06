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

断网期间不缓存重传（总览缺几分钟无妨，云端会显示"未上报"变灰），恢复自动续传。
上报失败只打日志，绝不影响本地扫描/告警。
"""
import os
import secrets
import threading
import time

import requests

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


def _post(c, path, payload):
    r = requests.post(c["url"].rstrip("/") + path, json=payload,
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
                })
                pushed = True
                if fails:
                    log.info("上报恢复(此前连续失败 %d 次)", fails)
                fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
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
                       "hours": cust_hours, "customers": rows})
                last_cust = t0
            except Exception as e:  # noqa: BLE001
                log.warning("客户报表上报失败(下轮重试): %s", e)
        _sleep(gen, max(5.0, interval - (time.time() - t0)))
