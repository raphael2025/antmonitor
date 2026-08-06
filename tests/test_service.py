# -*- coding: utf-8 -*-
"""扫描编排：把 miner_core.scan 换成假数据，跑真实的 _do_scan 全流程。

覆盖的是"改坏了不会立刻报错、但会静默出事"的那部分：
落库口径、内存快照、IP 迁移自动下架、世代作废。
"""
import threading

import pytest

import db
import miner_core
import service
from conftest import insert_scan, now_hour, rec


@pytest.fixture
def svc(cfg, monkeypatch, tmp_path):
    cfg["db"]["path"] = str(tmp_path / "t.db")
    cfg["schedule"]["enabled"] = False
    cfg["alerts"]["enabled"] = True
    monkeypatch.setattr(service.appconfig, "load_segments",
                        lambda _c: (["10.9.0"], 1, 5))
    s = service.MonitorService(cfg)
    yield s
    s.stop()
    s.conn.close()
    db.close_readers()


def fake_scan(records):
    """替换 miner_core.scan：按 IP 返回预设记录，未预设的算离线。"""
    by_ip = {r["ip"]: r for r in records}

    def _scan(ips, cfg, progress_cb=None, workers=None):
        out = [by_ip.get(ip) or miner_core._blank(ip, "offline") for ip in ips]
        if progress_cb:
            progress_cb(len(out), len(out))
        return out
    return _scan


def test_full_scan_persists_only_real_machines(svc, monkeypatch):
    """全新部署第一轮必须落全量(否则离线数恒为0、面板误显示全在线)；
    之后每轮只留在线机 + 名册内的离线机，丢掉上万个死地址。"""
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec("10.9.0.1"), rec("10.9.0.2")]))
    first = svc.scan_full("full")
    assert first["miners"] == 5        # 首轮：5 个地址全落库

    second = svc.scan_full("full")
    assert second["online"] == 2
    assert second["miners"] == 2       # 另外 3 个死地址被丢弃
    meta, recs = svc.latest()
    assert {r["ip"] for r in recs} == {"10.9.0.1", "10.9.0.2"}
    assert meta["scan_id"] == second["scan_id"]


def test_snapshot_is_published_and_matches_db(svc, monkeypatch):
    """内存快照必须和落库内容一致——API 读内存、历史查库，两边不能对不上。"""
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec("10.9.0.1")]))
    res = svc.scan_full("full")
    _meta, mem = svc.latest()
    from_db = db.scan_records(svc.conn, res["scan_id"])
    assert [r["ip"] for r in mem] == [r["ip"] for r in from_db]
    assert set(mem[0]) == set(from_db[0])


def test_offline_machine_keeps_identity_for_search(svc, monkeypatch):
    """掉线机要回填最后已知型号/SN/矿工名，否则列表里只剩一个光秃秃的 IP，
    运维按客户名或 SN 根本搜不到它。"""
    monkeypatch.setattr(miner_core, "scan",
                        fake_scan([rec("10.9.0.1", worker="张三", model="S21")]))
    svc.scan_full("full")
    monkeypatch.setattr(miner_core, "scan", fake_scan([]))     # 全掉线
    svc.scan_full("full")
    _m, recs = svc.latest()
    off = next(r for r in recs if r["ip"] == "10.9.0.1")
    assert off["status"] == "offline"
    assert off["worker"] == "张三" and off["model"] == "S21"


def test_ip_migration_auto_removes_ghost(svc, monkeypatch):
    """同一台机(同SN)换了IP：旧IP的"掉线"是残影，自动下架，不该报掉线告警。"""
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec("10.9.0.1", sn="REALSN000123")]))
    svc.scan_full("full")
    # 同 SN 出现在新 IP，旧 IP 掉线
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec("10.9.0.4", sn="REALSN000123")]))
    svc.scan_full("full")
    _m, recs = svc.latest()
    assert {r["ip"] for r in recs} == {"10.9.0.4"}
    assert db.active_alert(svc.conn, "10.9.0.1", "offline") is None


def test_ip_migration_ignores_placeholder_sn(svc, monkeypatch):
    """未烧录SN的机器返回同一个占位串——绝不能据此认定"换了IP"去删记录。"""
    ph = "no miner sn stored on board"
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec("10.9.0.1", sn=ph)]))
    svc.scan_full("full")
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec("10.9.0.4", sn=ph)]))
    svc.scan_full("full")
    _m, recs = svc.latest()
    assert "10.9.0.1" in {r["ip"] for r in recs}    # 旧机仍在(判为掉线)，没被误删


def test_ip_migration_uses_mac_when_sn_is_unavailable(svc, monkeypatch):
    placeholder = "no miner sn stored on board"
    first = rec("10.9.0.1", sn=placeholder, mac="10:0A:41:96:B6:F2")
    monkeypatch.setattr(miner_core, "scan", fake_scan([first]))
    svc.scan_full("full")

    moved = rec("10.9.0.4", sn="", mac="10-0a-41-96-b6-f2")
    monkeypatch.setattr(miner_core, "scan", fake_scan([moved]))
    svc.scan_full("full")
    _m, records = svc.latest()
    assert {r["ip"] for r in records} == {"10.9.0.4"}
    assert db.active_alert(svc.conn, "10.9.0.1", "offline") is None
    assert db.known_identity(svc.conn)["10.9.0.1"]["state"] == "migrated"

    # Quarantine is reversible: if the old address really comes back online,
    # its normal upsert restores it to the active roster.
    db.upsert_known_miners(svc.conn, [first], 123456)
    assert db.known_identity(svc.conn)["10.9.0.1"]["state"] == "active"


def test_stale_scan_result_is_discarded(svc, monkeypatch):
    """核心防护：被判卡死的旧扫描线程杀不掉，它苏醒后必须发现自己已作废并丢弃结果。
    否则会用十几分钟前的探测覆盖现状——刚恢复的机器被标回离线 + 一轮误告警。"""
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec("10.9.0.1"), rec("10.9.0.2")]))
    svc.scan_full("full")
    good_id = svc.snapshot["scan_id"]

    started = threading.Event()

    def slow_scan(ips, cfg, progress_cb=None, workers=None):
        started.set()
        bump.wait(2)          # 模拟卡住期间发生了强制恢复
        return [miner_core._blank(ip, "offline") for ip in ips]

    bump = threading.Event()
    monkeypatch.setattr(miner_core, "scan", slow_scan)
    t = threading.Thread(target=svc.scan_full, args=("full",))
    t.start()
    started.wait(2)
    svc._force_recover("test")   # 世代 +1
    bump.set()
    t.join(5)

    assert svc.snapshot["scan_id"] == good_id      # 快照没被过期数据覆盖
    assert db.latest_scan(svc.conn)["scan_id"] == good_id


def test_obsolete_scan_cannot_clear_or_overwrite_recovery_progress(svc, monkeypatch):
    """旧扫描苏醒时，新世代可能仍在跑；旧 finally/callback 不能篡改新进度。"""
    old_started = threading.Event()
    old_release = threading.Event()
    new_started = threading.Event()
    new_release = threading.Event()
    calls = 0

    def overlapping_scan(ips, cfg, progress_cb=None, workers=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            old_started.set()
            old_release.wait(2)
            if progress_cb:
                progress_cb(999, 999)  # 模拟旧线程醒来后的迟到回调
        else:
            if progress_cb:
                progress_cb(1, len(ips))
            new_started.set()
            new_release.wait(2)
        return [rec(ip) for ip in ips]

    monkeypatch.setattr(miner_core, "scan", overlapping_scan)
    old = threading.Thread(target=svc.scan_full, args=("full",))
    old.start()
    assert old_started.wait(2)
    svc._force_recover("test-overlap")

    new = threading.Thread(target=svc.scan_full, args=("full",))
    new.start()
    assert new_started.wait(2)
    old_release.set()
    old.join(5)

    assert svc.progress["running"] is True
    assert svc.progress["done"] == 1
    assert svc.progress["total"] == 5

    new_release.set()
    new.join(5)
    assert svc.progress["running"] is False


def test_quick_scan_targets_roster_only(svc, monkeypatch):
    """巡检只打名册，不该把整段地址再扫一遍(这正是把负载降一个数量级的地方)。"""
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec("10.9.0.1")]))
    svc.scan_full("full")
    seen = {}

    def counting_scan(ips, cfg, progress_cb=None, workers=None):
        seen["ips"] = list(ips)
        return [rec("10.9.0.1")]
    monkeypatch.setattr(miner_core, "scan", counting_scan)
    svc.scan_quick()
    assert seen["ips"] == ["10.9.0.1"]        # 只探已知真机，而不是 10.9.0.1-5


def test_maintenance_tick_runs_rollup_prune_and_checkpoint(svc):
    """维护线程的三件事必须都能跑通(以前压根没有 checkpoint/vacuum/rollup)。
    注意只归档"已结束"的整点小时——当前这个小时还在累积，提前归档会少算。"""
    h = now_hour(-2)
    insert_scan(svc.conn, h + 60, "quick", [rec("10.9.0.1", worker="X", hr=100.0)])
    svc._maintenance_tick(now=10_000_000)
    assert int(db.meta_get(svc.conn, "rollup_hour")) > h    # 那一小时已归档
    assert db.checkpoint(svc.conn) is not None
    assert {r["worker"] for r in db.customer_report(svc.conn, 6)} == {"X"}
