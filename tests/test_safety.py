# -*- coding: utf-8 -*-
import sqlite3

import pytest

import alerts
import appconfig
import backup_db
import control
import updater


def test_command_targets_are_ipv4_and_deduplicated():
    ips, error = control.normalize_ips(
        ["10.0.0.1", " 10.0.0.1 ", "10.0.0.2"], max_batch=2)
    assert error == ""
    assert ips == ["10.0.0.1", "10.0.0.2"]

    for bad in (["not-an-ip"], ["2001:db8::1"], [123], "10.0.0.1"):
        ips, error = control.normalize_ips(bad, max_batch=10)
        assert ips is None
        assert error


def test_backup_rejects_zero_retention_before_creating_files(tmp_path):
    src = tmp_path / "source.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.close()
    out = tmp_path / "backups"

    assert backup_db.backup(str(src), str(out), 0, "20260801_000000") == 2
    assert not out.exists()


def test_telegram_http_error_is_not_silently_accepted(monkeypatch):
    class Response:
        def raise_for_status(self):
            raise RuntimeError("telegram rejected request")

    monkeypatch.setattr(alerts.requests, "post", lambda *a, **kw: Response())
    with pytest.raises(RuntimeError, match="telegram rejected"):
        alerts._tg_send({"bot_token": "x", "chat_id": "y"}, "hello")


def test_updater_refuses_dirty_worktree_before_fetch_or_merge(monkeypatch):
    calls = []

    def fake_git(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("status", "--porcelain"):
            return " M server.py"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(updater, "_git", fake_git)
    result = updater.apply(restart=False)
    assert result["ok"] is False
    assert "未提交改动" in result["msg"]
    assert calls == [("status", "--porcelain", "--untracked-files=normal")]


def test_updater_restart_mode_picks_guardian_or_self(monkeypatch):
    """有守护(run.bat/systemd)→退出码42交给守护；没有守护→自己拉起，否则点了更新监控就不回来了。"""
    monkeypatch.delenv("MINER_GUARDIAN", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    if updater.os.name != "nt":
        assert updater.restart_mode() == "self"
        monkeypatch.setenv("INVOCATION_ID", "abc")
        assert updater.restart_mode() == "guardian"
        monkeypatch.delenv("INVOCATION_ID")
    monkeypatch.setenv("MINER_GUARDIAN", "1")
    assert updater.restart_mode() == "guardian"


def test_updater_refuses_detached_head_and_branch_mismatch(monkeypatch):
    def fake_git(head):
        def g(*args, **kw):
            if args[0] == "rev-parse" and args[1] == "--git-dir":
                return ".git"
            if args[:3] == ("rev-parse", "--abbrev-ref", "HEAD"):
                return head
            raise AssertionError(f"不该走到 fetch/merge: {args}")
        return g

    monkeypatch.setattr(updater, "_git", fake_git("HEAD"))
    assert "detached" in updater.check()["error"]
    monkeypatch.setattr(updater, "_git", fake_git("main"))
    assert "release" in updater.check("release")["error"]


def test_updater_git_never_prompts(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    updater._git("status")
    assert seen["stdin"] is updater.subprocess.DEVNULL
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_updater_guardian_env_zero_forces_self(monkeypatch):
    monkeypatch.setenv("MINER_GUARDIAN", "0")
    assert updater.restart_mode() == "self"


def test_empty_or_placeholder_password_can_never_log_in():
    """照抄 config.example.yaml 忘了改密码 / password 留空：以前空口令或公开的占位符
    就能以 admin 登录，进而重启全场。这两种绝不是真口令，必须拒绝。"""
    import auth
    cfg = {"auth": {"enabled": True, "users": [
        {"username": "admin", "password": "改成 python auth.py 生成的哈希", "role": "admin"},
        {"username": "ops", "password": "", "role": "ops"},
        {"username": "viewer", "role": "viewer"},
        {"username": "legacy", "password": "Plain-But-Real-1", "role": "viewer"},
    ]}}
    assert auth.login(cfg, "admin", "改成 python auth.py 生成的哈希") is None
    assert auth.login(cfg, "ops", "") is None
    assert auth.login(cfg, "viewer", "") is None
    assert auth.login(cfg, "legacy", "Plain-But-Real-1")      # 已在用的明文口令不锁死现场
    assert auth.login(cfg, ["admin"], "x") is None            # 非字符串用户名不能冒泡成 500


def test_saved_zero_pps_from_old_version_is_ignored_not_clamped_to_one():
    import appconfig
    cfg = appconfig._merge(appconfig.DEFAULTS, {"scan": {"max_pps": 120}})
    appconfig.apply_settings(cfg, {"max_pps": 0, "discovery_workers": 0})
    assert cfg["scan"]["max_pps"] == 120
    assert cfg["scan"]["discovery_workers"] == appconfig.DEFAULTS["scan"]["discovery_workers"]


def test_backup_finds_db_when_started_from_another_directory(tmp_path, monkeypatch):
    """计划任务没设"起始于"时 CWD 是 System32：以前按 CWD 找 config.yaml/库文件，
    找不到就退出码 2，计划任务没人看输出 → 长期没有备份也没人知道。"""
    base = tmp_path / "app"
    base.mkdir()
    (base / "config.yaml").write_text('db:\n  path: "data.db"\n', encoding="utf-8")
    c = sqlite3.connect(base / "data.db")
    c.execute("CREATE TABLE t (id INTEGER)")
    c.commit()
    c.close()
    monkeypatch.setattr(backup_db, "BASE", str(base))
    monkeypatch.delenv("MINER_CONFIG", raising=False)
    elsewhere = tmp_path / "System32"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    src = backup_db._db_path()
    assert src == str(base / "data.db")
    out = backup_db._anchor("backups")
    assert backup_db.backup(src, out, 3, "20260101_000000") == 0
    assert (base / "backups" / "miner_monitor_20260101_000000.db").exists()


def test_backup_that_fails_integrity_check_is_deleted(tmp_path, monkeypatch):
    """校验失败的备份要删掉：留着会占一个保留名额，把一份好的旧备份挤出去。"""
    src = tmp_path / "s.db"
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE t (id INTEGER)")
    c.commit()
    c.close()
    real = sqlite3.connect

    class Bad:
        def __init__(self, conn):
            self.c = conn

        def execute(self, sql):
            if "integrity_check" in sql:
                return type("R", (), {"fetchone": lambda s: ("*** corrupt ***",)})()
            return self.c.execute(sql)

        def close(self):
            self.c.close()

    seen = []

    def fake_connect(p):   # 同一备份文件第 2 次连接 = 完整性校验那次
        if str(p).endswith("_bad.db"):
            seen.append(p)
            if len(seen) == 2:
                return Bad(real(p))
        return real(p)

    monkeypatch.setattr(backup_db.sqlite3, "connect", fake_connect)
    out = tmp_path / "b"
    assert backup_db.backup(str(src), str(out), 3, "x_bad") == 3
    assert not (out / "miner_monitor_x_bad.db").exists()


def test_log_rotation_blocked_by_locked_file_does_not_lose_records(tmp_path, monkeypatch):
    """Windows 上别的进程(db.py vacuum、记事本…)开着 miner.log 时轮转的 rename 会失败：
    以前之后每条日志都再试一次轮转、再失败，记录直接丢，控制台刷一堆 traceback。"""
    import logging
    import os
    import logs
    path = tmp_path / "m.log"
    h = logs._SafeRotatingFileHandler(str(path), maxBytes=200, backupCount=2,
                                      encoding="utf-8", delay=True)
    lg = logging.getLogger("rot-test")
    lg.propagate = False
    lg.addHandler(h)
    errors = []
    monkeypatch.setattr(h, "handleError", lambda rec: errors.append(rec))

    def locked(src, dst):
        raise PermissionError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(os, "rename", locked)
    try:
        for i in range(30):
            lg.warning("record-%02d %s", i, "x" * 40)
    finally:
        lg.removeHandler(h)
        h.close()
    text = path.read_text(encoding="utf-8")
    assert all(f"record-{i:02d}" in text for i in range(30)) and not errors


def test_telegram_push_retries_network_errors_and_respects_429(monkeypatch):
    """断网/限流时补推：告警已落库为活跃，之后不会再推第二次，丢了就永远丢了。"""
    import requests

    class Resp:
        status_code = 429

        def json(self):
            return {"parameters": {"retry_after": 7}}

    seq = [requests.ConnectionError("断网"),
           requests.HTTPError("限流", response=Resp()), None]
    sent, slept = [], []

    def send(cfg, text):
        e = seq.pop(0)
        if e:
            raise e
        sent.append(text)

    monkeypatch.setattr(alerts, "_tg_send", send)
    monkeypatch.setattr(alerts, "_sleep", slept.append)
    alerts._send_with_retry({}, "掉线")
    assert sent == ["掉线"] and slept == [5, 7]


def test_telegram_long_message_split_and_bad_request_not_retried(monkeypatch):
    import requests

    class Resp:
        status_code = 400

    sent, slept = [], []

    def send(cfg, text):
        sent.append(text)
        if text.startswith("坏"):
            raise requests.HTTPError("bad", response=Resp())

    monkeypatch.setattr(alerts, "_tg_send", send)
    monkeypatch.setattr(alerts, "_sleep", slept.append)
    alerts._send_with_retry({}, "\n".join(["x" * 1000] * 9))
    assert len(sent) == 3 and all(len(t) <= alerts._TG_MAX_LEN for t in sent)
    sent.clear()
    alerts._send_with_retry({}, "坏请求")
    assert sent == ["坏请求"] and slept == []


def test_config_typos_fall_back_instead_of_crashing_startup():
    """写错一处配置不能让服务起不来(守护连续失败后放弃，整站停摆)。"""
    cfg = appconfig._validate(appconfig._merge(appconfig.DEFAULTS, {
        "update": {"check_interval": "1h"}, "server": {"port": "88oo"},
        "auth": {"users": [{"user": "admin", "password": "x"},
                           {"username": "ops", "password": "y"}]},
        "alerts": {"overheat_c": 95, "overheat_clear_c": 97},
    }))
    assert cfg["update"]["check_interval"] == appconfig.DEFAULTS["update"]["check_interval"]
    assert cfg["server"]["port"] == 8800
    assert [u["username"] for u in cfg["auth"]["users"]] == ["ops"]
    assert cfg["alerts"]["overheat_clear_c"] < cfg["alerts"]["overheat_c"]


def test_zero_values_are_respected_not_replaced_by_defaults(conn):
    """重启最小间隔设 0 = 不限间隔：以前 `or 900` 把 0 当成没设置，实际仍按 900 秒限流。"""
    import time
    import control
    import db
    cfg = appconfig._merge(appconfig.DEFAULTS, {})
    cfg["control"]["reboot_min_interval_sec"] = 0
    with db._wlock, conn:
        conn.execute("INSERT INTO command_log(ts,user,action,ip,ok,msg) VALUES(?,?,?,?,?,?)",
                     (int(time.time()) - 10, "x", "reboot", "10.0.0.1", 1, "ok"))
    allowed, skipped = control.filter_reboot_targets(conn, [("10.0.0.1", "")], cfg)
    assert allowed == [("10.0.0.1", "")] and skipped == []


def test_settings_json_overrides_are_listed():
    cfg = appconfig._merge(appconfig.DEFAULTS, {})
    over = appconfig.settings_overrides(cfg, {"reboot_enabled": True, "scan_interval": 300})
    assert over == ["reboot_enabled: False → True"]
