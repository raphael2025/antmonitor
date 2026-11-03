"""Queued automatic actions must obey the current operator and measurement state."""
from concurrent.futures import Future

import pytest

import alerts
import control
import db
import service
from conftest import rec


BASE = 2_000_000_000
IPS = ("192.0.2.1", "192.0.2.2")


@pytest.fixture
def automatic(cfg, monkeypatch):
    cfg["db"]["path"] = ":memory:"
    cfg["schedule"]["enabled"] = False
    cfg["control"].update(enabled=True, reboot_enabled=True, reboot_concurrency=1,
                          reboot_delay_sec=8, reboot_shuffle=False)
    clock = [BASE]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    monkeypatch.setattr(alerts, "push_text", lambda *args: None)
    svc = service.MonitorService(cfg)
    rows = [rec(ip, hr=0, measured_ts=BASE) for ip in IPS]
    db.upsert_known_miners(svc.conn, rows, BASE)
    db.start_zero_reboot_states(svc.conn, IPS, BASE - 900)
    svc._publish(1, BASE, "quick", rows)
    sent = []

    def send(ip, action, params, passwords, timeout):
        sent.append(ip)
        return True, "mock command only"

    # Exercise the real dispatcher and audit path, with no device/network access.
    monkeypatch.setattr(control, "_stock", send)
    yield svc, clock, sent
    svc.stop()
    svc.conn.close()
    db.close_readers()


def execute(svc):
    targets = svc.zero_reboot_candidates(svc.latest()[1])
    assert [ip for ip, _fw in targets] == list(IPS)
    assert svc._reboot_lock.acquire(blocking=False)
    svc._run_auto_zero_reboot(targets, "system@auto-zero-reboot")
    assert not svc._reboot_lock.locked()


@pytest.mark.parametrize("change", [
    "switch_off", "repair", "removed", "recovered", "unknown", "stale",
    "warming", "mass_paused", "quota_changed",
])
def test_later_automatic_group_rechecks_current_state(automatic, monkeypatch, change):
    svc, clock, sent = automatic

    def between_groups(_seconds):
        assert sent == [IPS[0]]
        rows = [dict(r) for r in svc.latest()[1]]
        if change == "switch_off":
            svc.cfg["control"]["reboot_enabled"] = False
        elif change == "repair":
            db.set_machine_state(svc.conn, [IPS[1]], "repair")
        elif change == "removed":
            db.remove_miners(svc.conn, [IPS[1]])
        elif change == "recovered":
            rows[1]["hr_rt"] = 300
        elif change == "unknown":
            rows[1]["status"] = "unknown"
        elif change == "stale":
            clock[0] += db.MAX_SAMPLE_GAP + 1
        elif change == "warming":
            rows[1]["uptime"] = 30
        elif change == "mass_paused":
            svc._alert_state["zero_mass_segs"] = {"192.0.2"}
        elif change == "quota_changed":
            # A new shared limit can invalidate work that was already queued.
            svc.cfg["control"]["reboot_max_per_day"] = 1
            db.log_commands(svc.conn, "ops@test", "reboot", [
                {"ip": IPS[1], "ok": True, "msg": "earlier mock reboot"}])
        svc._publish(2, clock[0], "quick", rows)

    monkeypatch.setattr(control.time, "sleep", between_groups)
    execute(svc)
    assert sent == [IPS[0]]
    states = db.zero_reboot_states(svc.conn)
    assert states[IPS[0]]["attempts"] == 1
    assert states[IPS[1]]["attempts"] == 0
    assert IPS[1] not in svc._alert_state.get("rebooting", {})
    skipped = [r for r in db.list_commands(svc.conn) if r["ip"] == IPS[1] and not r["ok"]]
    assert len(skipped) == 1


def test_eligible_automatic_groups_still_run_and_are_audited(automatic, monkeypatch):
    svc, _clock, sent = automatic
    monkeypatch.setattr(control.time, "sleep", lambda _seconds: None)
    execute(svc)
    assert sent == list(IPS)
    assert all(s["attempts"] == 1 for s in db.zero_reboot_states(svc.conn).values())
    assert len(db.list_commands(svc.conn)) == 2


def test_automatic_limit_is_read_only_after_acquiring_shared_lock(automatic, monkeypatch):
    svc, _clock, sent = automatic
    checked = []

    def limit(conn, targets, cfg):
        checked.append(svc._reboot_lock.locked())
        return [], [{"ip": ip, "ok": False, "msg": "mock rate limit"} for ip, _ in targets]

    monkeypatch.setattr(control, "filter_reboot_targets", limit)
    svc._maybe_auto_reboot_zero([(ip, "stock") for ip in IPS])
    assert checked == [True]
    assert sent == []
    assert not svc._reboot_lock.locked()


def test_busy_automatic_request_does_not_read_an_unlocked_quota(automatic, monkeypatch):
    svc, _clock, sent = automatic
    monkeypatch.setattr(control, "filter_reboot_targets",
                        lambda *args: pytest.fail("busy request must not check quota"))
    assert svc._reboot_lock.acquire(blocking=False)
    try:
        svc._maybe_auto_reboot_zero([(ip, "stock") for ip in IPS])
        assert svc._reboot_lock.locked()
        assert sent == []
    finally:
        svc._reboot_lock.release()


def test_automatic_thread_start_failure_releases_shared_lock(automatic, monkeypatch):
    svc, _clock, sent = automatic

    def fail_start(_thread):
        raise RuntimeError("mock thread start failure")

    monkeypatch.setattr(service.threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="mock thread start failure"):
        svc._maybe_auto_reboot_zero([(ip, "stock") for ip in IPS])
    assert not svc._reboot_lock.locked()
    assert sent == []
    assert all(s["attempts"] == 0 for s in db.zero_reboot_states(svc.conn).values())


def test_automatic_audit_failure_blocks_new_commands_until_original_result_is_saved(
        automatic, monkeypatch):
    svc, clock, sent = automatic
    original_log = db.log_commands

    def fail_log(*args, **kwargs):
        raise RuntimeError("mock audit write failure")

    monkeypatch.setattr(db, "log_commands", fail_log)
    execute(svc)
    assert sent == [IPS[0]]  # Abort the batch after a real command cannot be audited.
    assert db.zero_reboot_states(svc.conn)[IPS[0]]["attempts"] == 1
    with pytest.raises(RuntimeError, match="mock audit write failure"):
        svc._maybe_auto_reboot_zero([(IPS[1], "stock")])
    assert sent == [IPS[0]]
    assert not svc._reboot_lock.locked()

    monkeypatch.setattr(db, "log_commands", original_log)
    clock[0] += 901
    svc.cfg["control"]["reboot_max_per_day"] = 1
    # The next entry repairs audit first, then sees the previous success in the quota.
    svc._maybe_auto_reboot_zero([(IPS[0], "stock")])
    successful = [r for r in db.list_commands(svc.conn) if r["ok"]]
    assert len(successful) == 1 and successful[0]["ts"] == BASE
    assert sent == [IPS[0]]
    assert not svc._reboot_lock.locked()


def test_automatic_attempt_counter_failure_keeps_real_command_audit(automatic, monkeypatch):
    svc, _clock, sent = automatic

    def fail_counter(*args, **kwargs):
        raise RuntimeError("mock attempt counter write failure")

    monkeypatch.setattr(db, "record_zero_reboot_attempts", fail_counter)
    execute(svc)
    assert sent == [IPS[0]]
    commands = db.list_commands(svc.conn)
    assert len(commands) == 1 and commands[0]["ip"] == IPS[0] and commands[0]["ok"]


def test_unsubmitted_automatic_target_keeps_budget_and_audit(automatic, monkeypatch):
    svc, _clock, sent = automatic
    svc.cfg["control"]["reboot_concurrency"] = 2

    class InterruptedExecutor:
        def __init__(self, **kwargs):
            self.submitted = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, *args):
            self.submitted += 1
            if self.submitted == 2:
                raise RuntimeError("mock worker startup failure")
            result = Future()
            result.set_result(fn(*args))
            return result

    monkeypatch.setattr(control, "ThreadPoolExecutor", InterruptedExecutor)
    execute(svc)
    assert sent == [IPS[0]]
    states = db.zero_reboot_states(svc.conn)
    assert states[IPS[0]]["attempts"] == 1
    assert states[IPS[1]]["attempts"] == 0
    assert states[IPS[1]]["last_attempt_at"] is None
    assert IPS[1] not in svc._alert_state.get("rebooting", {})
    commands = {r["ip"]: r for r in db.list_commands(svc.conn)}
    assert set(commands) == set(IPS)
    assert commands[IPS[0]]["ok"] and not commands[IPS[1]]["ok"]
    assert "未下发" in commands[IPS[1]]["msg"]
