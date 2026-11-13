# -*- coding: utf-8 -*-
"""矿工名：统计用基础名，列表显示 基础名.后缀（与 MAC 列无关）。"""
import miner_core
import db
from conftest import rec


def test_fetch_pool_keeps_suffix_for_display(monkeypatch):
    monkeypatch.setattr(miner_core, "_cgminer", lambda *a, **k: {
        "POOLS": [{
            "POOL": 0, "URL": "stratum+tcp://pool.example:3333",
            "User": "Asuna.rig12", "Status": "Alive",
            "Accepted": 10, "Rejected": 0, "Stale": 0,
        }]
    })
    pi = miner_core.fetch_pool("10.0.0.1", 1)
    assert pi["worker"] == "Asuna"
    assert pi["worker_suffix"] == "rig12"


def test_fetch_pool_no_dot_empty_suffix(monkeypatch):
    monkeypatch.setattr(miner_core, "_cgminer", lambda *a, **k: {
        "POOLS": [{
            "POOL": 0, "URL": "stratum+tcp://pool.example:3333",
            "User": "Asuna", "Status": "Alive",
            "Accepted": 1, "Rejected": 0, "Stale": 0,
        }]
    })
    pi = miner_core.fetch_pool("10.0.0.1", 1)
    assert pi["worker"] == "Asuna" and pi["worker_suffix"] == ""


def test_snapshot_and_roster_persist_suffix(conn):
    now = 1_700_000_300
    row = rec("10.4.0.1", worker="Asuna", worker_suffix="rig12",
              mac="AA:BB:CC:DD:EE:10", measured_ts=now)
    db.upsert_known_miners(conn, [row], now)
    ident = db.known_identity(conn)["10.4.0.1"]
    assert ident["worker"] == "Asuna"
    assert ident["worker_suffix"] == "rig12"

    _sid, _ts, kept = db.save_scan(conn, "quick", [row])
    assert kept[0]["worker"] == "Asuna"
    assert kept[0]["worker_suffix"] == "rig12"
    # 客户归组字段仍是基础名，不被后缀拆散
    assert kept[0]["worker"] == "Asuna"
