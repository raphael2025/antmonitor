"""Device payloads, observation time and unknowns stay consistent across ingestion.

All device/network replies are mocked; no miner command or external request is sent.
"""
import copy

import pytest

import alerts
import cloud_report
import db
import miner_core
import service
from conftest import rec


BASE = 2_000_000_000
IP = "192.0.2.10"


class Response:
    status_code = 200

    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text

    def json(self):
        return self.payload


@pytest.mark.parametrize("value", [True, False, -1, "-0.1", "NaN", float("inf")])
def test_invalid_rates_are_missing_not_zero_or_negative(value):
    assert miner_core.gh_to_th(value) is None
    assert miner_core._nonnegative(value, integer=True) is None


def test_real_zero_and_hashrate_unit_conversions(monkeypatch):
    assert miner_core.gh_to_th(0) == 0
    assert miner_core.gh_to_th(293_000) == 293
    monkeypatch.setattr(miner_core, "_cgminer", lambda *a: {
        "SUMMARY": [{"MHS 5s": 293_000_000, "MHS av": 291_000_000, "Elapsed": 3600}]})
    assert miner_core.fetch_summary(IP, 1) == {"hr_rt": 293, "hr_avg": 291, "uptime": 3600}
    monkeypatch.setattr(miner_core, "_cgminer", lambda *a: {
        "SUMMARY": [{"GHS av": 293_000, "Elapsed": 3600}]})
    assert miner_core.fetch_summary(IP, 1) is None  # Firmware average is not current rate.


@pytest.mark.parametrize("bad_optional", [["bad"], "bad", 7, True])
def test_uniplus_malformed_optional_fields_preserve_valid_online_rate(monkeypatch, bad_optional):
    payload = {"miner": {"hr_realtime": 293_000, "hr_average": 291_000,
                         "power_consumption": 6000, "chip_temp": bad_optional,
                         "miner_status": bad_optional, "miner_type": {"bad": "model"}}}
    class Session:
        def get(self, *args, **kwargs):
            return Response(payload)
    monkeypatch.setattr(miner_core, "get_session", Session)
    monkeypatch.setattr(miner_core, "probe_stock", lambda *a: None)
    result = miner_core.scan([IP], {"workers": 1, "fetch_worker": False})[0]
    assert result["status"] == "online"
    assert (result["hr_rt"], result["hr_avg"], result["power"]) == (293, 291, 6000)
    assert result["temp"] is None and result["uptime"] is None
    assert result["model"] == ""


def test_uniplus_invalid_numeric_fields_never_enter_database_totals(conn, monkeypatch):
    payload = {"miner": {"hr_realtime": -293_000, "hr_average": True,
                         "power_consumption": -6000, "power_efficiency": True,
                         "miner_status": {"miner_state_time": -0.1}}}
    class Session:
        def get(self, *args, **kwargs):
            return Response(payload)
    monkeypatch.setattr(miner_core, "get_session", Session)
    result = miner_core.probe_uniplus(IP, 1)
    assert all(result[k] is None for k in ("hr_rt", "hr_avg", "power", "eff", "uptime"))
    sid, _, kept = db.save_scan(conn, "quick", [result])
    assert kept[0]["status"] == "online"
    assert db.latest_scan(conn)["total_hr"] is None
    assert db.scan_records(conn, sid)[0]["hr_rt"] is None


def test_stock_bad_metadata_does_not_discard_valid_measurements(monkeypatch):
    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/get_sn"):
                return Response(text="REALSN0123456")
            return Response({"INFO": ["bad"], "STATS": [{
                "rate_5s": 293_000, "rate_avg": 291_000, "watt": "6000", "jt": "20.48",
                "chain": ["bad", {"temp_chip": "90"}, {"temp_chip": [70, True, "NaN", 78]}],
            }]})
    monkeypatch.setattr(miner_core, "get_session", Session)
    result = miner_core.probe_stock(IP, 1, 1, [("root", "root")])
    assert result["status"] == "online"
    assert (result["hr_rt"], result["hr_avg"], result["power"], result["temp"]) == (293, 291, 6000, 78)
    assert result["model"] == ""


def test_probe_exception_is_unknown_not_confirmed_offline(monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("malformed device payload")
    monkeypatch.setattr(miner_core, "probe", broken)
    result = miner_core.scan([IP], {"workers": 1})[0]
    assert result["status"] == "unknown"
    assert result["hr_rt"] is None


def test_driver_timestamp_survives_later_pool_request_and_scan_completion(monkeypatch):
    clock = [BASE]
    monkeypatch.setattr(miner_core.time, "time", lambda: clock[0])
    def stock(*args):
        row = miner_core._blank(IP, "online")
        row["hr_rt"] = 293
        return row
    def pools(*args):
        clock[0] += 10
        return {"worker": "A"}
    monkeypatch.setattr(miner_core, "probe_stock", stock)
    monkeypatch.setattr(miner_core, "fetch_pool", pools)
    row = miner_core.scan([IP], {"workers": 1})[0]
    assert row["measured_ts"] == BASE
    assert clock[0] == BASE + 10


@pytest.fixture
def audit_service(cfg, monkeypatch, tmp_path):
    cfg["db"]["path"] = str(tmp_path / "audit.db")
    cfg["schedule"]["enabled"] = False
    cfg["scan"]["discovery_pause_sec"] = 0
    cfg["scan"]["discovery_batch"] = 1
    s = service.MonitorService(cfg)
    monkeypatch.setattr(s, "_reconfirm", lambda rows, gen: rows)
    monkeypatch.setattr(service.alerts, "evaluate", lambda *a, **k: [])
    monkeypatch.setattr(s, "_maybe_auto_reboot", lambda rows: None)
    yield s
    s.stop()
    s.conn.close()
    db.close_readers()


@pytest.mark.parametrize("rate", [0, 293])
def test_long_full_scan_keeps_raw_time_but_never_acts_on_expired_reading(audit_service, monkeypatch, rate):
    clock = [BASE]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    db.upsert_known_miners(audit_service.conn, [rec(IP)], BASE)
    db.set_machine_state(audit_service.conn, [IP], "repair")
    actions, evaluations = [], []
    def scan(ips, cfg, **kwargs):
        if IP in ips:
            # Also exercises older/custom probes without measured_ts: stamp this
            # phase before the empty-address discovery phase advances the clock.
            return [rec(IP, hr=rate, worker="A")]
        clock[0] += 1800
        return [miner_core._blank(ip, "offline") for ip in ips]
    def evaluate(conn, sid, records, cfg, **kwargs):
        evaluations.extend(copy.deepcopy(records))
        return []
    monkeypatch.setattr(miner_core, "scan", scan)
    monkeypatch.setattr(service.alerts, "evaluate", evaluate)
    monkeypatch.setattr(audit_service, "_maybe_auto_reboot", lambda rows: actions.extend(copy.deepcopy(rows)))
    result = audit_service._do_scan("full", [IP], discover=["192.0.2.99"])
    meta, raw = audit_service.latest()
    assert result["online"] == 1 and result["miners"] == 1
    assert raw[0]["status"] == "online" and raw[0]["hr_rt"] == rate
    assert raw[0]["measured_ts"] == BASE and meta["ts"] == BASE + 1800
    assert db.latest_scan(audit_service.conn)["unknown"] == 0
    assert db.latest_scan(audit_service.conn)["total_hr"] == rate
    assert evaluations[0]["status"] == actions[0]["status"] == "unknown"
    assert evaluations[0]["hr_rt"] is actions[0]["hr_rt"] is None
    known = db.known_identity(audit_service.conn)[IP]
    assert known["state"] == "repair"  # Old positive rate must not restore repair state.
    assert known["last_online"] == BASE
    current = db.customer_hashrates(meta, raw, now=clock[0])[0]
    assert current["hashrate_ths"] == rate and current["hashrate_ts"] == BASE


def test_new_machine_identity_survives_a_long_first_discovery(audit_service, monkeypatch):
    clock = [BASE]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    def scan(ips, cfg, **kwargs):
        if IP in ips:
            return [rec(IP, measured_ts=BASE, hr=293)]
        clock[0] += 1800
        return [miner_core._blank(ip, "offline") for ip in ips]
    monkeypatch.setattr(miner_core, "scan", scan)
    result = audit_service._do_scan("full", [], discover=[IP, "192.0.2.99"])
    assert result["miners"] == 1 and result["online"] == 1
    assert db.known_identity(audit_service.conn)[IP]["last_online"] == BASE
    assert audit_service.latest()[1][0]["measured_ts"] == BASE


def test_only_known_unknown_machine_is_counted(audit_service, monkeypatch):
    db.upsert_known_miners(audit_service.conn, [rec(IP)], int(service.time.time()))
    monkeypatch.setattr(miner_core, "scan", lambda ips, cfg, **kwargs: [
        miner_core._blank(IP, "unknown"),
        miner_core._blank("192.0.2.20", "unknown"),
        miner_core._blank("192.0.2.30", "offline"),
    ])
    result = audit_service._do_scan("full", [IP, "192.0.2.20", "192.0.2.30"])
    assert result["miners"] == 1 and result["online"] == 0
    assert db.latest_scan(audit_service.conn)["unknown"] == 1
    assert {r["ip"] for r in audit_service.latest()[1]} == {IP}


def test_removal_publishes_inventory_event_and_cannot_be_overwritten_by_older_scan(audit_service, monkeypatch):
    clock = [BASE]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    second = "192.0.2.20"
    monkeypatch.setattr(miner_core, "scan", lambda ips, cfg, **kwargs: [
        rec(IP, measured_ts=BASE), rec(second, measured_ts=BASE)])
    audit_service._do_scan("quick", [IP, second])
    original, original_rows = audit_service.latest()
    clock[0] += 180
    db.remove_miners(audit_service.conn, [IP])
    audit_service.drop_from_snapshot([IP])
    latest, rows = audit_service.latest()
    assert latest["kind"] == "inventory" and latest["ts"] == BASE + 180
    assert latest["scan_id"] > original["scan_id"]
    assert [r["ip"] for r in rows] == [second]
    assert rows[0]["measured_ts"] == BASE
    assert len(db.scan_records(audit_service.conn, original["scan_id"])) == 2
    audit_service._publish(original["scan_id"], original["ts"], "quick", original_rows)
    assert audit_service.latest()[0]["scan_id"] == latest["scan_id"]


@pytest.mark.parametrize("value,complete", [(None, False), (0, True), (293, True)])
def test_report_preserves_unknown_zero_and_independent_quality_flags(monkeypatch, value, complete):
    summary = {"ok": True, "scanned": True, "scan_ts": BASE,
               "measurement_ts": BASE - 300,
               "total": 2, "online": 1, "unknown": 1,
               "total_hashrate_ths": value, "total_power_kw": None,
               "hashrate_complete": complete, "power_complete": False,
               "hashrate_known_machines": 1, "power_known_machines": 0,
               "known_hashrate_ths": value, "known_power_kw": None}
    calls = []
    class Session:
        def close(self):
            pass
    def post(c, path, payload, session):
        calls.append((path, payload))
        cloud_report._GEN += 1
    monkeypatch.setattr(cloud_report, "_GEN", 99)
    monkeypatch.setattr(cloud_report, "_session", lambda c: Session())
    monkeypatch.setattr(cloud_report, "_set_status", lambda *a, **k: True)
    monkeypatch.setattr(cloud_report, "_post", post)
    cloud_report._loop(99, {"site_name": "A"}, "site-a", lambda: summary, lambda h: [])
    payload = calls[0][1]
    assert payload["hashrate_ths"] == value
    assert payload["measurement_ts"] == BASE - 300
    assert payload["hashrate_complete"] is complete
    assert payload["power_kw"] is None and payload["power_complete"] is False
    for key in ("unknown", "hashrate_known_machines", "power_known_machines",
                "known_hashrate_ths", "known_power_kw"):
        assert payload[key] == summary[key]


def _pool(number, accepted, rejected, status="Alive"):
    return {"POOL": number, "URL": f"stratum+tcp://pool{number}.example:3333",
            "User": "A.worker1", "Status": status, "Priority": number,
            "Accepted": accepted, "Rejected": rejected, "Stale": 0}


@pytest.mark.parametrize("bad", [None, True, -1, "NaN", 1.25])
def test_missing_invalid_pool_counts_are_not_filled_with_zero(monkeypatch, bad):
    monkeypatch.setattr(miner_core, "_cgminer", lambda *a: {"POOLS": [_pool(0, bad, 10)]})
    sample = miner_core.fetch_pool(IP, 1)
    assert sample["accepted"] is None
    assert sample["rejected"] == 10


def test_pool_counter_identity_is_opaque_and_zero_is_not_replaced_by_alias(monkeypatch):
    pool = _pool(0, 0, 0)
    pool.update(URL="stratum+tcp://dummy:dummy-secret@pool.example:3333", accepted=999)
    monkeypatch.setattr(miner_core, "_cgminer", lambda *a: {"POOLS": [pool]})
    sample = miner_core.fetch_pool(IP, 1)
    assert sample["accepted"] == 0
    assert "dummy-secret" not in sample["counter_basis"]
    assert "pool.example" not in str(sample["counter_samples"])


def _evaluate_pools(conn, cfg, monkeypatch, state, pools, uptime=100000):
    monkeypatch.setattr(miner_core, "_cgminer", lambda *a: {"POOLS": pools})
    monkeypatch.setattr(alerts, "_enqueue", lambda *a, **k: None)
    pi = miner_core.fetch_pool(IP, 1)
    record = rec(IP, hr=293, uptime=uptime, **pi)
    db.upsert_known_miners(conn, [record], int(service.time.time()))
    sid, _, kept = db.save_scan(conn, "quick", [record])
    kept[0].update(counter_basis=pi["counter_basis"], counter_samples=pi["counter_samples"])
    fired = alerts.evaluate(conn, sid, kept, cfg, state=state, kind="quick")
    return [f for f in fired if f[1] == "reject"]


def test_alive_pool_switch_does_not_turn_old_counters_into_new_rejections(conn, cfg, monkeypatch):
    state = {}
    pools = [_pool(0, 1000, 0), _pool(1, 10000, 1000, "Dead")]
    assert _evaluate_pools(conn, cfg, monkeypatch, state, pools) == []
    pools[0]["Status"], pools[1]["Status"] = "Dead", "Alive"
    assert _evaluate_pools(conn, cfg, monkeypatch, state, pools) == []
    assert db.active_alert(conn, IP, "reject") is None
    pools[1]["Rejected"] += 100
    fired = _evaluate_pools(conn, cfg, monkeypatch, state, pools)
    assert len(fired) == 1 and "100.0%" in fired[0][3]


def test_one_pool_counter_reset_is_not_hidden_by_another_pool_growth(conn, cfg, monkeypatch):
    state = {}
    assert _evaluate_pools(conn, cfg, monkeypatch, state,
                           [_pool(0, 1000, 0), _pool(1, 1000, 100)]) == []
    assert _evaluate_pools(conn, cfg, monkeypatch, state,
                           [_pool(0, 10, 0), _pool(1, 3000, 300)]) == []
    assert db.active_alert(conn, IP, "reject") is None
    assert _evaluate_pools(conn, cfg, monkeypatch, state,
                           [_pool(0, 100, 0), _pool(1, 3010, 300)]) == []


def test_uptime_rollback_resets_share_origin_even_if_total_counters_grew(conn, cfg, monkeypatch):
    state = {}
    assert _evaluate_pools(conn, cfg, monkeypatch, state, [_pool(0, 9000, 0)]) == []
    assert _evaluate_pools(conn, cfg, monkeypatch, state, [_pool(0, 9500, 1000)], uptime=10) == []
    assert db.active_alert(conn, IP, "reject") is None
    fired = _evaluate_pools(conn, cfg, monkeypatch, state, [_pool(0, 9500, 1100)], uptime=20)
    assert len(fired) == 1 and "100.0%" in fired[0][3]


def test_counter_reset_is_compared_with_last_sample_not_only_min_share_origin():
    bases = {}
    assert alerts._share_delta(rec(IP, accepted=0, rejected=0), bases) is None
    assert alerts._share_delta(rec(IP, accepted=5, rejected=0), bases) == (5, 0)
    assert alerts._share_delta(rec(IP, accepted=2, rejected=0), bases) is None
    assert alerts._share_delta(rec(IP, accepted=2, rejected=60), bases) == (0, 60)


def test_missing_counter_does_not_imply_zero_accepted_or_keep_a_comparable_origin():
    bases = {}
    assert alerts._share_delta(rec(IP, accepted=1000, rejected=0), bases) is None
    assert alerts._share_delta(rec(IP, accepted=None, rejected=1000), bases) is None
    assert IP not in bases
    assert alerts._share_delta(rec(IP, accepted=1000, rejected=1000), bases) is None


@pytest.mark.parametrize("packet_kind", ["list", "dict", "empty_dict"])
def test_customer_report_sends_the_actual_fixed_window_from_the_query(monkeypatch, packet_kind):
    end = BASE // 600 * 600
    period = {"period_start_ts": end - 24 * 3600, "period_end_ts": end,
              "billing_period_start_ts": end - 24 * 3600, "billing_period_end_ts": end}
    rows = [] if packet_kind == "empty_dict" else [{"worker": "A", "avg_hashrate_ths": 293, **period}]
    calls, requested = [], []
    class Session:
        def close(self):
            pass
    def customers(hours):
        requested.append(hours)
        return rows if packet_kind == "list" else {"hours": hours, "customers": rows, **period}
    def post(c, path, payload, session):
        calls.append((path, payload))
        if path.endswith("/customers"):
            cloud_report._GEN += 1
    monkeypatch.setattr(cloud_report.time, "time", lambda: BASE)
    monkeypatch.setattr(cloud_report, "_GEN", 99)
    monkeypatch.setattr(cloud_report, "_session", lambda c: Session())
    monkeypatch.setattr(cloud_report, "_set_status", lambda *a, **k: True)
    monkeypatch.setattr(cloud_report, "_post", post)
    cloud_report._loop(99, {"site_name": "A"}, "site-a",
                       lambda: {"ok": True, "scanned": True, "scan_ts": BASE}, customers)
    assert requested == [24]
    assert len(calls) == 2
    payload = calls[1][1]
    assert payload["customers"] == rows
    assert {key: payload[key] for key in period} == period


def test_old_or_empty_customer_rows_are_not_relabelled_as_fixed_window():
    assert cloud_report._customer_period([], 24) == {}
    assert cloud_report._customer_period(None, 24) == {}
    assert cloud_report._customer_period([{"worker": "A", "avg_hashrate_ths": 293}], 24) == {}


@pytest.mark.parametrize("change", [
    {"hours": 1}, {"hours": True}, {"hours": "24"},
    {"customers": None}, {"customers": {}},
    {"customers": [{"worker": "A"}]},
    {"period_end_ts": BASE + 600, "period_start_ts": BASE + 600 - 86400},
    {"billing_period_start_ts": None}, {"period_start_ts": float("nan")},
])
def test_customer_packet_metadata_must_agree_with_requested_window_and_rows(change):
    period = {"period_start_ts": BASE - 86400, "period_end_ts": BASE,
              "billing_period_start_ts": BASE - 86400, "billing_period_end_ts": BASE}
    packet = {"hours": 24, "customers": [{"worker": "A", **period}], **period, **change}
    with pytest.raises(ValueError):
        cloud_report._customer_period(packet, 24)


@pytest.mark.parametrize("packet", [False, 0, "", [None], ["A"],
                                    {"hours": 24, "customers": []}])
def test_malformed_customer_packet_is_not_turned_into_an_empty_report(packet):
    with pytest.raises(ValueError):
        cloud_report._customer_period(packet, 24)


@pytest.mark.parametrize("change", [{"period_end_ts": BASE + 1},
                                    {"period_start_ts": None},
                                    {"billing_period_end_ts": True}])
def test_mixed_or_invalid_customer_windows_are_rejected(change):
    row = {"worker": "A", "period_start_ts": BASE - 86400, "period_end_ts": BASE,
           "billing_period_start_ts": BASE - 86400, "billing_period_end_ts": BASE}
    with pytest.raises(ValueError):
        cloud_report._customer_period([row, {**row, "worker": "B", **change}], 24)
