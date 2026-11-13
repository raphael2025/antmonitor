# -*- coding: utf-8 -*-
"""SN 客户台账 + 日汇总；维修机不压客户可用率。"""
import time

import db
from conftest import rec, insert_scan


def test_sn_ledger_overrides_floating_worker(conn):
    now = int(time.time())
    db.upsert_known_miners(conn, [rec("10.0.0.1", worker="alice.1", sn="SNALICE01")], now)
    assert db.asset_ledger_map(conn)["SNALICE01"] == "alice.1"
    # 矿工名漂了，台账仍按上次可靠写入；再扫到新名则更新台账
    db.upsert_known_miners(conn, [rec("10.0.0.1", worker="bob.1", sn="SNALICE01")], now + 10)
    assert db.asset_ledger_map(conn)["SNALICE01"] == "bob.1"
    assert db.ledger_worker_for_ip(conn)["10.0.0.1"] == "bob.1"


def test_quiet_machine_excluded_from_uptime(conn, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 2_000_000_000)
    now = 2_000_000_000
    start = now - 3600
    a = rec("10.0.0.1", worker="cust.1", hr=100, status="online", sn="SN1")
    a["measured_ts"] = start + 1
    insert_scan(conn, start + 30, "quick", [a])
    db.upsert_known_miners(conn, [a], start + 30)
    db.set_machine_state(conn, ["10.0.0.1"], "repair")
    # 离线样本本会压低可用率；维修机应从分母剔除
    b = rec("10.0.0.1", worker="cust.1", status="offline", sn="SN1")
    b["measured_ts"] = start + 1800
    insert_scan(conn, start + 1830, "quick", [b])
    _s, workers = db._customer_metric_window(conn, start, now)
    # 维修中被剔除后可能没有 cust.1 样本，或 known_machine_s 不再累计离线段
    row = workers.get("cust.1")
    if row:
        assert row["known_machine_s"] == 0 or row["online_machine_s"] == row["known_machine_s"]


def test_customer_daily_rollup(conn, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 2_000_100_000)
    now = 2_000_100_000
    for i in range(3):
        ts = now - 7200 + i * 600
        r = rec("10.1.0.1", worker="day.1", hr=50, sn="SNDAY1")
        r["measured_ts"] = ts
        insert_scan(conn, ts + 5, "quick", [r])
        db.upsert_known_miners(conn, [r], ts + 5)
    db.rollup_hours(conn, now=now, max_hours=24)
    n = db.rollup_customer_daily(conn, day_offsets=(0, 1), now=now)
    assert n >= 1
    packet = db.customer_daily_report(conn, days=3, until_ts=now)
    assert packet["tz"] == "UTC+8"
    assert any(r["worker"] == "day.1" for r in packet["customers_daily"])
