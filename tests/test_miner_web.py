# -*- coding: utf-8 -*-
import miner_web


def test_validate_miner_ip_accepts_v4():
    assert miner_web.validate_miner_ip("172.16.119.27") == "172.16.119.27"


def test_validate_miner_ip_rejects_junk():
    for bad in ("", "localhost", "999.1.1.1", "fe80::1", "127.0.0.1/24"):
        try:
            miner_web.validate_miner_ip(bad)
            assert False, bad
        except ValueError:
            pass


def test_looks_like_uniplus_html():
    assert miner_web.looks_like_uniplus_html(
        '<meta name="firmware" content="UniPlusOS" /><title>Firmware Console</title>')
    assert not miner_web.looks_like_uniplus_html("401 - Unauthorized")


def test_stock_plan_embeds_digest_credentials():
    plan = miner_web.console_plan("10.1.2.3", firmware_hint="stock", live=False)
    assert plan["kind"] == "stock"
    assert plan["open_url"] == "http://root:root@10.1.2.3/"
    assert plan["method"] == "digest_url"


def test_uniplus_plan_uses_bridge_and_dashboard():
    plan = miner_web.console_plan("10.1.2.4", firmware_hint="uniplus",
                                  uniplus_password="admin", live=False)
    assert plan["kind"] == "uniplus"
    assert plan["open_url"].endswith("/dashboard")
    assert plan["password"] == "admin"
    assert plan["method"] == "uniplus_bridge"


def test_uniplus_autologin_url_uses_hash_token():
    url = miner_web.uniplus_autologin_url("10.1.2.4", "abc123")
    assert url.startswith("http://10.1.2.4/dashboard#?token=")
    assert "abc123" in url


def test_uniplus_bridge_html_with_token(monkeypatch):
    monkeypatch.setattr(miner_web, "uniplus_login", lambda *a, **k: "tok_alive")
    html = miner_web.uniplus_bridge_html("10.1.2.4", "admin")
    assert "tok_alive" in html
    assert "#?token=" in html
    assert "location.replace" in html


def test_uniplus_bridge_html_fallback_when_login_fails(monkeypatch):
    monkeypatch.setattr(miner_web, "uniplus_login", lambda *a, **k: "")
    html = miner_web.uniplus_bridge_html("10.1.2.4", "admin")
    assert "自动登录" in html
    assert "10.1.2.4" in html and "/dashboard" in html


def test_uniplus_login_parses_token(monkeypatch):
    class Resp:
        ok = True
        status_code = 200
        headers = {"Content-Type": "application/json"}
        def json(self):
            return {"status": "ok", "token": "deadbeef"}

    monkeypatch.setattr(miner_web.requests, "post", lambda *a, **k: Resp())
    assert miner_web.uniplus_login("10.1.2.5", "admin") == "deadbeef"


def test_detect_console_kind_stock(monkeypatch):
    class Resp:
        status_code = 401
        headers = {"WWW-Authenticate": 'Digest realm="antMiner Configuration"'}
        text = "401"

    monkeypatch.setattr(miner_web.requests, "get", lambda *a, **k: Resp())
    assert miner_web.detect_console_kind("10.0.0.1") == "stock"


def test_detect_console_kind_uniplus(monkeypatch):
    class Resp:
        status_code = 200
        headers = {}
        text = '<meta name="firmware" content="UniPlusOS" />'

    monkeypatch.setattr(miner_web.requests, "get", lambda *a, **k: Resp())
    assert miner_web.detect_console_kind("10.0.0.2") == "uniplus"
