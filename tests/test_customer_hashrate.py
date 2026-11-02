"""客户算力是速率：验证时间权重、停机、缺测、跨客户和归档，而非仅校验字段。"""
import copy
import importlib
from pathlib import Path

import pytest

import cloud_report
import db
from conftest import insert_scan, rec


BASE = 1_800_000_000 - 1_800_000_000 % 3600


def report(conn, monkeypatch, now, hours=24):
    monkeypatch.setattr(db.time, "time", lambda: now)
    return {r["worker"]: r for r in db.customer_report(conn, hours)}


def readings(conn, start, stop, rate=293, worker="A", machines=1, status="online"):
    for ts in range(start, stop + 1, 300):
        insert_scan(conn, ts, "quick", [
            rec(f"10.0.0.{i + 1}", worker=worker, hr=rate, status=status)
            for i in range(machines)])


def test_one_293t_machine_remains_293t_over_24_hours(conn, monkeypatch):
    readings(conn, BASE, BASE + 24 * 3600)
    r = report(conn, monkeypatch, BASE + 24 * 3600)["A"]
    assert r["hashrate_ths"] == r["avg_hashrate_ths"] == 293
    assert r["hashrate_hours"] == r["observed_hours"] == 24
    assert r["coverage_pct"] == 100
    assert r["current_machines"] == 1
    # 兼容旧结算积分，不能为了显示 TH/s 而偷改 TH·h 的含义。
    assert round(r["delivered_th_h"] - 293 * 24, 1) <= 0.1


def test_customer_total_is_sum_of_actual_machines_not_nameplate(conn, monkeypatch):
    for ts in range(BASE, BASE + 3600 + 1, 300):
        insert_scan(conn, ts, "quick", [rec("10.0.0.1", worker="A", hr=300),
                                         rec("10.0.0.2", worker="A", hr=290)])
    r = report(conn, monkeypatch, BASE + 3600, 1)["A"]
    assert r["current_machines"] == 2
    assert r["hashrate_ths"] == r["avg_hashrate_ths"] == 590
    assert r["observed_hours"] == 1  # 不是两台机器的 2 machine-hours。


def test_unequal_scan_intervals_are_weighted_by_seconds(conn, monkeypatch):
    for offset in range(0, 3600, 300):
        insert_scan(conn, BASE + offset, "quick", [rec("10.0.0.1", worker="A", hr=300)])
        insert_scan(conn, BASE + offset + 200, "quick", [rec("10.0.0.1", worker="A", hr=290)])
    r = report(conn, monkeypatch, BASE + 3600, 1)["A"]
    assert r["avg_hashrate_ths"] == pytest.approx((300 * 200 + 290 * 100) / 300, abs=0.01)
    assert r["coverage_pct"] == 100


def test_confirmed_outage_counts_zero_in_full_period_average(conn, monkeypatch):
    readings(conn, BASE, BASE + 12 * 3600 - 300)
    readings(conn, BASE + 12 * 3600, BASE + 24 * 3600, status="offline")
    r = report(conn, monkeypatch, BASE + 24 * 3600)["A"]
    assert r["hashrate_ths"] == 0
    assert r["avg_hashrate_ths"] == 146.5
    assert r["coverage_pct"] == 100
    assert r["current_machines"] == 1


def test_first_scan_is_not_backfilled_to_beginning_of_day(conn, monkeypatch):
    readings(conn, BASE + 23 * 3600, BASE + 24 * 3600)
    r = report(conn, monkeypatch, BASE + 24 * 3600)["A"]
    assert r["avg_hashrate_ths"] == 293
    assert r["observed_hours"] == 1
    assert r["coverage_pct"] == pytest.approx(100 / 24, abs=0.0001)


def test_monitor_gap_is_unobserved_not_customer_downtime(conn, monkeypatch):
    insert_scan(conn, BASE, "quick", [rec("10.0.0.1", worker="A", hr=293)])
    insert_scan(conn, BASE + 3000, "quick", [rec("10.0.0.1", worker="A", hr=293)])
    r = report(conn, monkeypatch, BASE + 3600, 1)["A"]
    assert r["avg_hashrate_ths"] == 293
    assert r["observed_hours"] == pytest.approx(1500 / 3600, abs=0.000001)
    assert r["coverage_pct"] == pytest.approx(1500 / 3600 * 100, abs=0.0001)


@pytest.mark.parametrize("bad_status,bad_rate", [("unknown", None), ("online", None),
                                                 ("online", float("nan"))])
def test_one_unknown_machine_excludes_whole_account_interval(conn, monkeypatch, bad_status, bad_rate):
    for offset in range(0, 3600 + 1, 300):
        bad = 1800 <= offset < 2700
        rows = [rec("10.0.0.1", worker="A", hr=300),
                rec("10.0.0.2", worker="A", hr=bad_rate if bad else 290,
                    status=bad_status if bad else "online")]
        insert_scan(conn, BASE + offset, "quick", rows)
    r = report(conn, monkeypatch, BASE + 3600, 1)["A"]
    assert r["avg_hashrate_ths"] == 590
    assert r["observed_hours"] == 0.75
    assert r["coverage_pct"] == 75


def test_unknown_current_machine_makes_realtime_unknown_not_zero(conn, monkeypatch):
    readings(conn, BASE, BASE + 3300)
    insert_scan(conn, BASE + 3600, "quick", [rec("10.0.0.1", worker="A", status="unknown")])
    r = report(conn, monkeypatch, BASE + 3600, 1)["A"]
    assert r["hashrate_ths"] is None
    assert r["avg_hashrate_ths"] == 293
    assert r["current_machines"] == 1


def test_stale_realtime_is_unknown_even_if_old_reading_was_positive(conn, monkeypatch):
    readings(conn, BASE, BASE + 300)
    r = report(conn, monkeypatch, BASE + 3600, 1)["A"]
    assert r["hashrate_ths"] is None
    assert r["current_machines"] is None
    assert r["hashrate_ts"] == BASE + 300


def test_window_start_uses_prior_sample_and_exact_boundary(conn, monkeypatch):
    for offset in range(-300, 4201, 300):
        insert_scan(conn, BASE + offset, "quick", [rec("10.0.0.1", worker="A", hr=293)])
    r = report(conn, monkeypatch, BASE + 3600 + 123, 1)["A"]
    assert r["avg_hashrate_ths"] == 293
    assert r["observed_hours"] == 1
    assert r["coverage_pct"] == 100


def test_worker_transfer_keeps_history_with_each_owner(conn, monkeypatch):
    for offset in range(0, 3600 + 1, 300):
        insert_scan(conn, BASE + offset, "quick", [
            rec("10.0.0.1", worker="A" if offset < 1800 else "B", hr=300)])
    rows = report(conn, monkeypatch, BASE + 3600, 1)
    assert rows["A"]["avg_hashrate_ths"] == rows["B"]["avg_hashrate_ths"] == 150
    assert rows["A"]["hashrate_ths"] == 0
    assert rows["B"]["hashrate_ths"] == 300
    assert rows["A"]["current_machines"] == 0
    assert rows["B"]["current_machines"] == 1
    assert rows["A"]["coverage_pct"] == rows["B"]["coverage_pct"] == 100


def test_blank_worker_across_hours_keeps_past_owner_without_future_reassignment(conn, monkeypatch):
    for offset in range(0, 10801, 300):
        worker = "" if 2400 <= offset < 7200 else "A"
        insert_scan(conn, BASE + offset, "quick", [
            rec("10.0.0.1", worker=worker, hr=293)])
    rows = report(conn, monkeypatch, BASE + 10800, 3)
    assert set(rows) == {"A"}
    assert rows["A"]["avg_hashrate_ths"] == rows["A"]["hashrate_ths"] == 293
    assert rows["A"]["coverage_pct"] == 100

    # 当前名册变成 B，不能倒过来把旧的空名时段认成 B。
    db.upsert_known_miners(conn, [rec("10.0.0.1", worker="B", hr=293)], BASE + 10800)
    conn.execute("DELETE FROM hashrate_hourly")
    conn.execute("DELETE FROM worker_hashrate_hourly")
    conn.commit()
    rows = report(conn, monkeypatch, BASE + 10800, 3)
    assert rows["A"]["avg_hashrate_ths"] == 293
    assert "B" not in rows


def test_new_archive_survives_pruning_and_legacy_bill_stays_unchanged(conn, monkeypatch):
    readings(conn, BASE, BASE + 3600)
    monkeypatch.setattr(db.time, "time", lambda: BASE + 3600)
    db.rollup_hours(conn, now=BASE + 3600)
    before = db.customer_report(conn, 1)[0]
    conn.execute("DELETE FROM snapshots")
    conn.execute("DELETE FROM scans")
    conn.commit()
    after = db.customer_report(conn, 1)[0]
    assert after["avg_hashrate_ths"] == before["avg_hashrate_ths"] == 293
    assert after["observed_hours"] == 1
    assert after["coverage_pct"] == 100
    assert after["hashrate_ths"] is None
    assert after["delivered_th_h"] == before["delivered_th_h"]


def test_old_integral_without_raw_history_is_not_presented_as_measured_mean(conn, monkeypatch):
    conn.execute("INSERT INTO worker_hourly VALUES(?,?,?,?,?,?,?)",
                 (BASE, "A", 1, 12, 12, 293, 3))
    conn.commit()
    db.meta_set(conn, "rollup_hour", BASE + 3600)
    r = report(conn, monkeypatch, BASE + 3600, 1)["A"]
    assert r["delivered_th_h"] == 293
    assert r["avg_hashrate_ths"] is None
    assert r["coverage_pct"] == r["observed_hours"] == 0


def test_pruned_partial_hour_is_not_invented_from_whole_hour_mean(conn, monkeypatch):
    for offset in range(0, 9001, 300):
        insert_scan(conn, BASE + offset, "quick", [
            rec("10.0.0.1", worker="A", hr=300 if offset < 1800 else 0)])
    now = BASE + 9000  # 查询近 2 小时，起点恰在首小时的 30 分。
    monkeypatch.setattr(db.time, "time", lambda: now)
    db.rollup_hours(conn, now=now)
    before = db.customer_report(conn, 2)[0]
    assert before["avg_hashrate_ths"] == 0 and before["coverage_pct"] == 100
    conn.execute("DELETE FROM snapshots WHERE scan_id IN (SELECT scan_id FROM scans WHERE ts<?)",
                 (BASE + 7200,))
    conn.execute("DELETE FROM scans WHERE ts<?", (BASE + 7200,))
    conn.commit()
    after = db.customer_report(conn, 2)[0]
    # 归档首小时均值 150；但剩余半小时真实为 0，不能拿 150 来按比例伪造完整窗口。
    assert after["avg_hashrate_ths"] == 0
    assert after["observed_hours"] == 1.5 and after["coverage_pct"] == 75


def test_upgrade_rebuilds_last_day_from_retained_raw_data_without_rebilling(conn, monkeypatch):
    readings(conn, BASE, BASE + 24 * 3600)
    # 已有旧归档进度，但没有新均值归档；首次查询即可复算仍在的原始记录。
    db.meta_set(conn, "rollup_hour", BASE + 24 * 3600)
    r = report(conn, monkeypatch, BASE + 24 * 3600)["A"]
    assert r["avg_hashrate_ths"] == 293
    assert r["coverage_pct"] == 100
    assert conn.execute("SELECT COUNT(*) FROM hashrate_hourly").fetchone()[0] == 24
    assert conn.execute("SELECT COUNT(*) FROM worker_hourly").fetchone()[0] == 0


@pytest.mark.parametrize("archive_first", [False, True])
def test_rolling_day_with_irregular_scans_has_no_hour_boundary_coverage_loss(
        conn, monkeypatch, archive_first):
    # 实际扫描完成时间不落整点；开始/结束两截不能凭空少算成 23.x/24h。
    now = BASE + 24 * 3600 + 137
    ts = BASE - 600
    intervals = (337, 418, 295, 531)
    i = 0
    while ts <= now:
        insert_scan(conn, ts, "quick", [rec("10.0.0.1", worker="A", hr=293)])
        ts += intervals[i % len(intervals)]
        i += 1
    if archive_first:
        db.rollup_hours(conn, now=now, max_hours=100)
    r = report(conn, monkeypatch, now)["A"]
    assert r["avg_hashrate_ths"] == r["hashrate_ths"] == 293
    assert r["observed_hours"] == r["hashrate_hours"] == 24
    assert r["coverage_pct"] == 100


def test_opening_legacy_database_migrates_and_reuses_existing_history(
        tmp_path, monkeypatch):
    # 复现升级前真实文件库没有新表的情况，而非只清空新版表。
    monkeypatch.setattr(db, "_DB_PATH", None)
    path = str(tmp_path / "legacy.db")
    old = db.init_db(path)
    now = BASE + 24 * 3600 + 137
    try:
        readings(old, BASE, BASE + 24 * 3600)
        old.execute("INSERT INTO worker_hourly VALUES(?,?,?,?,?,?,?)",
                    (BASE, "A", 1, 12, 12, 293, 3))
        old.execute("DROP TABLE worker_hashrate_hourly")
        old.execute("DROP TABLE hashrate_hourly")
        old.commit()
        db.meta_set(old, "rollup_hour", BASE + 24 * 3600)
        legacy_bill = [tuple(r) for r in old.execute("SELECT * FROM worker_hourly")]
    finally:
        db.close_readers()
        old.close()
    upgraded = db.init_db(path)
    try:
        r = report(upgraded, monkeypatch, now)["A"]
        assert r["avg_hashrate_ths"] == r["hashrate_ths"] == 293
        assert r["observed_hours"] == r["hashrate_hours"] == 24
        assert r["coverage_pct"] == 100
        assert r["current_machines"] == 1
        assert r["hashrate_ts"] == BASE + 24 * 3600
        assert upgraded.execute("SELECT COUNT(*) FROM hashrate_hourly").fetchone()[0] > 0
        assert [tuple(row) for row in upgraded.execute("SELECT * FROM worker_hourly")] == legacy_bill
    finally:
        db.close_readers()
        upgraded.close()


def test_new_metrics_include_completed_manual_discovery_scan(conn, monkeypatch):
    insert_scan(conn, BASE, "manual", [rec("10.0.0.1", worker="A", hr=293)])
    r = report(conn, monkeypatch, BASE + 300, 1)["A"]
    assert r["hashrate_ths"] == r["avg_hashrate_ths"] == 293
    assert r["observed_hours"] == pytest.approx(300 / 3600, abs=0.000001)


def test_lightweight_summary_does_not_mutate_cached_miner_records():
    records = [rec("10.0.0.1", worker="A", hr=300),
               rec("10.0.0.2", worker="A", status="offline")]
    original = copy.deepcopy(records)
    rows = db.customer_hashrates({"ts": BASE}, records, now=BASE + 10)
    assert records == original
    assert rows == [{"worker": "A", "machines": 2, "hashrate_ths": 300, "hashrate_ts": BASE}]


@pytest.mark.parametrize("status,expected", [("online", 293), ("unknown", None)])
def test_fast_summary_preserves_known_owner_when_worker_fetch_is_blank(conn, monkeypatch, status, expected):
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")
    monkeypatch.setattr(server.SVC, "conn", conn)
    monkeypatch.setattr(db.time, "time", lambda: BASE)
    records = [rec("10.0.0.1", worker="", hr=293, status=status)]
    db.upsert_known_miners(conn, [rec("10.0.0.1", worker="A", hr=293)], BASE - 300)
    monkeypatch.setattr(server, "_latest_records", lambda: ({"scan_id": 1, "ts": BASE, "kind": "quick"}, records))
    summary = server._build_public_summary()
    assert summary["ok"] is True
    assert summary["customer_hashrates"] == [
        {"worker": "A", "machines": 1, "hashrate_ths": expected, "hashrate_ts": BASE}]
    assert records[0]["worker"] == ""  # 不改写共享快照。


def test_cloud_summary_preserves_optional_live_rows_and_scan_timestamp(monkeypatch):
    live = [{"worker": "A", "machines": 1, "hashrate_ths": 293, "hashrate_ts": BASE}]
    summary = {"ok": True, "scanned": True, "scan_ts": BASE, "customer_hashrates": live}
    calls = []
    class Session:
        def close(self):
            pass
    monkeypatch.setattr(cloud_report, "_GEN", 99)
    monkeypatch.setattr(cloud_report, "_session", lambda c: Session())
    monkeypatch.setattr(cloud_report, "_set_status", lambda *args, **kwargs: True)
    def post(c, path, payload, sess):
        calls.append((path, payload))
        cloud_report._GEN += 1
    monkeypatch.setattr(cloud_report, "_post", post)
    cloud_report._loop(99, {"site_name": "A"}, "site-a", lambda: summary, lambda h: [])
    assert calls[0][1]["scan_ts"] == BASE
    assert calls[0][1]["customer_hashrates"] == live


def test_report_api_retains_requested_period_when_history_is_short(conn, monkeypatch):
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")
    monkeypatch.setattr(server.SVC, "conn", conn)
    monkeypatch.setattr(server, "_cache", {})
    readings(conn, BASE, BASE + 3600)
    monkeypatch.setattr(db.time, "time", lambda: BASE + 3600)
    result = server.api_report_customers(hours=24)
    assert result["hours"] == 24 and result["covered_hours"] == 1
    assert result["truncated"] is True
    row = result["customers"][0]
    assert row["hashrate_hours"] == 24
    assert row["observed_hours"] == 1
    assert row["coverage_pct"] == pytest.approx(100 / 24, abs=0.0001)
