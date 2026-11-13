# -*- coding: utf-8 -*-
import importlib
from pathlib import Path

from fastapi.testclient import TestClient

import db


def test_list_reboot_batches_groups_by_user_and_gap(conn):
    t0 = 1_700_000_000
    db.log_commands(conn, "admin@10.0.0.9", "reboot", [
        {"ip": "10.1.0.1", "ok": True, "msg": "ok"},
        {"ip": "10.1.0.2", "ok": False, "msg": "fail"},
    ], ts=t0)
    # 同操作者、间隔内 → 同一批次
    db.log_commands(conn, "admin@10.0.0.9", "reboot", [
        {"ip": "10.1.0.3", "ok": True, "msg": "ok"},
    ], ts=t0 + 60)
    # 自动重启单独一批
    db.log_commands(conn, "system@auto-zero-reboot", "reboot", [
        {"ip": "10.1.0.8", "ok": True, "msg": "ok"},
        {"ip": "10.1.0.9", "ok": True, "msg": "ok"},
    ], ts=t0 + 120)
    # 同操作者但超过 gap → 新批次
    db.log_commands(conn, "admin@10.0.0.9", "reboot", [
        {"ip": "10.1.0.4", "ok": True, "msg": "ok"},
    ], ts=t0 + 600)

    batches = db.list_reboot_batches(conn, limit=10, gap_sec=180)
    assert len(batches) == 3
    assert batches[0]["user"] == "admin@10.0.0.9"
    assert batches[0]["total"] == 1 and batches[0]["success"] == 1
    assert batches[0]["source"] == "manual"
    assert batches[1]["source"] == "auto"
    assert (batches[1]["total"], batches[1]["success"]) == (2, 2)
    assert batches[2]["user"] == "admin@10.0.0.9"
    assert (batches[2]["total"], batches[2]["success"], batches[2]["failed"]) == (3, 2, 1)

    detail = db.list_reboot_batch_detail(
        conn, batches[2]["user"], batches[2]["id_from"], batches[2]["id_to"])
    assert [r["ip"] for r in detail] == ["10.1.0.1", "10.1.0.2", "10.1.0.3"]


def test_reboot_history_api_ops_only(monkeypatch):
    config = Path(__file__).with_name("server_config.yaml")
    monkeypatch.setenv("MINER_CONFIG", str(config))
    server = importlib.import_module("server")
    client = TestClient(server.app)

    denied = client.get("/api/command/reboot-history")
    assert denied.status_code in (401, 403)

    login = client.post("/api/login", json={
        "username": "admin", "password": "test-admin-password"})
    assert login.status_code == 200

    db.log_commands(server.SVC.conn, "system@auto-zero-reboot", "reboot", [
        {"ip": "10.0.0.11", "ok": True, "msg": "reboot ok"},
        {"ip": "10.0.0.12", "ok": False, "msg": "timeout"},
    ])
    hist = client.get("/api/command/reboot-history", params={"limit": 20})
    assert hist.status_code == 200
    batches = hist.json()["batches"]
    assert batches
    b = next(x for x in batches if x["source"] == "auto")
    assert b["total"] == 2 and b["success"] == 1 and b["failed"] == 1

    detail = client.get("/api/command/reboot-history/detail", params={
        "user": b["user"], "id_from": b["id_from"], "id_to": b["id_to"]})
    assert detail.status_code == 200
    items = detail.json()["items"]
    assert {x["ip"] for x in items} == {"10.0.0.11", "10.0.0.12"}
