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


@pytest.mark.parametrize("age,known", [(899.9, 2), (900, 2), (900.1, 2), (-61, 1)])
def test_completed_result_keeps_long_scan_readings_but_rejects_future_clocks(conn, monkeypatch, age, known):
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


def test_old_partial_stays_unknown_without_reinterpreting_snapshots_or_rewriting_history(conn, monkeypatch):
    save(conn, monkeypatch, [rec("10.0.0.1", hr=999), rec("10.0.0.2", hr=999)])
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=None)], offset=300)
    clear_new_metadata(conn, sid)
    before = tuple(conn.execute("SELECT * FROM scans WHERE scan_id=?", (sid,)).fetchone())
    raw = [tuple(r) for r in conn.execute("SELECT * FROM snapshots WHERE scan_id=?", (sid,))]
    queries = []
    conn.set_trace_callback(queries.append)
    try:
        point = db.hashrate_trend(conn, 1)[0]
    finally:
        conn.set_trace_callback(None)
    assert point["ts"] == BASE + 300 and point["total_hr"] is None
    assert point["known_total_hr"] is point["hashrate_known_machines"] is None
    assert not any("FROM snapshots" in q for q in queries)
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


@pytest.mark.parametrize("invalid,stored_value", [("9000", 9000), (True, 1), (False, 0)])
def test_old_partial_cannot_recover_converted_invalid_value_with_another_missing_reading(
        conn, monkeypatch, invalid, stored_value):
    # A third, missing reading keeps recalculation partial with unchanged
    # online counts, so aggregate completeness/status checks cannot detect
    # the lost source type. Only newly saved coverage is valid evidence.
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=invalid),
                                   rec("10.0.0.3", hr=None)])
    new = db.hashrate_trend(conn)[0]
    assert new["total_hr"] is None
    assert new["known_total_hr"] == 300 and new["hashrate_known_machines"] == 1
    stored = conn.execute("SELECT hr_rt,typeof(hr_rt) FROM snapshots WHERE scan_id=? AND ip=?",
                          (sid, "10.0.0.2")).fetchone()
    assert tuple(stored) == (stored_value, "real")
    clear_new_metadata(conn, sid)
    old = db.hashrate_trend(conn)[0]
    assert old["online"] == old["total"] == 3 and old["unknown"] == 0
    assert old["total_hr"] is old["known_total_hr"] is old["hashrate_known_machines"] is None


def test_old_partial_with_changed_status_counts_stays_unknown(conn, monkeypatch):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300),
                                   rec("10.0.0.2", hr=200, measured_ts=str(BASE)),
                                   rec("10.0.0.3", hr=None)])
    clear_new_metadata(conn, sid)
    # An untrusted source timestamp cannot become valid through SQLite's REAL
    # affinity. Missing original subtotal evidence must also stay missing.
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


def test_completed_long_scan_keeps_verified_total_and_original_measurement_time(conn, monkeypatch):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300),
                                   rec("10.0.0.2", hr=200, measured_ts=BASE-901)])
    clear_new_metadata(conn, sid)
    monkeypatch.setattr(db.time, "time", lambda: BASE + 86400)
    point = db.hashrate_trend(conn)[0]
    assert point["known_total_hr"] == 500 and point["hashrate_known_machines"] == 2
    assert point["ts"] == BASE and point["total_hr"] == 500
    rows = db.scan_records(conn, sid)
    assert rows[1]["measured_ts"] == BASE - 901
    assert not db.record_freshness(rows[1], BASE, BASE + 86400)


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
    assert db.hashrate_trend(conn) == []


@pytest.mark.parametrize("age", [300, 900, 901])
def test_inventory_event_keeps_completed_result_and_does_not_renew_timestamp(conn, monkeypatch, age):
    save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=None)])
    monkeypatch.setattr(db.time, "time", lambda: BASE + age)
    db.remove_miners(conn, ["10.0.0.2"])
    point = db.hashrate_trend(conn, 1)[0]
    assert point["ts"] == BASE + age and point["total"] == 1
    assert point["total_hr"] == point["known_total_hr"] == 300
    assert point["hashrate_known_machines"] == 1 and point["unknown"] == 0
    original = db.scan_records(conn, db.latest_scan(conn)["scan_id"])[0]
    assert original["measured_ts"] == BASE
    assert db.record_freshness(original, point["ts"], BASE + age) is (age <= 900)


@pytest.mark.parametrize("old_metadata", [False, True])
@pytest.mark.parametrize("invalid", ["9000", True, False])
def test_inventory_never_promotes_converted_invalid_readings(conn, monkeypatch, old_metadata, invalid):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=invalid),
                                   rec("10.0.0.3", hr=None), rec("10.0.0.4", hr=200)])
    if old_metadata:
        clear_new_metadata(conn, sid)
    original = tuple(conn.execute("SELECT * FROM scans WHERE scan_id=?", (sid,)).fetchone())
    raw = [tuple(r) for r in conn.execute("SELECT * FROM snapshots WHERE scan_id=?", (sid,))]
    # Removing an unrelated valid miner must not turn the coerced source value
    # into a measured subtotal. Later removals cannot restore lost provenance.
    for offset, ip, total in [(10, "10.0.0.4", 3), (20, "10.0.0.2", 2), (30, "10.0.0.3", 1)]:
        monkeypatch.setattr(db.time, "time", lambda: BASE + offset)
        db.remove_miners(conn, [ip])
        point = db.hashrate_trend(conn, 1)[0]
        assert point["total"] == total
        assert point["total_hr"] is point["known_total_hr"] is point["hashrate_known_machines"] is None
        assert all(r["measured_ts"] == BASE for r in db.scan_records(conn, db.latest_scan(conn)["scan_id"]))
    assert tuple(conn.execute("SELECT * FROM scans WHERE scan_id=?", (sid,)).fetchone()) == original
    assert [tuple(r) for r in conn.execute("SELECT * FROM snapshots WHERE scan_id=?", (sid,))] == raw


def test_inventory_preserves_verified_partial_zero_offline_and_empty_readings(conn, monkeypatch):
    save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=200),
                            rec("10.0.0.3", hr=None), rec("10.0.0.4", status="offline")])
    for offset, ip, total, full, subtotal, known in [
            (10, "10.0.0.2", 3, None, 300, 2), (20, "10.0.0.3", 2, 300, 300, 2),
            (30, "10.0.0.1", 1, 0, 0, 1), (40, "10.0.0.4", 0, 0, 0, 0)]:
        monkeypatch.setattr(db.time, "time", lambda: BASE + offset)
        db.remove_miners(conn, [ip])
        point = db.hashrate_trend(conn, 1)[0]
        assert (point["total"], point["total_hr"], point["known_total_hr"],
                point["hashrate_known_machines"]) == (total, full, subtotal, known)


def test_inventory_can_inherit_an_old_verified_complete_scan(conn, monkeypatch):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=200)])
    clear_new_metadata(conn, sid)
    monkeypatch.setattr(db.time, "time", lambda: BASE + 10)
    db.remove_miners(conn, ["10.0.0.2"])
    point = db.hashrate_trend(conn, 1)[0]
    assert point["total_hr"] == point["known_total_hr"] == 300
    assert point["hashrate_known_machines"] == point["total"] == 1


def test_inventory_checks_the_entire_source_before_removing_rows(conn, monkeypatch):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=200)])
    clear_new_metadata(conn, sid)
    conn.execute("UPDATE snapshots SET hr_rt=9000 WHERE scan_id=? AND ip='10.0.0.2'", (sid,))
    conn.commit()
    monkeypatch.setattr(db.time, "time", lambda: BASE + 10)
    db.remove_miners(conn, ["10.0.0.2"])
    point = db.hashrate_trend(conn, 1)[0]
    assert point["total"] == 1
    assert point["total_hr"] is point["known_total_hr"] is point["hashrate_known_machines"] is None


@pytest.mark.parametrize("source_kind", ["old_partial", "legacy_complete"])
def test_inventory_without_hash_evidence_stays_unknown_until_proven_empty(conn, monkeypatch, source_kind):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300),
                                   rec("10.0.0.2", hr=None if source_kind == "old_partial" else 200),
                                   rec("10.0.0.3", hr=100)])
    clear_new_metadata(conn, sid)
    if source_kind == "legacy_complete":
        conn.execute("UPDATE scans SET unknown=NULL WHERE scan_id=?", (sid,))
        conn.commit()
    monkeypatch.setattr(db.time, "time", lambda: BASE + 10)
    db.remove_miners(conn, ["10.0.0.3"])
    point = db.hashrate_trend(conn, 1)[0]
    assert point["total"] == 2
    assert point["total_hr"] is point["known_total_hr"] is point["hashrate_known_machines"] is None
    # Inventory membership is complete even though source hash validity is not.
    monkeypatch.setattr(db.time, "time", lambda: BASE + 20)
    db.remove_miners(conn, ["10.0.0.1", "10.0.0.2"])
    empty = db.hashrate_trend(conn, 1)[0]
    assert empty["total_hr"] == empty["known_total_hr"] == 0
    assert empty["hashrate_known_machines"] == empty["total"] == 0


@pytest.mark.parametrize("first_removed", ["10.0.0.1", "10.0.0.99"])
def test_inventory_missing_source_rows_cannot_become_empty_after_repeated_removals(conn, monkeypatch, first_removed):
    sid = save(conn, monkeypatch, [rec("10.0.0.1", hr=300), rec("10.0.0.2", hr=200)])
    conn.execute("DELETE FROM snapshots WHERE scan_id=? AND ip='10.0.0.2'", (sid,))
    conn.commit()
    for offset, ips in [(10, [first_removed]), (20, ["10.0.0.1", "10.0.0.2"])]:
        monkeypatch.setattr(db.time, "time", lambda: BASE + offset)
        db.remove_miners(conn, ips)
        point = db.hashrate_trend(conn, 1)[0]
        assert point["total"] is None
        assert point["total_hr"] is point["known_total_hr"] is point["hashrate_known_machines"] is None
    # A subsequent actual scan supplies new, independently verified evidence.
    save(conn, monkeypatch, [rec("10.0.0.1", hr=300)], offset=30)
    restored = db.hashrate_trend(conn, 1)[0]
    assert restored["total_hr"] == restored["known_total_hr"] == 300
    assert restored["hashrate_known_machines"] == restored["total"] == 1


def test_inventory_of_an_already_empty_scan_remains_verified_zero(conn, monkeypatch):
    save(conn, monkeypatch, [])
    monkeypatch.setattr(db.time, "time", lambda: BASE + 10)
    db.remove_miners(conn, ["10.0.0.99"])
    point = db.hashrate_trend(conn, 1)[0]
    assert point["total_hr"] == point["known_total_hr"] == 0
    assert point["hashrate_known_machines"] == point["total"] == 0


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
