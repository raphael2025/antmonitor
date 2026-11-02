"""A reachable controller with missing PLC fields is not evidence of recovery."""
import alerts
import db
import miner_core
import service


BASE = 2_000_000_000
IP = "192.0.2.100"


def _payload(monkeypatch, params):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Session:
        def get(self, url, **kwargs):
            if "minerInfo" in url:
                return Response({"params": {}})
            return Response({"ok": True, "method": "coolerState", "params": params})

    monkeypatch.setattr(miner_core, "get_session", Session)
    return miner_core.probe_antbox(IP, 1, 1)


def test_missing_fault_bits_never_clear_an_active_leak_alarm(conn, cfg, monkeypatch):
    clock = [BASE]
    monkeypatch.setattr(alerts.time, "time", lambda: clock[0])
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    state = {}
    faulty = _payload(monkeypatch, {"leakage_fault": True, "supply_liquid_temp": 40})
    alerts.evaluate_containers(conn, [faulty], {IP}, cfg, state=state)
    for offset in (10, 320, 700):
        clock[0] = BASE + offset
        missing = _payload(monkeypatch, {})
        alerts.evaluate_containers(conn, [missing], {IP}, cfg, state=state)
    assert db.active_alert(conn, IP, "cooler:leakage_fault") is not None
    assert missing["status"] == "unknown"
    assert missing["observed_fault_flags"] == []


def test_partial_payload_needs_explicit_false_and_uninterrupted_recovery(conn, cfg, monkeypatch):
    clock = [BASE]
    monkeypatch.setattr(alerts.time, "time", lambda: clock[0])
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    state = {}
    def observe(offset, params):
        clock[0] = BASE + offset
        record = _payload(monkeypatch, params)
        alerts.evaluate_containers(conn, [record], {IP}, cfg, state=state)
    observe(0, {"leakage_fault": True})
    observe(10, {"leakage_fault": False})
    observe(200, {"supply_liquid_temp": 40})  # A valid temperature does not clear the leak.
    observe(320, {"leakage_fault": False})
    assert db.active_alert(conn, IP, "cooler:leakage_fault") is not None
    observe(621, {"leakage_fault": "false"})
    assert db.active_alert(conn, IP, "cooler:leakage_fault") is None


def test_fault_boolean_parser_does_not_treat_invalid_values_as_normal(monkeypatch):
    row = _payload(monkeypatch, {"leakage_fault": "invalid", "freezing_alarm": None,
                                "power_fault": "false", "circulating_pump_fault": 1})
    assert set(row["observed_fault_flags"]) == {"power_fault", "circulating_pump_fault"}
    assert [f["flag"] for f in row["faults"]] == ["circulating_pump_fault"]


def test_container_keeps_unobserved_fault_with_original_measurement_time(conn, monkeypatch):
    clock = [BASE]
    monkeypatch.setattr(miner_core.time, "time", lambda: clock[0])
    first = _payload(monkeypatch, {"leakage_fault": True, "power_fault": False,
                                   "distribution_box1_power": 1000, "distribution_box2_power": 500})
    db.upsert_containers(conn, [first], BASE)
    assert db.get_containers(conn)[0]["data_complete"]
    clock[0] += 60
    partial = _payload(monkeypatch, {"power_fault": False, "distribution_box1_power": 900})
    db.upsert_containers(conn, [partial], clock[0])
    box = db.get_containers(conn)[0]
    assert box["online"] == 1
    assert box["data_status"] == "unknown" and not box["data_complete"]
    assert box["last_observed_ts"] == BASE
    assert box["faults"][0]["flag"] == "leakage_fault"
    assert box["faults"][0]["measured_ts"] == BASE
    assert box["faults"][0]["observed"] is False
    assert box["power1"] == 900 and box["power2"] is None
    clock[0] += 60
    restored = _payload(monkeypatch, {"leakage_fault": False, "power_fault": False})
    db.upsert_containers(conn, [restored], clock[0])
    box = db.get_containers(conn)[0]
    assert box["faults"] == [] and box["data_complete"]
    assert box["last_observed_ts"] == BASE + 120


def test_reachable_empty_plc_response_is_unknown_without_offline_escalation(cfg, monkeypatch, tmp_path):
    cfg["db"]["path"] = str(tmp_path / "container.db")
    cfg["schedule"]["enabled"] = False
    s = service.MonitorService(cfg)
    clock = [BASE]
    monkeypatch.setattr(miner_core.time, "time", lambda: clock[0])
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    try:
        first = _payload(monkeypatch, {"leakage_fault": True})
        db.upsert_containers(s.conn, [first], BASE)
        alerts.evaluate_containers(s.conn, [first], {IP}, cfg, state=s._alert_state)
        for offset in (10, 320, 700):
            clock[0] = BASE + offset
            missing = _payload(monkeypatch, {})
            monkeypatch.setattr(miner_core, "scan_containers", lambda *a, **k: [missing])
            s.scan_containers()
        box = db.get_containers(s.conn)[0]
        assert box["online"] == 1 and box["data_status"] == "unknown"
        assert db.active_alert(s.conn, IP, "cooler_offline") is None
        assert db.active_alert(s.conn, IP, "cooler:leakage_fault") is not None
        monkeypatch.setattr(miner_core, "scan_containers", lambda *a, **k: [])
        for offset in (710, 720, 730):
            clock[0] = BASE + offset
            s.scan_containers()
        assert db.get_containers(s.conn)[0]["online"] == 0
        assert db.get_containers(s.conn)[0]["measured_ts"] == BASE + 730
        assert db.get_containers(s.conn)[0]["faults"][0]["measured_ts"] == BASE
        assert db.active_alert(s.conn, IP, "cooler_offline") is not None
    finally:
        s.stop()
        s.conn.close()
        db.close_readers()
