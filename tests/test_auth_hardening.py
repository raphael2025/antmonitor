# -*- coding: utf-8 -*-
"""登录加固：弱口令只准本机登录并强制改密；限流绕不过；空闲超时；网页改密码写回配置。"""
import importlib
import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import appconfig
import auth


@pytest.fixture(autouse=True)
def _clean_auth_state():
    auth._fails.clear()
    auth._ufails.clear()
    auth._sessions.clear()
    yield
    auth._fails.clear()
    auth._ufails.clear()
    auth._sessions.clear()


def _cfg(*users):
    return {"auth": {"enabled": True, "users": [dict(u) for u in users]}}


WEAK = {"username": "admin", "password": "admin888", "role": "admin"}
STRONG = {"username": "ops", "password": auth.hash_password("Str0ng-Pass!"), "role": "ops"}


def test_weak_password_only_from_the_monitor_pc_and_must_change():
    cfg = _cfg(WEAK)
    tok, why = auth.login_ex(cfg, "admin", "admin888", src="192.168.1.50")
    assert tok is None and "本机" in why
    tok, why = auth.login_ex(cfg, "admin", "admin888", src="127.0.0.1")
    assert tok and auth.session(tok)["must_change"] is True


def test_strong_password_logs_in_from_lan_without_forced_change():
    tok, _ = auth.login_ex(_cfg(STRONG), "ops", "Str0ng-Pass!", src="192.168.1.50")
    assert tok and auth.session(tok)["must_change"] is False


def test_per_user_lock_defeats_ip_rotation_but_never_locks_out_the_pc():
    cfg = _cfg(STRONG)
    for i in range(auth._MAX_USER_FAILS):
        auth.login_ex(cfg, "ops", "wrong", src=f"10.1.{i // 250}.{i % 250 + 1}")
    tok, why = auth.login_ex(cfg, "ops", "Str0ng-Pass!", src="10.9.9.9")   # 新 IP、正确口令
    assert tok is None and "锁定" in why
    tok, _ = auth.login_ex(cfg, "ops", "Str0ng-Pass!", src="127.0.0.1")
    assert tok                                                              # 本机永远能进


def test_concurrent_wrong_logins_cannot_exceed_the_per_ip_limit(monkeypatch):
    calls = []
    real = auth._verify_password

    def slow_verify(stored, pw):
        calls.append(1)
        time.sleep(0.05)
        return real(stored, pw)

    monkeypatch.setattr(auth, "_verify_password", slow_verify)
    cfg = _cfg({"username": "ops", "password": "Plain-But-Real-1", "role": "ops"})
    ts = [threading.Thread(target=auth.login_ex, args=(cfg, "ops", "x", "10.0.0.7"))
          for _ in range(40)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) <= auth._MAX_FAILS


def test_idle_session_expires(monkeypatch):
    tok, _ = auth.login_ex(_cfg(STRONG), "ops", "Str0ng-Pass!", src="10.0.0.1")
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + auth.IDLE + 1)
    assert auth.session(tok) is None


def test_web_password_change_forces_weak_session_through_and_writes_config(monkeypatch, tmp_path):
    src = Path(__file__).with_name("server_config.yaml")
    monkeypatch.setenv("MINER_CONFIG", str(src))
    server = importlib.import_module("server")
    cfg_copy = tmp_path / "config.yaml"
    shutil.copy(src, cfg_copy)
    monkeypatch.setattr(appconfig, "CONFIG_FILE", str(cfg_copy))
    monkeypatch.setattr(server.appconfig, "load_segments", lambda cfg: (["10.0.0"], 1, 254))
    users = [dict(u) for u in server.CFG["auth"]["users"]]
    monkeypatch.setitem(server.CFG["auth"], "users", users)

    c = TestClient(server.app)
    r = c.post("/api/login", json={"username": "weakops", "password": "ops888"})
    assert r.status_code == 401 and "本机" in r.json()["error"]        # TestClient 不是本机
    tok, _ = auth.login_ex(server.CFG, "weakops", "ops888", src="127.0.0.1")
    c.cookies.set(auth.COOKIE, tok)
    assert c.get("/api/me").json()["must_change"] is True
    blocked = c.post("/api/command", json={"ips": ["10.0.0.1"], "action": "locate",
                                           "params": {"on": True}})
    assert blocked.status_code == 403 and "修改密码" in blocked.json()["detail"]

    assert c.post("/api/password", json={"old": "ops888", "new": "123"}).status_code == 400
    r = c.post("/api/password", json={"old": "ops888", "new": "N3w-Strong-Pw"})
    assert r.status_code == 200, r.text
    assert c.get("/api/me").json()["must_change"] is False
    assert "pbkdf2$" in cfg_copy.read_text(encoding="utf-8")
    assert src.read_text(encoding="utf-8").count("ops888") == 1          # 仓库里的测试配置没被动
    tok2, _ = auth.login_ex(server.CFG, "weakops", "N3w-Strong-Pw", src="10.0.0.5")
    assert tok2                                                          # 改完就能远程登录
    c.close()
