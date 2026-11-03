"""Trend gaps distinguish a missing fleet total from usable partial observations."""
import sqlite3

import pytest

import db
from conftest import insert_scan, rec


BASE = 2_000_000_000


def save(conn, monkeypatch, rows, offset=0, kind="quick"):
    monkeypatch.setattr(db.time, "time", lambda: BASE + offset)
    return db.save_scan(conn, kind, rows)[0]


def clear_new_metadata(conn, scan_id):
    """Represent a pre-upgrade, quality-aware scan without the new subtotal fields."""
    conn.execute("UPDATE scans SET known_total_hr=NULL,hashrate_known_machines=NULL WHERE scan_id=?",
                 (scan_id,))
    conn.commit()


@pytest.mark.parametrize("status,rate", [("unknown", None), ("online", None)])
def test_one_missing_miner_does_not_hide_538_observed_hashrates(conn, monkeypatch, status, rate):
    rows = [rec("10.0.%d.%d" % (i // 254, i % 254 + 1), hr=300) for i in range(539)]
    rows[-1].update(status=status, hr_rt=rate)
    sid = save(conn, monkeypatch, rows)
    point = db.hashrate_trend(conn)[0]
    assert point["total_hr"] is None
    assert point["known_total_hr"] == 161400
    assert point["hashrate_known_machines"] == 538
    assert point["total"] == 539
    assert point["data_quality"] == "incomplete"
    stored = conn.execute("SELECT known_total_hr,hashrate_known_machines FROM scans WHERE scan_id=?",
                          (sid,)).fetchone()
    assert tuple(stored) == (161400, 538)


def test_power_only_missing_does_not_make_hashrate_partial(conn, monkeypatch):
    save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=290, power=None)])
    point = db.hashrate_trend(conn)[0]
    assert point["total_hr"] == point["known_total_hr"] == 590
    assert point["hashrate_known_machines"] == point["total"] == 2
    assert point["data_quality"] == "complete"


def test_zero_partial_all_unknown_and_empty_are_distinct(conn, monkeypatch):
    save(conn, monkeypatch, [rec("10.0.0.1", hr=0), rec("10.0.0.2", status="offline"),
                            rec("10.0.0.3", status="unknown")])
    save(conn, monkeypatch, [rec("10.0.0.1", status="unknown")], offset=300)
    save(conn, monkeypatch, [], offset=600)
    partial, missing, empty = db.hashrate_trend(conn)
    assert partial["total_hr"] is None
    assert partial["known_total_hr"] == 0 and partial["hashrate_known_machines"] == 2
    assert missing["total_hr"] is missing["known_total_hr"] is None
    assert missing["hashrate_known_machines"] == 0 and missing["total"] == 1
    assert empty["total_hr"] == empty["known_total_hr"] == 0
    assert empty["hashrate_known_machines"] == empty["total"] == 0
    assert empty["data_quality"] == "complete"


def test_confirmed_offline_contributes_known_zero_without_hiding_current_total(conn, monkeypatch):
    save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", status="offline")])
    point = db.hashrate_trend(conn)[0]
    assert point["total_hr"] == point["known_total_hr"] == 300
    assert point["hashrate_known_machines"] == point["total"] == 2


@pytest.mark.parametrize("age,known", [(899.9, 2), (900, 2), (900.1, 1), (-61, 1)])
def test_only_readings_fresh_at_scan_completion_enter_subtotal(conn, monkeypatch, age, known):
    save(conn, monkeypatch, [rec("10.0.0.1", hr=300),
                            rec("10.0.0.2", hr=200, measured_ts=BASE-age)])
    point = db.hashrate_trend(conn)[0]
    assert point["hashrate_known_machines"] == known
    assert point["known_total_hr"] == (500 if known == 2 else 300)
    assert point["total_hr"] == (500 if known == 2 else None)


@pytest.mark.parametrize("invalid", [True, False, "300", "bad", -1, float("inf"), float("nan")])
def test_persisted_coverage_preserves_original_validity_and_does_not_reread_snapshots(conn, monkeypatch, invalid):
    # SQLite REAL affinity turns True and numeric strings into numbers. The
    # saved aggregate must retain the scan's original measurement decision.
    save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=invalid)])
    queries = []
    conn.set_trace_callback(queries.append)
    try:
        point = db.hashrate_trend(conn)[0]
    finally:
        conn.set_trace_callback(None)
    assert point["known_total_hr"] == 300 and point["hashrate_known_machines"] == 1
    assert point["total_hr"] is None
    assert not any("FROM snapshots" in q for q in queries)


def test_old_partial_recovers_only_same_scan_evidence_without_rewriting_history(conn, monkeypatch):
    save(conn, monkeypatch, [rec("10.0.0.1", hr=999), rec("10.0.0.2", hr=999)])
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=None)], offset=300)
    clear_new_metadata(conn, sid)
    before = tuple(conn.execute("SELECT * FROM scans WHERE scan_id=?", (sid,)).fetchone())
    raw = [tuple(r) for r in conn.execute("SELECT * FROM snapshots WHERE scan_id=?", (sid,))]
    point = db.hashrate_trend(conn, 1)[0]
    assert point["ts"] == BASE + 300 and point["total_hr"] is None
    assert point["known_total_hr"] == 300 and point["hashrate_known_machines"] == 1
    assert tuple(conn.execute("SELECT * FROM scans WHERE scan_id=?", (sid,)).fetchone()) == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM snapshots WHERE scan_id=?", (sid,))] == raw


@pytest.mark.parametrize("invalid", [True, False, "300"])
def test_old_partial_with_contradictory_complete_recalculation_stays_unknown(conn, monkeypatch, invalid):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=invalid)])
    clear_new_metadata(conn, sid)
    # A persisted REAL value cannot prove its former transport type. The old
    # missing total is evidence against treating this as a complete reading.
    point = db.hashrate_trend(conn)[0]
    assert point["total_hr"] is point["known_total_hr"] is point["hashrate_known_machines"] is None


def test_old_partial_with_changed_status_counts_stays_unknown(conn, monkeypatch):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300),
                                   rec("10.0.0.2", hr=200, measured_ts=str(BASE)),
                                   rec("10.0.0.3", hr=None)])
    clear_new_metadata(conn, sid)
    # SQLite converted an untrusted source timestamp to REAL. One missing
    # hashrate still makes the recalculation partial, but its status counts
    # contradict the saved scan and must not establish a usable subtotal.
    point = db.hashrate_trend(conn)[0]
    assert point["unknown"] == 1
    assert point["total_hr"] is point["known_total_hr"] is point["hashrate_known_machines"] is None


@pytest.mark.parametrize("remove_count", [1, 2])
def test_old_partial_without_complete_snapshot_set_remains_unknown(conn, monkeypatch, remove_count):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=None)])
    clear_new_metadata(conn, sid)
    ips = ["10.0.0.1", "10.0.0.2"][:remove_count]
    conn.executemany("DELETE FROM snapshots WHERE scan_id=? AND ip=?", [(sid, ip) for ip in ips])
    conn.commit()
    point = db.hashrate_trend(conn)[0]
    assert point["total_hr"] is point["known_total_hr"] is point["hashrate_known_machines"] is None
    assert point["total"] == 2


def test_old_partial_recovery_obeys_original_measurement_time(conn, monkeypatch):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300),
                                   rec("10.0.0.2", hr=200, measured_ts=BASE-901)])
    clear_new_metadata(conn, sid)
    monkeypatch.setattr(db.time, "time", lambda: BASE + 86400)
    point = db.hashrate_trend(conn)[0]
    assert point["known_total_hr"] == 300 and point["hashrate_known_machines"] == 1
    assert point["ts"] == BASE and point["total_hr"] is None


def test_old_complete_scan_reuses_verified_total_without_raw_data(conn, monkeypatch):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300)])
    clear_new_metadata(conn, sid)
    conn.execute("DELETE FROM snapshots")
    conn.commit()
    point = db.hashrate_trend(conn)[0]
    assert point["total_hr"] == point["known_total_hr"] == 300
    assert point["hashrate_known_machines"] == point["total"] == 1


def test_legacy_snapshot_is_not_promoted_to_verified_partial(conn):
    insert_scan(conn, BASE, "quick", [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=None)])
    point = db.hashrate_trend(conn)[0]
    assert point["data_quality"] == "legacy" and point["legacy_total_hr"] == 300
    assert point["total_hr"] is point["known_total_hr"] is point["hashrate_known_machines"] is None


@pytest.mark.parametrize("age", [300, 901])
def test_inventory_event_keeps_timestamp_and_rechecks_coverage(conn, monkeypatch, age):
    save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=None)])
    monkeypatch.setattr(db.time, "time", lambda: BASE + age)
    db.remove_miners(conn, ["10.0.0.2"])
    point = db.hashrate_trend(conn, 1)[0]
    assert point["ts"] == BASE + age and point["total"] == 1
    assert point["total_hr"] == point["known_total_hr"] == (300 if age <= 900 else None)
    assert point["hashrate_known_machines"] == (1 if age <= 900 else 0)
    assert point["unknown"] == (0 if age <= 900 else 1)
    assert db.scan_records(conn, db.latest_scan(conn)["scan_id"])[0]["measured_ts"] == BASE


def test_upgrade_adds_nullable_columns_without_backfilling_history(tmp_path):
    path = str(tmp_path / "old-trend.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE scans (scan_id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER NOT NULL,"
                "kind TEXT,total INTEGER,online INTEGER,offline INTEGER,unknown INTEGER,total_hr REAL)")
    old.execute("INSERT INTO scans(ts,kind,total,online,offline,unknown,total_hr) VALUES(?,'quick',2,2,0,0,NULL)",
                (BASE,))
    old.commit()
    old.close()
    upgraded = db.init_db(path)
    try:
        row = dict(upgraded.execute("SELECT * FROM scans").fetchone())
        assert row["known_total_hr"] is row["hashrate_known_machines"] is None
        assert row["total_hr"] is None and row["unknown"] == 0
        assert db.hashrate_trend(upgraded)[0]["known_total_hr"] is None
        assert dict(upgraded.execute("SELECT * FROM scans").fetchone()) == row
    finally:
        db.close_readers()
        upgraded.close()
