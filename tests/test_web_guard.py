# -*- coding: utf-8 -*-
"""网页层防护：DNS 重绑定 / 跨站写请求 / 跨站 WebSocket / CSV 公式 / 网段 / 云端明文 / 日志打码。"""
import importlib
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import auth


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")
    monkeypatch.setattr(server.appconfig, "save_segments", lambda *a: None)
    monkeypatch.setattr(server.appconfig, "save_settings", lambda *a, **k: None)
    monkeypatch.setattr(server.appconfig, "update_settings", lambda *a, **k: {})
    monkeypatch.setattr(server.appconfig, "load_segments", lambda cfg: (["10.0.0"], 1, 254))
    auth._fails.clear()
    auth._ufails.clear()
    c = TestClient(server.app)
    assert c.post("/api/login", json={"username": "admin",
                                      "password": "test-admin-password"}).status_code == 200
    yield server, c
    c.close()


def test_dns_rebinding_host_is_refused_but_ip_and_localhost_work(app):
    _server, c = app
    assert c.get("/api/me", headers={"Host": "rebind.evil.example:8800"}).status_code == 400
    assert c.get("/api/me", headers={"Host": "192.168.1.10:8800"}).status_code == 200
    assert c.get("/api/me", headers={"Host": "localhost:8800"}).status_code == 200
    assert c.get("/api/me", headers={"Host": "[::1]:8800"}).status_code == 200


def test_cross_site_writes_are_refused(app):
    _server, c = app
    body = {"ips": ["10.0.0.1"], "action": "locate", "params": {"on": True}}
    r = c.post("/api/command", json=body, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = c.post("/api/command", content=b'{"ips":["10.0.0.1"],"action":"locate"}',
               headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = c.post("/api/command", content=b'{"ips":["10.0.0.1"],"action":"locate"}')   # 无类型 Blob
    assert r.status_code == 415
    r = c.post("/api/logout", headers={"Origin": "http://testserver"})   # 同源、无请求体：放行
    assert r.status_code == 200


def test_cross_site_websocket_is_refused(app):
    _server, c = app
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"Origin": "http://evil.example"}) as ws:
            ws.receive_text()


def test_segments_must_be_private_and_are_normalized(app):
    server, c = app
    saved = []
    server.appconfig.save_segments = lambda segs, hs, he: saved.append(segs)
    r = c.post("/api/segments", json={"segments": ["8.8.8"], "host_start": 1, "host_end": 254})
    assert r.status_code == 400 and not saved
    r = c.post("/api/segments", json={"segments": ["１７２.16.5"]})    # 全角数字
    assert r.status_code == 400
    r = c.post("/api/segments", json={"segments": ["172.016.005", "10.1.2.x"]})
    assert r.status_code == 200 and saved[-1] == ["172.16.5", "10.1.2"]


def test_public_cloud_url_must_be_https(app):
    _server, c = app
    cloud = {"enabled": True, "token": "t", "site_name": "s", "site_type": "air"}
    r = c.post("/api/settings", json={"cloud": dict(cloud, url="http://overview.example.com")})
    assert r.status_code == 400
    r = c.post("/api/settings", json={"cloud": dict(cloud, url="http://192.168.1.9:8000")})
    assert r.status_code == 200


def test_csv_formula_injection_is_neutralized(app, monkeypatch):
    server, c = app
    monkeypatch.setattr(server.db, "customer_report", lambda conn, h: [
        {"worker": '=HYPERLINK("http://x","y")', "machines": 1, "uptime_pct": 100,
         "delivered_th_h": 1, "power_kwh": 1}])
    server._CACHE.clear() if hasattr(server, "_CACHE") else None
    text = c.get("/api/reports/customers?hours=24&format=csv").text
    assert "'=HYPERLINK" in text


def test_access_log_masks_token():
    import logs
    logs.setup(None)
    f = [x for x in logging.getLogger("uvicorn.access").filters
         if x.__class__.__name__ == "_MaskSecrets"][0]
    rec = logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s"', (
        "1.2.3.4", "GET", "/api/public/summary?token=SECRET123&x=1"), None)
    f.filter(rec)
    assert "SECRET123" not in rec.getMessage() and "token=***" in rec.getMessage()


def test_settings_cannot_silently_stall_scanning(app, monkeypatch):
    """ARP 限速存成 0：以前接口回显 0，实际被压成 1 pps，5000 台巡检要一个多小时，监控
    形同停摆且界面上看不出来。现在超范围直接拒绝，保存成功时回显的就是实际生效的值。"""
    server, c = app
    monkeypatch.setitem(server.CFG["scan"], "max_pps", 100)
    for bad in (0, 2000):
        r = c.post("/api/settings", json={"max_pps": bad})
        assert r.status_code == 400, bad
        assert server.CFG["scan"]["max_pps"] == 100
    r = c.post("/api/settings", json={"max_pps": 200})
    assert r.status_code == 200 and r.json()["max_pps"] == 200 == server.CFG["scan"]["max_pps"]


def test_low_pressure_container_counts_as_faulty(app, monkeypatch):
    server, _c = app
    monkeypatch.setitem(server.CFG["alerts"], "container_supply_pressure_min", 0.2)
    assert server._container_faulty({"online": True, "faults": [], "supply_pressure": 0.1}, set())
    assert not server._container_faulty({"online": True, "faults": [], "supply_pressure": 0.3}, set())
    assert not server._container_faulty({"online": True, "faults": [], "supply_pressure": None}, set())


def test_ip_sort_is_numeric(app, monkeypatch):
    server, c = app
    from conftest import rec
    recs = [rec(f"10.0.0.{n}") for n in (100, 2, 10, 1)]
    monkeypatch.setattr(server, "_latest_records", lambda: (None, recs))
    got = [m["ip"] for m in c.get("/api/miners?sort=ip&order=asc").json()["miners"]]
    assert got == ["10.0.0.1", "10.0.0.2", "10.0.0.10", "10.0.0.100"]


def test_hostile_inputs_get_400_not_500(app):
    _server, c = app
    J = {"Content-Type": "application/json"}
    assert c.post("/api/alerts/ack", content=b'{"id": Infinity}', headers=J).status_code == 400
    assert c.post("/api/segments", content=b'{"segments": ["10.0.0"], "host_start": Infinity}',
                  headers=J).status_code == 400
    r = c.post("/api/command", json={"ips": ["10.0.0.1"], "action": "set_pools",
                                     "confirm": True, "params": ["x"]})
    assert r.status_code == 400


def test_short_cache_is_bounded(app):
    server, _c = app
    server._cache.clear()
    for i in range(1000):
        server._cached(("k", i), 30, lambda: [0] * 10)
    assert len(server._cache) <= server._CACHE_MAX


def test_websocket_via_trusted_proxy_is_accepted_and_rejections_carry_a_distinct_code(app, monkeypatch):
    """HTTP 对受信代理免检 Host/Origin，WS 以前没有 → 经 nginx/frp 访问时 WS 被 1008 关闭，
    前端把 1008 当成会话失效 → "登录→又被踢回登录"循环。"""
    server, c = app
    monkeypatch.setattr(server, "_via_trusted_proxy", lambda req: True)
    with c.websocket_connect("/ws", headers={"Host": "miners.example.com",
                                             "Origin": "https://miners.example.com"}):
        pass                                                    # 能连上
    monkeypatch.setattr(server, "_via_trusted_proxy", lambda req: False)
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws", headers={"Origin": "http://evil.example"}) as ws:
            ws.receive_text()
    assert e.value.code == 4403                                 # 不是 1008：前端不会误弹登录
