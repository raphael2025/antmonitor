"""A scan is a completed display snapshot, not a stream of temporary zeroes.

Run the real scanner orchestration and SQLite writes with only miner probes and
outbound actions replaced.  The wall clock advances without a real network wait.
"""
import copy
import threading

import pytest

import data_metrics
import db
import miner_core
import service
from conftest import rec


BASE = 2_000_000_000
IP_A, IP_B, EMPTY = "192.0.2.1", "192.0.2.2", "192.0.2.3"


@pytest.fixture
def completed_service(cfg, monkeypatch, tmp_path):
    # init_db changes the process-wide reader path. Restore it for API fixtures
    # that intentionally keep an in-memory service alive across tests.
    monkeypatch.setattr(db, "_DB_PATH", db._DB_PATH)
    cfg["db"]["path"] = str(tmp_path / "completed-scans.sqlite3")
    cfg["schedule"]["enabled"] = False
    cfg["scan"].update(reconfirm_enabled=False, discovery_pause_sec=0)
    clock = [BASE]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    calls = {"alerts": [], "auto": []}

    def evaluate(_conn, _sid, rows, _cfg, **_kwargs):
        calls["alerts"].append(copy.deepcopy(rows))
        return []

    monkeypatch.setattr(service.alerts, "evaluate", evaluate)
    opened = []

    def make():
        svc = service.MonitorService(cfg)
        monkeypatch.setattr(svc, "_maybe_auto_reboot",
                            lambda rows: calls["auto"].append(copy.deepcopy(rows)))
        opened.append(svc)
        return svc

    svc = make()
    yield svc, clock, calls, make
    for instance in opened:
        instance.stop()
        instance.conn.close()
    db.close_readers()


def seed(svc, monkeypatch):
    rows = [rec(IP_A, hr=300, power=6000, measured_ts=BASE),
            rec(IP_B, hr=200, power=4000, measured_ts=BASE)]
    monkeypatch.setattr(miner_core, "scan", lambda *_a, **_kw: copy.deepcopy(rows))
    return svc._do_scan("quick", [IP_A, IP_B])


@pytest.mark.parametrize("kind", ["quick", "full"])
def test_previous_complete_scan_stays_visible_until_next_scan_is_committed(
        completed_service, monkeypatch, kind):
    svc, clock, calls, make = completed_service
    previous = seed(svc, monkeypatch)
    previous_meta, previous_rows = svc.latest()
    before = copy.deepcopy(previous_rows)
    entered, release = threading.Event(), threading.Event()
    results, failures = [], []
    clock[0] = BASE + 300
    next_rows = [rec(IP_A, hr=0, power=800, measured_ts=clock[0]),
                 rec(IP_B, status="offline", measured_ts=clock[0])]

    def probe(ips, _cfg, progress_cb=None, **_kwargs):
        ips = list(ips)
        # A full scan probes known miners first, then can spend a long time on
        # empty-address discovery. A quick scan can also have slow probes.
        if kind == "full" and ips == [IP_A, IP_B]:
            return copy.deepcopy(next_rows)
        clock[0] = BASE + 3000
        if progress_cb:
            progress_cb(0 if kind == "full" else 1, len(ips))
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test scan was not released")
        return ([miner_core._blank(EMPTY, "offline")] if kind == "full"
                else copy.deepcopy(next_rows))

    def run():
        try:
            results.append(svc._do_scan(kind, [IP_A, IP_B],
                                       discover=[EMPTY] if kind == "full" else []))
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(miner_core, "scan", probe)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(3)
        assert svc.progress["running"]
        meta, visible = svc.latest_display()
        totals = data_metrics.aggregate(visible)
        assert meta["scan_id"] == previous["scan_id"]
        assert meta["ts"] == previous_meta["ts"] == BASE
        assert meta["started_ts"] == BASE
        assert (totals["online"], totals["offline"], totals["unknown"], totals["total"]) == (2, 0, 0, 2)
        assert totals["total_hashrate_th"] == 500
        assert totals["total_power_kw"] == 10
        assert db.latest_scan(svc.conn)["scan_id"] == previous["scan_id"]
        assert svc.latest()[1] == before  # Neither ages nor partial results rewrite the evidence.
        assert not db.record_freshness(before[0], BASE, clock[0])
        # A new service/API reader arriving halfway through the scan must also
        # restore the preceding completed result; no browser-local cache is needed.
        restored_meta, restored_rows = make().latest_display()
        assert restored_meta["scan_id"] == previous["scan_id"]
        assert data_metrics.aggregate(restored_rows)["total_hashrate_th"] == 500
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive() and not failures
    assert not svc.progress["running"]
    meta, visible = svc.latest_display()
    totals = data_metrics.aggregate(visible)
    assert meta["scan_id"] == results[0]["scan_id"] > previous["scan_id"]
    assert meta["ts"] == BASE + 3000
    assert meta["started_ts"] == BASE + 300
    assert db.latest_scan(svc.conn)["started_ts"] == meta["started_ts"]
    assert (totals["online"], totals["offline"], totals["unknown"], totals["total"]) == (1, 1, 0, 2)
    assert totals["total_hashrate_th"] == 0  # A completed real zero must replace old positive output.
    assert totals["total_power_kw"] == .8
    assert {r["measured_ts"] for r in visible} == {BASE + 300}
    assert results[0]["online"] == 1  # Completion/log counts describe the completed scan.
    # Display retention must not authorize a reboot or a new alert from old readings.
    assert all(r["status"] == "unknown" and r["hr_rt"] is None for r in calls["alerts"][-1])
    assert all(r["status"] == "unknown" and r["hr_rt"] is None for r in calls["auto"][-1])


def test_restart_restores_the_same_completed_display_without_renewing_samples(
        completed_service, monkeypatch):
    svc, clock, _calls, make = completed_service
    previous = seed(svc, monkeypatch)
    clock[0] = BASE + 3600
    restarted = make()
    assert restarted.snapshot["scan_id"] == 0
    meta, visible = restarted.latest_display()
    assert meta["scan_id"] == previous["scan_id"]
    assert meta["ts"] == BASE
    assert meta["started_ts"] == BASE
    assert data_metrics.aggregate(visible)["total_hashrate_th"] == 500
    assert {r["measured_ts"] for r in visible} == {BASE}
    assert db.latest_scan(restarted.conn)["scan_id"] == previous["scan_id"]
    assert db.record_freshness(restarted.latest()[1][0], meta["ts"], clock[0]) is False


def test_failed_scan_does_not_replace_last_completed_values(completed_service, monkeypatch):
    svc, clock, _calls, _make = completed_service
    previous = seed(svc, monkeypatch)
    clock[0] = BASE + 1800

    def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic scan failure")

    monkeypatch.setattr(miner_core, "scan", broken)
    with pytest.raises(RuntimeError, match="synthetic scan failure"):
        svc._do_scan("quick", [IP_A, IP_B])
    meta, visible = svc.latest_display()
    assert not svc.progress["running"]
    assert meta["scan_id"] == previous["scan_id"]
    assert data_metrics.aggregate(visible)["total_hashrate_th"] == 500


def test_empty_completed_scan_is_cached_and_replaces_an_older_populated_scan(
        completed_service, monkeypatch):
    svc, clock, _calls, _make = completed_service
    previous = seed(svc, monkeypatch)
    clock[0] = BASE + 300
    latest = svc._do_scan("full", [])
    assert latest["scan_id"] > previous["scan_id"] and latest["miners"] == 0

    def do_not_reload(*_args, **_kwargs):
        raise AssertionError("empty completed snapshots must not fall back to the database")

    monkeypatch.setattr(db, "latest_scan", do_not_reload)
    meta, visible = svc.latest_display()
    assert meta["scan_id"] == latest["scan_id"] and visible == []
    assert data_metrics.aggregate(visible)["total_hashrate_th"] == 0


def test_no_completed_scan_is_distinct_from_an_empty_completed_scan(completed_service):
    svc, _clock, _calls, _make = completed_service
    assert svc.latest_display() == (None, [])


@pytest.mark.parametrize("invalid", [True, False, str(BASE), "bad", 0, -1,
                                    float("inf"), float("nan"), BASE + 61])
def test_invalid_measurement_clock_stays_unknown_after_database_restore(
        completed_service, invalid):
    svc, _clock, _calls, make = completed_service
    sid, ts, rows = db.save_scan(svc.conn, "quick", [rec(IP_A, measured_ts=invalid)])
    svc._publish(sid, ts, "quick", rows)
    for reader in (svc, make()):
        meta, visible = reader.latest_display()
        assert meta["scan_id"] == sid
        assert visible[0]["status"] == "unknown"
        assert visible[0]["hr_rt"] is None
        original = reader.latest()[1][0]
        assert not db.record_time_valid(original, ts)
        assert not db.record_freshness(original, ts, ts)
    assert db.latest_scan(svc.conn)["unknown"] == 1


def test_display_normalization_keeps_raw_evidence_unchanged(completed_service, monkeypatch):
    svc, clock, _calls, _make = completed_service
    seed(svc, monkeypatch)
    raw = copy.deepcopy(svc.latest()[1])
    clock[0] = BASE + 3600
    _meta, visible = svc.latest_display()
    assert visible[0]["eff"] == 20
    visible[0]["hr_rt"] = 999
    visible[0]["status"] = "offline"
    assert svc.latest()[1] == raw
    assert data_metrics.aggregate(svc.latest_display()[1])["total_hashrate_th"] == 500


@pytest.mark.parametrize("removed,remaining,hashrate", [([IP_B], [IP_A], 300),
                                                       ([IP_A, IP_B], [], 0)])
def test_removing_inventory_after_a_long_scan_preserves_remaining_completed_values(
        completed_service, monkeypatch, removed, remaining, hashrate):
    svc, clock, _calls, _make = completed_service

    def long_scan(*_args, **_kwargs):
        clock[0] = BASE + 1800
        return [rec(IP_A, hr=300, measured_ts=BASE),
                rec(IP_B, hr=200, measured_ts=BASE)]

    monkeypatch.setattr(miner_core, "scan", long_scan)
    previous = svc._do_scan("full", [IP_A, IP_B])
    assert data_metrics.aggregate(svc.latest_display()[1])["total_hashrate_th"] == 500
    clock[0] += 300
    db.remove_miners(svc.conn, removed)
    svc.drop_from_snapshot(removed)
    meta, visible = svc.latest_display()
    totals = data_metrics.aggregate(visible)
    assert meta["kind"] == "inventory" and meta["scan_id"] > previous["scan_id"]
    assert [r["ip"] for r in visible] == remaining
    assert totals["online"] == len(remaining) and totals["unknown"] == 0
    assert totals["total_hashrate_th"] == hashrate
    assert all(r["measured_ts"] == BASE for r in visible)
    assert len(db.scan_records(svc.conn, previous["scan_id"])) == 2
