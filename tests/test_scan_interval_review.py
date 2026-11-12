"""Independent regressions for completed-scan intervals, inventory and archival.

All samples and inventory events use real SQLite transactions. No scanner,
network client or miner command is started.
"""
import pytest

import db
from conftest import rec


BASE = 1_800_000_000 - 1_800_000_000 % 3600
IP_A, IP_B = "192.0.2.1", "192.0.2.2"


def _at(monkeypatch, seconds):
    monkeypatch.setattr(db.time, "time", lambda: BASE + seconds)


def _save(conn, monkeypatch, started, completed, ips=(IP_A,)):
    _at(monkeypatch, completed)
    records = [rec(ip, worker="A", hr=100, power=3000, measured_ts=BASE + started)
               for ip in ips]
    return db.save_scan(conn, "full", records, started_ts=BASE + started)


def _progress(conn):
    return {key: int(db.meta_get(conn, key, 0) or 0) for key in
            ("rollup_hour", "hashrate_rollup_hour", "customer_metrics_rollup_hour")}


def test_maintenance_does_not_advance_past_an_unsealed_hour(conn, monkeypatch):
    _save(conn, monkeypatch, 0, 0)
    _at(monkeypatch, 3600)
    db.rollup_hours(conn, now=BASE + 3600)
    assert conn.execute("SELECT COUNT(*) FROM customer_metrics_hourly").fetchone()[0] == 0
    assert all(stamp <= BASE for stamp in _progress(conn).values())

    # The next scan was already running at the earlier rollup, but its result
    # only commits now. Maintenance must return to the first provisional hour.
    _save(conn, monkeypatch, 300, 7200)
    db.rollup_hours(conn, now=BASE + 7200)
    archived = conn.execute("SELECT hour,observed_s FROM customer_metrics_hourly ORDER BY hour").fetchall()
    assert [tuple(row) for row in archived] == [(BASE, 3600), (BASE + 3600, 3600)]
    values = conn.execute("SELECT hour,power_kwh,th_h FROM worker_metrics_hourly ORDER BY hour").fetchall()
    assert [tuple(row) for row in values] == [(BASE, 3, 100), (BASE + 3600, 3, 100)]
    assert set(_progress(conn).values()) == {BASE + 7200}


def test_inventory_change_during_a_long_scan_preserves_both_intervals(conn, monkeypatch):
    first_id, _ts, _records = _save(conn, monkeypatch, 0, 0, (IP_A, IP_B))
    _at(monkeypatch, 1200)
    db.remove_miners(conn, [IP_B])
    inventory = db.latest_scan(conn)
    remaining = db.scan_records(conn, inventory["scan_id"])
    assert remaining[0]["measured_ts"] == BASE
    assert len(db.scan_records(conn, first_id)) == 2
    _save(conn, monkeypatch, 300, 1800)

    seconds, workers = db._customer_metric_window(conn, BASE, BASE + 1800)
    a = workers["A"]
    assert seconds == 1800
    # Two 3 kW devices for 20 min; one remaining device for the last 10 min.
    assert a["power_kwh"] == pytest.approx(2.5)
    assert a["th_h"] == pytest.approx(100 * (2 * 1200 + 600) / 3600)
    assert a["online_machine_s"] == a["known_machine_s"] == 3000
    assert a["unknown_s"] == a["power_unknown_s"] == a["status_unknown_s"] == 0


@pytest.mark.parametrize("next_start", [None, 3300])
def test_inventory_edit_cannot_renew_a_scan_after_a_real_outage(conn, monkeypatch, next_start):
    _save(conn, monkeypatch, 0, 0, (IP_A, IP_B))
    _at(monkeypatch, 2700)
    db.remove_miners(conn, [IP_B])
    inventory = db.latest_scan(conn)
    assert db.scan_records(conn, inventory["scan_id"])[0]["measured_ts"] == BASE
    if next_start is not None:
        # This new scan starts near the inventory edit, but far too late after
        # the original scan to prove continuous collection during the outage.
        _save(conn, monkeypatch, next_start, 3600)

    seconds, workers = db._customer_metric_window(conn, BASE, BASE + 3600)
    assert seconds == 900
    assert workers["A"]["power_kwh"] == pytest.approx(1.5)
    assert workers["A"]["th_h"] == pytest.approx(50)


def test_inventory_event_does_not_seal_a_pending_scan_hour(conn, monkeypatch):
    _save(conn, monkeypatch, 0, 0, (IP_A, IP_B))
    _at(monkeypatch, 4200)
    db.remove_miners(conn, [IP_B])
    # A report is opened while the real next scan is still running. An
    # inventory timestamp after the hour boundary must not seal that hour.
    db.customer_report(conn, 1, until_ts=BASE + 3600)
    assert conn.execute("SELECT COUNT(*) FROM customer_metrics_hourly WHERE hour=?", (BASE,)).fetchone()[0] == 0

    _save(conn, monkeypatch, 300, 7200)
    report = db.customer_report(conn, 2)[0]
    # Two devices for 70 min, then one for 50 min: 7 + 2.5 kWh.
    assert report["power_kwh"] == pytest.approx(9.5)
    assert report["delivered_th_h"] == pytest.approx(316.667)
    assert report["power_observed_hours"] == 2
