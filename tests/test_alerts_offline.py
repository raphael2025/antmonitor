# -*- coding: utf-8 -*-
"""掉线告警不能永久漏报。

旧实现只在"上一轮同类扫描 online → 本轮 offline"那一刻判一次；那一刻因任何原因没报出来
(冷却期、上一轮是 unknown、被网段事件/维修静音)，之后上一轮已是 offline，就再也不会报。
现在按状态判：名册内的机器当前离线、且自它最后一次在线以来还没报过掉线 → 报。
冷却只让告警晚一点出来，不会让它消失。
"""
import time

import pytest

import alerts
import db
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


def _round(conn, cfg, clock, records, state, kind="quick"):
    """与 service._do_scan 同序：落库 → 在线机写名册(last_online) → 评估告警。"""
    clock.tick()
    sid, ts, kept = db.save_scan(conn, kind, records)
    db.upsert_known_miners(conn, [r for r in records if r["status"] == "online"], ts)
    return [(f[0], f[1]) for f in alerts.evaluate(conn, sid, kept, cfg, kind=kind, state=state)]


def _active(conn, type_="offline"):
    return sorted(a["ip"] for a in db.active_alerts_by_type(conn, type_))


IP = "10.0.0.1"


def test_offline_again_within_cooldown_is_delayed_not_lost(conn, cfg, clock):
    cfg["alerts"]["cooldown"] = 1800
    st = {}
    _round(conn, cfg, clock, [rec(IP)], st)
    assert _round(conn, cfg, clock, [rec(IP, status="offline")], st) == [(IP, "offline")]
    _round(conn, cfg, clock, [rec(IP)], st)                        # 恢复 → 消警
    assert _round(conn, cfg, clock, [rec(IP, status="offline")], st) == []   # 冷却中，先不报
    clock.tick(1800)                                                # 冷却过去，仍离线
    assert _round(conn, cfg, clock, [rec(IP, status="offline")], st) == [(IP, "offline")]


def test_offline_after_an_unknown_round_still_alerts(conn, cfg, clock):
    st = {}
    _round(conn, cfg, clock, [rec(IP)], st)
    _round(conn, cfg, clock, [rec(IP, status="unknown")], st)      # 拥堵超时那一轮
    assert _round(conn, cfg, clock, [rec(IP, status="offline")], st) == [(IP, "offline")]


def test_machines_still_down_when_segment_event_clears_get_their_own_alert(conn, cfg, clock):
    ips = [f"10.0.1.{i}" for i in range(1, 11)]
    st = {}
    _round(conn, cfg, clock, [rec(ip) for ip in ips], st)
    fired = _round(conn, cfg, clock, [rec(ip, status="offline") for ip in ips], st)
    assert fired == [("10.0.1.x", "segment_down")]                  # 整段只报一条
    most = [rec(ip) for ip in ips[:6]] + [rec(ip, status="offline") for ip in ips[6:]]
    _round(conn, cfg, clock, most, st)                              # 4/10 低于消除线 → 网段事件消除
    assert _active(conn, "segment_down") == []
    assert set(_active(conn)) == set(ips[6:])                       # 剩下 4 台必须单独报


def test_segment_down_again_within_cooldown_is_not_silent(conn, cfg, clock):
    cfg["alerts"]["cooldown"] = 1800
    ips = [f"10.0.1.{i}" for i in range(1, 11)]
    st = {}
    _round(conn, cfg, clock, [rec(ip) for ip in ips], st)
    _round(conn, cfg, clock, [rec(ip, status="offline") for ip in ips], st)
    _round(conn, cfg, clock, [rec(ip) for ip in ips], st)           # 来电，全部恢复
    _round(conn, cfg, clock, [rec(ip, status="offline") for ip in ips], st)  # 冷却期内又整段掉电
    assert _active(conn, "segment_down") == ["10.0.1.x"]           # 整段事件必须在


def test_repair_cancelled_while_still_offline_alerts(conn, cfg, clock):
    st = {}
    _round(conn, cfg, clock, [rec(IP)], st)
    db.set_machine_state(conn, [IP], "repair")
    assert _round(conn, cfg, clock, [rec(IP, status="offline")], st) == []
    db.set_machine_state(conn, [IP], "active")
    assert _round(conn, cfg, clock, [rec(IP, status="offline")], st) == [(IP, "offline")]


def test_long_offline_machine_is_not_re_alerted_every_round(conn, cfg, clock):
    """报过一次就够了：一直离线的机器不能每轮都新增一条。"""
    st = {}
    _round(conn, cfg, clock, [rec(IP)], st)
    assert _round(conn, cfg, clock, [rec(IP, status="offline")], st) == [(IP, "offline")]
    for _ in range(5):
        assert _round(conn, cfg, clock, [rec(IP, status="offline")], st) == []
    assert len(db.list_alerts(conn, True, 100)) == 1


def test_never_seen_online_address_does_not_alert(conn, cfg, clock):
    """名册外(从没在线过)的地址离线不报——约1.5万个死 IP 不能刷屏。"""
    st = {}
    assert _round(conn, cfg, clock, [rec("10.0.9.9", status="offline")], st) == []


def test_reboot_that_never_comes_back_after_a_recent_flap_is_not_lost(conn, cfg, clock):
    """刚抖过(掉线告警冷却中)的机器被重启且没起来：告警可以晚，但不能丢。"""
    cfg["alerts"]["cooldown"] = 1800
    cfg["control"]["reboot_grace_sec"] = 600
    st = {}
    _round(conn, cfg, clock, [rec(IP)], st)
    _round(conn, cfg, clock, [rec(IP, status="offline")], st)
    _round(conn, cfg, clock, [rec(IP)], st)                         # 恢复 → 消警，进入冷却
    st.setdefault("rebooting", {})[IP] = int(time.time())
    for _ in range(3):                                              # 静默期 + 冷却期内
        _round(conn, cfg, clock, [rec(IP, status="offline")], st)
    clock.tick(1800)
    _round(conn, cfg, clock, [rec(IP, status="offline")], st)
    assert _active(conn) == [IP]


def test_machines_under_repair_do_not_count_toward_segment_down(conn, cfg, clock):
    """送修拔电的机器不能算进网段掉线比例：10 台里 6 台在修 → 以前报 crit 网段掉线且不消，
    同段剩下机器的真实故障(零算力/掉线)全被网段事件静音。"""
    ips = [f"10.0.1.{i}" for i in range(1, 11)]
    st = {}
    _round(conn, cfg, clock, [rec(ip) for ip in ips], st)
    db.set_machine_state(conn, ips[:6], "repair")
    recs = [rec(ip, status="offline") for ip in ips[:6]] + [rec(ip) for ip in ips[6:9]] \
        + [rec(ips[9], status="offline")]
    fired = _round(conn, cfg, clock, recs, st)
    assert _active(conn, "segment_down") == []
    assert (ips[9], "offline") in fired                 # 同段真掉线的那台照常报


def test_machine_already_down_before_a_segment_outage_is_not_forgotten(conn, cfg, clock):
    """两台先坏并已报警 → 整段断电 → 来电后这两台仍离线。以前单机告警在断电时被"静音消掉"，
    又因为"报过了"(旧告警时间 > 最后在线时间)不再报，直到 3 天后记录被清理才冒出来。"""
    ips = [f"10.0.1.{i}" for i in range(1, 11)]
    st = {}
    _round(conn, cfg, clock, [rec(ip) for ip in ips], st)
    broken = ips[:2]
    _round(conn, cfg, clock, [rec(ip, status="offline") for ip in broken]
           + [rec(ip) for ip in ips[2:]], st)
    assert set(_active(conn)) == set(broken)
    _round(conn, cfg, clock, [rec(ip, status="offline") for ip in ips], st)   # 整段断电
    assert _active(conn, "segment_down") == ["10.0.1.x"]
    assert set(_active(conn)) == set(broken)            # 断电前就坏的那两台，告警不能被抹掉
    _round(conn, cfg, clock, [rec(ip, status="offline") for ip in broken]
           + [rec(ip) for ip in ips[2:]], st)           # 来电，其它恢复
    assert _active(conn, "segment_down") == []
    assert set(_active(conn)) == set(broken)


def test_repair_cancelled_long_ago_resolved_alert_does_not_suppress_forever(conn, cfg, clock):
    """维修期间消掉的告警(已恢复记录)不能算"报过了"：取消维修后仍离线就要报。"""
    st = {}
    _round(conn, cfg, clock, [rec(IP)], st)
    _round(conn, cfg, clock, [rec(IP, status="offline")], st)           # 报警
    db.set_machine_state(conn, [IP], "repair")
    _round(conn, cfg, clock, [rec(IP, status="offline")], st)           # 维修 → 消警
    clock.tick(3600)
    db.set_machine_state(conn, [IP], "active")
    _round(conn, cfg, clock, [rec(IP, status="offline")], st)
    assert _active(conn) == [IP]


def test_segment_hovering_around_threshold_does_not_spam(conn, cfg, clock):
    """离线率在 60% 上下来回：以前每次回落都消警、再超过又因 force 立刻重报，每轮一条推送。
    加回差：超过 60% 才报，报了之后降到 45% 以下才消。"""
    ips = [f"10.0.1.{i}" for i in range(1, 11)]
    st = {}
    _round(conn, cfg, clock, [rec(ip) for ip in ips], st)
    for k in range(10):
        n_off = 6 if k % 2 == 0 else 5                              # 60% / 50% 交替
        _round(conn, cfg, clock, [rec(ip, status="offline") for ip in ips[:n_off]]
               + [rec(ip) for ip in ips[n_off:]], st)
    rows = [a for a in db.list_alerts(conn, False, 100) if a["type"] == "segment_down"]
    assert len(rows) == 1 and not rows[0]["resolved"]
