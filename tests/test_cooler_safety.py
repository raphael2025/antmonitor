# -*- coding: utf-8 -*-
"""集装箱漏液静音，冻结、急停和数据停更仍产生告警。"""
import time

import alerts
import db
import service


def test_leakage_is_ignored_and_freezing_and_emergency_stop_still_alert(conn, monkeypatch):
    cfg = {
        "alerts": {
            "enabled": True,
            "cooldown": 0,
            "container_faults_ignore": ["freezing_alarm", "emergency_stop", "supply_liquid_flow_low"],
            "container_stale_sec": 0,
        },
        "telegram": {"enabled": False},
    }
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    flags = ["leakage_fault", "leakage", "freezing_alarm", "emergency_stop", "supply_liquid_flow_low"]
    boxes = [{
        "ip": "10.0.0.8", "status": "online", "measured_ts": time.time(),
        "faults": [{"flag": flag, "label": flag, "sev": "crit"} for flag in flags],
        "observed_fault_flags": flags,
    }]
    fired = alerts.evaluate_containers(conn, boxes, {"10.0.0.8"}, cfg, state={})
    assert {t for _, t, *_ in fired} == {"cooler:freezing_alarm", "cooler:emergency_stop"}
    assert db.count_active_by_type(conn) == {"cooler:freezing_alarm": 1, "cooler:emergency_stop": 1}


def test_startup_silently_closes_existing_leakage_alerts(cfg, tmp_path, monkeypatch):
    cfg["db"]["path"] = str(tmp_path / "container.db")
    conn = db.init_db(cfg["db"]["path"])
    ip = "10.0.0.8"
    for flag in ("leakage_fault", "leakage", "freezing_alarm"):
        db.raise_alert(conn, ip, f"cooler:{flag}", "crit", flag)
    conn.close()
    sent = []
    monkeypatch.setattr(alerts, "_enqueue", lambda *args: sent.append(args))

    monitor = service.MonitorService(cfg)
    try:
        assert db.count_active_by_type(monitor.conn) == {"cooler:freezing_alarm": 1}
        for flag in ("leakage_fault", "leakage"):
            assert db.recent_alert(monitor.conn, ip, f"cooler:{flag}")["resolved"] == 1
        assert sent == []
    finally:
        monitor.conn.close()
        db.close_readers()


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
