# -*- coding: utf-8 -*-
"""存储层：计费聚合（管钱，必须确定性）、名册、下架清理。"""
import db
from conftest import insert_scan, now_hour, rec


def test_save_scan_returns_db_shaped_records(conn):
    """save_scan 返回的 kept 必须与数据库行同构：API 走内存快照与回落查库
    要返回完全一样的字段，否则前端会出现"刷新一下字段就变了"的怪象。"""
    _sid, _ts, kept = db.save_scan(conn, "quick", [rec("10.1.0.1"), rec("10.1.0.2")])
    assert len(kept) == 2
    from_db = db.scan_records(conn, _sid)
    assert set(kept[0]) == set(from_db[0])
    assert "device" not in kept[0]      # 扫描内部字段不该泄漏到 API


def test_save_scan_keep_ips_drops_dead_addresses(conn):
    """全网扫描每轮上万个死 IP，只有名册内的离线机才值得落库。"""
    records = [rec("10.1.1.1"), rec("10.1.1.2", status="offline"),
               rec("10.1.1.3", status="offline")]
    _sid, _ts, kept = db.save_scan(conn, "full", records, keep_ips={"10.1.1.2"})
    assert {r["ip"] for r in kept} == {"10.1.1.1", "10.1.1.2"}


def test_customer_report_worker_attribution_is_deterministic(conn):
    """机器中途换客户时，归属必须取"区间内最后一次非空矿工名"并且稳定。
    旧实现依赖 SELECT 的返回顺序，同样的数据可能算给不同客户——这是出账数据。"""
    h = now_hour(-2)
    insert_scan(conn, h + 60, "quick", [rec("10.2.0.1", worker="老客户")])
    insert_scan(conn, h + 120, "quick", [rec("10.2.0.1", worker="新客户")])
    for _ in range(3):
        rows = {r["worker"]: r for r in db.customer_report(conn, hours=6)}
        assert set(rows) == {"新客户"}


def test_customer_report_counts_offline_time_against_uptime(conn):
    """离线样本必须归到该机已知客户名下并拉低可用率，否则停机对客户"不可见"。"""
    h = now_hour(-1)
    insert_scan(conn, h + 60, "quick", [rec("10.2.1.1", worker="A")])
    insert_scan(conn, h + 360, "quick", [rec("10.2.1.1", status="offline")])
    rows = {r["worker"]: r for r in db.customer_report(conn, hours=6)}
    assert rows["A"]["machines"] == 1
    assert 0 < rows["A"]["uptime_pct"] < 100


def test_rollup_survives_snapshot_pruning(conn):
    """核心保障：明细快照被保留期清掉后，计费数字仍然查得到。
    没有这条，"近30天报表"在 retention_days=3 的库上永远是错的。"""
    h = now_hour(-3)
    for i in range(6):    # 3 小时前的那一小时里跑了 6 次扫描
        insert_scan(conn, h + i * 600, "quick", [rec("10.2.2.1", worker="B", hr=100.0)])
    assert db.rollup_hours(conn) >= 1
    before = {r["worker"]: r for r in db.customer_report(conn, hours=6)}["B"]
    assert before["delivered_th_h"] > 0

    # 把明细全删掉，模拟 prune 之后
    conn.execute("DELETE FROM snapshots")
    conn.execute("DELETE FROM scans")
    conn.commit()
    after = {r["worker"]: r for r in db.customer_report(conn, hours=6)}["B"]
    assert after["delivered_th_h"] == before["delivered_th_h"]
    assert after["machines"] == 1


def test_rollup_is_idempotent(conn):
    """重复归档同一小时不能把数字翻倍（重启/补跑都会触发）。"""
    h = now_hour(-2)
    insert_scan(conn, h + 60, "quick", [rec("10.2.3.1", worker="C", hr=50.0)])
    db.rollup_hours(conn)
    first = {r["worker"]: r for r in db.customer_report(conn, hours=6)}["C"]
    db.meta_set(conn, "rollup_hour", h)      # 强制重跑那一小时
    db.rollup_hours(conn)
    again = {r["worker"]: r for r in db.customer_report(conn, hours=6)}["C"]
    assert again == first


def test_rollup_caps_gap_so_downtime_is_not_billed(conn):
    """监控自己停摆 40 分钟，不能把这段时间按"最后一次读数"算成交付算力。"""
    h = now_hour(-2)
    insert_scan(conn, h + 60, "quick", [rec("10.2.4.1", worker="D", hr=3600.0)])
    insert_scan(conn, h + 3000, "quick", [rec("10.2.4.1", worker="D", hr=3600.0)])
    db.rollup_hours(conn)
    row = {r["worker"]: r for r in db.customer_report(conn, hours=6)}["D"]
    # 两个样本各自最多代表 MAX_SAMPLE_GAP(900s)，即合计 ≤ 0.5 小时
    assert row["delivered_th_h"] <= 3600.0 * (2 * db.MAX_SAMPLE_GAP / 3600.0) + 1


def test_prune_never_deletes_unrolled_data(conn):
    """prune 只能删已归档的时段，否则计费数据会在归档前被物理删除。"""
    h = now_hour(-40 * 24)      # 40 天前，远超 retention_days
    insert_scan(conn, h + 60, "quick", [rec("10.2.5.1", worker="E")])
    db.prune(conn, retention_days=3)                    # 还没 rollup → 不该删
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
    db.rollup_hours(conn, max_hours=100000)
    db.prune(conn, retention_days=3)
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0


def test_alert_state_preloads_active_and_cooldown(conn):
    import time
    db.raise_alert(conn, "10.3.0.1", "offline", "crit", "x")
    aid = db.raise_alert(conn, "10.3.0.2", "zero", "warn", "y")
    db.resolve_alert(conn, "10.3.0.2", "zero")
    active, cooled = db.alert_state(conn, cooldown_since=int(time.time()) - 60)
    assert ("10.3.0.1", "offline") in active
    assert ("10.3.0.2", "zero") not in active
    assert ("10.3.0.2", "zero") in cooled
    assert aid


def test_remove_miners_clears_roster_snapshot_and_alerts(conn):
    """下架必须一次清干净：名册、最近快照、残留告警。"""
    sid, ts, _ = db.save_scan(conn, "quick", [rec("10.4.0.1"), rec("10.4.0.2")])
    db.upsert_known_miners(conn, ["10.4.0.1", "10.4.0.2"], ts)
    db.raise_alert(conn, "10.4.0.1", "offline", "crit", "x")
    db.remove_miners(conn, ["10.4.0.1"])
    assert "10.4.0.1" not in db.roster_ips(conn)
    assert [r["ip"] for r in db.scan_records(conn, sid)] == ["10.4.0.2"]
    assert db.active_alert(conn, "10.4.0.1", "offline") is None


def test_segment_down_cleared_only_when_segment_emptied(conn):
    """整段还有机器时下架个别机器，不能把 segment_down 误消(会掩盖真实断电)。"""
    ts = now_hour()
    db.upsert_known_miners(conn, ["10.5.0.1", "10.5.0.2"], ts)
    db.raise_alert(conn, "10.5.0.x", "segment_down", "crit", "seg down")
    db.remove_miners(conn, ["10.5.0.1"])
    assert db.active_alert(conn, "10.5.0.x", "segment_down") is not None
    db.remove_miners(conn, ["10.5.0.2"])
    assert db.active_alert(conn, "10.5.0.x", "segment_down") is None


def test_hourly_boundary_uses_full_hour(conn):
    """一小时内首次扫描要回补到整点，避免每小时开头出现计费空洞。"""
    h = now_hour(-2)
    insert_scan(conn, h + 1800, "quick", [rec("10.6.0.1", worker="F", hr=3600.0)])
    db.rollup_hours(conn)
    row = conn.execute("SELECT th_h FROM worker_hourly WHERE hour=? AND worker='F'",
                       (h,)).fetchone()
    assert row is not None and row["th_h"] > 0
