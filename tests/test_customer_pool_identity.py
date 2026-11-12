"""Primary-pool ownership survives failover through the real scan/database path.

Only device replies are mocked.  Probe, scan, identity backfill, storage and
customer aggregation run normally; no real miner or production database is used.
"""
from types import SimpleNamespace

import pytest

import db
import miner_core
import service
from conftest import insert_scan, rec


BASE = 2_000_000_000 - 2_000_000_000 % 3600
A1, A2, B1 = "192.0.2.11", "192.0.2.12", "192.0.2.21"
IPS = (A1, A2, B1)
RATES = {A1: 300, A2: 300, B1: 200}
POWER = {A1: 5000, A2: 5000, B1: 4000}


@pytest.fixture
def pool_site(cfg, monkeypatch, tmp_path):
    state = SimpleNamespace(
        now=BASE,
        owners={A1: "customer_A", A2: "customer_A", B1: "customer_B"},
        backups={A1: "backup_A", A2: "backup_A", B1: "backup_B"},
        serials={ip: "REALSN" + ip.replace(".", "") for ip in IPS},
        failed=set(), missing=set(),
    )
    monkeypatch.setattr(miner_core.time, "time", lambda: state.now)

    def stock(ip, *args):
        return rec(ip, worker="", hr=RATES[ip], power=POWER[ip], sn=state.serials[ip])

    def pools(ip, command, timeout):
        assert command == "pools"
        failover = ip in state.failed
        # All customers use the same public pool: a shared URL is not an owner.
        url = "stratum+tcp://shared-pool.example:3333"
        return {"POOLS": [
            {"POOL": 0, "Priority": int(failover), "Status": "Dead" if failover else "Alive",
             "User": "" if ip in state.missing else state.owners[ip] + ".rig",
             "URL": url, "Accepted": 100, "Rejected": 0, "Stale": 0},
            {"POOL": 1, "Priority": 0 if failover else 1, "Status": "Alive",
             "User": state.backups[ip] + ".rig", "URL": url,
             "Accepted": 200, "Rejected": 0, "Stale": 0},
        ]}

    monkeypatch.setattr(miner_core, "probe_stock", stock)
    monkeypatch.setattr(miner_core, "_cgminer", pools)
    cfg["db"]["path"] = str(tmp_path / "pool-customers.db")
    cfg["schedule"]["enabled"] = False
    cfg["scan"].update(liveness_gate=False, max_pps=0, fetch_worker=True)
    site = service.MonitorService(cfg)
    monkeypatch.setattr(service.alerts, "evaluate", lambda *a, **k: [])
    monkeypatch.setattr(site, "_maybe_auto_reboot", lambda records: None)

    def scan(offset, ips=IPS):
        state.now = BASE + offset
        result = site._do_scan("quick", list(ips), workers=1)
        assert result is not None
        return site.latest()

    state.scan = scan
    state.site = site
    yield state
    site.stop()
    site.conn.close()
    db.close_readers()


def customer_rows(site):
    return {r["worker"]: r for r in db.customer_report(site.conn, 1)}


def test_failover_keeps_two_customers_and_conserves_all_machine_metrics(pool_site):
    s = pool_site
    for offset in range(0, 3601, 300):
        s.failed = {A1} if 1200 <= offset < 1800 else set()
        head, rows = s.scan(offset)
        current = {r["worker"]: r for r in db.customer_hashrates(head, rows)}
        assert set(current) == {"customer_A", "customer_B"}
        assert current["customer_A"]["machines"] == 2
        assert current["customer_B"]["machines"] == 1
        assert sum(r["machines"] for r in current.values()) == len(IPS)
        assert sum(r["hashrate_ths"] for r in current.values()) == 800
        assert sum(r["power"] for r in rows) == 14000

    for archived in (False, True):
        if archived:
            db.rollup_hours(s.site.conn, s.now)
        report = customer_rows(s.site)
        assert set(report) == {"customer_A", "customer_B"}
        assert report["customer_A"]["avg_hashrate_ths"] == 600
        assert report["customer_B"]["avg_hashrate_ths"] == 200
        assert sum(r["delivered_th_h"] for r in report.values()) == 800
        assert sum(r["power_kwh"] for r in report.values()) == 14
        assert {r["worker"] for r in db.known_identity(s.site.conn).values()} == set(report)


def test_upgrade_keeps_old_backup_history_without_a_current_third_customer(pool_site):
    s = pool_site
    # The former collector attributed only this machine to the active backup.
    for offset in (0, 300):
        insert_scan(s.site.conn, BASE + offset, "quick", [
            rec(ip, worker="backup_A" if ip == A1 else s.owners[ip],
                hr=RATES[ip], power=POWER[ip], sn=s.serials[ip]) for ip in IPS])
    old_evidence = [tuple(r) for r in s.site.conn.execute(
        "SELECT scan_id,ip,worker FROM snapshots ORDER BY scan_id,ip")]

    s.failed = {A1}
    for offset in range(600, 3601, 300):
        s.scan(offset)
    for archived in (False, True):
        if archived:
            db.rollup_hours(s.site.conn, s.now)
        rows = customer_rows(s.site)
        assert set(rows) == {"customer_A", "customer_B", "backup_A"}
        assert {w for w, r in rows.items() if r["current_machines"] > 0} == {
            "customer_A", "customer_B"}
        assert rows["backup_A"]["current_machines"] == 0
        assert rows["backup_A"]["hashrate_ths"] == 0
        assert rows["backup_A"]["delivered_th_h"] == 50
        assert rows["backup_A"]["power_kwh"] == pytest.approx(5 / 6, abs=.001)
        assert sum(r["current_machines"] for r in rows.values()) == len(IPS)
        assert sum(r["hashrate_ths"] for r in rows.values()) == 800
        assert sum(r["delivered_th_h"] for r in rows.values()) == 800
        assert sum(r["power_kwh"] for r in rows.values()) == pytest.approx(14, abs=.001)
        assert [tuple(r) for r in s.site.conn.execute(
            "SELECT scan_id,ip,worker FROM snapshots WHERE scan_id<=2 ORDER BY scan_id,ip"
        )] == old_evidence


def test_real_primary_account_transfer_keeps_both_owners_history(pool_site):
    s = pool_site
    s.failed = {A1}
    for offset in range(0, 3601, 300):
        if offset >= 1800:
            s.owners[A1] = s.owners[A2] = "customer_C"
        s.scan(offset)
    db.rollup_hours(s.site.conn, s.now)
    rows = customer_rows(s.site)
    assert set(rows) == {"customer_A", "customer_B", "customer_C"}
    assert rows["customer_A"]["current_machines"] == 0
    assert rows["customer_C"]["current_machines"] == 2
    for worker in ("customer_A", "customer_C"):
        assert rows[worker]["delivered_th_h"] == 300
        assert rows[worker]["power_kwh"] == 5
    assert rows["customer_B"]["delivered_th_h"] == 200
    assert sum(r["power_kwh"] for r in rows.values()) == 14


@pytest.mark.parametrize("known_worker", ["customer_A", "old_backup_A"])
def test_missing_primary_keeps_last_identity_then_recovers_when_readable(pool_site, known_worker):
    s = pool_site
    db.upsert_known_miners(s.site.conn, [rec(A1, worker=known_worker, sn=s.serials[A1])], BASE)
    s.failed = s.missing = {A1}
    for offset in (0, 300):
        _head, rows = s.scan(offset, [A1])
        assert rows[0]["worker"] == known_worker
    # A readable configured primary replaces even an old erroneous backup name.
    s.missing = set()
    _head, rows = s.scan(600, [A1])
    assert rows[0]["worker"] == "customer_A"
    assert db.known_identity(s.site.conn)[A1]["worker"] == "customer_A"


def test_missing_primary_on_replacement_machine_does_not_inherit_previous_owner(pool_site):
    s = pool_site
    s.scan(0, [A1])
    s.serials[A1] = "REPLACEMENT0001"
    s.failed = s.missing = {A1}
    _head, rows = s.scan(300, [A1])
    assert rows[0]["worker"] == "(未知)"
    assert db.known_identity(s.site.conn)[A1]["worker"] == "(未知)"
