# -*- coding: utf-8 -*-
"""MAC 单独显示在 SN 下：有 SN 也采；快巡检同 SN 才回填名册 MAC。"""
import db
import miner_core
from conftest import rec


def test_stock_collects_mac_even_when_sn_valid(monkeypatch):
    class Resp:
        def __init__(self, code=200, payload=None, text=""):
            self.status_code = code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class Session:
        def get(self, url, timeout=None, auth=None, **kwargs):
            if ":6060/get_sn" in url:
                return Resp(text="SNREAL12345678")
            if url.endswith("/cgi-bin/stats.cgi"):
                return Resp(payload={
                    "INFO": {"type": "S19"},
                    "STATS": [{
                        "rate_5s": 100000, "rate_avg": 99000, "watt": 3000,
                        "jt": 30, "elapsed": 1000, "chain": [],
                    }],
                })
            if url.endswith("/cgi-bin/get_system_info.cgi"):
                return Resp(payload={"macaddr": "10-0a-41-96-b6-f2"})
            return Resp(404)

    monkeypatch.setattr(miner_core, "get_session", lambda: Session())
    result = miner_core.probe_stock(
        "10.0.0.1", 1, 1, [("root", "root")], collect_identity=True)
    assert miner_core.sn_valid(result["sn"])
    assert result["mac"] == "10:0A:41:96:B6:F2"


def test_online_same_sn_backfills_roster_mac(conn):
    now = 1_700_000_200
    db.upsert_known_miners(conn, [rec(
        "10.3.0.1", sn="SNKEEP0012345", mac="AA:BB:CC:DD:EE:01",
        worker="Asuna", measured_ts=now - 60,
    )], now - 60)
    info = db.known_identity(conn)["10.3.0.1"]

    online = rec("10.3.0.1", sn="SNKEEP0012345", mac="", measured_ts=now)
    assert (miner_core.sn_valid(online["sn"]) and online["sn"] == info["sn"])
    online["mac"] = online.get("mac") or info.get("mac") or ""
    assert online["mac"] == "AA:BB:CC:DD:EE:01"

    swapped = rec("10.3.0.1", sn="SNOTHER999999", mac="", measured_ts=now)
    assert swapped["sn"] != info["sn"]
