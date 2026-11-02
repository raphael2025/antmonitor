"""Reconcile live totals across APIs, including missing readings and sample age."""
import importlib
import time
from pathlib import Path

import pytest

import data_metrics
import db
from conftest import insert_scan, rec


def test_unknown_machine_keeps_inventory_and_invalidates_full_totals():
    s = data_metrics.aggregate([rec("10.0.0.1", hr=293),
                                rec("10.0.0.2", status="unknown", worker="cust1")])
    assert (s["online"], s["offline"], s["unknown"], s["total"]) == (1, 0, 1, 2)
    assert s["total_hashrate_th"] is None and s["known_hashrate_ths"] == 293
    assert s["total_power_kw"] is None and s["known_power_kw"] == 3
    assert s["avg_hashrate_th"] is None and s["avg_efficiency"] is None
    assert not s["hashrate_complete"] and not s["power_complete"]


def test_fleet_efficiency_is_ratio_of_total_watts_to_total_hashrate():
    s = data_metrics.aggregate([rec("10.0.0.1", hr=100, power=2000, eff=20),
                                rec("10.0.0.2", hr=300, power=12000, eff=40)])
    assert s["total_hashrate_th"] == 400 and s["total_power_kw"] == 14
    assert s["avg_efficiency"] == 35  # (2000+12000)/(100+300), not mean(20,40)
    assert s["known_efficiency"] == 35 and s["efficiency_complete"]
    assert s["efficiency_known_machines"] == 2


def test_partial_efficiency_uses_only_matched_power_and_hashrate_readings():
    s = data_metrics.aggregate([
        rec("10.0.0.1", hr=100, power=2000),
        rec("10.0.0.2", hr=300, power=None),
        rec("10.0.0.3", hr=None, power=1000),
    ])
    assert s["known_power_kw"] == 3 and s["power_known_machines"] == 2
    assert s["known_hashrate_ths"] == 400
    assert s["total_power_kw"] is None and s["avg_efficiency"] is None
    # 3000/400 would combine different machine sets and falsely show 7.5 J/TH.
    assert s["known_efficiency"] == 20
    assert s["efficiency_known_machines"] == 1 and not s["efficiency_complete"]
    disjoint = data_metrics.aggregate([rec("10.0.0.1", hr=100, power=None),
                                       rec("10.0.0.2", hr=None, power=2000)])
    assert disjoint["known_efficiency"] is None and disjoint["efficiency_known_machines"] == 0


def test_partial_efficiency_includes_idle_power_and_preserves_measured_zero():
    s = data_metrics.aggregate([rec("10.0.0.1", hr=100, power=2000),
                                rec("10.0.0.2", hr=0, power=1000),
                                rec("10.0.0.3", status="unknown")])
    assert s["known_efficiency"] == 30  # Idle power still costs energy.
    assert s["efficiency_known_machines"] == 2 and not s["efficiency_complete"]
    zero = data_metrics.aggregate([rec("10.0.0.1", hr=100, power=0),
                                   rec("10.0.0.2", status="unknown")])
    assert zero["known_power_kw"] == zero["known_efficiency"] == 0
    stopped = data_metrics.aggregate([rec("10.0.0.1", hr=0, power=1000)])
    assert stopped["known_power_kw"] == 1 and stopped["known_efficiency"] is None
    assert stopped["efficiency_complete"] and stopped["efficiency_known_machines"] == 1


def test_current_miner_efficiency_uses_realtime_readings_without_changing_raw():
    raw = rec("10.0.0.1", hr=313.34, power=5539, eff=18.28)
    assert data_metrics.live_record(raw)["eff"] == 17.68
    assert raw["eff"] == 18.28
    assert data_metrics.live_record(raw, fresh=False)["eff"] is None
    for values in ({"hr_rt": 0}, {"hr_rt": None}, {"power": None}, {"status": "unknown"}):
        assert data_metrics.live_record(dict(raw, **values))["eff"] is None
    assert data_metrics.live_record(dict(raw, power=0))["eff"] == 0
    extreme = dict(raw, power=1e100, hr_rt=1e-300)
    assert data_metrics.live_record(extreme)["eff"] is None
    assert data_metrics.aggregate([extreme])["known_efficiency"] is None


def test_zero_is_valid_and_power_completeness_is_independent():
    s = data_metrics.aggregate([rec("10.0.0.1", hr=293), rec("10.0.0.2", hr=0, power=None)])
    assert s["avg_hashrate_th"] == 146.5
    assert s["with_hashrate"] == 2 and s["zero_hashrate"] == 1
    assert s["hashrate_complete"] and not s["power_complete"]
    assert s["total_power_kw"] is None and s["avg_efficiency"] is None


@pytest.mark.parametrize("bad", [True, False, -1, float("nan"), float("inf"), "bad"])
def test_invalid_readings_remain_unknown(bad):
    s = data_metrics.aggregate([rec("10.0.0.1", hr=bad, power=bad)])
    assert s["total_hashrate_th"] is None and s["known_hashrate_ths"] is None
    assert s["total_power_kw"] is None and s["known_power_kw"] is None


def test_offline_and_unknown_are_distinct_and_raw_evidence_survives():
    raw = rec("10.0.0.1", hr=293)
    view = data_metrics.live_record(raw, fresh=False)
    assert view["status"] == "unknown" and view["hr_rt"] is None and view["data_stale"]
    assert raw["status"] == "online" and raw["hr_rt"] == 293
    offline = data_metrics.aggregate([rec("10.0.0.2", status="offline")])
    assert offline["total_hashrate_th"] == offline["total_power_kw"] == 0
    assert offline["avg_hashrate_th"] is None and offline["avg_efficiency"] is None


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("MINER_CONFIG", str(Path(__file__).with_name("server_config.yaml")))
    server = importlib.import_module("server")
    server._cache.clear()
    monkeypatch.setattr(server, "_check_public_token", lambda request: None)
    monkeypatch.setattr(server.appconfig, "load_segments", lambda cfg: (["10.0.0"], 1, 254))
    return server


def publish(api, monkeypatch, records):
    stamp = int(time.time())
    scan = {"scan_id": 1, "ts": stamp, "kind": "quick"}
    monkeypatch.setattr(api.SVC, "latest", lambda: (scan, records))
    return stamp


def test_complete_site_customer_rack_and_public_totals_reconcile(api, monkeypatch):
    records = [rec("10.0.0.1", worker="A", hr=293),
               rec("10.0.0.2", worker="B", hr=300),
               rec("10.0.0.3", worker="B", status="offline")]
    publish(api, monkeypatch, records)
    summary = api.api_summary()
    public = api._build_public_summary()
    miners = api.api_miners(limit=50000)
    pub_miners = api.api_public_miners(None)
    workers = api.api_workers()["workers"]
    racks = api.api_racks()["racks"]
    assert summary["total_hashrate_th"] == public["total_hashrate_ths"] == 593
    assert miners["agg"]["total_hr"] == pub_miners["total_hashrate_ths"] == 593
    assert sum(w["hashrate"] for w in workers) == sum(r["hashrate"] for r in racks) == 593
    assert sum(r["hashrate_ths"] for r in public["customer_hashrates"]) == 593
    assert summary["total"] == sum(w["total"] for w in workers) == 3
    assert public["hashrate_complete"] and public["power_complete"]


def test_antbox_distribution_power_is_not_added_again_to_miner_totals(api, monkeypatch):
    # The PDU measures the same 3 kW + 2 kW miner load. Adding it again would
    # falsely report 10 kW; only the independent container view shows that meter.
    records = [rec("10.0.0.1", power=3000), rec("10.0.0.2", power=2000)]
    stamp = publish(api, monkeypatch, records)
    monkeypatch.setattr(db, "get_containers", lambda conn: [
        {"ip": "10.0.0.250", "online": 1, "data_status": "online", "data_complete": 1,
         "measured_ts": stamp, "power1": 3000, "power2": 2000, "faults": []},
    ])
    local = api.api_summary()
    public = api._build_public_summary()
    assert local["total_power_kw"] == public["total_power_kw"] == 5
    assert local["containers"] == public["containers"] == 1
    assert api.api_miners(limit=50000)["agg"]["total_power"] == 5000
    assert api.api_public_miners(None)["total_power_w"] == 5000
    box = api.api_containers()["containers"][0]
    assert box["power1"] + box["power2"] == 5000


def test_summary_filtered_totals_and_current_rows_expose_consistent_available_values(api, monkeypatch):
    rows = [rec("10.0.0.1", worker="A", hr=100, power=2000, eff=99),
            rec("10.0.0.2", worker="A", hr=300, power=None),
            rec("10.0.0.3", worker="B", hr=None, power=1000)]
    publish(api, monkeypatch, rows)
    summary = api.api_summary()
    assert summary["total_power_kw"] is None and summary["avg_efficiency"] is None
    assert summary["known_power_kw"] == 3 and summary["known_efficiency"] == 20
    assert summary["efficiency_known_machines"] == 1 and not summary["efficiency_complete"]
    miners = api.api_miners(limit=50000)
    assert miners["agg"]["known_power"] == 3000  # List API remains in watts.
    assert miners["agg"]["power_known_machines"] == 2
    assert miners["agg"]["known_efficiency"] == 20
    filtered = api.api_miners(q="A", limit=50000)["agg"]
    assert filtered["count"] == 2 and filtered["known_power"] == 2000
    assert filtered["known_efficiency"] == 20 and filtered["efficiency_known_machines"] == 1
    assert next(r for r in miners["miners"] if r["ip"] == "10.0.0.1")["eff"] == 20
    assert api.api_miner("10.0.0.1")["current"]["eff"] == 20
    assert api.api_public_miners(None)["miners"][0]["efficiency_jth"] == 20
    assert rows[0]["eff"] == 99
    # Quality and full-total fields sent to the cloud retain their existing meaning.
    public = api._build_public_summary()
    assert public["total_power_kw"] is None and public["known_power_kw"] == 3
    assert public["power_complete"] is False


def test_public_missing_sample_never_turns_into_zero(api, monkeypatch):
    publish(api, monkeypatch, [rec("10.0.0.1", hr=None, power=None, temp=None)])
    summary = api._build_public_summary()
    miner = api.api_public_miners(None)["miners"][0]
    assert summary["total_hashrate_ths"] is None and summary["total_power_kw"] is None
    assert miner["hashrate_ths"] is None and miner["power_w"] is None and miner["temp_c"] is None
    assert summary["miners"][0]["hashrate_ths"] is None


def test_long_discovery_does_not_make_old_sample_fresh(api, monkeypatch):
    raw = rec("10.0.0.1", hr=293, measured_ts=int(time.time()) - 1800)
    publish(api, monkeypatch, [raw])
    current = api.api_public_miners(None)["miners"][0]
    summary = api.api_summary()
    assert current["hashrate_ths"] is None and current["status"] == "unknown"
    assert current["data_stale"] and summary["unknown"] == 1
    assert summary["total_hashrate_th"] is None and summary["known_hashrate_ths"] is None
    assert raw["hr_rt"] == 293 and raw["status"] == "online"


def test_public_local_and_push_reports_share_exact_window_without_flooring_history(api, conn, monkeypatch):
    base = 1_800_000_000 // 3600 * 3600
    end = base + 3 * 3600
    monkeypatch.setattr(db.time, "time", lambda: end + 47)
    monkeypatch.setattr(api.SVC, "conn", conn)
    # 2.9 measured hours: the former integer-hours clamp silently discarded 0.9h.
    for stamp in [*range(base + 360, end, 300), end]:
        insert_scan(conn, stamp, "quick", [rec("10.0.0.1", worker="A", hr=293)])
    local = api.api_report_customers(hours=24)
    public = api.api_public_customers(None, hours=24)
    callbacks = []
    monkeypatch.setattr(api.cloud_report, "start", lambda cfg, summary, customers: callbacks.append(customers))
    api._start_cloud_report()
    push = callbacks[0](24)
    assert local == push
    assert local["customers"] == public["customers"]
    row = local["customers"][0]
    assert row["billing_hours"] == row["hashrate_hours"] == 24
    assert row["period_end_ts"] == row["billing_period_end_ts"] == end
    assert row["period_start_ts"] == end - 24 * 3600
    assert row["power_observed_hours"] == pytest.approx(2.9)
    assert row["power_kwh"] == pytest.approx(8.7)
    assert row["delivered_th_h"] == pytest.approx(849.7)
    assert row["avg_hashrate_ths"] == 293
    csv = api.api_report_customers(hours=24, format="csv").body.decode("utf-8-sig")
    assert "功率有效时长(小时)" in csv and "旧口径耗电(kWh,未验证)" in csv
    assert "8.7" in csv and "849.7" in csv


def test_empty_customer_packet_still_proves_the_queried_window(api, conn, monkeypatch):
    end = 1_800_000_000 // 600 * 600
    monkeypatch.setattr(db.time, "time", lambda: end + 30)
    monkeypatch.setattr(api.SVC, "conn", conn)
    packet = api._customer_report_packet(24)
    assert packet["customers"] == []
    assert packet["period_end_ts"] == packet["billing_period_end_ts"] == end
    assert packet["period_start_ts"] == packet["billing_period_start_ts"] == end - 86400


def test_report_cache_distinguishes_real_endpoints_and_rejects_future(api, conn, monkeypatch):
    base = 1_800_000_000 // 3600 * 3600
    monkeypatch.setattr(db.time, "time", lambda: base + 3600)
    monkeypatch.setattr(api.SVC, "conn", conn)
    for stamp in range(base, base + 3601, 300):
        insert_scan(conn, stamp, "quick", [rec("10.0.0.1", worker="A", hr=293)])
    first = api.api_report_customers(hours=24, until_ts=base + 1800)
    last = api.api_report_customers(hours=24, until_ts=base + 3600)
    assert first["customers"][0]["power_kwh"] == pytest.approx(1.5)
    assert last["customers"][0]["power_kwh"] == pytest.approx(3)
    assert first["period_end_ts"] != last["period_end_ts"]
    for end in (base + 3601, True, float("nan"), 0):
        with pytest.raises(api.HTTPException) as exc:
            api.api_report_customers(until_ts=end)
        assert exc.value.status_code == 400


def test_summary_time_uses_oldest_machine_and_unknown_has_no_trusted_time(api, monkeypatch):
    now = int(time.time())
    publish(api, monkeypatch, [rec("10.0.0.1", measured_ts=now - 800),
                              rec("10.0.0.2", measured_ts=now - 10)])
    assert api._build_public_summary()["measurement_ts"] == now - 800
    publish(api, monkeypatch, [rec("10.0.0.1", measured_ts=now - 901)])
    assert api._build_public_summary()["measurement_ts"] is None
    publish(api, monkeypatch, [])
    assert api._build_public_summary()["measurement_ts"] == now


def test_container_unknown_and_expired_evidence_are_not_healthy_or_offline(api, monkeypatch):
    now = int(time.time())
    raw = [
        {"ip": "10.0.0.1", "online": 1, "data_status": "online", "data_complete": 1,
         "measured_ts": now, "faults": []},
        {"ip": "10.0.0.2", "online": 1, "data_status": "unknown", "data_complete": 0,
         "measured_ts": now, "faults": [{"flag": "leakage_fault", "observed": False}]},
        {"ip": "10.0.0.3", "online": 0, "data_status": "offline", "data_complete": 0,
         "measured_ts": now, "faults": []},
        {"ip": "10.0.0.4", "online": 1, "data_status": "online", "data_complete": 1,
         "measured_ts": now - 3600, "faults": []},
    ]
    monkeypatch.setattr(db, "get_containers", lambda conn: raw)
    result = api.api_containers()
    assert (result["count"], result["unknown"], result["offline"], result["faulty"]) == (4, 2, 1, 0)
    assert result["containers"][1]["faults"][0]["observed"] is False
    assert result["containers"][3]["data_status"] == "unknown"
    assert raw[3]["data_complete"] == 1  # Last-known evidence is never rewritten by a read.
