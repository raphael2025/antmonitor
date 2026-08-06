# -*- coding: utf-8 -*-
"""告警状态机：这是全项目最容易改坏、坏了又最难发现的地方（漏报没人会注意到）。"""
import alerts
import db
from conftest import rec


def _roster(conn, ips):
    db.upsert_known_miners(conn, list(ips), int(__import__("time").time()))


def _run(conn, cfg, records, kind="quick", state=None):
    sid, _ts, kept = db.save_scan(conn, kind, records)
    return alerts.evaluate(conn, sid, kept, cfg, kind=kind, state=state if state is not None else {})


def test_offline_fires_only_after_being_online(conn, cfg):
    """只有"上次在线、这次掉线"才报离线：一直不在的地址不该刷告警。"""
    _roster(conn, ["10.0.0.1"])
    _run(conn, cfg, [rec("10.0.0.1")])                       # 第一轮在线
    fired = _run(conn, cfg, [rec("10.0.0.1", status="offline")])
    assert [(f[0], f[1]) for f in fired] == [("10.0.0.1", "offline")]

    # 恢复在线 → 离线告警自动消，且不再有活跃告警
    _run(conn, cfg, [rec("10.0.0.1")])
    assert db.active_alert(conn, "10.0.0.1", "offline") is None


def test_zero_hashrate_grace_and_null_is_not_zero(conn, cfg):
    """hr_rt=0 才是零算力；hr_rt=None(读不到)绝不能当零算力报警——
    否则一次接口超时就会误报一片，运维会开始不信任告警。"""
    _roster(conn, ["10.0.0.2", "10.0.0.3"])
    cfg["alerts"]["zero_grace_sec"] = 600
    # 刚开机(uptime<grace)的零算力不报
    fired = _run(conn, cfg, [rec("10.0.0.2", hr=0.0, uptime=100)])
    assert not [f for f in fired if f[1] == "zero"]
    # 跑了很久仍是 0 → 报
    fired = _run(conn, cfg, [rec("10.0.0.2", hr=0.0, uptime=99999)])
    assert ("10.0.0.2", "zero") in [(f[0], f[1]) for f in fired]
    # 读不到算力(None) → 既不报也不影响
    fired = _run(conn, cfg, [rec("10.0.0.3", hr=None, uptime=99999)])
    assert not [f for f in fired if f[1] == "zero"]


def test_segment_down_merges_and_suppresses_individual_alerts(conn, cfg):
    """整段掉线合并成一条网段事件，段内不再逐台刷离线/零算力。"""
    ips = [f"10.0.9.{i}" for i in range(1, 11)]
    _roster(conn, ips)
    _run(conn, cfg, [rec(ip) for ip in ips])                  # 全在线
    fired = _run(conn, cfg, [rec(ip, status="offline") for ip in ips])
    types = [f[1] for f in fired]
    assert "segment_down" in types
    assert "offline" not in types          # 被网段事件覆盖，不逐台报

    # 恢复后网段事件自动消
    _run(conn, cfg, [rec(ip) for ip in ips])
    assert db.active_alert(conn, "10.0.9.x", "segment_down") is None


def test_low_hashrate_needs_consecutive_rounds(conn, cfg):
    """掉算力必须连续 N 轮成立才报：单轮读数抖动不该惊动运维。"""
    peers = [rec(f"10.0.1.{i}", hr=100.0) for i in range(1, 7)]   # 6 台同型号基线 100
    bad = rec("10.0.1.9", hr=40.0)                                # 明显掉算力
    _roster(conn, [r["ip"] for r in peers] + ["10.0.1.9"])
    state = {}
    fired = _run(conn, cfg, peers + [bad], state=state)
    assert not [f for f in fired if f[1] == "low_hashrate"]       # 第一轮只记账
    fired = _run(conn, cfg, peers + [bad], state=state)
    assert ("10.0.1.9", "low_hashrate") in [(f[0], f[1]) for f in fired]

    # 算力恢复 → 消警且计数清零
    _run(conn, cfg, peers + [rec("10.0.1.9", hr=100.0)], state=state)
    assert db.active_alert(conn, "10.0.1.9", "low_hashrate") is None
    assert "10.0.1.9" not in state["low_hr_streak"]


def test_low_hashrate_skipped_when_too_few_peers(conn, cfg):
    """同型号样本太少时中位数不可信，不做掉算力判定(否则两台机互相当基线)。"""
    _roster(conn, ["10.0.2.1", "10.0.2.2"])
    state = {}
    for _ in range(3):
        fired = _run(conn, cfg, [rec("10.0.2.1", hr=100.0), rec("10.0.2.2", hr=10.0)],
                     state=state)
        assert not [f for f in fired if f[1] == "low_hashrate"]


def test_overheat_uses_hysteresis(conn, cfg):
    """高温告警滞回：95 触发、回落到 90 以下才消，避免临界值反复刷屏。"""
    _roster(conn, ["10.0.3.1"])
    fired = _run(conn, cfg, [rec("10.0.3.1", temp=97)])
    assert ("10.0.3.1", "overheat") in [(f[0], f[1]) for f in fired]
    _run(conn, cfg, [rec("10.0.3.1", temp=92)])          # 落到 92：仍在滞回区
    assert db.active_alert(conn, "10.0.3.1", "overheat") is not None
    _run(conn, cfg, [rec("10.0.3.1", temp=85)])          # 落到 85：消警
    assert db.active_alert(conn, "10.0.3.1", "overheat") is None


def test_repair_state_mutes_alerts(conn, cfg):
    """标记维修中的机器不再产生告警——否则维修期间会持续刷屏掩盖真故障。"""
    _roster(conn, ["10.0.4.1"])
    _run(conn, cfg, [rec("10.0.4.1")])
    db.set_machine_state(conn, ["10.0.4.1"], "repair")
    fired = _run(conn, cfg, [rec("10.0.4.1", status="offline")])
    assert not fired


def test_cooldown_blocks_flapping(conn, cfg):
    """刚恢复不久的同类告警在冷却期内不重复报。"""
    _roster(conn, ["10.0.5.1"])
    cfg["alerts"]["cooldown"] = 3600
    _run(conn, cfg, [rec("10.0.5.1")])
    _run(conn, cfg, [rec("10.0.5.1", status="offline")])      # 报离线
    _run(conn, cfg, [rec("10.0.5.1")])                        # 恢复(resolved_ts=now)
    fired = _run(conn, cfg, [rec("10.0.5.1", status="offline")])
    assert not [f for f in fired if f[1] == "offline"]        # 冷却期内不再报


def test_batch_does_not_write_for_nonexistent_alerts(conn, cfg):
    """一轮评估对没有告警的机器不应产生任何 UPDATE——
    这正是把每轮上万次事务压到个位数的关键，回归了 WAL 会重新膨胀。"""
    ips = [f"10.0.6.{i}" for i in range(1, 51)]
    _roster(conn, ips)
    b = alerts._Batch(conn, 1800, 0)
    for ip in ips:
        b.resolve(ip, "offline")
        b.resolve(ip, "zero")
    assert b.to_resolve == []
    assert b.commit() == []
