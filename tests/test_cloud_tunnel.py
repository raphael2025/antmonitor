# -*- coding: utf-8 -*-
"""云端远程面板转发：本机执行转发请求、暗号识别、不给转发来源"本机操作员"特权。"""
import base64
import importlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from starlette.requests import Request

import cloud_tunnel


class _Echo(BaseHTTPRequestHandler):
    seen = {}

    def do_GET(self):
        type(self).seen = {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "a=1; Path=/")
        self.send_header("Set-Cookie", "b=2; Path=/")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    do_POST = do_GET

    def log_message(self, *a):
        pass


@pytest.fixture
def local_panel():
    srv = HTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_call_local_forwards_with_secret_and_strips_origin(local_panel):
    job = {"method": "GET", "path": "/api/me?x=1",
           "headers": {"cookie": "mm_token=t", "origin": "https://cloud", "x-co-client-ip": "9.9.9.9",
                       "accept": "application/json"}}
    status, headers, body = cloud_tunnel._call_local(local_panel, job)
    seen = _Echo.seen
    assert status == 200 and body == b'{"ok": true}'
    assert seen["path"] == "/api/me?x=1"
    assert seen["headers"]["x-am-tunnel"] == cloud_tunnel.secret()
    assert seen["headers"]["x-forwarded-for"] == "9.9.9.9"
    assert "origin" not in seen["headers"] and seen["headers"]["cookie"] == "mm_token=t"
    assert [v for k, v in headers if k == "set-cookie"] == ["a=1; Path=/", "b=2; Path=/"]


def test_call_local_rejects_bad_requests(local_panel):
    for job in ({"method": "TRACE", "path": "/"}, {"method": "GET", "path": "//evil.com/x"},
                {"method": "GET", "path": "http://evil.com/"}, {"method": "GET", "path": "/a\\b"}):
        assert cloud_tunnel._call_local(local_panel, job)[0] == 400


def test_caller_cannot_forge_secret_header(local_panel):
    job = {"method": "GET", "path": "/", "headers": {"x-am-tunnel": "forged", "x-forwarded-for": "1.1.1.1"}}
    cloud_tunnel._call_local(local_panel, job)
    assert _Echo.seen["headers"]["x-am-tunnel"] == cloud_tunnel.secret()   # 覆盖成真暗号，不是传来的值


def test_unreachable_local_panel_gives_502():
    assert cloud_tunnel._call_local("http://127.0.0.1:1", {"method": "GET", "path": "/"})[0] == 502


def test_tunnel_off_by_default_and_needs_cloud_enabled():
    assert not cloud_tunnel.wanted({})
    assert not cloud_tunnel.wanted({"cloud": {"tunnel": True}})
    cfg = {"cloud": {"enabled": True, "url": "https://x", "token": "t", "site_name": "s", "tunnel": True}}
    assert cloud_tunnel.wanted(cfg)
    assert cloud_tunnel.start({}) is None and cloud_tunnel.status()["state"] == "disabled"


def _req(peer, headers):
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                    "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                    "client": (peer, 5555)})


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    return importlib.import_module("server")


def test_client_ip_marks_tunnel_requests_as_remote(server):
    good = {cloud_tunnel.MARK_HEADER: cloud_tunnel.secret(), cloud_tunnel.CLIENT_HEADER: "8.8.4.4"}
    ip = server._client_ip(_req("127.0.0.1", good))
    assert ip == "tunnel:8.8.4.4" and not server.auth.is_local(ip)
    assert not server._is_console(_req("127.0.0.1", {**good, "host": "127.0.0.1:8800",
                                                     "x-forwarded-for": "8.8.4.4"}), ip)
    # 暗号不对 / 不是本机来源：不认
    assert server._client_ip(_req("127.0.0.1", {cloud_tunnel.MARK_HEADER: "wrong"})) == "127.0.0.1"
    assert server._client_ip(_req("203.0.113.5", good)) == "203.0.113.5"


def test_cloud_form_keeps_and_validates_tunnel_flag(server, monkeypatch):
    monkeypatch.setitem(server.CFG, "cloud", {"tunnel": True})
    cl, err = server._cloud_form({"enabled": False})
    assert not err and cl["tunnel"] is True                 # 表单没带 = 沿用
    cl, err = server._cloud_form({"enabled": False, "tunnel": False})
    assert cl["tunnel"] is False
    cl, err = server._cloud_form({"enabled": False, "tunnel": "yes"})
    assert cl is None and "布尔" in err


def test_cloud_status_reports_tunnel_state(server, monkeypatch):
    monkeypatch.setitem(server.CFG, "cloud", {"enabled": True, "url": "https://x", "token": "t",
                                              "site_name": "s", "tunnel": True})
    out = server.api_cloud_status(_={})
    assert out["tunnel"]["wanted"] is True and "state" in out["tunnel"]
    monkeypatch.setitem(server.CFG, "cloud", {"enabled": True, "url": "https://x", "token": "t",
                                              "site_name": "s"})
    assert server.api_cloud_status(_={})["tunnel"]["wanted"] is False
