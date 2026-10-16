# -*- coding: utf-8 -*-
"""更新器自愈：自检没过的坏提交不反复拉；新版本启动就崩溃时自动回滚到上一版。"""
import json

import pytest

import updater


@pytest.fixture
def state(tmp_path, monkeypatch):
    p = tmp_path / ".update_state.json"
    monkeypatch.setattr(updater, "STATE_FILE", str(p))
    return p


def _fake_git(calls, head="aaa", remote="bbb", fail_reset=False):
    def g(*args, **kw):
        calls.append(args)
        if args[:2] == ("status", "--porcelain"):
            return ""
        if args[:2] == ("rev-parse", "--git-dir"):
            return ".git"
        if args[:3] == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "dev"
        if args[0] == "fetch":
            return ""
        if args == ("rev-parse", "HEAD"):
            return head
        if args[:2] == ("rev-parse", "origin/dev"):
            return remote
        if args[0] == "rev-list":
            return "1"
        if args[0] == "log":
            return "bbb 坏版本"
        if args[0] == "merge":
            return ""
        if args[0] == "reset":
            if fail_reset:
                raise RuntimeError("index.lock 被占用")
            return ""
        raise AssertionError(args)
    return g


def test_commit_that_failed_selfcheck_is_not_pulled_again(state, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_git", _fake_git(calls))
    monkeypatch.setattr(updater, "_selfcheck", lambda: "SyntaxError")
    r = updater.apply(restart=False)
    assert not r["ok"] and "回滚" in r["msg"]
    calls.clear()
    r = updater.apply(restart=False)
    assert not r["ok"] and "等待新的提交" in r["msg"]
    assert not any(c[0] == "merge" for c in calls)          # 没再合并那个坏提交
    assert updater.check().get("known_bad") is True


def test_failed_rollback_is_reported_not_500(state, monkeypatch):
    monkeypatch.setattr(updater, "_git", _fake_git([], fail_reset=True))
    monkeypatch.setattr(updater, "_selfcheck", lambda: "SyntaxError")
    r = updater.apply(restart=False)
    assert not r["ok"] and "回滚失败" in r["msg"]


def test_new_version_that_keeps_crashing_is_rolled_back(state, monkeypatch):
    calls, restarts = [], []
    monkeypatch.setattr(updater, "_git", _fake_git(calls))
    monkeypatch.setattr(updater, "_restart", lambda: restarts.append(1))
    updater._set_pending("aaa", "bbb")
    for _ in range(updater.MAX_BOOT_ATTEMPTS):              # 前几次照常启动(给新版本机会)
        updater.boot_guard()
    assert not any(c[0] == "reset" for c in calls) and not restarts
    updater.boot_guard()                                    # 再起不来 → 回滚并重启
    assert ("reset", "--hard", "aaa") in calls and restarts == [1]
    st = json.loads(state.read_text(encoding="utf-8"))
    assert "pending" not in st and "bbb" in st["bad"]


def test_healthy_new_version_clears_the_pending_marker(state, monkeypatch):
    updater._set_pending("aaa", "bbb")
    updater.boot_guard()
    updater.mark_healthy()
    assert "pending" not in json.loads(state.read_text(encoding="utf-8"))
    updater.boot_guard()                                    # 之后正常启动不受影响


def test_manual_retry_ignores_the_bad_mark(state, monkeypatch):
    """自检失败常见原因是新依赖没装：运维 pip install 后手动重试必须能过，不能被永久拉黑。
    自动更新仍然跳过它(别每小时合并→回滚一遍)。"""
    calls = []
    monkeypatch.setattr(updater, "_git", _fake_git(calls))
    monkeypatch.setattr(updater, "_selfcheck", lambda: "ModuleNotFoundError: newdep")
    updater.apply(restart=False)                                   # 第一次：自检失败，记坏
    assert "等待新的提交" in updater.apply(restart=False)["msg"]    # 自动/普通调用：跳过
    monkeypatch.setattr(updater, "_selfcheck", lambda: "")          # 装好依赖了
    calls.clear()
    r = updater.apply(restart=False, force=True)                    # 手动重试
    assert r["ok"] and any(c[0] == "merge" for c in calls)
    assert "bbb" not in json.loads(state.read_text(encoding="utf-8")).get("bad", [])


def test_git_env_does_not_override_site_ssh_setup():
    assert "GIT_SSH_COMMAND" not in updater._GIT_ENV


def test_changed_requirements_installed_before_selfcheck(state, monkeypatch, tmp_path):
    """新版本加了依赖：合并后先 pip install，再自检；否则自检 import 必失败、永远更新不上去。"""
    calls, order = [], []
    base = _fake_git(calls)

    def g(*args, **kw):
        if args[0] == "diff":
            return "requirements.txt"
        return base(*args, **kw)

    (tmp_path / "requirements.txt").write_text("newdep\n")
    monkeypatch.setattr(updater, "BASE", str(tmp_path))
    monkeypatch.setattr(updater, "_git", g)
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda cmd, **kw: order.append(cmd[1:4]) or
                        updater.subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(updater, "_selfcheck", lambda: order.append("selfcheck") or "")
    assert updater.apply(restart=False)["ok"]
    assert order == [["-m", "pip", "install"], "selfcheck"]


def test_update_deferred_while_reboot_running(state, monkeypatch):
    """分批重启进行中不更新：更新会重启进程，把后面几批打断。"""
    calls = []
    monkeypatch.setattr(updater, "_git", _fake_git(calls))
    monkeypatch.setattr(updater, "BUSY_CHECK", lambda: "正在执行重启/批量命令")
    r = updater.apply(restart=False)
    assert not r["ok"] and "推迟" in r["msg"]
    assert not any(c[0] == "merge" for c in calls)
