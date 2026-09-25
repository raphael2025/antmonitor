# -*- coding: utf-8 -*-
"""扫描核心与配置：SN 判定直接决定“要不要自动删这台机的记录”，必须锁死。"""
import copy

import appconfig
import miner_core
from conftest import rec


def test_sn_valid_rejects_placeholder_serials():
    """未烧录 SN 的控制板会返回同一个占位串。把它当唯一身份用 → 不同机器被认成
    同一台 → IP 迁移逻辑会误删一大片记录。这是能造成数据丢失的判定。"""
    assert miner_core.sn_valid("BHTX1234567890AB")
    for bad in ("", None, "N/A", "n/a", "unknown", "none", "short",
                "no miner sn stored on board", "ERROR: read failed"):
        assert not miner_core.sn_valid(bad), bad


def test_model_baselines_ignores_zero_hashrate_machines():
    """零算力机本身就是故障对象；算进中位数会把基线拉低，真掉算力的反而"达标"。"""
    records = [rec(f"1.1.1.{i}", hr=100.0) for i in range(1, 6)]
    records += [rec(f"1.1.2.{i}", hr=0.0) for i in range(1, 20)]
    assert miner_core.model_baselines(records)["S19"] == 100.0


def test_model_baselines_separates_models():
    records = [rec("1.1.1.1", hr=100.0, model="S19"),
               rec("1.1.1.2", hr=100.0, model="S19"),
               rec("1.1.1.3", hr=300.0, model="S21"),
               rec("1.1.1.4", hr=310.0, model="S21")]
    b = miner_core.model_baselines(records)
    assert b["S19"] == 100.0 and b["S21"] == 305.0


def test_reject_pct():
    assert miner_core.reject_pct(0, 0) == 0.0
    assert miner_core.reject_pct(99, 1) == 1.0
    assert miner_core.reject_pct(None, None) == 0.0


def test_gh_to_th():
    assert miner_core.gh_to_th(100000) == 100.0
    assert miner_core.gh_to_th("abc") is None
    assert miner_core.gh_to_th(None) is None
    assert miner_core.gh_to_th("Infinity") is None


def test_device_values_are_typed_and_false_strings_do_not_raise_faults():
    assert miner_core._number("12.5") == 12.5
    assert miner_core._number("12.9", integer=True) == 12
    assert miner_core._number("1e100", integer=True) is None
    assert miner_core._number("<img onerror=alert(1)>") is None
    assert miner_core._number(float("nan")) is None
    assert miner_core.antbox_faults({"leakage_fault": "false"}) == []
    assert miner_core.antbox_faults({"leakage_fault": "true"})[0]["flag"] == "leakage_fault"
    assert miner_core.normalize_mac("10-0a-41-96-b6-f2") == "10:0A:41:96:B6:F2"
    assert miner_core.normalize_mac("not-a-mac") == ""


def test_probe_antbox_normalizes_untrusted_controller_payload(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Session:
        def get(self, url, timeout):
            if "minerInfo" in url:
                return Response({"params": {
                    "miner_num": "12", "chip_max_temp": "87.5",
                    "miner_info": {"10.0.0.2": {}},
                }})
            return Response({
                "ok": "true", "method": "coolerState", "params": {
                    "supply_liquid_temp": "<img onerror=alert(1)>",
                    "return_liquid_temp": "45.5",
                    "leakage_fault": "false",
                    "circulating_pump": "false",
                },
            })

    monkeypatch.setattr(miner_core, "get_session", lambda: Session())
    result = miner_core.probe_antbox("10.0.0.1", 1, 1)
    assert result["supply_temp"] is None
    assert result["return_temp"] == 45.5
    assert result["faults"] == []
    assert result["pumps"]["循环泵"] is False
    assert result["miner_num"] == 12
    assert result["chip_max_temp"] == 87.5


def test_probe_uniplus_collects_nested_mac_and_serial(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Session:
        def get(self, url, timeout):
            if url.endswith("/api/v1/info"):
                return Response({
                    "serial": "UNI1234567890",
                    "system": {"network_status": {"mac": "10-0a-41-96-b6-f2"}},
                })
            return Response({"miner": {"miner_type": "S19", "hr_realtime": 100000}})

    monkeypatch.setattr(miner_core, "get_session", lambda: Session())
    result = miner_core.probe_uniplus("10.0.0.1", 1, 1, collect_identity=True)
    assert result["sn"] == "UNI1234567890"
    assert result["mac"] == "10:0A:41:96:B6:F2"


def test_gen_ips_seg_respects_host_range():
    ips = miner_core.gen_ips_seg(["172.16.5", "172.16.6.x"], 10, 12)
    assert ips == ["172.16.5.10", "172.16.5.11", "172.16.5.12",
                   "172.16.6.10", "172.16.6.11", "172.16.6.12"]


def test_config_defaults_fill_missing_sections():
    """配置少写一段不能让后台线程在运行中抛 KeyError 静默停摆。"""
    cfg = appconfig._validate(appconfig._merge(appconfig.DEFAULTS, {"db": {"path": "x.db"}}))
    assert cfg["db"]["path"] == "x.db"
    assert cfg["schedule"]["scan_interval"] == 300
    assert cfg["alerts"]["overheat_c"] == 95


def test_config_validation_clamps_and_orders_intervals():
    cfg = appconfig._validate(appconfig._merge(appconfig.DEFAULTS, {
        "schedule": {"scan_interval": 5, "full_interval": 60},
        "scan": {"host_start": 200, "host_end": 10, "max_pps": 99999},
    }))
    assert cfg["schedule"]["scan_interval"] == 30            # 收敛到下限
    assert cfg["schedule"]["full_interval"] >= cfg["schedule"]["scan_interval"]
    assert cfg["scan"]["host_start"] < cfg["scan"]["host_end"]   # 起止被对调
    assert cfg["scan"]["max_pps"] == 500   # 三层CoPP硬上限，代码层面不可配更高


def test_apply_settings_only_touches_known_keys(cfg):
    appconfig.apply_settings(cfg, {"scan_interval": 600, "full_interval": 7200,
                                   "max_pps": 50, "bogus": 1})
    assert cfg["schedule"]["scan_interval"] == 600
    assert cfg["schedule"]["full_interval"] == 7200
    assert cfg["scan"]["max_pps"] == 50
    assert "bogus" not in cfg


def test_config_instances_do_not_mutate_global_defaults():
    before = copy.deepcopy(appconfig.DEFAULTS)
    cfg = appconfig._merge(appconfig.DEFAULTS, {})
    appconfig.apply_settings(cfg, {"scan_interval": 777, "max_pps": 42})
    assert appconfig.DEFAULTS == before


def test_apply_settings_revalidates_hand_edited_values(cfg):
    """settings.json 可手工编辑；非法间隔不能让调度器进入忙循环。"""
    appconfig.apply_settings(cfg, {"scan_interval": 0, "full_interval": 10,
                                   "container_interval": -1, "discovery_workers": 0})
    assert cfg["schedule"]["scan_interval"] == 30
    assert cfg["schedule"]["full_interval"] == 60
    assert cfg["schedule"]["container_interval"] == 5
    # 0 并发不夹成 1(那等于扫描停摆)，当作没设置、保留原值
    assert cfg["scan"]["discovery_workers"] == appconfig.DEFAULTS["scan"]["discovery_workers"]


def test_scan_overall_timeout_returns_unknown_instead_of_raising(monkeypatch):
    """整体超时必须走 unknown 兜底返回已探到的结果。Python 3.8~3.10 上
    concurrent.futures.TimeoutError 不是内置 TimeoutError，只写 except TimeoutError
    会让异常逃出去，整轮几千台的结果全部丢弃。"""
    import time as _t
    import miner_core
    monkeypatch.setattr(miner_core, "probe",
                        lambda ip, cfg, limiter=None: (_t.sleep(2), miner_core._blank(ip, "online"))[1])
    orig = miner_core.as_completed
    monkeypatch.setattr(miner_core, "as_completed", lambda fs, timeout=None: orig(fs, timeout=0.3))
    out = miner_core.scan(["10.0.0.1", "10.0.0.2"], {"workers": 2})
    assert sorted(r["status"] for r in out) == ["unknown", "unknown"]


def test_scan_containers_overall_timeout_does_not_raise(monkeypatch):
    import time as _t
    import miner_core
    monkeypatch.setattr(miner_core, "probe_antbox", lambda ip, a, b: _t.sleep(2))
    orig = miner_core.as_completed
    monkeypatch.setattr(miner_core, "as_completed", lambda fs, timeout=None: orig(fs, timeout=0.3))
    assert miner_core.scan_containers(["10.0.0.9"], {}) == []


class _R:
    def __init__(self, payload, code=200):
        import json as _json
        self._p, self.status_code = payload, code
        self.text = payload.get("sn", "") if isinstance(payload, dict) and "sn" in payload \
            else _json.dumps(payload)

    def json(self):
        return self._p


class _S:
    """按 URL 片段返回预设 JSON 的假会话。"""
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kw):
        for frag, payload in self.routes.items():
            if frag in url:
                return _R(payload)
        raise AssertionError(url)


def test_antbox_leak_fault_survives_malformed_minerinfo(monkeypatch):
    """minerInfo 返回怪结构(params 是列表)时，已解析好的漏液故障不能随整条记录丢掉。"""
    import miner_core
    monkeypatch.setattr(miner_core, "get_session", lambda: _S({
        "coolerState": {"ok": True, "method": "coolerState",
                        "params": {"leakage_fault": 1, "supply_liquid_temp": 30}},
        "minerInfo": {"params": ["n/a"]}}))
    rec = miner_core.probe_antbox("10.0.0.9", 1, 1)
    assert rec is not None
    assert [f["flag"] for f in rec["faults"]] == ["leakage_fault"]


def test_antbox_malformed_coolerstate_counts_as_not_collected(monkeypatch):
    """coolerState 的 params 不是字典：按"没采到"(交给连续未采到→控制器离线告警)，
    绝不能返回一条故障位全空的记录——那会把正在报的漏液告警当成已恢复消掉。"""
    import miner_core
    monkeypatch.setattr(miner_core, "get_session", lambda: _S({
        "coolerState": {"ok": True, "method": "coolerState", "params": ["garbage"]}}))
    assert miner_core.probe_antbox("10.0.0.9", 1, 1) is None


def test_stock_miner_with_malformed_stats_stays_online(monkeypatch):
    """stats.cgi 回了结构不对的 JSON：机器明明在线，不能被当成离线误报掉线。"""
    import miner_core
    for bad in ([1, 2], {"INFO": {}, "STATS": "x"}, {"STATS": [{"chain": ["bad"]}]}):
        monkeypatch.setattr(miner_core, "get_session", lambda b=bad: _S({
            "get_sn": {"sn": "SN1234567890"}, "stats.cgi": b}))
        monkeypatch.setattr(miner_core, "tcp_open", lambda *a, **k: True)
        rec = miner_core.probe_stock("10.0.0.1", 1, 1, [("root", "root")])
        assert rec is not None and rec["status"] == "online", bad
