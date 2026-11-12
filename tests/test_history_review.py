"""Independent review examples for anonymous identity and historical query boundaries."""
import copy

import pytest

import db
import miner_core
import service
from conftest import insert_scan, rec


BASE = 1_800_000_000 - 1_800_000_000 % 3600
IP = "192.0.2.1"


@pytest.mark.parametrize("archive_first", [False, True])
def test_unnamed_known_machine_does_not_disappear_when_readings_become_unknown(conn, monkeypatch, archive_first):
    for offset in range(0, 3601, 300):
        record = rec(IP, worker="", hr=300) if not offset else rec(IP, status="unknown", worker="")
        insert_scan(conn, BASE + offset, "quick", [record])
    db.upsert_known_miners(conn, [rec(IP, worker="")], BASE)
    monkeypatch.setattr(db.time, "time", lambda: BASE + 3600)
    if archive_first:
        db.rollup_hours(conn, BASE + 3600)
    row = {r["worker"]: r for r in db.customer_report(conn, 1)}["(未知)"]
    assert row["avg_hashrate_ths"] == 300
    assert row["observed_hours"] == pytest.approx(1 / 12, abs=1e-6)
    assert row["coverage_pct"] == pytest.approx(100 / 12, abs=1e-4)
    assert row["delivered_th_h"] == 25 and row["power_kwh"] == .25
    assert row["hashrate_ths"] is None and row["current_machines"] == 1


def test_anonymous_identity_survives_hour_boundary_without_borrowing_future_owner(conn, monkeypatch):
    insert_scan(conn, BASE - 300, "quick", [rec(IP, worker="", hr=300)])
    for offset in range(0, 3601, 300):
        insert_scan(conn, BASE + offset, "quick", [rec(IP, status="unknown", worker="")])
    db.upsert_known_miners(conn, [rec(IP, worker="future-owner")], BASE + 3600)
    monkeypatch.setattr(db.time, "time", lambda: BASE + 3600)
    row = {r["worker"]: r for r in db.customer_report(conn, 1)}["(未知)"]
    assert row["avg_hashrate_ths"] is None
    assert row["observed_hours"] == 0 and row["delivered_th_h"] is None


def test_current_anonymous_roster_member_remains_unknown_but_unknown_address_is_excluded():
    rows = [rec(IP, status="unknown", worker=""), rec("192.0.2.99", status="unknown", worker="")]
    current = db.customer_hashrates({"ts": BASE}, rows, now=BASE, identities={IP: ""})
    assert current == [{"worker": "(未知)", "machines": 1,
                        "hashrate_ths": None, "hashrate_ts": BASE}]


def test_blank_worker_keeps_last_named_owner_across_hours(conn, monkeypatch):
    insert_scan(conn, BASE - 1800, "quick", [rec(IP, worker="A", hr=300)])
    for offset in range(-1500, 3601, 300):
        insert_scan(conn, BASE + offset, "quick", [rec(IP, worker="", hr=300)])
    monkeypatch.setattr(db.time, "time", lambda: BASE + 3600)
    row = {r["worker"]: r for r in db.customer_report(conn, 1)}["A"]
    assert row["avg_hashrate_ths"] == 300 and row["coverage_pct"] == 100


@pytest.mark.parametrize("end", [3600, 5400])
def test_fixed_window_endpoints_keep_current_statistics_without_legacy_rows(conn, monkeypatch, end):
    for offset in range(0, 10801, 300):
        insert_scan(conn, BASE + offset, "quick", [rec(IP, worker="A", hr=300)])
    monkeypatch.setattr(db.time, "time", lambda: BASE + 10800)
    db.rollup_hours(conn, now=BASE + 10800)
    old = [tuple(r) for r in conn.execute("SELECT * FROM worker_hourly ORDER BY hour,worker")]
    assert old == []
    row = db.customer_report(conn, 1, until_ts=BASE + end)[0]
    assert row["delivered_th_h"] == 300 and row["legacy_delivered_th_h"] is None
    assert row["power_kwh"] == 3 and row["legacy_power_kwh"] is None
    assert [tuple(r) for r in conn.execute("SELECT * FROM worker_hourly ORDER BY hour,worker")] == old


@pytest.mark.parametrize("old_owner,new_serial,expected", [
    ("A", "REAL-SERIAL-1", "A"),
    ("", "REAL-SERIAL-1", "(未知)"),
    ("A", "REAL-SERIAL-2", "(未知)"),
])
def test_snapshot_persists_past_owner_context_for_later_retention(cfg, tmp_path, monkeypatch, old_owner, new_serial, expected):
    cfg["db"]["path"] = str(tmp_path / "identity.db")
    cfg["schedule"]["enabled"] = False
    s = service.MonitorService(cfg)
    monkeypatch.setattr(db.time, "time", lambda: BASE + 300)
    monkeypatch.setattr(s, "_reconfirm", lambda rows, gen: rows)
    monkeypatch.setattr(service.alerts, "evaluate", lambda *a, **k: [])
    monkeypatch.setattr(s, "_maybe_auto_reboot", lambda rows: None)
    try:
        db.upsert_known_miners(s.conn, [rec(IP, worker=old_owner, sn="REAL-SERIAL-1")], BASE)
        original = rec(IP, worker="", sn=new_serial, hr=300)
        monkeypatch.setattr(miner_core, "scan", lambda *a, **k: [copy.deepcopy(original)])
        result = s._do_scan("quick", [IP])
        assert db.scan_records(s.conn, result["scan_id"])[0]["worker"] == expected
        assert original["worker"] == ""
    finally:
        s.stop()
        s.conn.close()
        db.close_readers()
