# -*- coding: utf-8 -*-
"""原厂重启：请求结果判定 + 重启后告警静默期。"""
import socket
import threading
import time
from types import SimpleNamespace

import requests

import alerts
import control
import db
from conftest import rec


# ---------- 请求结果判定 ----------

class _Resp:
    def __init__(self, code):
        self.status_code = code


class _FakeSession:
    """按顺序返回预设结果(状态码或异常)，并记下每次请求。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def request(self, method, url, auth=None, timeout=None):
        self.calls.append((method, url, auth.username))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return _Resp(r)


PW = [("root", "root")]


def _authed(exc_cls, msg):
    """带 Digest 凭据那一跳上发生的异常(矿机已经收到并开始执行)。"""
    return exc_cls(msg, request=SimpleNamespace(headers={"Authorization": "Digest x"}))


def test_reboot_uses_get_like_stock_web_ui():
    s = _FakeSession(200)
    assert control._stock_reboot(s, "10.0.0.1", PW, 3) == (True, "reboot ok")
    assert s.calls == [("GET", "http://10.0.0.1/cgi-bin/reboot.cgi", "root")]


def test_reboot_falls_back_to_post_only_on_405():
    s = _FakeSession(405, 200)
    ok, _ = control._stock_reboot(s, "10.0.0.1", PW, 3)
    assert ok and [c[0] for c in s.calls] == ["GET", "POST"]


def test_reboot_tries_next_password_on_401():
    s = _FakeSession(401, 200)
    ok, _ = control._stock_reboot(s, "10.0.0.1", [("root", "bad"), ("root", "root")], 3)
    assert ok and len(s.calls) == 2


def test_reboot_all_passwords_rejected():
    s = _FakeSession(401)
    assert control._stock_reboot(s, "10.0.0.1", PW, 3) == (False, "密码无效(401)")


def test_reboot_read_timeout_counts_as_sent_and_is_not_resent():
    s = _FakeSession(_authed(requests.ReadTimeout, "read timed out"))
    ok, msg = control._stock_reboot(s, "10.0.0.1", PW, 3)
    assert ok and "已下发" in msg
    assert len(s.calls) == 1          # 绝不对正在重启的机器补发


def test_reboot_timeout_on_unauthenticated_first_hop_is_a_failure():
    """Digest 第一跳不带凭据，矿机不可能执行；web 卡死的机器就是在这一跳超时——必须算失败。"""
    first_hop = requests.ReadTimeout(
        "read timed out", request=SimpleNamespace(headers={}))
    ok, _ = control._stock_reboot(_FakeSession(first_hop), "10.0.0.1", PW, 3)
    assert not ok


def test_reboot_connect_failure_is_a_failure():
    for e in (requests.ConnectTimeout("connect timed out"),
              requests.ConnectionError("connection refused")):
        ok, msg = control._stock_reboot(_FakeSession(e), "10.0.0.1", PW, 3)
        assert not ok and "连不上" in msg


def _digest_miner(on_authed):
    """真实 socket 模拟原厂矿机：无凭据请求回 Digest 401 质询，带凭据的交给 on_authed(conn)。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    challenge = (b"HTTP/1.1 401 Unauthorized\r\n"
                 b'WWW-Authenticate: Digest realm="antMiner", nonce="abc", qop="auth"\r\n'
                 b"Content-Length: 0\r\n\r\n")

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            while True:
                req = c.recv(65536)
                if not req:
                    c.close()
                    break
                if b"Authorization: Digest" in req:
                    on_authed(c)
                    break
                c.sendall(challenge)

    threading.Thread(target=serve, daemon=True).start()
    return srv


def _session():
    s = requests.Session()
    s.trust_env = False
    return s


def test_reboot_connection_dropped_after_auth_counts_as_sent():
    """带凭据的请求送到后不回响应直接断开(矿机开始重启时的表现) → 已下发。"""
    srv = _digest_miner(lambda c: c.close())
    ok, msg = control._stock_reboot(_session(), f"127.0.0.1:{srv.getsockname()[1]}", PW, 3)
    srv.close()
    assert ok and "已下发" in msg


def test_reboot_miner_that_hangs_before_auth_is_a_failure():
    """web 服务卡死：连得上但第一跳就不回 → 矿机没收到重启，必须显示失败。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    ok, _ = control._stock_reboot(_session(), f"127.0.0.1:{srv.getsockname()[1]}", PW, 1)
    srv.close()
    assert not ok


def test_run_batch_calls_before_group_right_before_each_group(monkeypatch, cfg):
    """静默期按每组实际下发时间起算：分批重启可能跑十几分钟。"""
    order = []
    monkeypatch.setattr(control, "run_one",
                        lambda ip, fw, a, p, c: order.append(("send", ip)) or
                        {"ip": ip, "ok": True, "msg": ""})
    cfg["control"].update(reboot_concurrency=2, reboot_delay_sec=0, reboot_shuffle=False)
    targets = [(f"10.0.0.{i}", "stock") for i in range(1, 6)]
    control.run_batch(targets, "reboot", {}, cfg,
                      before_group=lambda ips: order.append(("mark", tuple(ips))))
    marks = [i for i, e in enumerate(order) if e[0] == "mark"]
    assert len(marks) == 3
    for i in marks:                   # 每组标记紧跟着就是这一组的下发
        assert all(order[i + 1 + k] == ("send", ip) for k, ip in enumerate(order[i][1]))


# ---------- 重启后告警静默期 ----------

def _roster(conn, ips):
    db.upsert_known_miners(conn, list(ips), int(time.time()))


def _run(conn, cfg, records, state):
    sid, _ts, kept = db.save_scan(conn, "quick", records)
    return [(f[0], f[1]) for f in alerts.evaluate(conn, sid, kept, cfg, kind="quick", state=state)]


def test_rebooted_miner_offline_is_silent_during_grace(conn, cfg):
    _roster(conn, ["10.0.0.1"])
    state = {}
    _run(conn, cfg, [rec("10.0.0.1")], state)
    state.setdefault("rebooting", {})["10.0.0.1"] = int(time.time())
    assert _run(conn, cfg, [rec("10.0.0.1", status="offline")], state) == []
    # 回来了(uptime 很小，零算力也在开机宽限内) → 不报，静默期结束后自动清掉标记
    assert _run(conn, cfg, [rec("10.0.0.1", hr=0.0, uptime=60)], state) == []


def test_rebooted_miner_that_never_comes_back_still_alerts(conn, cfg):
    """静默期过了仍不在线必须报——否则上轮已是 offline，普通规则永远不会再报。"""
    _roster(conn, ["10.0.0.1"])
    cfg["control"]["reboot_grace_sec"] = 600
    state = {}
    _run(conn, cfg, [rec("10.0.0.1")], state)
    state.setdefault("rebooting", {})["10.0.0.1"] = int(time.time())
    assert _run(conn, cfg, [rec("10.0.0.1", status="offline")], state) == []
    state["rebooting"]["10.0.0.1"] -= 601            # 模拟静默期已过
    assert _run(conn, cfg, [rec("10.0.0.1", status="offline")], state) == [("10.0.0.1", "offline")]
    assert "10.0.0.1" not in state["rebooting"]


def test_rebooting_whole_segment_is_not_a_segment_down_event(conn, cfg):
    ips = [f"10.0.0.{i}" for i in range(1, 11)]
    _roster(conn, ips)
    state = {}
    _run(conn, cfg, [rec(ip) for ip in ips], state)
    now = int(time.time())
    state.setdefault("rebooting", {}).update({ip: now for ip in ips})
    assert _run(conn, cfg, [rec(ip, status="offline") for ip in ips], state) == []


def test_reboot_grace_zero_never_fires_for_unmarked_miners(conn, cfg):
    """静默期设 0：没被重启过的离线机不能被报成"重启后未上线"。"""
    _roster(conn, ["10.0.0.1", "10.0.0.2"])
    cfg["control"]["reboot_grace_sec"] = 0
    state = {}
    _run(conn, cfg, [rec("10.0.0.1"), rec("10.0.0.2", status="offline")], state)
    fired = _run(conn, cfg, [rec("10.0.0.1", status="offline"), rec("10.0.0.2", status="offline")], state)
    assert fired == [("10.0.0.1", "offline")]


# ---------- UniPlus 1.3.x 重启 ----------

class _UniResp:
    def __init__(self, code, text="", content_type="application/json"):
        self.status_code = code
        self.text = text
        self.headers = {"Content-Type": content_type}

    def json(self):
        import json
        return json.loads(self.text)


def test_uniplus_reboot_uses_bearer_cgi_like_web_ui(monkeypatch):
    """1.3.x：login.cgi 拿 token 后 GET /cgi-bin/reboot.cgi，与官方 SPA 一致。"""
    calls = []

    class Sess:
        def get(self, url, headers=None, timeout=None):
            calls.append(("GET", url, (headers or {}).get("Authorization")))
            return _UniResp(200, '{"status":"ok"}')

        def post(self, *a, **k):
            raise AssertionError("1.3.x 成功路径不应落到旧 /api/v1/system/reboot")

    monkeypatch.setattr(control.miner_core, "_uniplus_login_token",
                        lambda s, ip, pw, to: "tok123")
    ok, msg = control._uniplus_reboot(Sess(), "10.0.0.9", "", 3)
    assert ok and msg == "reboot ok"
    assert calls == [("GET", "http://10.0.0.9/cgi-bin/reboot.cgi", "Bearer tok123")]


def test_uniplus_reboot_rejects_spa_html_false_success(monkeypatch):
    """旧代码把 SPA HTML 的 200 当成 reboot ok——必须拒绝。"""
    monkeypatch.setattr(control.miner_core, "_uniplus_login_token",
                        lambda *a, **k: "")

    class Sess:
        def post(self, url, json=None, timeout=None):
            return _UniResp(200, "<!doctype html><html>", "text/html")

    ok, msg = control._uniplus_reboot(Sess(), "10.0.0.9", "admin", 3)
    assert not ok and "网页壳" in msg


def test_uniplus_reboot_with_token_does_not_fall_back_to_legacy(monkeypatch):
    """已 login 拿到 token 后，cgi 非 ok 不得再打 /api/v1/system/reboot。"""
    posts = []

    class Sess:
        def get(self, url, headers=None, timeout=None):
            return _UniResp(500, '{"status":"error"}')

        def post(self, url, json=None, timeout=None):
            posts.append(url)
            return _UniResp(200, "{}")

    monkeypatch.setattr(control.miner_core, "_uniplus_login_token",
                        lambda *a, **k: "tok")
    ok, msg = control._uniplus_reboot(Sess(), "10.0.0.9", "admin", 3)
    assert not ok and "500" in msg
    assert posts == []


def test_uniplus_legacy_api_reboot_still_works(monkeypatch):
    monkeypatch.setattr(control.miner_core, "_uniplus_login_token",
                        lambda *a, **k: "")
    posts = []

    class Sess:
        def post(self, url, json=None, timeout=None):
            posts.append(url)
            if url.endswith("/unlock"):
                return _UniResp(200, "{}")
            return _UniResp(204, "")

    ok, msg = control._uniplus_reboot(Sess(), "10.0.0.9", "secret", 3)
    assert ok and msg == "reboot ok"
    assert posts[-1].endswith("/api/v1/system/reboot")


def test_uniplus_locate_uses_bearer_blink(monkeypatch):
    calls = []

    class Sess:
        def post(self, url, headers=None, data=None, json=None, timeout=None):
            calls.append((url, data, (headers or {}).get("Authorization")))
            return _UniResp(200, '{"status":"ok"}')

    monkeypatch.setattr(control.miner_core, "_uniplus_login_token",
                        lambda *a, **k: "tok")
    ok, msg = control._uniplus_locate(Sess(), "10.0.0.9", "", True, 3)
    assert ok and msg == "locate=True"
    assert calls == [("http://10.0.0.9/cgi-bin/blink.cgi",
                      {"action": "startBlink"}, "Bearer tok")]


def test_uniplus_set_pools_uses_bearer_cgi(monkeypatch):
    calls = []

    class Sess:
        def get(self, url, headers=None, timeout=None):
            calls.append(("GET", url))
            return _UniResp(200, '{"pools":[{"url":"old"}],"bitmain-fan-ctrl":true}')

        def post(self, url, headers=None, json=None, data=None, timeout=None):
            calls.append(("POST", url, json))
            return _UniResp(200, '{"stats":"success"}')

    monkeypatch.setattr(control.miner_core, "_uniplus_login_token",
                        lambda *a, **k: "tok")
    pools = [{"url": "stratum+tcp://a:3333", "user": "w", "pass": "x"}]
    ok, msg = control._uniplus_set_pools(Sess(), "10.0.0.9", "admin", pools, 3)
    assert ok and msg == "pools updated"
    assert calls[0] == ("GET", "http://10.0.0.9/cgi-bin/get_miner_conf.cgi")
    assert calls[1][0] == "POST" and calls[1][1].endswith("set_miner_conf.cgi")
    assert calls[1][2]["pools"] == pools
