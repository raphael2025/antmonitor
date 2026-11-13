# -*- coding: utf-8 -*-
"""漏液不可被忽略；集装箱数据停更要告警。"""
import time

import alerts
import db


def test_leakage_cannot_be_ignored(conn, monkeypatch):
    cfg = {
        "alerts": {
            "enabled": True,
            "cooldown": 0,
            "container_faults_ignore": ["leakage_fault", "supply_liquid_flow_low"],
            "container_stale_sec": 0,
        },
        "telegram": {"enabled": False},
    }
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    boxes = [{
        "ip": "10.0.0.8", "status": "online", "measured_ts": time.time(),
        "faults": [{"flag": "leakage_fault", "label": "漏液", "sev": "crit"}],
        "observed_fault_flags": ["leakage_fault"],
    }]
    fired = alerts.evaluate_containers(conn, boxes, {"10.0.0.8"}, cfg, state={})
    assert any(t == "cooler:leakage_fault" for _, t, *_ in fired)
    assert db.active_alert(conn, "10.0.0.8", "cooler:leakage_fault")


def test_container_stale_watchdog(conn, monkeypatch):
    cfg = {
        "alerts": {
            "enabled": True, "cooldown": 0,
            "container_faults_ignore": [],
            "container_stale_sec": 120,
        },
        "telegram": {"enabled": False},
    }
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    boxes = [{
        "ip": "10.0.0.9", "status": "online",
        "measured_ts": time.time() - 500,
        "faults": [], "observed_fault_flags": [],
    }]
    fired = alerts.evaluate_containers(conn, boxes, {"10.0.0.9"}, cfg, state={})
    assert any(t == "cooler_stale" for _, t, *_ in fired)
    assert db.active_alert(conn, "10.0.0.9", "cooler_stale")
