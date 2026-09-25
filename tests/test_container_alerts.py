# -*- coding: utf-8 -*-
"""集装箱冷却告警：一次读数抖动/缺失不能让漏液这类故障"没有活跃告警"。

以前：某轮故障位没读到或压力读数为空 → 立即消警 → 下一轮故障还在，却被 30 分钟冷却拦下
→ 漏液持续存在但面板上没有告警，最长 30 分钟。
"""
import pytest

import alerts
import db

IP = "10.0.9.1"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)


def box(faults=(), sp=0.3, rp=0.2):
    return {"ip": IP, "faults": [{"flag": f, "label": f, "sev": s} for f, s in faults],
            "supply_pressure": sp, "return_pressure": rp}


def run(conn, cfg, c, st):
    return [(f[0], f[1]) for f in alerts.evaluate_containers(conn, [c], {IP}, cfg, state=st)]


def active(conn):
    return sorted(a["type"] for a in db.active_alerts_by_type(conn, like="cooler"))


LEAK = [("leakage_fault", "crit")]


def test_one_clean_reading_does_not_clear_a_leak(conn, cfg):
    st = {}
    assert run(conn, cfg, box(LEAK), st) == [(IP, "cooler:leakage_fault")]
    run(conn, cfg, box(), st)                              # 抖了一次
    assert active(conn) == ["cooler:leakage_fault"]
    run(conn, cfg, box(LEAK), st)                          # 故障还在
    assert active(conn) == ["cooler:leakage_fault"]
    assert len(db.list_alerts(conn, False, 100)) == 1      # 没有重复刷一条新的


def test_fault_clears_after_consecutive_clean_readings(conn, cfg):
    st = {}
    run(conn, cfg, box(LEAK), st)
    for _ in range(alerts.COOLER_CLEAR_ROUNDS):
        run(conn, cfg, box(), st)
    assert active(conn) == []


def test_crit_fault_recurring_within_cooldown_alerts_again(conn, cfg):
    cfg["alerts"]["cooldown"] = 1800
    st = {}
    run(conn, cfg, box(LEAK), st)
    for _ in range(alerts.COOLER_CLEAR_ROUNDS):
        run(conn, cfg, box(), st)                          # 真的恢复了
    assert run(conn, cfg, box(LEAK), st) == [(IP, "cooler:leakage_fault")]   # 又漏了：必须报


def test_missing_pressure_reading_keeps_the_pressure_alert(conn, cfg):
    cfg["alerts"]["container_supply_pressure_min"] = 0.2
    st = {}
    run(conn, cfg, box(sp=0.1), st)
    assert active(conn) == ["cooler:supply_pressure_low"]
    for _ in range(alerts.COOLER_CLEAR_ROUNDS + 2):
        run(conn, cfg, box(sp=None), st)                   # 传感器读不到 ≠ 压力恢复
    assert active(conn) == ["cooler:supply_pressure_low"]
