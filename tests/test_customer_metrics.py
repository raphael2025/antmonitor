"""以可手算的时间区间检验交付、耗电、在线率和库存变更，保留旧账本证据。"""
import math

import pytest

import db
from conftest import insert_scan, rec


BASE = 1_800_000_000 - 1_800_000_000 % 3600


def report(conn, monkeypatch, end=3600, hours=1):
    monkeypatch.setattr(db.time, "time", lambda: BASE + end)
    return {r["worker"]: r for r in db.customer_report(conn, hours)}


def scans(conn, build, start=0, end=3600, kind="quick"):
    for offset in range(start, end + 1, 300):
        insert_scan(conn, BASE + offset, kind, build(offset))


def test_transfer_preserves_each_customers_delivery_and_electricity(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A" if t < 1800 else "B", hr=300)])
    for archived in (False, True):
        if archived:
            db.rollup_hours(conn, BASE + 3600)
        rows = report(conn, monkeypatch)
        for worker in ("A", "B"):
            assert rows[worker]["delivered_th_h"] == 150
            assert rows[worker]["power_kwh"] == 1.5
            assert rows[worker]["avg_hashrate_ths"] == 150
            assert rows[worker]["uptime_pct"] == 100
            assert rows[worker]["billing_method"] == "time_weighted_v2"
        assert rows["B"]["legacy_delivered_th_h"] >= 300
        assert rows["B"]["legacy_power_kwh"] == 3


def test_unknown_does_not_synthesize_hours_of_delivery(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A", hr=300) if t == 0 else
                           rec("10.0.0.1", status="unknown", worker="A")])
    r = report(conn, monkeypatch)["A"]
    assert r["delivered_th_h"] == 25
    assert r["power_kwh"] == .25
    assert r["uptime_pct"] == 100  # 仅已知的 5 分钟；覆盖必须同时说明。
    for metric in ("delivery", "power", "uptime"):
        assert r[metric + "_observed_hours"] == pytest.approx(1 / 12, abs=1e-6)
        assert r[metric + "_coverage_pct"] == pytest.approx(100 / 12, abs=1e-4)
    assert r["legacy_delivered_th_h"] >= 300


def test_all_unknown_is_not_a_measured_zero(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", status="unknown", worker="A")])
    r = report(conn, monkeypatch)["A"]
    assert r["delivered_th_h"] is r["power_kwh"] is r["uptime_pct"] is None
    assert r["billing_source"] == "missing"
    assert r["delivery_coverage_pct"] == r["power_coverage_pct"] == r["uptime_coverage_pct"] == 0


def test_uptime_is_weighted_by_machine_seconds_not_sample_count(conn, monkeypatch):
    insert_scan(conn, BASE, "quick", [rec("10.0.0.1", worker="A", hr=300)])
    for offset in (60, 900, 1800, 2700):
        insert_scan(conn, BASE + offset, "quick", [rec("10.0.0.1", worker="A", status="offline")])
    r = report(conn, monkeypatch)["A"]
    assert r["uptime_pct"] == 1.67  # 在线 1 分钟，离线 59 分钟。
    assert r["uptime_coverage_pct"] == 100
    assert r["power_kwh"] == .05
    assert r["delivered_th_h"] == 5
    assert r["legacy_uptime_pct"] == 20


def test_machine_count_changes_use_machine_time_denominator(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A")] +
          ([rec("10.0.0.2", worker="A", status="offline")] if t < 1800 else []))
    r = report(conn, monkeypatch)["A"]
    assert r["uptime_pct"] == 66.67  # 在线机器秒 3600 / 总机器秒 (2*1800 + 1*1800)。
    assert r["machines"] == 2


def test_rolling_boundary_does_not_import_consumption_from_outside_window(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A", hr=300 if t < 1800 else 0,
                              power=3000 if t < 1800 else 0)], end=5400)
    before = report(conn, monkeypatch, 5400)["A"]
    db.rollup_hours(conn, BASE + 5400)
    after = report(conn, monkeypatch, 5400)["A"]
    assert before["delivered_th_h"] == after["delivered_th_h"] == 0
    assert before["power_kwh"] == after["power_kwh"] == 0
    assert after["legacy_delivered_th_h"] == 75  # 保存旧摊分值供核对，不再作为当前实测值。


def test_first_observation_is_not_backfilled(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A", hr=300)], start=300)
    r = report(conn, monkeypatch)["A"]
    assert r["delivered_th_h"] == 275
    assert r["power_kwh"] == 2.75
    assert r["delivery_observed_hours"] == pytest.approx(55 / 60, abs=1e-6)


def test_complete_manual_scans_participate_in_all_metrics(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A", hr=300)], kind="manual")
    r = report(conn, monkeypatch)["A"]
    assert r["delivered_th_h"] == 300 and r["power_kwh"] == 3
    assert r["uptime_pct"] == r["uptime_coverage_pct"] == 100


@pytest.mark.parametrize("missing", ["hr_rt", "power"])
def test_power_and_hashrate_have_independent_coverage(conn, monkeypatch, missing):
    scans(conn, lambda t: [dict(rec("10.0.0.1", worker="A", hr=300), **{missing: None})])
    r = report(conn, monkeypatch)["A"]
    assert r["uptime_pct"] == r["uptime_coverage_pct"] == 100
    if missing == "hr_rt":
        assert r["delivered_th_h"] is None and r["delivery_coverage_pct"] == 0
        assert r["power_kwh"] == 3 and r["power_coverage_pct"] == 100
    else:
        assert r["power_kwh"] is None and r["power_coverage_pct"] == 0
        assert r["delivered_th_h"] == 300 and r["delivery_coverage_pct"] == 100


def test_real_zero_is_retained(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A", hr=0, power=0)])
    r = report(conn, monkeypatch)["A"]
    assert r["power_kwh"] == r["delivered_th_h"] == 0
    assert r["power_coverage_pct"] == r["delivery_coverage_pct"] == 100


def test_peak_machines_does_not_union_successive_ips(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1" if t < 1800 else "10.0.0.2", worker="A", hr=300)])
    r = report(conn, monkeypatch)["A"]
    assert r["machines"] == r["current_machines"] == 1
    assert r["delivered_th_h"] == 300


def test_expiry_uses_actual_measurement_time_not_completion_time(conn, monkeypatch):
    insert_scan(conn, BASE + 600, "full", [rec("10.0.0.1", worker="A", hr=300, measured_ts=BASE)])
    r = report(conn, monkeypatch, 1000)["A"]
    assert r["hashrate_ths"] is None and r["hashrate_stale"] is True
    assert r["hashrate_ts"] == BASE and r["hashrate_age_s"] == 1000
    assert r["delivered_th_h"] == 25  # 完成600s到原采样900s，只能再代表300s。
    assert r["power_kwh"] == .25
    assert r["delivery_observed_hours"] == pytest.approx(300 / 3600, abs=1e-6)


def test_scan_keeps_raw_evidence_but_does_not_count_old_values_as_current(conn, monkeypatch):
    monkeypatch.setattr(db.time, "time", lambda: BASE + 1000)
    rows = [rec("10.0.0.1", worker="A", hr=300, measured_ts=BASE),
            rec("10.0.0.2", worker="A", hr=290, measured_ts=BASE + 999)]
    sid, ts, kept = db.save_scan(conn, "full", rows)
    head = db.latest_scan(conn)
    assert (head["total"], head["online"], head["offline"], head["unknown"]) == (2, 1, 0, 1)
    assert head["total_hr"] is None
    assert kept[0]["hr_rt"] == 300 and kept[0]["status"] == "online"
    assert db.scan_records(conn, sid)[0]["measured_ts"] == BASE


def test_old_rate_archive_cannot_manufacture_energy_or_uptime(conn, monkeypatch):
    conn.execute("INSERT INTO hashrate_hourly VALUES(?,?)", (BASE, 3600))
    conn.execute("INSERT INTO worker_hashrate_hourly VALUES(?,?,?,?,?)", (BASE, "A", 300, 0, 1))
    conn.commit()
    r = report(conn, monkeypatch)["A"]
    assert r["avg_hashrate_ths"] == r["delivered_th_h"] == 300
    assert r["power_kwh"] is None and r["uptime_pct"] is None
    assert r["power_coverage_pct"] == r["uptime_coverage_pct"] == 0


def test_v2_archive_survives_pruning_without_rewriting_old_ledger(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A", hr=300)])
    conn.execute("INSERT INTO worker_hourly VALUES(?,?,?,?,?,?,?)", (BASE, "A", 1, 12, 12, 999, 99))
    conn.commit()
    db.meta_set(conn, "rollup_hour", BASE + 3600)
    before_ledger = [tuple(r) for r in conn.execute("SELECT * FROM worker_hourly")]
    before = report(conn, monkeypatch)["A"]
    conn.execute("DELETE FROM snapshots")
    conn.execute("DELETE FROM scans")
    conn.commit()
    after = report(conn, monkeypatch)["A"]
    assert before["delivered_th_h"] == after["delivered_th_h"] == 300
    assert before["power_kwh"] == after["power_kwh"] == 3
    assert before["uptime_pct"] == after["uptime_pct"] == 100
    assert after["legacy_delivered_th_h"] == 999
    assert [tuple(r) for r in conn.execute("SELECT * FROM worker_hourly")] == before_ledger


def test_inventory_event_keeps_historical_contribution_and_old_sample_times(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A", hr=300),
                           rec("10.0.0.2", worker="B", hr=200)], end=3300)
    old = db.latest_scan(conn)
    monkeypatch.setattr(db.time, "time", lambda: BASE + 3500)
    db.remove_miners(conn, ["10.0.0.1"])
    event = db.latest_scan(conn)
    assert event["kind"] == "inventory" and event["ts"] == BASE + 3500
    assert len(db.scan_records(conn, old["scan_id"])) == 2
    left = db.scan_records(conn, event["scan_id"])
    assert [r["ip"] for r in left] == ["10.0.0.2"]
    assert left[0]["measured_ts"] == BASE + 3300
    rows = report(conn, monkeypatch, 3600)
    assert rows["A"]["delivered_th_h"] == 291.667  # 3500s真实贡献；下架后100s为零。
    assert rows["A"]["current_machines"] == 0
    assert rows["B"]["delivered_th_h"] == 200


def test_same_second_inventory_event_wins_by_scan_id(conn, monkeypatch):
    insert_scan(conn, BASE, "quick", [rec("10.0.0.1", worker="A", hr=300)])
    monkeypatch.setattr(db.time, "time", lambda: BASE)
    db.remove_miners(conn, ["10.0.0.1"])
    rows = report(conn, monkeypatch, 300)
    assert "A" not in rows or rows["A"]["delivered_th_h"] == 0


@pytest.mark.parametrize("value", [True, -1, math.inf, math.nan, "invalid"])
def test_invalid_explicit_sample_time_does_not_fall_back_to_now(value):
    assert db.record_freshness({"measured_ts": value}, BASE, BASE) is False


def test_future_clock_error_does_not_become_valid_as_query_window_moves():
    assert db.record_freshness({"measured_ts": BASE + 400}, BASE, BASE + 500) is False


def test_explicit_period_end_really_excludes_later_readings(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A", hr=300 if t < 1800 else 900)] +
          ([rec("10.0.0.2", worker="A", hr=50)] if t >= 1800 else []))
    monkeypatch.setattr(db.time, "time", lambda: BASE + 3600)
    r = db.customer_report(conn, 1, until_ts=BASE + 1800)[0]
    assert r["period_end_ts"] == r["billing_period_end_ts"] == BASE + 1800
    assert r["period_start_ts"] == r["billing_period_start_ts"] == BASE - 1800
    assert r["avg_hashrate_ths"] == 300 and r["delivered_th_h"] == 150
    assert r["observed_hours"] == .5 and r["coverage_pct"] == 50
    assert r["hashrate_ths"] == 950  # 实时列仍表示最新有效采样。
    assert r["current_machines"] == 2 and r["machines"] == 1


def test_future_period_end_is_rejected(conn, monkeypatch):
    monkeypatch.setattr(db.time, "time", lambda: BASE)
    with pytest.raises(ValueError):
        db.customer_report(conn, 1, until_ts=BASE + 1)


def test_rollup_work_limit_counts_empty_hours(conn, monkeypatch):
    insert_scan(conn, BASE, "quick", [rec("10.0.0.1", worker="A")])
    monkeypatch.setattr(db.time, "time", lambda: BASE + 100 * 3600)
    db.rollup_hours(conn, max_hours=2)
    assert int(db.meta_get(conn, "rollup_hour")) == BASE + 2 * 3600
    assert int(db.meta_get(conn, "customer_metrics_rollup_hour")) == BASE + 2 * 3600


def test_trend_separates_legacy_values_and_unknown_current_totals(conn, monkeypatch):
    insert_scan(conn, BASE, "quick", [rec("10.0.0.1", worker="A", hr=300)])
    monkeypatch.setattr(db.time, "time", lambda: BASE + 300)
    db.save_scan(conn, "quick", [rec("10.0.0.1", worker="A", hr=300)])
    monkeypatch.setattr(db.time, "time", lambda: BASE + 600)
    db.save_scan(conn, "quick", [rec("10.0.0.1", worker="A", status="unknown")])
    old, valid, missing = db.hashrate_trend(conn)
    assert old["total_hr"] is old["online"] is None
    assert old["legacy_total_hr"] == 300 and old["legacy_online"] == 1
    assert valid["total_hr"] == 300 and valid["online"] == 1
    assert valid["legacy_total_hr"] is None
    assert missing["total_hr"] is missing["online"] is None
    assert missing["known_online"] == 0 and missing["unknown"] == 1


def test_history_span_includes_manual_v2_archive_after_raw_cleanup(conn, monkeypatch):
    scans(conn, lambda t: [rec("10.0.0.1", worker="A", hr=300)], kind="manual")
    monkeypatch.setattr(db.time, "time", lambda: BASE + 3600)
    db.rollup_hours(conn)
    assert conn.execute("SELECT COUNT(*) FROM worker_hourly").fetchone()[0] == 0
    with conn:
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM scans")
    monkeypatch.setattr(db.time, "time", lambda: BASE + 3 * 3600)
    assert db.coverage_hours(conn) == 3
