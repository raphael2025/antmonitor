# -*- coding: utf-8 -*-
"""重启限流(15min/24h) + 零算力自动重启候选过滤。"""
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


def test_auto_reboot_only_targets_online_zero_hashrate(conn, cfg):
    cfg["control"]["enabled"] = True
    cfg["control"]["reboot_enabled"] = True
    svc = _svc(conn, cfg)
    now = int(time.time())
    ip = "10.0.3.2"
    wait = svc._zero_reboot_timing()[0]  # 默认 2×scan_interval
    db.upsert_known_miners(conn, [rec(ip)], now)

    # 在线且明确读到 0 算力才启动计时；掉线期间清掉未尝试的计时且不重启。
    assert svc.zero_reboot_candidates([rec(ip, status="online", hr_rt=0)], now=now) == []
    assert svc.zero_reboot_candidates([rec(ip, status="offline")], now=now + wait) == []
    assert ip not in db.zero_reboot_states(conn)

    # 恢复在线零算力后重新等满两轮快巡检才进入候选。
    assert svc.zero_reboot_candidates(
        [rec(ip, status="online", hr_rt=0)], now=now + wait) == []
    candidates = svc.zero_reboot_candidates(
        [rec(ip, status="online", hr_rt=0)], now=now + wait * 2)
    assert [target[0] for target in candidates] == [ip]


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


def _zero_svc(conn, cfg, monkeypatch):
    cfg["control"]["enabled"] = True
    cfg["control"]["reboot_enabled"] = True
    svc = _svc(conn, cfg)
    pushed = []
    monkeypatch.setattr(service.alerts, "push_text", lambda c, t: pushed.append(t))
    return svc, pushed


def test_maintenance_segment_paused_other_segments_still_reboot(conn, cfg, monkeypatch):
    """可选成片熔断开启时：维护网段暂停，其它网段零算力机照常。"""
    cfg["control"]["zero_reboot_mass_ratio"] = 0.2
    cfg["control"]["zero_reboot_wait_sec"] = 900
    svc, pushed = _zero_svc(conn, cfg, monkeypatch)
    now = int(time.time())
    maint = [f"10.0.1.{i}" for i in range(1, 151)]
    other = [f"10.0.{s}.{i}" for s in (2, 3, 4) for i in range(1, 201)]
    db.upsert_known_miners(conn, [rec(ip) for ip in maint + other], now)
    recs = ([rec(ip, hr_rt=0) for ip in maint] + [rec(other[0], hr_rt=0)]
            + [rec(ip) for ip in other[1:]])
    assert svc.zero_reboot_candidates(recs, now=now) == []
    got = svc.zero_reboot_candidates(recs, now=now + 900)
    assert [t[0] for t in got] == [other[0]]
    assert len(pushed) == 1 and "10.0.1.x" in pushed[0] and "其他网段照常" in pushed[0]
    assert not any(ip in db.zero_reboot_states(conn) for ip in maint)
    recs = [rec(maint[0], hr_rt=0)] + [rec(ip) for ip in maint[1:] + other]
    assert svc.zero_reboot_candidates(recs, now=now + 1000) == []
    got = svc.zero_reboot_candidates(recs, now=now + 1000 + 900)
    assert [t[0] for t in got] == [maint[0]]


def test_site_wide_zero_hashrate_pauses_everything(conn, cfg, monkeypatch):
    """可选成片熔断开启时：全场成片零算力 → 全场暂停。"""
    cfg["control"]["zero_reboot_mass_ratio"] = 0.2
    cfg["control"]["zero_reboot_wait_sec"] = 900
    svc, pushed = _zero_svc(conn, cfg, monkeypatch)
    now = int(time.time())
    ips = [f"10.0.{s}.{i}" for s in range(1, 6) for i in range(1, 101)]
    db.upsert_known_miners(conn, [rec(ip) for ip in ips], now)
    zero = [rec(ip, hr_rt=0) for ip in ips]
    assert svc.zero_reboot_candidates(zero, now=now) == []
    assert svc.zero_reboot_candidates(zero, now=now + 1800) == []
    assert len(pushed) == 1
    assert db.zero_reboot_states(conn) == {}


def test_scattered_zero_across_site_pauses_everything(conn, cfg, monkeypatch):
    """可选成片熔断开启时：全场占比过高 → 全场暂停。"""
    cfg["control"]["zero_reboot_mass_ratio"] = 0.2
    cfg["control"]["zero_reboot_wait_sec"] = 900
    svc, pushed = _zero_svc(conn, cfg, monkeypatch)
    now = int(time.time())
    ips = [f"10.0.{s}.{i}" for s in range(1, 11) for i in range(1, 11)]   # 10 段 × 10 台
    db.upsert_known_miners(conn, [rec(ip) for ip in ips], now)
    recs = [rec(ip, hr_rt=0 if int(ip.rsplit(".", 1)[1]) <= 3 else 100.0) for ip in ips]
    svc.zero_reboot_candidates(recs, now=now)        # 每段 3 台(<5 台不算段事件)，全场 30%
    assert svc.zero_reboot_candidates(recs, now=now + 900) == []
    assert len(pushed) == 1 and "全场" in pushed[0]


def test_mass_fuse_off_by_default_reboots_segment_zeros(conn, cfg, monkeypatch):
    """默认关闭成片熔断：成片零算力网段里的机，等满两轮快巡检后仍可进候选。"""
    svc, pushed = _zero_svc(conn, cfg, monkeypatch)
    now = int(time.time())
    wait = svc._zero_reboot_timing()[0]
    ips = [f"10.0.1.{i}" for i in range(1, 21)]
    db.upsert_known_miners(conn, [rec(ip) for ip in ips], now)
    zero = [rec(ip, hr_rt=0) for ip in ips]
    assert svc.zero_reboot_candidates(zero, now=now) == []
    got = svc.zero_reboot_candidates(zero, now=now + wait)
    assert sorted(t[0] for t in got) == sorted(ips)
    assert pushed == []


def test_mass_zero_ratio_needs_min_count(conn, cfg):
    """小场地 1 台零算力占比虽高，不算成片事件。"""
    cfg["control"]["enabled"] = True
    cfg["control"]["reboot_enabled"] = True
    cfg["control"]["zero_reboot_mass_ratio"] = 0.2
    cfg["control"]["zero_reboot_wait_sec"] = 900
    svc = _svc(conn, cfg)
    now = int(time.time())
    db.upsert_known_miners(conn, [rec("10.0.9.1"), rec("10.0.9.2")], now)
    recs = [rec("10.0.9.1", hr_rt=0), rec("10.0.9.2")]
    svc.zero_reboot_candidates(recs, now=now)
    got = svc.zero_reboot_candidates(recs, now=now + 900)
    assert [t[0] for t in got] == ["10.0.9.1"]

def test_auto_zero_wait_and_attempts_read_config(conn, cfg):
    """自动零算力等候/次数读 zero_reboot_*，不再写死 900/4。"""
    cfg["control"].update(enabled=True, reboot_enabled=True,
                          zero_reboot_wait_sec=60, zero_reboot_max_attempts=2)
    svc = _svc(conn, cfg)
    now = int(time.time())
    ip = "10.0.8.1"
    db.upsert_known_miners(conn, [rec(ip)], now)
    zero = [rec(ip, hr_rt=0)]
    assert svc.zero_reboot_candidates(zero, now=now) == []
    assert svc.zero_reboot_candidates(zero, now=now + 59) == []
    assert svc.zero_reboot_candidates(zero, now=now + 60) == [(ip, "stock")]
    db.record_zero_reboot_attempts(conn, [ip], now + 60)
    assert svc.zero_reboot_candidates(zero, now=now + 119) == []
    assert svc.zero_reboot_candidates(zero, now=now + 120) == [(ip, "stock")]
    db.record_zero_reboot_attempts(conn, [ip], now + 120)
    assert svc.zero_reboot_candidates(zero, now=now + 9999) == []
    assert db.zero_reboot_states(conn)[ip]["attempts"] == 2


def test_crash_loop_low_uptime_still_auto_reboots_after_grace(conn, cfg):
    """挖矿崩溃环：uptime 永远 < zero_grace，但连续零算力已满宽限 → 仍进自动候选。"""
    cfg["control"].update(enabled=True, reboot_enabled=True,
                          zero_reboot_wait_sec=900)
    cfg["alerts"]["zero_grace_sec"] = 600
    svc = _svc(conn, cfg)
    now = int(time.time())
    ip = "10.0.8.2"
    db.upsert_known_miners(conn, [rec(ip)], now)
    assert svc.zero_reboot_candidates(
        [rec(ip, hr_rt=0, uptime=30)], now=now) == []
    assert svc.zero_reboot_candidates(
        [rec(ip, hr_rt=0, uptime=30)], now=now + 500) == []
    got = svc.zero_reboot_candidates(
        [rec(ip, hr_rt=0, uptime=30)], now=now + 900)
    assert [t[0] for t in got] == [ip]


def test_default_wait_is_two_quick_scan_rounds(conn, cfg):
    cfg["control"].update(enabled=True, reboot_enabled=True)
    cfg["schedule"]["scan_interval"] = 300
    cfg["control"]["zero_reboot_wait_sec"] = 0
    cfg["control"]["zero_reboot_wait_rounds"] = 2
    svc = _svc(conn, cfg)
    assert svc._zero_reboot_timing() == (600, 4)
    now = int(time.time())
    ip = "10.0.8.3"
    db.upsert_known_miners(conn, [rec(ip)], now)
    zero = [rec(ip, hr_rt=0)]
    assert svc.zero_reboot_candidates(zero, now=now) == []
    assert svc.zero_reboot_candidates(zero, now=now + 599) == []
    assert svc.zero_reboot_candidates(zero, now=now + 600) == [(ip, "stock")]