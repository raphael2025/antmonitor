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
