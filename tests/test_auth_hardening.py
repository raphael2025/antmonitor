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


def test_weak_password_remote_can_operate_and_change_password(tmp_path):
    """弱口令只提示：远程也能登录、能运维、能改密码(Ubuntu 服务器部署首次登录几乎都是局域网 IP)。
    must_change/weak_remote 仍打标给前端提示，但不锁权限。"""
    cfg = _cfg(WEAK)
    tok, why = auth.login_ex(cfg, "admin", "admin888", src="192.168.1.50")
    s = auth.session(tok)
    assert tok and s["must_change"] and s["weak_remote"]
    err = auth.change_password(cfg, "admin", "admin888", "N3w-Strong-Pw",
                               keep_token=tok, path=str(tmp_path / "x.yaml"))
    # 无 config 文件路径时 set_user_password 可能失败；这里主要断言不再被 weak_remote 拦
    assert err != "这个账号的密码太弱，只能在监控电脑本机(打开 http://127.0.0.1:端口)登录后修改"
    tok, _ = auth.login_ex(cfg, "admin", "admin888", src="127.0.0.1")
    s = auth.session(tok)
    assert s["must_change"] and not s["weak_remote"]


def test_strong_password_logs_in_from_lan_without_forced_change():
    tok, _ = auth.login_ex(_cfg(STRONG), "ops", "Str0ng-Pass!", src="192.168.1.50")
    assert tok and auth.session(tok)["must_change"] is False


def test_per_user_lock_defeats_ip_rotation_but_never_locks_out_the_pc():
    cfg = _cfg(STRONG)
    auth.login_ex(cfg, "ops", "Str0ng-Pass!", src="10.8.8.8")    # 值班员平时用的电脑
    for i in range(auth._MAX_USER_FAILS):
        auth.login_ex(cfg, "ops", "wrong", src=f"10.1.{i // 250}.{i % 250 + 1}")
    tok, why = auth.login_ex(cfg, "ops", "Str0ng-Pass!", src="10.9.9.9")   # 新 IP、正确口令
    assert tok is None and "锁定" in why
    tok, _ = auth.login_ex(cfg, "ops", "Str0ng-Pass!", src="127.0.0.1")
    assert tok                                                              # 本机永远能进
    tok, _ = auth.login_ex(cfg, "ops", "Str0ng-Pass!", src="10.8.8.8")
    assert tok      # 以前成功登录过的电脑也不受这道锁影响：别人没法恶意把值班员锁在外面


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


def test_web_password_change_weak_session_keeps_ops_and_writes_config(monkeypatch, tmp_path):
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
    assert r.status_code == 200 and r.json()["must_change"]          # 仍提示弱口令
    # 弱口令不再锁写权限：远程也能下发命令、也能改密码
    assert c.post("/api/command", json={"ips": ["10.0.0.1"], "action": "locate",
                                        "params": {"on": True}}).status_code != 403
    assert c.post("/api/password", json={"old": "ops888", "new": "123"}).status_code == 400
    r = c.post("/api/password", json={"old": "ops888", "new": "N3w-Strong-Pw"})
    assert r.status_code == 200, r.text
    assert c.get("/api/me").json()["must_change"] is False
    assert "pbkdf2$" in cfg_copy.read_text(encoding="utf-8")
    assert src.read_text(encoding="utf-8").count("ops888") == 1          # 仓库里的测试配置没被动
    tok2, _ = auth.login_ex(server.CFG, "weakops", "N3w-Strong-Pw", src="10.0.0.5")
    assert tok2                                                          # 改完就能远程登录
    c.close()


def test_random_usernames_do_not_grow_the_user_lock_table(monkeypatch):
    monkeypatch.setattr(auth, "_verify_password", lambda stored, pw: False)   # 只测计数表，不跑真哈希
    cfg = _cfg(STRONG)
    for i in range(500):
        auth.login_ex(cfg, f"nobody{i}", "x", src=f"10.2.{i // 250}.{i % 250 + 1}")
    assert len(auth._ufails) == 0


def test_loopback_behind_local_reverse_proxy_is_not_the_console(monkeypatch):
    """同机 nginx/frp 转发：外部请求也来自 127.0.0.1，但不能享受"本机"待遇。"""
    import importlib
    from pathlib import Path
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")

    class Req:
        def __init__(self, headers):
            self.headers = headers
            self.client = type("C", (), {"host": "127.0.0.1"})()

    assert server._is_console(Req({"host": "127.0.0.1:8800"}), "127.0.0.1")
    assert server._is_console(Req({"host": "localhost:8800"}), "127.0.0.1")
    assert not server._is_console(Req({"host": "127.0.0.1:8800", "x-forwarded-for": "8.8.8.8"}),
                                  "127.0.0.1")
    assert not server._is_console(Req({"host": "miners.example.com"}), "127.0.0.1")
    assert not server._is_console(Req({"host": "127.0.0.1:8800"}), "192.168.1.9")


def test_password_change_endpoint_cannot_be_used_to_brute_force(tmp_path):
    """会话 cookie 被偷后，拿"改密码要验证旧密码"当在线爆破接口：连错 5 次就把这个会话踢掉。"""
    cfg = _cfg(STRONG)
    tok, _ = auth.login_ex(cfg, "ops", "Str0ng-Pass!", src="10.0.0.1")
    for i in range(auth._MAX_PW_FAILS):
        assert auth.change_password(cfg, "ops", f"guess{i}", "N3w-Strong-Pw", keep_token=tok,
                                    path=str(tmp_path / "x.yaml"))
    assert auth.session(tok) is None


def test_request_from_trusted_proxy_is_never_the_console(monkeypatch):
    import importlib
    from pathlib import Path
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")
    import ipaddress as _ip
    monkeypatch.setattr(server, "_TRUSTED", [_ip.ip_network("127.0.0.1/32")])

    class Req:
        headers = {"host": "127.0.0.1:8800"}
        client = type("C", (), {"host": "127.0.0.1"})()

    # 同机 nginx 默认不加 X-Forwarded-*、Host 也是 127.0.0.1：只要对端是受信代理就不算本机
    assert not server._is_console(Req(), "127.0.0.1")
