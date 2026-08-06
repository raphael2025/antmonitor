# -*- coding: utf-8 -*-
"""测试公共夹具：内存库 + 记录构造器。

用 :memory: 库时 db._r() 会回退到写连接（没有独立文件可开第二条连接），
这正是我们要的：测试里读写同一条连接，行为与生产等价但无需落盘。
"""
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必须在导入业务模块之前配好日志：任何模块 import 时就会 logs.get() 触发惰性 setup，
# 那样测试就会往生产的 logs/miner.log 里写噪音。setup 只生效第一次，先下手为强。
import logs  # noqa: E402

logs.setup({"logging": {"file": os.path.join(tempfile.gettempdir(), "miner_test.log"),
                        "level": "WARNING", "max_mb": 1, "backups": 1}})

import appconfig  # noqa: E402
import db  # noqa: E402

HOUR = 3600


@pytest.fixture
def conn():
    c = db.init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def cfg():
    return appconfig._merge(appconfig.DEFAULTS, {})


def rec(ip, status="online", hr=100.0, model="S19", **kw):
    """构造一条与 miner_core 输出同构的扫描记录。"""
    r = {"device": "miner", "ip": ip, "status": status, "firmware": "stock",
         "model": model, "sn": "SN" + ip.replace(".", ""), "hr_rt": hr, "hr_avg": hr,
         "power": 3000, "temp": 70, "eff": 20.0, "uptime": 100000, "worker": "cust1",
         "accepted": 1000, "rejected": 0, "stale": 0, "note": ""}
    if status != "online":
        # 与 miner_core._blank 一致：离线机读不到任何运行数据，矿工名也为空
        # （真实流程里由 service 从 known_miners 回填身份，但 worker 归属要靠
        #   在线样本推导——报表逻辑必须能处理这种"离线行没有客户名"的情况）
        r.update({"hr_rt": None, "hr_avg": None, "power": None, "temp": None,
                  "eff": None, "uptime": None, "worker": ""})
    r.update(kw)
    return r


def insert_scan(conn, ts, kind, records):
    """按指定时间戳直接插入一次扫描（save_scan 用 time.time()，测历史时不可控）。"""
    online = sum(1 for r in records if r["status"] == "online")
    total_hr = round(sum(r.get("hr_rt") or 0 for r in records), 2)
    cur = conn.execute(
        "INSERT INTO scans(ts,kind,total,online,offline,total_hr) VALUES(?,?,?,?,?,?)",
        (ts, kind, len(records), online, len(records) - online, total_hr))
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO snapshots(scan_id,ip,status,firmware,model,sn,hr_rt,hr_avg,power,temp,"
        "eff,uptime,worker,accepted,rejected,stale,note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(sid, r["ip"], r["status"], r["firmware"], r["model"], r["sn"], r.get("hr_rt"),
          r.get("hr_avg"), r.get("power"), r.get("temp"), r.get("eff"), r.get("uptime"),
          r.get("worker", ""), r.get("accepted"), r.get("rejected"), r.get("stale"),
          r.get("note", "")) for r in records])
    conn.commit()
    return sid


def now_hour(offset_hours=0):
    n = int(time.time())
    return n - n % HOUR + offset_hours * HOUR
