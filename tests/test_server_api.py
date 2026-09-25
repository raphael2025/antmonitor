# -*- coding: utf-8 -*-
import importlib
from pathlib import Path

from fastapi.testclient import TestClient


def test_server_auth_validation_and_read_endpoints(monkeypatch):
    config = Path(__file__).with_name("server_config.yaml")
    monkeypatch.setenv("MINER_CONFIG", str(config))

    # server 在导入时构造服务；测试配置使用内存库且关闭调度，绝不触碰生产库/设备。
    server = importlib.import_module("server")
    client = TestClient(server.app)

    admin = client.post("/api/login", json={
        "username": "admin", "password": "test-admin-password"})
    assert admin.status_code == 200 and admin.json()["role"] == "admin"
    assert client.get("/api/me").status_code == 200
    assert client.get("/api/summary").json()["scanned"] is False
    assert client.get("/api/miners", params={"limit": 0}).status_code == 422

    # 只提交一个间隔时也必须与当前另一个间隔交叉校验。
    bad_interval = client.post("/api/settings", json={"scan_interval": 4000})
    assert bad_interval.status_code == 400

    called = []

    def fake_run_batch(targets, action, params, cfg, progress=None, before_group=None):
        if before_group:
            before_group([ip for ip, _fw in targets])
        called.extend(targets)
        return ([{"ip": ip, "ok": True, "msg": "ok"} for ip, _fw in targets], "")

    monkeypatch.setattr(server.control, "run_batch", fake_run_batch)
    # 命令目标必须落在已配置网段内；固定成 10.0.0.x 让用例不依赖生产 segments.json
    monkeypatch.setattr(server.appconfig, "load_segments",
                        lambda cfg: (["10.0.0"], 1, 254))

    invalid = client.post("/api/command", json={
        "ips": ["not-an-ip"], "action": "locate", "params": {"on": True}})
    assert invalid.status_code == 400 and called == []

    duplicate = client.post("/api/command", json={
        "ips": ["10.0.0.1", "10.0.0.1"], "action": "locate", "params": {"on": True}})
    assert duplicate.status_code == 200
    assert [ip for ip, _fw in called] == ["10.0.0.1"]

    # 不在已配置网段的地址一律拒绝下发(否则本服务会带着矿机口令去连任意地址)
    called.clear()
    off_seg = client.post("/api/command", json={
        "ips": ["10.0.0.2", "8.8.8.8"], "action": "locate", "params": {"on": True}})
    assert off_seg.status_code == 400 and called == []
    assert off_seg.json()["rejected"] == ["8.8.8.8"]

    # 破坏性命令必须由请求体显式带 confirm:true（前端弹窗补上），后端不接受隐式确认
    no_confirm = client.post("/api/command", json={
        "ips": ["10.0.0.1"], "action": "reboot"})
    assert no_confirm.status_code == 400 and called == []
    assert no_confirm.json()["need_confirm"] is True

    confirmed = client.post("/api/command", json={
        "ips": ["10.0.0.1"], "action": "reboot", "confirm": True})
    assert confirmed.status_code == 200
    assert [ip for ip, _fw in called] == ["10.0.0.1"]
    # 重启成功的机器进入告警静默期；失败的不进(否则真掉线会被静默)
    rebooting = server.SVC._alert_state["rebooting"]
    assert "10.0.0.1" in rebooting

    def half_fail(targets, action, params, cfg, progress=None, before_group=None):
        # 下发时整批已标记(分批重启要跑几分钟，期间扫描不能报前几批掉线)
        assert all(ip in rebooting for ip, _fw in targets)
        return ([{"ip": "10.0.0.3", "ok": True, "msg": "ok"},
                 {"ip": "10.0.0.4", "ok": False, "msg": "连不上矿机"}], "")

    monkeypatch.setattr(server.control, "run_batch", half_fail)
    assert client.post("/api/command", json={
        "ips": ["10.0.0.3", "10.0.0.4"], "action": "reboot", "confirm": True}).status_code == 200
    assert "10.0.0.3" in rebooting and "10.0.0.4" not in rebooting
    monkeypatch.setattr(server.control, "run_batch", fake_run_batch)

    # action 非字符串不应冒泡成 500
    assert client.post("/api/command", json={
        "ips": ["10.0.0.1"], "action": ["reboot"]}).status_code == 400

    assert client.get("/api/public/health").status_code == 401
    assert client.get("/api/public/health", headers={
        "Authorization": "Bearer test-public-token"}).status_code == 200
    # 参数越界不得先于鉴权被 FastAPI 拦成 422（那等于确认端点存在）
    assert client.get("/api/public/miners", params={"limit": 0}).status_code == 401
    assert client.get("/api/public/miners", params={"limit": 0}, headers={
        "Authorization": "Bearer test-public-token"}).status_code == 200

    client.post("/api/logout")
    viewer = client.post("/api/login", json={
        "username": "viewer", "password": "test-viewer-password"})
    assert viewer.status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.post("/api/scan").status_code == 403

    client.close()
