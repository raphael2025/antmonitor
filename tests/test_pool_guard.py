# -*- coding: utf-8 -*-
"""矿池防篡改：换池只能换到白名单内；矿机上出现白名单外的矿池 → 告警。

改矿池 = 把全场算力送给别人，是这个系统里最值钱的攻击。面板账号被盗/内鬼可以直接
下发 set_pools；局域网里有人拿矿机默认口令 root/root 绕过面板直接改矿机，面板也得能发现。
"""
import json
import socket
import threading
import time

import pytest

import alerts
import control
import db
import miner_core
from conftest import rec


def test_pool_host_parsing_and_allowlist_suffix_match():
    assert control.pool_host("stratum+tcp://BTC.F2Pool.com:3333") == "btc.f2pool.com"
    assert control.pool_host("stratum+ssl://ss.antpool.com:443/x") == "ss.antpool.com"
    allow = ["f2pool.com", "ss.antpool.com"]
    assert control.pool_allowed("stratum+tcp://btc.f2pool.com:3333", allow)
    assert control.pool_allowed("stratum+tcp://f2pool.com:3333", allow)
    assert control.pool_allowed("stratum+ssl://ss.antpool.com:443", allow)
    assert not control.pool_allowed("stratum+tcp://evilf2pool.com:3333", allow)   # 不是子域名
    assert not control.pool_allowed("stratum+tcp://f2pool.com.evil.io:3333", allow)
    assert not control.pool_allowed("stratum+tcp://btc.f2pool.com:3333", [])     # 没配白名单一律不放行


def test_fetch_pool_reports_every_configured_pool_url():
    """备用池也要报：攻击者常把自己的池塞在备用位，等主池一断就接管。"""
    resp = {"POOLS": [
        {"URL": "stratum+tcp://btc.f2pool.com:3333", "User": "cust1.001", "Status": "Alive",
         "Priority": 0, "Accepted": 10, "Rejected": 0, "Stale": 0},
        {"URL": "stratum+tcp://evil.example:3333", "User": "thief.1", "Status": "Dead",
         "Priority": 1, "Accepted": 0, "Rejected": 0, "Stale": 0}]}
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        c, _ = srv.accept()
        c.recv(1024)
        c.sendall(json.dumps(resp).encode() + b"\x00")
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    orig = socket.create_connection
    port = srv.getsockname()[1]
    try:
        socket.create_connection = lambda addr, timeout=None: orig(("127.0.0.1", port), timeout)
        pi = miner_core.fetch_pool("10.0.0.1", 2)
    finally:
        socket.create_connection = orig
        srv.close()
    assert pi["worker"] == "cust1"
    assert pi["pools"] == ["stratum+tcp://btc.f2pool.com:3333", "stratum+tcp://evil.example:3333"]


def _eval(conn, cfg, records, state=None):
    db.upsert_known_miners(conn, [r for r in records if r["status"] == "online"], int(time.time()))
    sid, _ts, kept = db.save_scan(conn, "quick", records)
    kept_by_ip = {r["ip"]: r for r in kept}
    for r in records:          # pools 不落库，只在本轮内存记录里(与 service 传给 evaluate 的一致)
        kept_by_ip[r["ip"]]["pools"] = r.get("pools")
    return [(f[0], f[1]) for f in alerts.evaluate(conn, sid, list(kept_by_ip.values()), cfg,
                                                  kind="quick", state=state or {})]


def test_pool_outside_allowlist_raises_crit_alert_and_clears_when_fixed(conn, cfg, monkeypatch):
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    cfg["control"]["pool_allowlist"] = ["f2pool.com"]
    good = ["stratum+tcp://btc.f2pool.com:3333"]
    bad = good + ["stratum+tcp://evil.example:3333"]
    assert _eval(conn, cfg, [rec("10.0.0.1", pools=good)]) == []
    assert _eval(conn, cfg, [rec("10.0.0.1", pools=bad)]) == [("10.0.0.1", "pool_hijack")]
    a = db.active_alert(conn, "10.0.0.1", "pool_hijack")
    assert a["severity"] == "crit" and "evil.example" in a["detail"]
    _eval(conn, cfg, [rec("10.0.0.1", pools=good)])
    assert db.active_alert(conn, "10.0.0.1", "pool_hijack") is None


def test_no_pool_alerts_until_allowlist_is_configured(conn, cfg, monkeypatch):
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    cfg["control"]["pool_allowlist"] = []
    assert _eval(conn, cfg, [rec("10.0.0.1", pools=["stratum+tcp://any.example:1"])]) == []


def _client(monkeypatch):
    import importlib
    from pathlib import Path
    from fastapi.testclient import TestClient
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")
    monkeypatch.setattr(server.appconfig, "load_segments", lambda cfg: (["10.0.0"], 1, 254))
    monkeypatch.setattr(server.alerts, "_enqueue", lambda *a, **k: None)
    c = TestClient(server.app)
    assert c.post("/api/login", json={"username": "admin",
                                      "password": "test-admin-password"}).status_code == 200
    return server, c


def _set_pools(c, url):
    return c.post("/api/command", json={
        "ips": ["10.0.0.1"], "action": "set_pools", "confirm": True,
        "params": {"pools": [{"url": url, "user": "cust1.001", "pass": "x"}]}})


def test_set_pools_blocked_without_allowlist_and_outside_it(monkeypatch):
    server, c = _client(monkeypatch)
    sent = []
    monkeypatch.setattr(server.control, "run_batch", lambda t, a, p, cfg, **k: (
        sent.append(p) or [{"ip": ip, "ok": True, "msg": "pools updated"} for ip, _ in t], ""))
    monkeypatch.setitem(server.CFG["control"], "pool_allowlist", [])
    r = _set_pools(c, "stratum+tcp://btc.f2pool.com:3333")
    assert r.status_code == 403 and "白名单" in r.json()["error"] and not sent

    monkeypatch.setitem(server.CFG["control"], "pool_allowlist", ["f2pool.com"])
    r = _set_pools(c, "stratum+tcp://evil.example:3333")
    assert r.status_code == 403 and not sent

    r = _set_pools(c, "stratum+tcp://btc.f2pool.com:3333")
    assert r.status_code == 200 and len(sent) == 1
    audit = [x for x in db.list_commands(server.SVC.conn, 50) if x["action"] == "set_pools"]
    assert "btc.f2pool.com" in audit[0]["msg"] and "cust1.001" in audit[0]["msg"]
    assert audit[0]["user"].startswith("admin@")               # 带来源 IP
    rejects = [x for x in db.list_commands(server.SVC.conn, 50) if x["action"] == "reject:set_pools"]
    assert any("evil.example" in x["msg"] for x in rejects)
    c.close()


def test_run_one_refuses_pool_outside_allowlist_even_if_called_directly(cfg):
    cfg["control"].update(enabled=True, pool_allowlist=["f2pool.com"])
    r = control.run_one("10.0.0.1", "stock", "set_pools",
                        {"pools": [{"url": "stratum+tcp://evil.example:1", "user": "x"}]}, cfg)
    assert not r["ok"] and "白名单" in r["msg"]


def test_audit_of_set_pools_survives_flood_of_low_value_rows(conn):
    db.log_commands(conn, "ops@1.2.3.4", "set_pools",
                    [{"ip": "10.0.0.1", "ok": True, "msg": "pools updated | stratum+tcp://x"}])
    for _ in range(6):
        db.log_commands(conn, "ops", "state:active",
                        [{"ip": f"10.0.{i // 250}.{i % 250}", "ok": True, "msg": ""}
                         for i in range(1000)])
    db.prune(conn, 3)
    acts = [r["action"] for r in conn.execute("SELECT action FROM command_log")]
    assert "set_pools" in acts
    assert acts.count("state:active") == 5000
