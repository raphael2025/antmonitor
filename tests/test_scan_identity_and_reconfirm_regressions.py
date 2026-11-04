"""Identity and measured-zero regressions through the real scan persistence path."""
import pytest

import db
import miner_core
import service
from conftest import rec


BASE = 2_000_000_000
IP = "192.0.2.10"
MAC_A = "AA:BB:CC:DD:EE:01"
MAC_B = "AA:BB:CC:DD:EE:02"


@pytest.fixture
def scan_service(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(db, "_DB_PATH", db._DB_PATH)
    cfg["db"]["path"] = str(tmp_path / "measurements.sqlite3")
    cfg["schedule"]["enabled"] = False
    cfg["scan"].update(reconfirm_delay_sec=0, reconfirm_passes=1)
    clock = [BASE]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    monkeypatch.setattr(service.alerts, "evaluate", lambda *a, **kw: [])
    svc = service.MonitorService(cfg)
    monkeypatch.setattr(svc, "_maybe_auto_reboot", lambda *a: None)
    yield svc, clock
    svc.stop()
    svc.conn.close()
    db.close_readers()


def run_scan(svc, clock, monkeypatch, rows, now, kind="full"):
    clock[0] = now
    monkeypatch.setattr(service, "_scan_batch", lambda *a, **kw: [dict(r) for r in rows])
    result = svc._do_scan(kind, [IP])
    assert result is not None
    return result, db.scan_records(svc.conn, result["scan_id"])[0]


@pytest.mark.parametrize("old_sn,new_sn,old_mac,new_mac,new_worker,expected", [
    ("", "", MAC_A, MAC_B, "", "(未知)"),
    ("unknown", "N/A", MAC_A, MAC_B, "", "(未知)"),
    ("SERIAL-A", "", MAC_A, MAC_B, "", "(未知)"),
    ("", "SERIAL-B", MAC_A, MAC_B, "", "(未知)"),
    ("SERIAL-A", "SERIAL-B", MAC_A, MAC_A, "", "(未知)"),
    # A reliable matching SN wins over a changed network interface.
    ("SERIAL-A", "SERIAL-A", MAC_A, MAC_B, "", "customer_A"),
    ("", "", MAC_A.lower().replace(":", "-"), MAC_A, "", "customer_A"),
    ("", "", MAC_A, "", "", "customer_A"),
    ("", "", MAC_A, "unreadable", "", "customer_A"),
    ("", "", MAC_A, "00:00:00:00:00:00", "", "customer_A"),
    ("", "", MAC_A, "ff-ff-ff-ff-ff-ff", "", "customer_A"),
    ("", "", MAC_A, MAC_B, "customer_B", "customer_B"),
])
def test_replacement_identity_controls_owner_in_snapshot_roster_and_customer_totals(
        scan_service, monkeypatch, old_sn, new_sn, old_mac, new_mac, new_worker, expected):
    svc, clock = scan_service
    old = rec(IP, sn=old_sn, mac=old_mac, worker="customer_A", measured_ts=BASE)
    first, _ = run_scan(svc, clock, monkeypatch, [old], BASE)
    changed = rec(IP, sn=new_sn, mac=new_mac, worker=new_worker,
                  measured_ts=BASE + 300)
    _head, stored = run_scan(svc, clock, monkeypatch, [changed], BASE + 300)
    meta, rows = svc.latest()
    assert stored["worker"] == expected
    assert db.known_identity(svc.conn)[IP]["worker"] == expected
    customers = db.customer_hashrates(meta, rows, now=BASE + 300)
    assert customers[0]["worker"] == expected
    assert customers[0]["hashrate_ths"] == changed["hr_rt"]
    assert db.scan_records(svc.conn, first["scan_id"])[0]["worker"] == "customer_A"
    assert changed["worker"] == new_worker  # No mutation of the scanner-owned input.


def test_mac_replacement_without_sn_cannot_be_quarantined_as_old_machine(
        scan_service, monkeypatch):
    """An old SN must not follow a replacement with a different measured MAC."""
    svc, clock = scan_service
    run_scan(svc, clock, monkeypatch,
             [rec(IP, sn="SERIAL-A", mac=MAC_A, worker="customer_A", measured_ts=BASE)], BASE)
    run_scan(svc, clock, monkeypatch,
             [rec(IP, sn="", mac=MAC_B, worker="", measured_ts=BASE + 300)], BASE + 300)
    replacement_identity = db.known_identity(svc.conn)[IP]

    # The old machine later appears elsewhere while the replacement genuinely fails.
    other_ip = "192.0.2.11"
    clock[0] = BASE + 600
    svc.cfg["scan"]["reconfirm_passes"] = 0
    current = [
        rec(IP, status="offline", sn="", mac="", measured_ts=BASE + 600),
        rec(other_ip, sn="SERIAL-A", mac=MAC_A, worker="customer_A", measured_ts=BASE + 600),
    ]
    monkeypatch.setattr(service, "_scan_batch", lambda *a, **kw: [dict(r) for r in current])
    result = svc._do_scan("full", [IP, other_ip])
    stored = {r["ip"]: r for r in db.scan_records(svc.conn, result["scan_id"])}
    assert db.known_identity(svc.conn)[IP]["state"] == "active"
    assert stored[IP]["status"] == "offline"
    assert stored[IP]["mac"] == MAC_B
    assert stored[IP]["worker"] == "(未知)"
    assert replacement_identity["sn"] == ""


@pytest.mark.parametrize("raw", ["00:00:00:00:00:00", " 00-00-00-00-00-00 ",
                                  "FF:FF:FF:FF:FF:FF", " ff-ff-ff-ff-ff-ff "])
def test_placeholder_mac_is_not_a_reliable_identity(raw):
    assert miner_core.normalize_mac(raw) == ""


@pytest.mark.parametrize("old_mac,new_mac,new_sn,expected_sn,expected_mac", [
    (MAC_A, MAC_A.lower().replace(":", "-"), "N/A", "SERIAL-A", MAC_A),
    (MAC_A, "  " + MAC_A.lower() + "  ", "unknown", "SERIAL-A", MAC_A),
    (MAC_A, "", "N/A", "SERIAL-A", MAC_A),
    (MAC_A, None, "", "SERIAL-A", MAC_A),
    (MAC_A, "not-a-mac", "N/A", "SERIAL-A", MAC_A),
    (MAC_A, "00:00:00:00:00:00", "N/A", "SERIAL-A", MAC_A),
    (MAC_A, "ff-ff-ff-ff-ff-ff", "N/A", "SERIAL-A", MAC_A),
    ("not-a-mac", MAC_B, "N/A", "SERIAL-A", MAC_B),
    ("", MAC_B, "N/A", "SERIAL-A", MAC_B),
    (MAC_A, MAC_B, "N/A", "", MAC_B),
    (MAC_A, MAC_B, "no miner sn stored on board", "", MAC_B),
    (MAC_A, MAC_B, "read error", "", MAC_B),
    (MAC_A, MAC_B, "SERIAL-A", "SERIAL-A", MAC_B),
    (MAC_A, MAC_B, "SERIAL-B", "SERIAL-B", MAC_B),
])
def test_roster_sn_invalidation_requires_reliable_changed_mac(
        conn, old_mac, new_mac, new_sn, expected_sn, expected_mac):
    db.upsert_known_miners(conn, [rec(IP, sn="SERIAL-A", mac=old_mac)], BASE)
    db.upsert_known_miners(conn, [rec(IP, sn=new_sn, mac=new_mac)], BASE + 300)
    identity = db.known_identity(conn)[IP]
    assert identity["sn"] == expected_sn
    assert identity["mac"] == expected_mac


@pytest.mark.parametrize("first_mac,second_mac,expected_sn", [
    (MAC_B, MAC_A, ""),
    ("", MAC_A, "SERIAL-B"),
])
def test_repeated_ip_in_one_upsert_compares_against_its_preceding_identity(
        conn, first_mac, second_mac, expected_sn):
    db.upsert_known_miners(conn, [rec(IP, sn="SERIAL-A", mac=MAC_A)], BASE)
    db.upsert_known_miners(conn, [
        rec(IP, sn="SERIAL-B", mac=first_mac),
        rec(IP, sn="N/A", mac=second_mac),
    ], BASE + 300)
    identity = db.known_identity(conn)[IP]
    assert identity["sn"] == expected_sn
    assert identity["mac"] == second_mac


@pytest.mark.parametrize("placeholder", ["00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"])
def test_placeholder_mac_cannot_migrate_a_genuinely_offline_machine(
        scan_service, monkeypatch, placeholder):
    svc, clock = scan_service
    run_scan(svc, clock, monkeypatch,
             [rec(IP, sn="", mac=placeholder, measured_ts=BASE)], BASE)
    other_ip = "192.0.2.11"
    clock[0] = BASE + 300
    svc.cfg["scan"]["reconfirm_passes"] = 0
    current = [
        rec(IP, status="offline", sn="", mac="", measured_ts=BASE + 300),
        rec(other_ip, sn="", mac=placeholder, measured_ts=BASE + 300),
    ]
    monkeypatch.setattr(service, "_scan_batch", lambda *a, **kw: [dict(r) for r in current])
    result = svc._do_scan("full", [IP, other_ip])
    stored = {r["ip"]: r for r in db.scan_records(svc.conn, result["scan_id"])}
    assert db.known_identity(svc.conn)[IP]["state"] == "active"
    assert stored[IP]["status"] == "offline"


@pytest.mark.parametrize("first_hr,confirm_hr,expected_hr,expected_stamp", [
    (None, 0, 0, BASE + 320),
    (0, None, 0, BASE + 300),
])
def test_reconfirmation_keeps_real_zero_and_its_original_sample_timestamp(
        scan_service, monkeypatch, first_hr, confirm_hr, expected_hr, expected_stamp):
    svc, clock = scan_service
    run_scan(svc, clock, monkeypatch,
             [rec(IP, hr=300, power=6000, measured_ts=BASE)], BASE)
    first = rec(IP, hr=first_hr, power=200 if first_hr == 0 else None,
                temp=70 if first_hr == 0 else None, measured_ts=BASE + 300)
    confirmed = rec(IP, hr=confirm_hr, power=200 if confirm_hr == 0 else None,
                    temp=70 if confirm_hr == 0 else None, measured_ts=BASE + 320)
    rounds = iter([[first], [confirmed]])
    calls = []

    def probe(ips, *a, **kw):
        calls.append(tuple(ips))
        return [dict(r) for r in next(rounds)]

    clock[0] = BASE + 320
    monkeypatch.setattr(service, "_scan_batch", probe)
    result = svc._do_scan("quick", [IP])
    stored = db.scan_records(svc.conn, result["scan_id"])[0]
    assert len(calls) == 2
    assert stored["status"] == "online"
    assert stored["hr_rt"] == expected_hr
    assert stored["power"] == 200
    assert stored["temp"] == 70
    assert stored["measured_ts"] == expected_stamp


def test_later_missing_reprobe_does_not_erase_confirmed_zero_or_refresh_its_time(
        scan_service, monkeypatch):
    svc, clock = scan_service
    run_scan(svc, clock, monkeypatch,
             [rec(IP, hr=300, power=6000, measured_ts=BASE)], BASE)
    svc.cfg["scan"]["reconfirm_passes"] = 2
    rounds = iter([
        [rec(IP, hr=None, power=None, measured_ts=BASE + 300)],
        [rec(IP, hr=0, power=200, measured_ts=BASE + 320)],
        [rec(IP, hr=None, power=None, measured_ts=BASE + 340)],
    ])
    clock[0] = BASE + 340
    monkeypatch.setattr(service, "_scan_batch", lambda *a, **kw: next(rounds))
    result = svc._do_scan("quick", [IP])
    stored = db.scan_records(svc.conn, result["scan_id"])[0]
    assert (stored["hr_rt"], stored["power"], stored["measured_ts"]) == (0, 200, BASE + 320)
