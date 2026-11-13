"""Thermal safety must hold at candidate selection and immediately before dispatch."""
import pytest

import alerts
import control
import db
import service
from conftest import rec


BASE = 2_000_000_000
IP = "192.0.2.20"


@pytest.fixture
def thermal(cfg, monkeypatch):
    cfg["db"]["path"] = ":memory:"
    cfg["schedule"]["enabled"] = False
    cfg["control"].update(enabled=True, reboot_enabled=True,
                          zero_reboot_wait_sec=900)
    clock = [BASE]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    monkeypatch.setattr(alerts, "push_text", lambda *args: None)
    svc = service.MonitorService(cfg)
    rows = [rec(IP, hr=0, temp=70, measured_ts=BASE)]
    db.upsert_known_miners(svc.conn, rows, BASE)
    db.start_zero_reboot_states(svc.conn, [IP], BASE - 900)
    svc._publish(1, BASE, "quick", rows)
    sent = []

    def send(ip, action, params, passwords, timeout):
        sent.append(ip)
        return True, "mock command only"

    monkeypatch.setattr(control, "_stock", send)
    yield svc, clock, sent
    svc.stop()
    svc.conn.close()
    db.close_readers()


def sample(svc, clock, temp, at=None):
    if at is not None:
        clock[0] = at
    rows = [rec(IP, hr=0, temp=temp, measured_ts=clock[0])]
    svc._publish(1, clock[0], "quick", rows)
    return rows


@pytest.mark.parametrize("temp", [95, 100, 90, 94, None, 0, 151, float("nan"),
                                  float("inf"), True])
def test_auto_zero_requires_a_valid_temperature_below_the_recovery_threshold(thermal, temp):
    svc, clock, sent = thermal
    rows = sample(svc, clock, temp)
    assert svc.zero_reboot_candidates(rows) == []
    assert IP not in db.zero_reboot_states(svc.conn)
    assert sent == []
    # Unknown/invalid temperatures must remain unchanged, never become a normal value.
    assert rows[0]["temp"] is temp or rows[0]["temp"] == temp


def test_active_overheat_alert_blocks_even_a_cool_reading_until_alert_is_resolved(thermal):
    svc, clock, sent = thermal
    db.raise_alert(svc.conn, IP, "overheat", "crit", "synthetic thermal shutdown")
    assert svc.zero_reboot_candidates(sample(svc, clock, 70)) == []
    assert IP not in db.zero_reboot_states(svc.conn)
    db.resolve_alert(svc.conn, IP, "overheat")
    assert svc.zero_reboot_candidates(sample(svc, clock, 70, BASE + 100)) == []
    assert svc.zero_reboot_candidates(sample(svc, clock, 70, BASE + 999)) == []
    assert svc.zero_reboot_candidates(sample(svc, clock, 70, BASE + 1000)) == [(IP, "stock")]
    assert sent == []


def test_thermal_pause_requires_cooling_and_restarts_an_unattempted_zero_timer(thermal):
    svc, clock, _sent = thermal
    for stamp, temp in ((BASE, 100), (BASE + 300, None), (BASE + 600, 92)):
        assert svc.zero_reboot_candidates(sample(svc, clock, temp, stamp)) == []
        assert IP not in db.zero_reboot_states(svc.conn)
    assert svc.zero_reboot_candidates(sample(svc, clock, 89, BASE + 900)) == []
    assert db.zero_reboot_states(svc.conn)[IP]["started_at"] == BASE + 900
    assert svc.zero_reboot_candidates(sample(svc, clock, 89, BASE + 1799)) == []
    assert svc.zero_reboot_candidates(sample(svc, clock, 89, BASE + 1800)) == [(IP, "stock")]


def test_thermal_pause_keeps_prior_attempts_and_their_audit_timestamp(thermal):
    svc, clock, _sent = thermal
    for _ in range(3):
        db.record_zero_reboot_attempts(svc.conn, [IP], BASE - 900)
    before = db.zero_reboot_states(svc.conn)[IP]
    assert svc.zero_reboot_candidates(sample(svc, clock, 100)) == []
    assert db.zero_reboot_states(svc.conn)[IP] == before
    assert svc.zero_reboot_candidates(sample(svc, clock, None, BASE + 300)) == []
    assert db.zero_reboot_states(svc.conn)[IP] == before
    assert svc.zero_reboot_candidates(sample(svc, clock, 70, BASE + 600)) == [(IP, "stock")]
    db.record_zero_reboot_attempts(svc.conn, [IP], BASE + 600)
    assert svc.zero_reboot_candidates(sample(svc, clock, 70, BASE + 1800)) == []
    assert db.zero_reboot_states(svc.conn)[IP]["attempts"] == 4


@pytest.mark.parametrize("change", ["hot", "missing", "active_alert"])
def test_queued_auto_command_rechecks_temperature_and_never_consumes_budget_when_blocked(
        thermal, change):
    svc, clock, sent = thermal
    targets = svc.zero_reboot_candidates(svc.latest()[1])
    assert targets == [(IP, "stock")]
    if change == "active_alert":
        db.raise_alert(svc.conn, IP, "overheat", "crit", "synthetic thermal shutdown")
    else:
        sample(svc, clock, 100 if change == "hot" else None)
    assert svc._reboot_lock.acquire(blocking=False)
    svc._run_auto_zero_reboot(targets, "system@auto-zero-reboot")
    assert not svc._reboot_lock.locked()
    assert sent == []
    assert IP not in db.zero_reboot_states(svc.conn)
    assert IP not in svc._alert_state.get("rebooting", {})
    commands = db.list_commands(svc.conn)
    assert len(commands) == 1 and not commands[0]["ok"]
    assert "未下发" in commands[0]["msg"]


def test_disabled_overheat_notifications_do_not_disable_automatic_thermal_safety(thermal):
    svc, clock, _sent = thermal
    svc.cfg["alerts"].update(enabled=False, overheat_c=0)
    assert svc.zero_reboot_candidates(sample(svc, clock, 100)) == []
    assert svc.zero_reboot_candidates(sample(svc, clock, 70, BASE + 300)) == []
    assert svc.zero_reboot_candidates(sample(svc, clock, 70, BASE + 1200)) == [(IP, "stock")]


def test_custom_temperature_threshold_is_honored_and_the_recovery_boundary_is_strict(thermal):
    svc, clock, _sent = thermal
    svc.cfg["alerts"].update(overheat_c=85, overheat_clear_c=80)
    assert svc.zero_reboot_candidates(sample(svc, clock, 80)) == []
    assert svc.zero_reboot_candidates(sample(svc, clock, 79.9, BASE + 300)) == []
    assert svc.zero_reboot_candidates(sample(svc, clock, 79.9, BASE + 1200)) == [(IP, "stock")]


def test_thermal_safety_does_not_enable_an_automatic_switch_the_operator_left_off(thermal):
    svc, clock, sent = thermal
    svc.cfg["control"]["reboot_enabled"] = False
    svc._maybe_auto_reboot(sample(svc, clock, 70))
    assert sent == []
    assert svc.cfg["control"]["reboot_enabled"] is False
