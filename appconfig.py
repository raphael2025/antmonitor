#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置装载、默认值补全与校验；网页可改参数(settings.json)与网段(segments.json)的读写。

为什么独立成模块：之前各处直接写 CFG["db"]["retention_days"]、CFG["schedule"][...]，
配置文件少一个段就在运行中抛 KeyError(而且往往是在后台线程里，静默停摆)。
现在统一在装载时把 DEFAULTS 深合并进去，代码里可以放心直接索引。
"""
import copy
import json
import os
import threading

import yaml

import logs

log = logs.get(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))


def _anchor(path):
    """相对路径一律相对 BASE(本文件所在目录)解析，绝对路径原样返回。

    为什么不能用裸相对路径：服务可能被 NSSM/计划任务/其它目录里的脚本拉起，
    此时进程CWD不是项目目录，config.yaml/segments.json/settings.json 会静默
    读不到(全部回退默认值)或写到意料之外的位置，且没有任何报错。
    """
    return path if os.path.isabs(path) else os.path.join(BASE, path)


def _file_from_env(env_key, default_name):
    """取环境变量指定的路径(绝对则直接用，相对则相对 BASE)，未设置则用 BASE 下的默认名。"""
    return _anchor(os.environ.get(env_key) or default_name)

# 全部可配置项的默认值。config.yaml 只需写要覆盖的部分。
DEFAULTS = {
    "scan": {
        "segments": [], "host_start": 1, "host_end": 254,
        "base": "172.16", "lo": 100, "hi": 160,
        "online_timeout": 2.5, "data_timeout": 5.0,
        "workers": 300, "discovery_workers": 300,
        "liveness_gate": True, "gate_timeout": 2.0, "max_pps": 100,
        "reconfirm_enabled": True, "reconfirm_online_timeout": 3.0,
        "reconfirm_data_timeout": 6.0, "reconfirm_passes": 2, "reconfirm_max": 800,
        "fetch_worker": True,
        "container_offline_kick_sec": 86400,
        "roster_retention_days": 7,
        "migrate_max": 200,
        "passwords": [["root", "root"]],
    },
    "schedule": {
        "enabled": True,
        "scan_interval": 300,      # 名册巡检间隔(秒)
        "full_interval": 3600,     # 全网发现间隔(秒)；发现新机/新网段
        "watchdog_minutes": 0,     # 0=自动取 full_interval 的 1.5 倍
        "container_interval": 10,
    },
    "alerts": {
        "enabled": True, "cooldown": 1800, "zero_grace_sec": 600, "reject_pct": 5.0,
        "segment_down_ratio": 0.6, "segment_down_min": 5,
        "low_hashrate_ratio": 0.7,      # 低于同型号中位数的此比例 → 掉算力告警；0=关
        "low_hashrate_rounds": 2,       # 需连续 N 轮成立才报(防单轮读数抖动)
        "low_hashrate_min_peers": 5,    # 同型号在线机少于此数不做基线(样本太少不可信)
        "overheat_c": 95,               # 芯片温 ≥ 此值 → 高温告警；0=关
        "overheat_clear_c": 90,         # 回落到此值以下才消警(滞回，防临界抖动刷屏)
        "container_faults_ignore": [],
        "container_supply_pressure_min": 0,
        "container_return_pressure_min": 0,
    },
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "cloud": {
        "enabled": False, "url": "", "token": "", "site_name": "", "site_type": "air",
        "site_id": "", "interval": 60, "customer_interval": 600,
        "customer_hours": 24, "timeout": 10,
    },
    "control": {
        "enabled": False, "uniplus_password": "", "timeout": 8, "max_batch": 1000,
        "reboot_concurrency": 30, "reboot_delay_sec": 8, "reboot_shuffle": True,
    },
    "auth": {"enabled": True, "secure_cookie": False, "users": []},
    "update": {"auto": False, "branch": "", "check_interval": 3600},
    "db": {
        "path": "miner_monitor.db",
        "retention_days": 3,             # 明细快照保留天数
        "rollup_retention_days": 400,    # 小时级计费聚合保留天数(结算依据，长期保留)
        "checkpoint_minutes": 10,        # 每 N 分钟 wal_checkpoint(TRUNCATE)，防 WAL 无限膨胀
        "vacuum_hour": 4,                # 每天几点做一次 VACUUM 回收空间；-1=关闭
    },
    "server": {
        "host": "0.0.0.0", "port": 8800,
        # 前置 nginx/frp 时填代理的IP(或CIDR)，才会信任 X-Forwarded-For 取真实客户端IP。
        # 留空=不信任任何代理(直连部署的正确选择，防伪造头绕过登录限流)
        "trusted_proxies": [],
    },
    "public_api": {"token": ""},
    "logging": {"level": "INFO", "file": "", "max_mb": 20, "backups": 5},
}


def _merge(defaults, override):
    """深合并：override 缺失的键用 defaults 补齐(只对 dict 递归，list/标量整体覆盖)。"""
    # 不能只浅拷贝顶层：网页热更新会修改 cfg["schedule"] 等嵌套字典，
    # 若它仍与 DEFAULTS 共用对象，后续 load_config() 的默认值会被永久污染。
    out = copy.deepcopy(defaults)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is not None:
            out[k] = copy.deepcopy(v)
    return out


def _validate(cfg):
    """把明显不合法的值纠正到安全范围并记日志(宁可跑起来也不要静默跑歪)。"""
    warn = []

    def clamp(section, key, lo, hi, cast=float):
        try:
            v = cast(cfg[section][key])
        except (TypeError, ValueError):
            warn.append(f"{section}.{key} 非数字，回退默认 {DEFAULTS[section][key]}")
            cfg[section][key] = DEFAULTS[section][key]
            return
        if v < lo or v > hi:
            nv = min(max(v, lo), hi)
            warn.append(f"{section}.{key}={v} 超出 [{lo},{hi}]，已收敛为 {nv}")
            cfg[section][key] = nv
        else:
            cfg[section][key] = v

    clamp("schedule", "scan_interval", 30, 86400, int)
    clamp("schedule", "full_interval", 60, 604800, int)
    clamp("schedule", "container_interval", 5, 3600, int)
    clamp("scan", "max_pps", 0, 5000, int)
    clamp("scan", "workers", 1, 2000, int)
    clamp("scan", "discovery_workers", 1, 2000, int)
    clamp("scan", "host_start", 1, 254, int)
    clamp("scan", "host_end", 1, 254, int)
    clamp("db", "retention_days", 1, 3650, int)
    clamp("db", "rollup_retention_days", 1, 3650, int)
    clamp("db", "checkpoint_minutes", 1, 1440, int)
    clamp("alerts", "cooldown", 0, 86400, int)
    clamp("control", "max_batch", 1, 100000, int)
    # 超时类：0/负数会一路传到 socket.settimeout()/requests(timeout=)，
    # miner_core.tcp_open() 拿到负数直接抛未捕获 ValueError，整轮扫描线程挂掉。
    for k in ("online_timeout", "data_timeout", "gate_timeout",
              "reconfirm_online_timeout", "reconfirm_data_timeout"):
        clamp("scan", k, 0.1, 120)
    clamp("control", "timeout", 0.1, 300)
    clamp("cloud", "timeout", 0.1, 300)
    clamp("cloud", "interval", 5, 86400, int)
    # 批量重启：并发 <1 等于永远不动；delay 负数传给 time.sleep() 抛 ValueError，
    # 会把整批重启中断在半路(前一半重启了后一半没有)。0 秒=不等待，合法。
    clamp("control", "reboot_concurrency", 1, 1000, int)
    clamp("control", "reboot_delay_sec", 0, 3600)
    # 告警阈值：0 在这几项里是"关闭该告警"的约定语义，故下限取 0 而非正数；
    # 比例类限制在 0~1，百分比类限制在 0~100，温度取物理上可能的范围。
    clamp("alerts", "overheat_c", 0, 200)
    clamp("alerts", "overheat_clear_c", 0, 200)
    clamp("alerts", "low_hashrate_ratio", 0, 1)
    clamp("alerts", "segment_down_ratio", 0, 1)
    clamp("alerts", "reject_pct", 0, 100)
    clamp("alerts", "zero_grace_sec", 0, 86400, int)

    if cfg["scan"]["host_end"] < cfg["scan"]["host_start"]:
        warn.append("scan.host_end < host_start，已对调")
        cfg["scan"]["host_start"], cfg["scan"]["host_end"] = \
            cfg["scan"]["host_end"], cfg["scan"]["host_start"]
    # 全网发现比巡检更重，间隔不该更短，否则等于一直在全扫
    if cfg["schedule"]["full_interval"] < cfg["schedule"]["scan_interval"]:
        warn.append("schedule.full_interval < scan_interval，已提升为 scan_interval")
        cfg["schedule"]["full_interval"] = cfg["schedule"]["scan_interval"]
    if cfg["auth"]["enabled"] and not cfg["auth"]["users"]:
        warn.append("auth.enabled=true 但没有配置任何用户，将无人能登录")
    pw = cfg["scan"]["passwords"]
    if not isinstance(pw, list) or not pw:
        warn.append("scan.passwords 非法，回退 [['root','root']]")
        cfg["scan"]["passwords"] = [["root", "root"]]

    for w in warn:
        log.warning("配置校验: %s", w)
    return cfg


CONFIG_FILE = _file_from_env("MINER_CONFIG", "config.yaml")


def load_config(path=None):
    """读 config.yaml → 补默认 → 校验。文件不存在时用全默认(便于测试/首次运行)。

    path 省略时用 MINER_CONFIG 环境变量，再退到 BASE/config.yaml；
    传入的相对路径也按 BASE 解析(不跟随进程CWD)。
    """
    path = _anchor(path) if path else CONFIG_FILE
    raw = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        log.warning("配置文件 %s 不存在，使用全部默认值", path)
    return _validate(_merge(DEFAULTS, raw))


# ---- 网段(网页可编辑，存 segments.json) ----
SEG_FILE = _file_from_env("MINER_SEGMENTS", "segments.json")


def load_segments(cfg):
    """返回 (segments, host_start, host_end)。优先 segments.json，其次 config，最后 lo/hi 回退。"""
    if os.path.exists(SEG_FILE):
        try:
            with open(SEG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            segs = d.get("segments") or []
            if segs:
                return segs, int(d.get("host_start", 1)), int(d.get("host_end", 254))
        except (OSError, ValueError, TypeError) as e:
            log.warning("segments.json 读取失败(%s)，回退 config.yaml", e)
    sc = cfg.get("scan", {})
    segs = sc.get("segments") or []
    hs, he = int(sc.get("host_start", 1)), int(sc.get("host_end", 254))
    if segs:
        return segs, hs, he
    base, lo, hi = sc.get("base", "172.16"), int(sc.get("lo", 100)), int(sc.get("hi", 160))
    return [f"{base}.{c}" for c in range(lo, hi + 1)], hs, he


def save_segments(segments, host_start=1, host_end=254):
    _atomic_json(SEG_FILE, {"segments": segments,
                            "host_start": host_start, "host_end": host_end})


# ---- 网页可改的运行参数(存 settings.json，覆盖 config.yaml) ----
SETTINGS_FILE = _file_from_env("MINER_SETTINGS", "settings.json")

# 可重入锁：保护 settings.json 的"读-改-写"整体过程。
# 两个管理员同时保存面板设置时，若各自先 load 再 save，后写的会把先写的改动
# 整段覆盖掉(lost update)且无人知晓。用 update_settings() 才能真正避免。
# 用 RLock 是因为 update_settings() 在持锁状态下会再调用 load_settings()。
_SETTINGS_LOCK = threading.RLock()


def load_settings():
    with _SETTINGS_LOCK:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError) as e:
                log.warning("settings.json 读取失败，忽略: %s", e)
        return {}


def save_settings(s):
    """整体覆盖写入 settings.json。

    注意：调用方若是"先 load_settings() 再合并再 save_settings()"，两次调用之间
    没有互斥，并发保存仍可能丢更新。这种场景请改用 update_settings()。
    """
    with _SETTINGS_LOCK:
        _atomic_json(SETTINGS_FILE, s)


def update_settings(patch):
    """原子地把 patch 合并进 settings.json(加锁→读→浅合并→写)，返回合并后的完整 dict。

    语义与 save_settings({**load_settings(), **patch}) 相同，但整个读-改-写在同一把
    锁内完成，多个管理员并发保存时不会互相覆盖。新代码请一律用这个函数。
    """
    with _SETTINGS_LOCK:
        merged = {**load_settings(), **(patch or {})}
        _atomic_json(SETTINGS_FILE, merged)
        return merged


def _atomic_json(path, data):
    """先写 .tmp 再 os.replace：断电/崩溃也不会留下半截 JSON 让下次启动读不出配置。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_INT_SETTINGS = ("scan_interval", "full_interval", "container_interval",
                 "max_pps", "discovery_workers")


def apply_settings(cfg, s):
    """把网页保存的参数覆盖进运行中的 cfg（各循环每轮重读，即时生效）。"""
    for k in _INT_SETTINGS:
        if k in s:
            try:
                v = int(s[k])
            except (TypeError, ValueError):
                continue
            if k in ("scan_interval", "full_interval", "container_interval"):
                cfg["schedule"][k] = v
            else:
                cfg["scan"][k] = v
    if isinstance(s.get("cloud"), dict):   # 云端上报(网页可配)
        cfg.setdefault("cloud", {}).update(s["cloud"])
    # settings.json 可被人工编辑，也可能来自旧版本。运行时覆盖必须再次走与
    # config.yaml 相同的边界校验，避免 0 秒间隔造成忙循环或 full < quick。
    return _validate(cfg)
