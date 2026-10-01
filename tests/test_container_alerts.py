# -*- coding: utf-8 -*-
"""集装箱冷却告警：一次读数抖动/缺失不能让漏液这类故障"没有活跃告警"。

以前：某轮故障位没读到或压力读数为空 → 立即消警 → 下一轮故障还在，却被 30 分钟冷却拦下
→ 漏液持续存在但面板上没有告警，最长 30 分钟。
"""
import time

import pytest

import alerts
import db

IP = "10.0.9.1"


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    c = Clock()
    monkeypatch.setattr(time, "time", c)
    return c


def box(faults=(), sp=0.3, rp=0.2):
    return {"ip": IP, "faults": [{"flag": f, "label": f, "sev": s} for f, s in faults],
            "supply_pressure": sp, "return_pressure": rp}


def run(conn, cfg, c, st, clock=None, dt=10):
    if clock is not None:
        clock.t += dt                   # 集装箱每 10 秒刷新一轮
    return [(f[0], f[1]) for f in alerts.evaluate_containers(conn, [c], {IP}, cfg, state=st)]


def active(conn):
    return sorted(a["type"] for a in db.active_alerts_by_type(conn, like="cooler"))


LEAK = [("leakage_fault", "crit")]


def test_one_clean_reading_does_not_clear_a_leak(conn, cfg, clock):
    st = {}
    assert run(conn, cfg, box(LEAK), st, clock) == [(IP, "cooler:leakage_fault")]
    run(conn, cfg, box(), st, clock)                       # 抖了一次
    assert active(conn) == ["cooler:leakage_fault"]
    run(conn, cfg, box(LEAK), st, clock)                   # 故障还在
    assert active(conn) == ["cooler:leakage_fault"]
    assert len(db.list_alerts(conn, False, 100)) == 1      # 没有重复刷一条新的


def test_fault_clears_after_sustained_clean_readings(conn, cfg, clock):
    st = {}
    run(conn, cfg, box(LEAK), st, clock)
    for _ in range(alerts.COOLER_CLEAR_SEC // 10 - 1):
        run(conn, cfg, box(), st, clock)
    assert active(conn) == ["cooler:leakage_fault"]        # 还没持续正常满 5 分钟
    for _ in range(3):
        run(conn, cfg, box(), st, clock)
    assert active(conn) == []


def test_crit_fault_recurring_within_cooldown_alerts_again(conn, cfg, clock):
    cfg["alerts"]["cooldown"] = 1800
    st = {}
    run(conn, cfg, box(LEAK), st, clock)
    run(conn, cfg, box(), st, clock)                       # 开始读到正常
    run(conn, cfg, box(), st, clock, dt=alerts.COOLER_CLEAR_SEC)   # 持续正常满 5 分钟 → 真的恢复了
    assert active(conn) == []
    assert run(conn, cfg, box(LEAK), st, clock) == [(IP, "cooler:leakage_fault")]   # 又漏了：必须报


def test_missing_pressure_reading_keeps_the_pressure_alert(conn, cfg, clock):
    cfg["alerts"]["container_supply_pressure_min"] = 0.2
    cfg["alerts"]["container_pressure_hold_sec"] = 0   # 本用例测"缺读数≠恢复"，关掉确认窗口
    st = {}
    run(conn, cfg, box(sp=0.1), st, clock)
    assert active(conn) == ["cooler:supply_pressure_low"]
    for _ in range(60):
        run(conn, cfg, box(sp=None), st, clock)            # 传感器读不到 ≠ 压力恢复
    assert active(conn) == ["cooler:supply_pressure_low"]


def test_flaky_leak_contact_does_not_spam(conn, cfg, clock):
    """漏液触点接触不良、每 50 秒闪一次：以前 3 轮(30 秒)就消警、复发又不受冷却立刻重报，
    每箱每小时 70 多条严重推送。现在持续正常满 5 分钟才消，一直闪就一直是同一条告警。"""
    st = {}
    for k in range(360):                                   # 1 小时，每 10 秒一轮
        run(conn, cfg, box(LEAK if k % 5 == 0 else ()), st, clock)
    assert len(db.list_alerts(conn, False, 1000)) == 1


def test_brief_return_pressure_dip_during_refill_does_not_alert(conn, cfg, clock):
    """自动补水瞬间回液压掉一下（常见仅 1 个 10s 采样），不应立刻报 crit。"""
    cfg["alerts"]["container_return_pressure_min"] = 0.05
    cfg["alerts"]["container_pressure_hold_sec"] = 60
    st = {}
    run(conn, cfg, box(sp=0.3, rp=0.04), st, clock)          # 掉压
    assert active(conn) == []
    for _ in range(4):                                         # 再抖几下，仍 < 60s
        run(conn, cfg, box(sp=0.3, rp=0.03), st, clock)
    assert active(conn) == []
    run(conn, cfg, box(sp=0.38, rp=0.15), st, clock)           # 补水结束恢复
    assert active(conn) == []


def test_sustained_low_return_pressure_alerts_after_hold(conn, cfg, clock):
    cfg["alerts"]["container_return_pressure_min"] = 0.05
    cfg["alerts"]["container_pressure_hold_sec"] = 60
    st = {}
    run(conn, cfg, box(sp=0.3, rp=0.04), st, clock)
    assert active(conn) == []
    run(conn, cfg, box(sp=0.3, rp=0.04), st, clock, dt=60)     # 持续满 hold
    assert active(conn) == ["cooler:return_pressure_low_th"]


def test_pressure_alert_clears_faster_than_leak(conn, cfg, clock):
    cfg["alerts"]["container_return_pressure_min"] = 0.05
    cfg["alerts"]["container_pressure_hold_sec"] = 0            # 立即报，测清警
    cfg["alerts"]["container_pressure_clear_sec"] = 30
    st = {}
    run(conn, cfg, box(sp=0.3, rp=0.02), st, clock)
    assert active(conn) == ["cooler:return_pressure_low_th"]
    run(conn, cfg, box(sp=0.38, rp=0.15), st, clock)           # 恢复开始计时
    assert active(conn) == ["cooler:return_pressure_low_th"]
    run(conn, cfg, box(sp=0.38, rp=0.15), st, clock, dt=30)
    assert active(conn) == []
