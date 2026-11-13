# -*- coding: utf-8 -*-
"""UniPlus 1.3.x：页面是 UniPlus 但 6060/旧 API 不可用时，不得标成 stock。"""
import miner_core


class Resp:
    def __init__(self, *, status=200, headers=None, text="", payload=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_probe_stock_rejects_uniplus_6060_error_stub(monkeypatch):
    class Session:
        def get(self, url, **kwargs):
            return Resp(text="error http cmd")

    monkeypatch.setattr(miner_core, "get_session", Session)
    assert miner_core.probe_stock("10.0.0.1", 1, 1, [("root", "root")]) is None


def test_probe_uniplus_uses_bearer_get_stats(monkeypatch):
    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/api/v1/summary"):
                return Resp(headers={"Content-Type": "text/html"},
                            text='<meta name="firmware" content="UniPlusOS" />')
            if url.endswith("/cgi-bin/get_stats.cgi"):
                return Resp(headers={"Content-Type": "application/json"}, payload={
                    "INFO": {"type": "Antminer S19 XP+ Hyd", "CompileTime": "Uniplus 1.3.1"},
                    "STATS": [{"rate_5s": 345000.0, "rate_avg": 344000.0, "elapsed": 100,
                               "chain": [{"temp_chip": [70, 71]}]}],
                })
            if url.endswith("/cgi-bin/get_system_info.cgi"):
                # 现场 CGI 常不带 application/json，只回 text/plain；以前会因此丢掉 MAC
                return Resp(headers={"Content-Type": "text/plain; charset=utf-8"},
                            payload={"macaddr": "a8:1b:db:b4:e7:fb", "minertype": "Antminer S19 XP+ Hyd"})
            if ":8000/api/v0/power" in url:
                return Resp(headers={"Content-Type": "application/json"},
                            payload={"power": 5703})
            return Resp(status=404, text="no")

        def post(self, url, **kwargs):
            return Resp(headers={"Content-Type": "application/json"},
                        payload={"status": "ok", "token": "tok123"})

    monkeypatch.setattr(miner_core, "get_session", Session)
    rec = miner_core.probe_uniplus("10.0.0.1", 1, 1, collect_identity=True, password="admin")
    assert rec["firmware"] == "uniplus"
    assert rec["model"] == "Antminer S19 XP+ Hyd"
    assert rec["hr_rt"] == 345.0
    assert rec["mac"] == "A8:1B:DB:B4:E7:FB"
    assert rec["power"] == 5703
    assert rec["eff"] == 16.53  # 5703 / 345 TH/s，与 Web 同源算法


def test_probe_uniplus_mac_not_blocked_by_missing_content_type(monkeypatch):
    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/api/v1/summary"):
                return Resp(headers={"Content-Type": "text/html"}, text="<html>")
            if url.endswith("/cgi-bin/get_stats.cgi"):
                return Resp(payload={
                    "INFO": {"type": "S19"}, "STATS": [{"rate_5s": 100000, "rate_avg": 99000, "elapsed": 1}],
                })
            if url.endswith("/cgi-bin/get_system_info.cgi"):
                return Resp(headers={}, payload={"mac": "aa:bb:cc:dd:ee:ff"})
            if ":8000/api/v0/power" in url:
                return Resp(status=404)
            return Resp(status=404)

        def post(self, url, **kwargs):
            return Resp(headers={"Content-Type": "application/json"},
                        payload={"status": "ok", "token": "t"})

    monkeypatch.setattr(miner_core, "get_session", Session)
    rec = miner_core.probe_uniplus("10.0.0.2", 1, 1, collect_identity=True)
    assert rec["mac"] == "AA:BB:CC:DD:EE:FF"
    assert rec["power"] is None
    assert rec["eff"] is None


def test_probe_uniplus_power_from_port_8000(monkeypatch):
    """1.3.x get_stats 无 watt；Web 功率来自 :8000/api/v0/power。"""
    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/api/v1/summary"):
                return Resp(headers={"Content-Type": "text/html"}, text="<html>")
            if url.endswith("/cgi-bin/get_stats.cgi"):
                return Resp(payload={
                    "INFO": {"type": "Antminer S19 XP+ Hyd"},
                    "STATS": [{"rate_5s": 314590.0, "rate_avg": 314590.0, "elapsed": 100}],
                })
            if ":8000/api/v0/power" in url:
                return Resp(payload={"power": 5703})
            return Resp(status=404)

        def post(self, url, **kwargs):
            return Resp(headers={"Content-Type": "application/json"},
                        payload={"status": "ok", "token": "t"})

    monkeypatch.setattr(miner_core, "get_session", Session)
    rec = miner_core.probe_uniplus("172.16.119.154", 1, 1)
    assert rec["power"] == 5703
    assert rec["eff"] == 18.13  # 5703 / 314.59


def test_probe_prefers_uniplus_when_html_says_so(monkeypatch):
    html = '<meta name="firmware" content="UniPlusOS" /><title>Firmware Console</title>'

    class Session:
        def get(self, url, **kwargs):
            if url.rstrip("/").endswith("10.0.0.9") or url.endswith("10.0.0.9/"):
                return Resp(text=html, headers={"Content-Type": "text/html"})
            if ":6060/get_sn" in url:
                return Resp(text="error http cmd")
            if url.endswith("/api/v1/summary"):
                return Resp(headers={"Content-Type": "text/html"}, text=html)
            if url.endswith("/cgi-bin/get_stats.cgi"):
                return Resp(headers={"Content-Type": "application/json"}, payload={
                    "INFO": {"type": "S19"}, "STATS": [{"rate_5s": 100000, "rate_avg": 99000, "elapsed": 1}],
                })
            return Resp(status=404)

        def post(self, url, **kwargs):
            return Resp(headers={"Content-Type": "application/json"},
                        payload={"status": "ok", "token": "t"})

    monkeypatch.setattr(miner_core, "get_session", Session)
    monkeypatch.setattr(miner_core, "tcp_open", lambda *a, **k: True)
    cfg = {"passwords": [["root", "root"]], "online_timeout": 1.0, "data_timeout": 1.0,
           "liveness_gate": True, "gate_timeout": 0.5, "collect_identity": False}
    rec = miner_core.probe("10.0.0.9", cfg)
    assert rec["firmware"] == "uniplus"
    assert rec["hr_rt"] == 100.0
