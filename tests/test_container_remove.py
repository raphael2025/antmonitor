# -*- coding: utf-8 -*-
"""运维手动删除集装箱：立刻出记忆并消冷却告警。"""
import time

import db


def _insert_box(conn, ip, online=1, last_online=None):
    now = int(time.time()) if last_online is None else int(last_online)
    conn.execute(
        "INSERT OR REPLACE INTO containers(ip,last_online,online,supply_temp,return_temp,"
        "pumps,faults,miner_ips) VALUES(?,?,?,?,?,'{}','[]','[]')",
        (ip, now, online, 20.0, 25.0))
    conn.commit()


def test_remove_containers_deletes_and_clears_alerts(conn):
    ip = "10.9.9.1"
    _insert_box(conn, ip)
    db.raise_alert(conn, ip, "cooler_offline", "crit", "离线")
    db.raise_alert(conn, ip, "cooler_stale", "warn", "停更")
    db.raise_alert(conn, ip, "cooler:leakage_fault", "crit", "漏液")
    assert db.remove_containers(conn, [ip]) == [ip]
    assert db.get_containers(conn) == []
    active = db.active_alerts_by_type(conn, like="cooler")
    assert active == []


def test_remove_containers_ignores_unknown_ip(conn):
    _insert_box(conn, "10.9.9.2")
    assert db.remove_containers(conn, ["10.9.9.3"]) == []
    assert [c["ip"] for c in db.get_containers(conn)] == ["10.9.9.2"]


def test_kick_also_clears_cooler_stale(conn, monkeypatch):
    ip = "10.9.9.4"
    monkeypatch.setattr(time, "time", lambda: 2_000_000_000)
    _insert_box(conn, ip, online=0, last_online=2_000_000_000 - 90000)
    db.raise_alert(conn, ip, "cooler_stale", "warn", "停更")
    gone = db.kick_offline_containers(conn, max_offline_sec=86400)
    assert gone == [ip]
    assert db.active_alerts_by_type(conn, like="cooler") == []
