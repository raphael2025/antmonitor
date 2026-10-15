# -*- coding: utf-8 -*-
"""重启限流(15min/24h) + 掉线自动重启候选过滤。"""
import threading
import time

import appconfig
import control
import db
import service
from conftest import rec


def _svc(conn, cfg):
    """轻量构造 MonitorService，不碰磁盘上的生产库。"""
    s = object.__new__(service.MonitorService)
    s.cfg = cfg
    s.conn = conn
    s._alert_state = {}
    s._reboot_lock = threading.Lock()
    return s


def _log_ok(conn, ip, ts, user="ops@test"):
    with db._wlock, conn:
        conn.execute(
            "INSERT INTO command_log(ts,user,action,ip,ok,msg) VALUES(?,?,?,?,?,?)",
            (ts, user, "reboot", ip, 1, "reboot ok"))


def _log_fail(conn, ip, ts):
    with db._wlock, conn:
        conn.execute(
            "INSERT INTO command_log(ts,user,action,ip,ok,msg) VALUES(?,?,?,?,?,?)",
            (ts, "ops@test", "reboot", ip, 0, "连不上"))


def test_reboot_stats_only_counts_success(conn):
    now = int(time.time())
    ip = "10.0.0.1"
    _log_ok(conn, ip, now - 100)
    _log_fail(conn, ip, now - 50)
    _log_ok(conn, ip, now - 10)
    st = db.reboot_stats(conn, [ip, "10.0.0.2"], window_sec=86400)
    assert st[ip]["count"] == 2
    assert st[ip]["last_ts"] == now - 10
    assert st["10.0.0.2"] == {"count": 0, "last_ts": 0}


def test_filter_rejects_within_min_interval(conn, cfg):
    cfg["control"]["reboot_min_interval_sec"] = 900
    cfg["control"]["reboot_max_per_day"] = 4
    now = int(time.time())
    ip = "10.0.0.1"
    _log_ok(conn, ip, now - 60)  # 1 分钟前成功
    allowed, skipped = control.filter_reboot_targets(
        conn, [(ip, "stock"), ("10.0.0.2", "stock")], cfg)
    assert [t[0] for t in allowed] == ["10.0.0.2"]
    assert len(skipped) == 1 and skipped[0]["ip"] == ip and not skipped[0]["ok"]
    assert "不足" in skipped[0]["msg"]


def test_filter_rejects_fifth_success_in_24h(conn, cfg):
    cfg["control"]["reboot_min_interval_sec"] = 0  # 只测次数
    cfg["control"]["reboot_max_per_day"] = 4
    now = int(time.time())
    ip = "10.0.0.1"
    for i in range(4):
        _log_ok(conn, ip, now - 3600 * (i + 1))
    allowed, skipped = control.filter_reboot_targets(conn, [(ip, "stock")], cfg)
    assert allowed == []
    assert skipped[0]["ip"] == ip and "上限" in skipped[0]["msg"]


def test_failed_reboot_does_not_consume_quota(conn, cfg):
    cfg["control"]["reboot_min_interval_sec"] = 900
    cfg["control"]["reboot_max_per_day"] = 4
    now = int(time.time())
    ip = "10.0.0.1"
    _log_fail(conn, ip, now - 30)
    allowed, skipped = control.filter_reboot_targets(conn, [(ip, "stock")], cfg)
    assert allowed == [(ip, "stock")] and skipped == []


def test_auto_reboot_off_is_noop(conn, cfg, monkeypatch):
    cfg["control"]["enabled"] = True
    cfg["control"]["reboot_enabled"] = False
    svc = _svc(conn, cfg)
    called = []
    monkeypatch.setattr(control, "run_batch",
                        lambda *a, **k: called.append(1) or ([], ""))
    db.upsert_known_miners(conn, [rec("10.0.0.1")], int(time.time()))
    svc._maybe_auto_reboot([rec("10.0.0.1", status="offline")])
    assert called == []


def test_auto_reboot_skips_repair_and_segment_down(conn, cfg):
    cfg["control"]["enabled"] = True
    cfg["control"]["reboot_enabled"] = True
    cfg["alerts"]["segment_down_ratio"] = 0.6
    cfg["alerts"]["segment_down_min"] = 5
    svc = _svc(conn, cfg)
    now = int(time.time())
    # 10 台同段：先全上线入册，再全掉线 → 整段掉线，不应进自动列表
    ips = [f"10.0.2.{i}" for i in range(1, 11)]
    db.upsert_known_miners(conn, [rec(ip) for ip in ips], now)
    offline = [rec(ip, status="offline") for ip in ips]
    assert svc.offline_reboot_candidates(offline) == []

    # 单机掉线 + 维修中 → 不进
    db.upsert_known_miners(conn, [rec("10.0.3.1")], now)
    db.set_machine_state(conn, ["10.0.3.1"], "repair")
    assert svc.offline_reboot_candidates([rec("10.0.3.1", status="offline")]) == []

    # 单机掉线正常 → 进候选
    db.upsert_known_miners(conn, [rec("10.0.3.2")], now)
    cands = svc.offline_reboot_candidates([rec("10.0.3.2", status="offline")])
    assert [r["ip"] for r in cands] == ["10.0.3.2"]

    # 重启静默期内 → 不进
    svc.mark_rebooting(["10.0.3.2"])
    assert svc.offline_reboot_candidates([rec("10.0.3.2", status="offline")]) == []


def test_apply_settings_reboot_keys(cfg):
    appconfig.apply_settings(cfg, {
        "reboot_enabled": True,
        "reboot_concurrency": 15,
        "reboot_delay_sec": 12,
        "reboot_max_per_day": 3,
        "reboot_min_interval_sec": 600,
    })
    ctl = cfg["control"]
    assert ctl["reboot_enabled"] is True
    assert ctl["reboot_concurrency"] == 15
    assert ctl["reboot_delay_sec"] == 12
    assert ctl["reboot_max_per_day"] == 3
    assert ctl["reboot_min_interval_sec"] == 600


def _auto_svc(conn, cfg, monkeypatch):
    """开启自动重启的 svc；下发线程替换成记录器，测试里同步等它跑完。"""
    cfg["control"]["enabled"] = True
    cfg["control"]["reboot_enabled"] = True
    svc = _svc(conn, cfg)
    sent, pushed = [], []

    def fake_run(targets, user):
        sent.append(sorted(t[0] for t in targets))
        svc._reboot_lock.release()

    svc._run_auto_reboot = fake_run
    monkeypatch.setattr(service.alerts, "push_text", lambda c, t: pushed.append(t))

    def tick(records):
        svc._maybe_auto_reboot(records)
        for t in threading.enumerate():
            if t.name == "auto-reboot":
                t.join(5)
    return svc, sent, pushed, tick


def test_auto_reboot_needs_consecutive_offline_rounds(conn, cfg, monkeypatch):
    """单轮离线(含服务刚启动第一轮)不重启；连续 2 轮确认离线才重启。"""
    svc, sent, _p, tick = _auto_svc(conn, cfg, monkeypatch)
    db.upsert_known_miners(conn, [rec("10.0.5.1"), rec("10.0.5.2")], int(time.time()))
    tick([rec("10.0.5.1", status="offline"), rec("10.0.5.2")])
    assert sent == []
    # 中间一轮在线 / 未探测 → 计数清零
    tick([rec("10.0.5.1", status="unknown"), rec("10.0.5.2")])
    tick([rec("10.0.5.1", status="offline"), rec("10.0.5.2")])
    assert sent == []
    tick([rec("10.0.5.1", status="offline"), rec("10.0.5.2")])
    assert sent == [["10.0.5.1"]]


def test_auto_reboot_breaker_on_mass_fresh_offline(conn, cfg, monkeypatch):
    """一轮大量新掉线(扫描误判) → 熔断不重启，且这批一直挂起直到恢复在线。"""
    cfg["control"]["reboot_auto_max"] = 10
    svc, sent, pushed, tick = _auto_svc(conn, cfg, monkeypatch)
    ips = [f"10.1.{i}.1" for i in range(20)]   # 每段 1 台，不触发整段掉线
    db.upsert_known_miners(conn, [rec(ip) for ip in ips], int(time.time()))
    off = [rec(ip, status="offline") for ip in ips]
    for _ in range(4):
        tick(off)
    assert sent == []
    assert len(pushed) == 1   # 只推一次
    # 恢复在线后再单独掉线：解除挂起，按正常流程重启
    tick([rec(ip) for ip in ips])
    one = [rec(ips[0], status="offline")] + [rec(ip) for ip in ips[1:]]
    tick(one)
    tick(one)
    assert sent == [[ips[0]]]


def test_auto_reboot_long_dead_do_not_trip_breaker(conn, cfg, monkeypatch):
    """常年掉线的坏机不计入"新掉线"熔断计数，不会把熔断一直顶开。"""
    cfg["control"]["reboot_auto_max"] = 3
    cfg["control"]["reboot_min_interval_sec"] = 0
    svc, sent, _p, tick = _auto_svc(conn, cfg, monkeypatch)
    dead = [f"10.2.{i}.1" for i in range(5)]
    db.upsert_known_miners(conn, [rec(ip) for ip in dead + ["10.3.0.1"]], int(time.time()))
    # 服务刚启动时 5 台都是"新掉线" → 熔断挂起
    tick([rec(ip, status="offline") for ip in dead] + [rec("10.3.0.1")])
    tick([rec(ip, status="offline") for ip in dead] + [rec("10.3.0.1")])
    assert sent == []
    # 之后新掉的单台照常重启
    recs = [rec(ip, status="offline") for ip in dead] + [rec("10.3.0.1", status="offline")]
    tick(recs)
    tick(recs)
    assert sent == [["10.3.0.1"]]
