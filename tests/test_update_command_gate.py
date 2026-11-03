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
def panel(monkeypatch, conn, cfg):
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
