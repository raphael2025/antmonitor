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


def test_stale_mac_on_online_ip_does_not_quarantine_a_really_offline_machine(svc, monkeypatch):
    """在线机不能用名册里的旧 MAC 冒充本轮实测值参与残影判定。

    无 SN 的 A(MAC=MA) 原在 ip1，搬到 ip2；ip1 换上有合法 SN 的 B(有 SN 就不读 MAC)。
    旧实现给在线的 ip1 回填了 A 的 MA，A 在 ip2 真掉线时被当成"已在 ip1 上线的残影"
    自动下架，掉线告警被静音。"""
    ip1, ip2, MA = "10.9.0.1", "10.9.0.2", "AA:BB:CC:DD:EE:01"
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec(ip1, sn="N/A", mac=MA)]))
    svc.scan_full("full")
    monkeypatch.setattr(miner_core, "scan", fake_scan([
        rec(ip1, sn="SNB1234567", mac=""), rec(ip2, sn="N/A", mac=MA)]))
    svc.scan_full("full")
    monkeypatch.setattr(miner_core, "scan", fake_scan([rec(ip1, sn="SNB1234567", mac="")]))
    svc.scan_full("full")                                       # A 在 ip2 真掉线
    ident = db.known_identity(svc.conn)
    assert ident[ip2]["state"] == "active"                      # 不能被当残影下架
    assert ident[ip1]["mac"] == ""                              # 换了机器(SN 变了)，旧 MAC 作废
    assert any(a["ip"] == ip2 and a["type"] == "offline"
               for a in db.list_alerts(svc.conn, True, 100))


def test_reconfirm_keeps_the_best_result_across_passes(svc, monkeypatch):
    """二次确认多轮时只升不降：任一轮确认在线就是在线。旧实现后一轮直接覆盖前一轮，
    第 1 轮已探到在线(算力接口慢)、第 2 轮瞬时丢包 → 最终判离线并误报掉线。"""
    A = "10.9.0.1"
    svc.cfg["scan"]["reconfirm_passes"] = 2
    rounds = iter([
        [rec(A)],                                          # 第一次全扫：在线
        [],                                                # 第二次主扫：没探到
        [rec(A, hr=None, note="stats.cgi 超时")],          # 重探第 1 轮：在线，算力读不到
        [],                                                # 重探第 2 轮：瞬时丢包
    ])

    def _scan(ips, cfg, progress_cb=None, workers=None):
        by_ip = {r["ip"]: r for r in next(rounds)}
        return [by_ip.get(ip) or miner_core._blank(ip, "offline") for ip in ips]

    monkeypatch.setattr(miner_core, "scan", _scan)
    svc.scan_full("full")
    svc.scan_full("full")
    _m, records = svc.latest()
    assert {r["ip"]: r["status"] for r in records}[A] == "online"
    assert db.active_alert(svc.conn, A, "offline") is None


def test_pool_hijack_alert_through_real_scan_pipeline(svc, monkeypatch):
    """pools 不落库，但必须一路带到告警评估(save_scan 返回的是按库列重建的记录)。"""
    svc.cfg["control"]["pool_allowlist"] = ["f2pool.com"]
    monkeypatch.setattr(miner_core, "scan", fake_scan([
        rec("10.9.0.1", pools=["stratum+tcp://btc.f2pool.com:3333"]),
        rec("10.9.0.2", pools=["stratum+tcp://evil.example:3333"])]))
    svc.scan_full("full")
    hijacked = {a["ip"] for a in db.active_alerts_by_type(svc.conn, "pool_hijack")}
    assert hijacked == {"10.9.0.2"}


def test_container_refresh_is_not_blocked_by_a_running_miner_scan(svc, monkeypatch):
    """巡检 5000 台要一两分钟；以前集装箱 10 秒刷新与它共用一把锁，这期间漏液/断流等
    冷却数据不刷新、控制器离线也不判。现在集装箱有自己的锁。"""
    called = []
    monkeypatch.setattr(db, "known_container_ips", lambda conn: ["10.9.0.200"])
    monkeypatch.setattr(miner_core, "scan_containers",
                        lambda ips, cfg, progress_cb=None: called.append(ips) or [])
    assert svc._scan_lock.acquire(blocking=False)      # 模拟一轮矿机扫描正在进行
    try:
        svc.scan_containers()
    finally:
        svc._scan_lock.release()
    assert called == [["10.9.0.200"]]


def test_watchdog_notices_a_stalled_scanner_within_15_minutes_by_default(svc):
    """以前阈值取 full_interval×1.5 = 90 分钟：巡检卡死一个半小时才告警/自愈。
    空闲时 last_finished 每个巡检间隔就会刷新，扫描中另有进度心跳，全扫间隔不是耗时。"""
    import time as _t
    now = _t.time()
    assert svc._stale_threshold() <= 15 * 60
    svc.progress.update(running=False, last_finished=now - 16 * 60, loop_beat=now)
    assert svc.is_stale(now)
    svc.progress.update(last_finished=now - 60)
    assert not svc.is_stale(now)


def test_steady_zero_hashrate_machines_do_not_trip_the_reconfirm_breaker(svc, monkeypatch):
    """限电休眠/密码不对的一批机器一直是 0 或读不到算力：以前每轮都算"疑似"，超过
    reconfirm_max 就整体跳过二次确认，别的机器一次丢包就直接报掉线。"""
    svc.cfg["scan"]["reconfirm_max"] = 3
    zeros = [rec(f"10.9.0.{i}", hr=0.0) for i in (1, 2, 3, 4)]
    flaky = "10.9.0.5"
    rounds = iter([
        zeros + [rec(flaky)],                 # 第一次：零算力的 4 台 + 正常的 1 台
        zeros,                                # 第二次主扫：flaky 丢包没探到
        [rec(flaky)],                         # 二次确认：其实在线
    ])

    def _scan(ips, cfg, progress_cb=None, workers=None):
        by_ip = {r["ip"]: r for r in next(rounds)}
        return [by_ip.get(ip) or miner_core._blank(ip, "offline") for ip in ips]

    monkeypatch.setattr(miner_core, "scan", _scan)
    svc.scan_full("full")
    svc.scan_full("full")
    assert db.active_alert(svc.conn, flaky, "offline") is None


def test_mass_hashrate_drop_does_not_reprobe_the_whole_farm(svc, monkeypatch):
    """全场同时掉算力(矿池故障)：零算力疑似超过上限就不重探它们，但掉线的照样二次确认。"""
    svc.cfg["scan"]["reconfirm_max"] = 3
    ok = [rec(f"10.9.0.{i}") for i in (1, 2, 3, 4)] + [rec("10.9.0.5")]
    zero = [rec(f"10.9.0.{i}", hr=0.0) for i in (1, 2, 3, 4)]
    calls = []
    rounds = iter([ok, zero, [rec("10.9.0.5")]])

    def _scan(ips, cfg, progress_cb=None, workers=None):
        calls.append(sorted(ips))
        by_ip = {r["ip"]: r for r in next(rounds)}
        return [by_ip.get(ip) or miner_core._blank(ip, "offline") for ip in ips]

    monkeypatch.setattr(miner_core, "scan", _scan)
    svc.scan_full("full")
    svc.scan_full("full")
    assert calls[-1] == ["10.9.0.5"]                  # 只重探了掉线的那台
