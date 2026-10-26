# -*- coding: utf-8 -*-
"""大场地防误报：跨轮次确认、全场事件合并、"连得上但读不到"不算离线。

大场地最常见的误报都是一两分钟就过去的事：交换机重启 / STP 收敛 / ARP 表刷新、矿机网页服务
(lighttpd/CGI)卡顿、换矿池瞬间读到 0 算力、单颗温度传感器跳值。这些用例用默认配置
(offline_confirm_sec=240 等)跑，确认"抖一下不报，持续才报"。
"""
import time

import pytest

import alerts
import appconfig
import db
import miner_core
from conftest import rec


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def tick(self, sec=300):
        self.t += sec


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(time, "time", c)
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    return c


@pytest.fixture
def dcfg():
    """出厂默认配置(带防误报确认)。"""
    return appconfig._validate(appconfig._merge(appconfig.DEFAULTS, {}))


def _round(conn, cfg, clock, records, state, sec=300):
    clock.tick(sec)
    sid, ts, kept = db.save_scan(conn, "quick", records)
    alive = {r["ip"] for r in records if r.get("alive")}
    for r in kept:
        if r["ip"] in alive:
            r["alive"] = True
    db.upsert_known_miners(conn, [r for r in records if r["status"] == "online"], ts)
    return sorted((f[0], f[1]) for f in alerts.evaluate(conn, sid, kept, cfg, state=state))


def _seg(n, prefix="10.0.1", **kw):
    return [rec(f"{prefix}.{i}", **kw) for i in range(1, n + 1)]


def test_offline_blip_is_not_reported(conn, dcfg, clock):
    st = {}
    _round(conn, dcfg, clock, [rec("10.0.0.1"), rec("10.0.0.2")], st)
    # 掉一轮又回来：不报
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", status="offline"), rec("10.0.0.2")], st) == []
    assert _round(conn, dcfg, clock, [rec("10.0.0.1"), rec("10.0.0.2")], st) == []
    assert not db.active_alerts_by_type(conn, "offline")


def test_offline_reported_after_confirm_window(conn, dcfg, clock):
    st = {}
    _round(conn, dcfg, clock, [rec("10.0.0.1"), rec("10.0.0.2")], st)
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", status="offline"), rec("10.0.0.2")], st) == []
    # 下一轮(300s > 240s)仍离线 → 报
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", status="offline"), rec("10.0.0.2")], st) \
        == [("10.0.0.1", "offline")]


def test_confirm_zero_restores_immediate_reporting(conn, dcfg, clock):
    dcfg["alerts"]["offline_confirm_sec"] = 0
    st = {}
    _round(conn, dcfg, clock, [rec("10.0.0.1"), rec("10.0.0.2")], st)
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", status="offline"), rec("10.0.0.2")], st) \
        == [("10.0.0.1", "offline")]


def test_switch_reboot_blip_raises_nothing(conn, dcfg, clock):
    """整段掉一轮(交换机重启)又回来：既不报整段掉线，也不逐台报。"""
    st = {}
    good = _seg(10) + _seg(10, "10.0.2")
    _round(conn, dcfg, clock, good, st)
    blip = _seg(10, status="offline") + _seg(10, "10.0.2")
    assert _round(conn, dcfg, clock, blip, st) == []
    assert _round(conn, dcfg, clock, good, st) == []


def test_segment_down_after_confirm_mutes_singles(conn, dcfg, clock):
    st = {}
    good = _seg(10) + _seg(10, "10.0.2")
    _round(conn, dcfg, clock, good, st)
    down = _seg(10, status="offline") + _seg(10, "10.0.2")
    assert _round(conn, dcfg, clock, down, st) == []
    assert _round(conn, dcfg, clock, down, st) == [("10.0.1.x", "segment_down")]


def test_site_wide_outage_is_one_alert(conn, dcfg, clock):
    dcfg["alerts"]["site_down_min"] = 10
    st = {}
    good = _seg(10) + _seg(10, "10.0.2") + _seg(10, "10.0.3")
    _round(conn, dcfg, clock, good, st)
    allout = (_seg(10, status="offline") + _seg(10, "10.0.2", status="offline")
              + _seg(10, "10.0.3"))
    assert _round(conn, dcfg, clock, allout, st) == []
    # 确认后只有一条全场告警：没有逐段 segment_down，也没有逐台 offline
    assert _round(conn, dcfg, clock, allout, st) == [(alerts.SITE_KEY, "site_down")]
    assert _round(conn, dcfg, clock, allout, st) == []
    # 恢复 → 消警
    _round(conn, dcfg, clock, good, st)
    assert not db.active_alerts_by_type(conn, "site_down")


def test_reachable_but_silent_miner_escalates_after_rounds(conn, dcfg, clock):
    st = {}
    _round(conn, dcfg, clock, [rec("10.0.0.1"), rec("10.0.0.2")], st)
    silent = rec("10.0.0.1", status="unknown", alive=True, hr=None)
    for _ in range(2):   # 前两轮：不报(可能只是网页服务卡一下)
        assert _round(conn, dcfg, clock, [silent, rec("10.0.0.2")], st) == []
    assert _round(conn, dcfg, clock, [silent, rec("10.0.0.2")], st) == [("10.0.0.1", "offline")]
    # 回来就消
    _round(conn, dcfg, clock, [rec("10.0.0.1"), rec("10.0.0.2")], st)
    assert not db.active_alerts_by_type(conn, "offline")


def test_scan_timeout_unknown_never_escalates(conn, dcfg, clock):
    """扫描超时没来得及探测(没有 alive 标记)：保持原状，不按掉线升级。"""
    st = {}
    _round(conn, dcfg, clock, [rec("10.0.0.1"), rec("10.0.0.2")], st)
    for _ in range(5):
        assert _round(conn, dcfg, clock, [rec("10.0.0.1", status="unknown", hr=None),
                                          rec("10.0.0.2")], st) == []


def test_zero_blip_is_not_reported(conn, dcfg, clock):
    st = {}
    _round(conn, dcfg, clock, [rec("10.0.0.1")], st)
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", hr=0)], st, sec=60) == []
    assert _round(conn, dcfg, clock, [rec("10.0.0.1")], st, sec=60) == []
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", hr=0)], st, sec=60) == []
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", hr=0)], st, sec=300) == [("10.0.0.1", "zero")]


def test_overheat_needs_consecutive_rounds(conn, dcfg, clock):
    st = {}
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", temp=99)], st) == []
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", temp=80)], st) == []
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", temp=99)], st) == []
    assert _round(conn, dcfg, clock, [rec("10.0.0.1", temp=99)], st) == [("10.0.0.1", "overheat")]


# ---- 探测层 ----

def test_implausible_chip_temps_are_dropped():
    assert miner_core._temp(255) is None
    assert miner_core._temp(0) is None
    assert miner_core._temp(-40) is None
    assert miner_core._temp("NaN") is None
    assert miner_core._temp(88) == 88


def test_summary_parses_ghs_and_mhs(monkeypatch):
    monkeypatch.setattr(miner_core, "_cgminer", lambda ip, cmd, to: {
        "SUMMARY": [{"GHS 5s": "110000.5", "GHS av": 109000, "Elapsed": 3600}]})
    assert miner_core.fetch_summary("x", 1) == {"hr_rt": 110.0, "hr_avg": 109.0, "uptime": 3600}
    monkeypatch.setattr(miner_core, "_cgminer", lambda ip, cmd, to: {
        "SUMMARY": [{"MHS 5s": 95000000, "Elapsed": 10}]})
    assert miner_core.fetch_summary("x", 1)["hr_rt"] == 95.0
    monkeypatch.setattr(miner_core, "_cgminer", lambda ip, cmd, to: None)
    assert miner_core.fetch_summary("x", 1) is None


def _dead_web(monkeypatch):
    monkeypatch.setattr(miner_core, "tcp_open", lambda *a, **k: True)
    for f in ("probe_stock", "probe_uniplus", "probe_antbox"):
        monkeypatch.setattr(miner_core, f, lambda *a, **k: None)
    monkeypatch.setattr(miner_core, "fetch_pool", lambda *a, **k: None)


def test_web_hung_but_cgminer_answers_is_online(monkeypatch):
    _dead_web(monkeypatch)
    monkeypatch.setattr(miner_core, "fetch_summary",
                        lambda *a, **k: {"hr_rt": 100.0, "hr_avg": 99.0, "uptime": 50})
    r = miner_core.probe("10.0.0.1", {"liveness_gate": True})
    assert r["status"] == "online" and r["hr_rt"] == 100.0


def test_web_hung_and_cgminer_silent_is_unknown_not_offline(monkeypatch):
    _dead_web(monkeypatch)
    monkeypatch.setattr(miner_core, "fetch_summary", lambda *a, **k: None)
    r = miner_core.probe("10.0.0.1", {"liveness_gate": True})
    assert r["status"] == "unknown" and r["alive"]


def test_nothing_reachable_is_offline(monkeypatch):
    _dead_web(monkeypatch)
    monkeypatch.setattr(miner_core, "tcp_open", lambda *a, **k: False)
    assert miner_core.probe("10.0.0.1", {"liveness_gate": False, "alive_probe": True})["status"] \
        == "offline"
    assert miner_core.probe("10.0.0.1", {"liveness_gate": True})["status"] == "offline"
