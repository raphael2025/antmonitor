# -*- coding: utf-8 -*-
import sqlite3

import pytest

import alerts
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
