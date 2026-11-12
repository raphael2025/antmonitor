"""Update/reboot interleavings with real locks and mocked device/network operations."""
import copy
import importlib
import threading
from pathlib import Path

import pytest
from starlette.requests import Request

import control
import service
import updater


@pytest.fixture
def panel(monkeypatch, conn, cfg, tmp_path):
    monkeypatch.setattr(updater, "STATE_FILE", str(tmp_path / "update-state.json"))
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")
    svc = object.__new__(service.MonitorService)
    svc.conn, svc.cfg = conn, copy.deepcopy(cfg)
    svc._reboot_lock, svc._alert_state = threading.Lock(), {}
    svc.cfg["control"].update(enabled=True, reboot_enabled=True, reboot_concurrency=30)
    monkeypatch.setattr(server, "SVC", svc)
    monkeypatch.setattr(server, "CFG", svc.cfg)
    monkeypatch.setattr(server, "CMD_PROGRESS", {})
    monkeypatch.setattr(server, "_latest_records", lambda: (None, []))
    monkeypatch.setattr(server.appconfig, "load_segments", lambda cfg: (["10.0.0"], 1, 254))
    monkeypatch.setattr(server.alerts, "push_text", lambda *a, **kw: None)
    monkeypatch.setattr(updater, "_apply_lock", threading.Lock())
    monkeypatch.setattr(updater, "BUSY_CHECK", server._update_busy)
    monkeypatch.setattr(updater, "_git", lambda *a, **kw: "")
    return server


def reboot(server):
    request = Request({"type": "http", "client": ("127.0.0.1", 1234), "headers": []})
    return server.api_command(request,
                              {"action": "reboot", "ips": ["10.0.0.1"], "confirm": True},
                              {"user": "test-ops", "role": "ops"})


def test_update_that_already_passed_busy_check_blocks_new_reboots(panel, monkeypatch):
    fetching, finish = threading.Event(), threading.Event()
    results, errors = [], []

    def check(*args):
        fetching.set()
        assert finish.wait(5)
        return {"behind": 0}

    def update():
        try:
            results.append(updater.apply(restart=False))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(updater, "check", check)
    monkeypatch.setattr(control, "run_batch", lambda *a, **kw: pytest.fail("miner rebooted during update"))
    thread = threading.Thread(target=update)
    thread.start()
    try:
        assert fetching.wait(5)  # apply already saw an idle monitor and is checking remote updates.
        response = reboot(panel)
        assert response.status_code == 409 and "程序正在更新" in response.body.decode()
        panel.SVC._maybe_auto_reboot_zero([("10.0.0.1", "stock")])
        assert not panel.SVC._reboot_lock.locked()
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive() and not errors and len(results) == 1
    assert not updater.is_applying()


def test_reboot_that_started_first_defers_update_before_fetch(panel, monkeypatch):
    sending, finish = threading.Event(), threading.Event()
    results, errors = [], []

    def send(targets, *args, **kwargs):
        sending.set()
        assert finish.wait(5)
        return ([{"ip": targets[0][0], "ok": True, "msg": "mock sent"}], "")

    def command():
        try:
            results.append(reboot(panel))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(control, "run_batch", send)
    monkeypatch.setattr(updater, "check", lambda *a: pytest.fail("update started during reboot"))
    thread = threading.Thread(target=command)
    thread.start()
    try:
        assert sending.wait(5)
        response = updater.apply(restart=False)
        assert response["ok"] is False and "本次更新推迟" in response["msg"]
        assert not updater.is_applying()
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert results[0]["success"] == 1
    assert not panel.SVC._reboot_lock.locked()


def test_pending_process_restart_keeps_new_reboots_blocked(panel, monkeypatch):
    # Avoid a real restart timer; apply must retain its lock in the response-to-exit window.
    monkeypatch.setattr(updater, "_apply", lambda *a: {"ok": True, "restarting": True})
    monkeypatch.setattr(control, "run_batch", lambda *a, **kw: pytest.fail("unexpected device command"))
    assert updater.apply()["restarting"]
    assert updater.is_applying()
    assert reboot(panel).status_code == 409
    panel.SVC._maybe_auto_reboot_zero([("10.0.0.1", "stock")])
    assert not panel.SVC._reboot_lock.locked()
    updater._restart_file_lock.close()  # simulated exit releases the OS lock too


def command(server, action="set_pools", ips=None):
    server.CFG["control"]["pool_allowlist"] = ["pool.example.invalid"]
    request = Request({"type": "http", "client": ("127.0.0.1", 1234), "headers": []})
    body = {"action": action, "ips": ips or ["10.0.0.1"], "confirm": True}
    if action == "set_pools":
        body["params"] = {"pools": [{"url": "stratum+tcp://pool.example.invalid:3333",
                                      "user": "test.worker", "pass": "x"}]}
    return server.api_command(request, body, {"user": "test-ops", "role": "ops"})


@pytest.mark.parametrize("action", ["set_pools", "locate"])
def test_pending_update_blocks_every_device_command(panel, monkeypatch, action):
    panel.updater._apply_lock.acquire()
    monkeypatch.setattr(control, "run_batch", lambda *a, **kw: pytest.fail("device command during update"))
    try:
        response = command(panel, action)
        assert response.status_code == 409
        assert "程序正在更新" in response.body.decode()
        assert not panel.SVC._reboot_lock.locked()
    finally:
        panel.updater._apply_lock.release()


@pytest.mark.parametrize("action", ["set_pools", "locate"])
def test_running_device_command_blocks_update_and_other_commands(panel, monkeypatch, action):
    sending, finish = threading.Event(), threading.Event()
    responses, errors = [], []

    def send(targets, *args, **kwargs):
        sending.set()
        assert finish.wait(5)
        rows = [{"ip": targets[0][0], "ok": True, "msg": "mock sent"}]
        if kwargs.get("after_group"):
            kwargs["after_group"](targets, rows)
        return rows, ""

    def run():
        try:
            responses.append(command(panel, action))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(control, "run_batch", send)
    monkeypatch.setattr(updater, "check", lambda *a: pytest.fail("update began during device command"))
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert sending.wait(5)
        assert updater.apply(restart=False)["ok"] is False
        assert command(panel, "set_pools").status_code == 409
        assert reboot(panel).status_code == 409
        assert panel.SVC._reboot_lock.locked()
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert responses[0]["success"] == 1
    assert not panel.SVC._reboot_lock.locked()


def test_pool_batches_are_audited_before_next_batch(panel, monkeypatch):
    import db
    sent = []

    def group(targets, action, params, cfg):
        if sent:
            assert len(db.list_commands(panel.SVC.conn, 1000)) == len(sent)
        assert panel.SVC._reboot_lock.locked()
        assert len(targets) <= 64
        sent.extend(ip for ip, _fw in targets)
        return [{"ip": ip, "ok": True, "msg": "pool updated"} for ip, _fw in targets]

    monkeypatch.setattr(control, "_run_group", group)
    response = command(panel, ips=["10.0.0.%d" % n for n in range(1, 131)])
    assert response["success"] == 130 and len(sent) == 130
    rows = db.list_commands(panel.SVC.conn, 1000)
    assert len(rows) == 130
    assert all(r["action"] == "set_pools" and "test.worker" in r["msg"] for r in rows)
    assert not panel.SVC._alert_state  # pool changes do not start reboot grace periods


def test_pool_audit_failure_stops_remaining_batches_and_defers_update(panel, monkeypatch):
    import db
    sent = []
    real_audit = db.log_commands

    def group(targets, action, params, cfg):
        sent.extend(ip for ip, _fw in targets)
        return [{"ip": ip, "ok": True, "msg": "pool updated"} for ip, _fw in targets]

    def fail(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(control, "_run_group", group)
    monkeypatch.setattr(db, "log_commands", fail)
    response = command(panel, ips=["10.0.0.%d" % n for n in range(1, 70)])
    import json
    result = json.loads(response.body)
    assert response.status_code == 503 and result["success"] == 64
    assert len(sent) == 64 and len(result["results"]) == 64
    assert not panel.SVC._reboot_lock.locked()
    assert updater.apply(restart=False)["ok"] is False
    # Automatic reboots use the same generic backlog gate before quota checks or threads.
    monkeypatch.setattr(control, "filter_reboot_targets",
                        lambda *a, **kw: pytest.fail("auto reboot passed unresolved pool audit"))
    with pytest.raises(OSError, match="disk full"):
        panel.SVC._maybe_auto_reboot_zero([("10.0.0.200", "stock")])
    assert not panel.SVC._reboot_lock.locked()
    assert len(sent) == 64
    monkeypatch.setattr(db, "log_commands", real_audit)
    # Recover the original audit, then allow the update's normal freshness check.
    monkeypatch.setattr(updater, "check", lambda *a: {"behind": 0})
    updater.apply(restart=False)
    assert len(db.list_commands(panel.SVC.conn, 1000)) == 64
