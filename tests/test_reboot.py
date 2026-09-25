# -*- coding: utf-8 -*-
"""原厂重启：请求结果判定 + 重启后告警静默期。"""
import socket
import threading
import time

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
    s = _FakeSession(requests.ReadTimeout("read timed out"))
    ok, msg = control._stock_reboot(s, "10.0.0.1", PW, 3)
    assert ok and "已下发" in msg
    assert len(s.calls) == 1          # 绝不对正在重启的机器补发


def test_reboot_connect_failure_is_a_failure():
    for e in (requests.ConnectTimeout("connect timed out"),
              requests.ConnectionError("connection refused")):
        ok, msg = control._stock_reboot(_FakeSession(e), "10.0.0.1", PW, 3)
        assert not ok and "连不上" in msg


def test_reboot_connection_dropped_before_reply_counts_as_sent():
    """真实 socket：收到请求后不回响应直接断开(矿机开始重启时的表现)。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        c.recv(4096)
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    s = requests.Session()
    s.trust_env = False
    ok, msg = control._stock_reboot(s, f"127.0.0.1:{port}", PW, 3)
    srv.close()
    assert ok and "已下发" in msg


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
