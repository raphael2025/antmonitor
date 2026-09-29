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
