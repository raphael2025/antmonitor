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

_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud_site_id.txt")


def site_id(cfg):
    """本场地稳定唯一ID。config.cloud.site_id 覆盖 > cloud_site_id.txt > 自动生成并持久化。"""
    c = cfg.get("cloud") or {}
    if c.get("site_id"):
        return str(c["site_id"]).strip()
    try:
        with open(_ID_FILE, encoding="ascii") as f:
            sid = f.read().strip()
        if sid:
            return sid
    except OSError:
        pass
    sid = "st-" + secrets.token_hex(6)
    with open(_ID_FILE, "w", encoding="ascii") as f:
        f.write(sid + "\n")
    return sid


def enabled(cfg):
    c = cfg.get("cloud") or {}
    return bool(c.get("enabled") and c.get("url") and c.get("token") and c.get("site_name"))


def start(cfg, get_summary, get_customers):
    """get_summary: () -> /api/public/summary 同构 dict
    get_customers: (hours:int) -> customers 列表(worker/machines/uptime_pct/delivered_th_h/power_kwh)"""
    if not enabled(cfg):
        return None
    sid = site_id(cfg)
    t = threading.Thread(target=_loop, args=(cfg.get("cloud"), sid, get_summary, get_customers),
                         daemon=True, name="cloud-report")
    t.start()
    return t


def _post(c, path, payload):
    r = requests.post(c["url"].rstrip("/") + path, json=payload,
                      timeout=float(c.get("timeout", 10)),
                      headers={"Authorization": f"Bearer {c.get('token', '')}"})
    r.raise_for_status()


def _loop(c, sid, get_summary, get_customers):
    interval = max(10, int(c.get("interval", 60)))
    cust_iv = max(60, int(c.get("customer_interval", 600)))
    cust_hours = int(c.get("customer_hours", 24))
    last_cust = 0.0
    fails = 0
    while True:
        t0 = time.time()
        pushed = False
        try:
            s = get_summary() or {}
            if s.get("ok") and s.get("scanned"):   # 尚未完成首扫时不推零数据
                now = int(time.time())
                scan_ts = s.get("scan_ts") or 0
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
                })
                pushed = True
                if fails:
                    print(f"[cloud] 上报恢复(此前连续失败 {fails} 次)")
                fails = 0
        except Exception as e:
            fails += 1
            if fails <= 3 or fails % 30 == 0:   # 断网时别刷屏
                print(f"[cloud] 上报失败×{fails}: {e}")
        # 客户报表独立重试：失败不推迟到下个 cust_iv，下一轮(interval)就再试
        if pushed and t0 - last_cust >= cust_iv:
            try:
                rows = get_customers(cust_hours) or []
                _post(c, "/api/ingest/customers",
                      {"site_id": sid, "site": c["site_name"],
                       "hours": cust_hours, "customers": rows})
                last_cust = t0
            except Exception as e:
                print(f"[cloud] 客户报表上报失败(下轮重试): {e}")
        time.sleep(max(5.0, interval - (time.time() - t0)))
