"""Customer ownership follows the first configured pool, not its availability.

All pool and HTTP replies are synthetic; these tests never contact a miner.
"""
import itertools

import pytest
import requests

import miner_core


IP = "192.0.2.10"


def pool(slot, user, status="Alive", priority=None, accepted=100, rejected=0):
    return {"POOL": slot, "Priority": slot if priority is None else priority,
            "URL": "stratum+tcp://shared.example:3333", "User": user,
            "Status": status, "Accepted": accepted, "Rejected": rejected, "Stale": 0}


def fetch(monkeypatch, pools):
    monkeypatch.setattr(miner_core, "_cgminer", lambda *args: {"POOLS": pools})
    return miner_core.fetch_pool(IP, 1)


def test_failover_keeps_customer_but_share_counters_follow_available_pools(monkeypatch):
    primary = pool(0, "customer_A.rig1", accepted=1000)
    backup = pool(1, "backup_alias.rig1", "Dead", accepted=50, rejected=9)
    before = fetch(monkeypatch, [primary, backup])
    primary["Status"], backup["Status"] = "Dead", "Alive"
    during = fetch(monkeypatch, [primary, backup])
    primary["Status"], backup["Status"] = "Alive", "Dead"
    after = fetch(monkeypatch, [primary, backup])

    assert [row["worker"] for row in (before, during, after)] == ["customer_A"] * 3
    assert (before["accepted"], before["rejected"]) == (1000, 0)
    assert (during["accepted"], during["rejected"]) == (50, 9)
    assert before["counter_basis"] != during["counter_basis"]
    assert before["counter_basis"] == after["counter_basis"]
    assert all(row["pools"] == [primary["URL"], backup["URL"]]
               for row in (before, during, after))


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_pool_slot_survives_response_reordering_and_manual_priority_switch(monkeypatch, order):
    # switchpool changes Priority. It does not move the configured POOL=0 slot.
    pools = [pool(0, "customer_A.rig1", "Dead", priority=2),
             pool(1, "backup_alias.rig1", priority=0),
             pool(2, "third_alias.rig1", priority=1)]
    result = fetch(monkeypatch, [pools[i] for i in order])
    assert result["worker"] == "customer_A"
    assert result["accepted"] == 200


def test_lowest_configured_slot_wins_when_pool_ids_do_not_start_at_zero(monkeypatch):
    result = fetch(monkeypatch, [pool(8, "backup.rig1", priority=0),
                                 pool(4, "customer_A.rig1", "Dead", priority=1)])
    assert result["worker"] == "customer_A"


def test_missing_slots_fall_back_to_valid_priority_not_alive_status(monkeypatch):
    primary = pool(0, "customer_A.rig1", "Dead")
    backup = pool(1, "backup.rig1")
    del primary["POOL"], backup["POOL"]
    primary["Priority"] = "0"
    result = fetch(monkeypatch, [backup, primary])
    assert result["worker"] == "customer_A"


@pytest.mark.parametrize("invalid", [None, True, -1, 0.5, "bad", float("inf"), {}, []])
def test_invalid_slots_and_priorities_fall_back_to_configuration_order(monkeypatch, invalid):
    primary = pool(invalid, "customer_A.rig1", "Dead", priority=invalid)
    backup = pool(invalid, "backup.rig1", priority=invalid)
    result = fetch(monkeypatch, [primary, backup])
    assert result is not None
    assert result["worker"] == "customer_A"
    assert result["accepted"] == 100


def test_explicit_slot_zero_is_not_displaced_by_unnumbered_backup(monkeypatch):
    backup = pool(1, "backup.rig1", priority=0)
    del backup["POOL"]
    result = fetch(monkeypatch, [backup, pool(0, "customer_A.rig1", "Dead", priority=1)])
    assert result["worker"] == "customer_A"


def test_partial_slot_information_does_not_promote_numbered_backup(monkeypatch):
    primary = pool(0, "customer_A.rig1", "Dead")
    del primary["POOL"]
    result = fetch(monkeypatch, [pool(1, "backup.rig1"), primary])
    assert result["worker"] == "customer_A"


@pytest.mark.parametrize("user", [None, "", "  ", ".rig1", False, 123, {}, []])
def test_missing_primary_user_never_falls_through_to_backup_alias(monkeypatch, user):
    result = fetch(monkeypatch, [pool(0, user, "Dead"), pool(1, "backup_alias.rig1")])
    assert result["worker"] is None
    assert result["accepted"] == 100


def test_lowercase_firmware_fields_keep_primary_account(monkeypatch):
    rows = [{key.lower(): value for key, value in item.items()} for item in
            [pool(1, "backup.rig1", priority=0), pool(0, "customer_A.rig1", "Dead", priority=1)]]
    assert fetch(monkeypatch, rows)["worker"] == "customer_A"


def test_shared_backup_pool_and_worker_never_merge_distinct_primary_customers(monkeypatch):
    workers = []
    for customer in ("customer_A", "customer_B"):
        result = fetch(monkeypatch, [pool(0, customer + ".rig1", "Dead"),
                                     pool(1, "shared_backup.rig1")])
        workers.append(result["worker"])
    assert workers == ["customer_A", "customer_B"]


def test_real_primary_account_change_is_not_frozen_to_previous_customer(monkeypatch):
    primary = pool(0, "customer_A.rig1")
    backup = pool(1, "backup.rig1")
    assert fetch(monkeypatch, [primary, backup])["worker"] == "customer_A"
    primary["User"] = "customer_B.rig1"
    assert fetch(monkeypatch, [primary, backup])["worker"] == "customer_B"


class Response:
    status_code = 200

    def __init__(self, payload=None, text=""):
        self.payload, self.text = payload, text

    def json(self):
        return self.payload


@pytest.mark.parametrize("driver", ["stock", "uniplus", "4028", "stock_without_hashrate"])
def test_primary_customer_survives_each_actual_probe_path(monkeypatch, driver):
    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/get_sn"):
                if driver.startswith("stock"):
                    return Response(text="REALSN0123456")
                raise requests.RequestException("synthetic unsupported endpoint")
            if url.endswith("/stats.cgi"):
                return Response({"INFO": {"type": "S19"}, "STATS": [{
                    "rate_5s": 293_000 if driver == "stock" else None,
                    "rate_avg": 291_000, "watt": 6000}]})
            if url.endswith("/api/v1/summary") and driver == "uniplus":
                return Response({"miner": {"hr_realtime": 293_000, "hr_average": 291_000,
                                            "power_consumption": 6000}})
            return Response({})

    pools = [pool(0, "customer_A.rig1", "Dead"), pool(1, "backup_alias.rig1")]
    def cgminer(ip, command, timeout):
        if command == "pools":
            return {"POOLS": pools}
        assert command == "summary"
        return {"SUMMARY": [{"MHS 5s": 293_000_000, "MHS av": 291_000_000,
                              "Elapsed": 3600}]}

    monkeypatch.setattr(miner_core, "get_session", Session)
    monkeypatch.setattr(miner_core, "_cgminer", cgminer)
    monkeypatch.setattr(miner_core, "probe_antbox", lambda *args: None)
    monkeypatch.setattr(miner_core, "tcp_open", lambda *args: True)
    result = miner_core.probe(IP, {"alive_probe": True})
    assert result["status"] == "online"
    assert result["worker"] == "customer_A"
    assert result["hr_rt"] == 293
