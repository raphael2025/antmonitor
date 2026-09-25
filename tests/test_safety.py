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
