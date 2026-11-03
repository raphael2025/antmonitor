# -*- coding: utf-8 -*-
"""运维命令的真实并发、审计和中断结果；不连接任何矿机。"""
import copy
import importlib
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from starlette.requests import Request

import control
import db
import service


@pytest.fixture
def commands(monkeypatch, conn, cfg):
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")
    svc = object.__new__(service.MonitorService)
    svc.conn, svc.cfg = conn, copy.deepcopy(cfg)
    svc._reboot_lock = threading.Lock()
    svc._alert_state = {}
    svc.cfg["control"].update(enabled=True, max_batch=1000, reboot_concurrency=30,
                              reboot_min_interval_sec=900, reboot_max_per_day=1)
    monkeypatch.setattr(server, "SVC", svc)
    monkeypatch.setattr(server, "CFG", svc.cfg)
    monkeypatch.setattr(server, "CMD_PROGRESS", {})
    monkeypatch.setattr(server, "_latest_records", lambda: (None, []))
    monkeypatch.setattr(server.appconfig, "load_segments", lambda cfg: (["10.0.0"], 1, 254))
    monkeypatch.setattr(server.alerts, "push_text", lambda *args: None)
    return server


def _reboot(server, ips):
    request = Request({"type": "http", "client": ("127.0.0.1", 1234), "headers": []})
    return server.api_command(request, {"ips": ips, "action": "reboot", "confirm": True},
                              {"user": "test-ops", "role": "ops"})


def test_concurrent_requests_cannot_reuse_old_reboot_quota(commands, monkeypatch):
    server = commands
    checked, proceed = threading.Event(), threading.Event()
    real_filter = control.filter_reboot_targets
    sent, outcomes, errors = [], [], []

    def pause_first_check(*args):
        result = real_filter(*args)
        if threading.current_thread().name == "first-reboot":
            checked.set()
            assert proceed.wait(5)
        return result

    def run(targets, *args, **kwargs):
        sent.extend(ip for ip, _ in targets)
        return ([{"ip": ip, "ok": True, "msg": "mock sent"} for ip, _ in targets], "")

    monkeypatch.setattr(control, "filter_reboot_targets", pause_first_check)
    monkeypatch.setattr(control, "run_batch", run)

    def first():
        try:
            outcomes.append(_reboot(server, ["10.0.0.1"]))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=first, name="first-reboot")
    thread.start()
    try:
        assert checked.wait(5)
        concurrent = _reboot(server, ["10.0.0.1"])
        assert getattr(concurrent, "status_code", None) == 409
    finally:
        proceed.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert outcomes[0]["success"] == 1
    # 稍后再点也必须读到刚才的成功审计，不能把旧检查结果带进锁内。
    retry = _reboot(server, ["10.0.0.1"])
    assert retry["success"] == 0 and retry["skipped"] == 1
    assert sent == ["10.0.0.1"]
    assert db.reboot_stats(server.SVC.conn, sent)["10.0.0.1"]["count"] == 1


def test_sync_success_is_audited_before_releasing_shared_lock(commands, monkeypatch):
    server = commands
    audit = server._audit_command
    seen = []

    def checked_audit(*args):
        seen.append(server.SVC._reboot_lock.locked())
        return audit(*args)

    monkeypatch.setattr(server, "_audit_command", checked_audit)
    monkeypatch.setattr(control, "run_batch", lambda targets, *a, **kw:
                        ([{"ip": targets[0][0], "ok": True, "msg": "mock sent"}], ""))
    assert _reboot(server, ["10.0.0.2"])["success"] == 1
    assert seen == [True] and not server.SVC._reboot_lock.locked()


def test_audit_failure_blocks_resend_and_keeps_original_execution_time(commands, monkeypatch):
    server = commands
    original_ts = 2_000_000
    clock = [original_ts]
    monkeypatch.setattr(control.time, "time", lambda: clock[0])
    real_audit = db.log_commands
    sent = []

    def send(targets, *args, **kwargs):
        sent.extend(ip for ip, _ in targets)
        return ([{"ip": ip, "ok": True, "msg": "mock sent"} for ip, _ in targets], "")

    def unavailable(*args, **kwargs):
        raise OSError("test database unavailable")

    monkeypatch.setattr(control, "run_batch", send)
    monkeypatch.setattr(db, "log_commands", unavailable)
    result = _reboot(server, ["10.0.0.8"])
    assert result.status_code == 503
    assert "已确认下发 1 台" in result.body.decode()
    assert sent == ["10.0.0.8"] and not server.SVC._reboot_lock.locked()
    clock[0] += 300
    assert _reboot(server, ["10.0.0.8"]).status_code == 503
    assert sent == ["10.0.0.8"]
    # 恢复后补记原始下发时刻，不能把“补记时间”伪装成另一次重启。
    clock[0] += 300
    monkeypatch.setattr(db, "log_commands", real_audit)
    retry = _reboot(server, ["10.0.0.8"])
    assert retry["success"] == 0 and retry["skipped"] == 1
    rows = [r for r in db.list_commands(server.SVC.conn) if r["ok"]]
    assert len(rows) == 1 and rows[0]["ts"] == original_ts
    assert not server.SVC._pending_reboot_audit


def test_probe_exception_keeps_sibling_success_and_reboot_limit(commands, monkeypatch):
    server = commands
    monkeypatch.setattr(server, "_latest_records", lambda: (None, [
        {"ip": "10.0.0.1", "firmware": "stock"},
        {"ip": "10.0.0.2", "firmware": ""},
    ]))
    sent = []

    def stock(ip, *args):
        sent.append(ip)
        return True, "mock sent"

    def broken_probe(*args):
        raise ValueError("test malformed probe result")

    monkeypatch.setattr(control, "_stock", stock)
    monkeypatch.setattr(control.miner_core, "probe", broken_probe)
    result = _reboot(server, ["10.0.0.1", "10.0.0.2"])
    assert result["success"] == 1 and result["failed"] == 1
    failed = next(row for row in result["results"] if row["ip"] == "10.0.0.2")
    assert failed["skipped"] and "未下发" in failed["msg"]
    rows = {row["ip"]: row for row in db.list_commands(server.SVC.conn)}
    assert rows["10.0.0.1"]["ok"] == 1 and rows["10.0.0.2"]["ok"] == 0
    retry = _reboot(server, ["10.0.0.1"])
    assert retry["success"] == 0 and retry["skipped"] == 1
    assert sent == ["10.0.0.1"]


def test_unexpected_worker_exception_does_not_discard_successful_sibling(commands, monkeypatch):
    server = commands

    def worker(ip, *args):
        if ip.endswith(".2"):
            raise RuntimeError("test unexpected worker failure")
        return {"ip": ip, "ok": True, "msg": "mock sent"}

    monkeypatch.setattr(control, "run_one", worker)
    result = _reboot(server, ["10.0.0.1", "10.0.0.2"])
    assert result["success"] == 1 and result["failed"] == 1
    failure = next(row for row in result["results"] if not row["ok"])
    assert failure["uncertain"] and "未确认" in failure["msg"]
    assert db.reboot_stats(server.SVC.conn, ["10.0.0.1"])["10.0.0.1"]["count"] == 1


@pytest.mark.parametrize("queued_before_error", [False, True])
def test_submit_interruption_preserves_all_already_queued_results(commands, monkeypatch,
                                                                 queued_before_error):
    server = commands
    real_executor = control.ThreadPoolExecutor
    release = threading.Event()
    sent = []

    class InterruptedSubmit(real_executor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.submitted = 0

        def submit(self, function, *args, **kwargs):
            self.submitted += 1
            if self.submitted == 2:
                # CPython submit queues work before attempting to start another worker.
                # Its exception therefore does not prove the target was never invoked.
                if queued_before_error:
                    super().submit(function, *args, **kwargs)
                release.set()
                raise RuntimeError("test thread creation failure")
            return super().submit(function, *args, **kwargs)

    def worker(ip, *args):
        assert release.wait(5)
        sent.append(ip)
        return {"ip": ip, "ok": True, "msg": "mock sent"}

    monkeypatch.setattr(control, "ThreadPoolExecutor", InterruptedSubmit)
    monkeypatch.setattr(control, "run_one", worker)
    result = _reboot(server, ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
    expected = {"10.0.0.1", "10.0.0.2"} if queued_before_error else {"10.0.0.1"}
    assert set(sent) == expected
    assert result["success"] == len(expected) and result["failed"] == 3 - len(expected)
    for row in result["results"]:
        if row["ip"] in expected:
            assert row["ok"]
        else:
            assert not row["ok"] and row["skipped"] and "未下发" in row["msg"]
    audited = {row["ip"] for row in db.list_commands(server.SVC.conn) if row["ok"]}
    assert audited == expected
    retry = _reboot(server, ["10.0.0.1"])
    assert retry["success"] == 0 and retry["skipped"] == 1
    assert set(sent) == expected


def test_async_interruption_keeps_completed_results_and_total(commands, monkeypatch):
    server = commands
    server.CMD_PROGRESS.update(running=True, user="test-ops", error="")

    def interrupted(targets, action, params, cfg, before_group=None, after_group=None):
        first = targets[:1]
        before_group([first[0][0]])
        after_group(first, [{"ip": first[0][0], "ok": True, "msg": "mock sent"}])
        raise RuntimeError("worker stopped before second group")

    monkeypatch.setattr(control, "run_batch", interrupted)
    assert server.SVC._reboot_lock.acquire(blocking=False)
    server._run_command_bg([("10.0.0.3", "stock"), ("10.0.0.4", "stock")],
                           "reboot", {}, "test-ops",
                           skipped=[{"ip": "10.0.0.5", "ok": False, "msg": "limited"}])
    progress = server.api_command_progress({})
    assert not progress["running"] and progress["error"]
    assert progress["done"] == progress["total"] == 3
    assert progress["success"] == 1 and progress["failed"] == 2
    assert set(progress["fail_ips"]) == {"10.0.0.4", "10.0.0.5"}
    assert "10.0.0.3" in server.SVC._alert_state["rebooting"]
    assert "10.0.0.4" not in server.SVC._alert_state["rebooting"]
    assert not server.SVC._reboot_lock.locked()
    rows = db.list_commands(server.SVC.conn)
    assert len(rows) == 3
    assert next(r for r in rows if r["ip"] == "10.0.0.3")["ok"] == 1
    assert "未下发" in next(r for r in rows if r["ip"] == "10.0.0.4")["msg"]


def test_async_thread_start_failure_releases_lock_without_silencing_miners(commands, monkeypatch):
    server = commands
    server.CFG["control"]["reboot_concurrency"] = 1

    def fail_start(self):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    result = _reboot(server, ["10.0.0.6", "10.0.0.7"])
    assert result.status_code == 503
    assert not server.SVC._reboot_lock.locked()
    assert not server.CMD_PROGRESS["running"] and server.CMD_PROGRESS["failed"] == 2
    assert not server.SVC._alert_state.get("rebooting")


def test_filter_group_skips_are_not_reboot_attempts(cfg, monkeypatch):
    cfg["control"].update(reboot_concurrency=1, reboot_delay_sec=0, reboot_shuffle=False)
    sent, marked, attempted, progress = [], [], [], []

    def send(ip, *args):
        sent.append(ip)
        return {"ip": ip, "ok": True, "msg": "mock sent"}

    def guard(group):
        if group[0][0].endswith(".2"):
            return [], [{"ip": group[0][0], "ok": False, "msg": "maintenance", "skipped": True}]
        return group, []

    monkeypatch.setattr(control, "run_one", send)
    results, error = control.run_batch(
        [("10.0.0.1", "stock"), ("10.0.0.2", "stock")], "reboot", {}, cfg,
        before_group=lambda ips: marked.extend(ips),
        after_group=lambda group, results: attempted.extend(ip for ip, _ in group),
        progress=lambda done, total: progress.append((done, total)), filter_group=guard)
    assert not error and len(results) == 2
    assert sent == marked == attempted == ["10.0.0.1"]
    assert results[1]["skipped"] and progress[-1] == (2, 2)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for command UI regression")
def test_browser_shows_interrupted_batch_as_error_even_when_sent_commands_succeeded():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('web/app.js', 'utf8');
const source = app.slice(app.indexOf('let _cmdPollTimer ='), app.indexOf('// toast 用 textContent'));
const seen = [];
const ctx = {
  _wsStop: false, document: {title: ''},
  jget: async () => ({running: false, action: 'reboot', total: 1, success: 1,
                     failed: 0, fail_ips: [], error: 'Audit unavailable; inspect status'}),
  toast: (msg, kind) => seen.push({msg, kind}), refreshMiners: () => {},
  t: (key, args) => key + JSON.stringify(args || {}),
  clearTimeout: () => {}, setTimeout: () => {throw Error('Finished batch must not poll again')},
};
vm.createContext(ctx);
vm.runInContext(source, ctx);
(async () => {
  await ctx.pollCmdProgress();
  assert.equal(seen.length, 1);
  assert.equal(seen[0].kind, 'fail');
  assert.match(seen[0].msg, /Audit unavailable/);
  assert.doesNotMatch(seen[0].msg, /cmd\.batch_done/);
})().catch(e => {console.error(e); process.exitCode = 1;});
"""
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
