#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控服务：扫描编排 + 后台定时巡检线程。"""
import os
import json
import time
import threading

import yaml

import db
import alerts
import miner_core

# #region agent log
_DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug-2be346.log")
def _dbg(hypothesis_id, location, message, data=None, run_id="pre-fix"):
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as _f:
            json.dump({"sessionId": "2be346", "hypothesisId": hypothesis_id, "location": location,
                       "message": message, "data": data or {}, "timestamp": int(time.time() * 1000),
                       "runId": run_id}, _f, ensure_ascii=False)
            _f.write("\n")
    except Exception:
        pass
# #endregion


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


SEG_FILE = os.environ.get("MINER_SEGMENTS", "segments.json")


def load_segments(cfg):
    """返回 (segments, host_start, host_end)。优先 segments.json，其次 config，最后 lo/hi 回退。"""
    if os.path.exists(SEG_FILE):
        try:
            with open(SEG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            segs = d.get("segments") or []
            if segs:
                return segs, int(d.get("host_start", 1)), int(d.get("host_end", 254))
        except Exception:
            pass
    sc = cfg.get("scan", {})
    segs = sc.get("segments") or []
    hs, he = int(sc.get("host_start", 1)), int(sc.get("host_end", 254))
    if segs:
        return segs, hs, he
    base, lo, hi = sc.get("base", "172.16"), int(sc.get("lo", 100)), int(sc.get("hi", 160))
    return [f"{base}.{c}" for c in range(lo, hi + 1)], hs, he


def save_segments(segments, host_start=1, host_end=254):
    data = {"segments": segments, "host_start": host_start, "host_end": host_end}
    tmp = SEG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SEG_FILE)


SETTINGS_FILE = os.environ.get("MINER_SETTINGS", "settings.json")


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def apply_settings(cfg, s):
    """把网页可调参数覆盖进运行中的 cfg（扫描循环每轮读取，即时生效）。"""
    if "scan_interval" in s:
        cfg["schedule"]["scan_interval"] = int(s["scan_interval"])
    if "container_interval" in s:
        cfg["schedule"]["container_interval"] = int(s["container_interval"])
    if "max_pps" in s:
        cfg["scan"]["max_pps"] = int(s["max_pps"])
    if "discovery_workers" in s:
        cfg["scan"]["discovery_workers"] = int(s["discovery_workers"])


def save_settings(s):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


class MonitorService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = db.init_db(cfg["db"]["path"])
        db.resolve_types_except(self.conn, ("offline", "zero", "segment_down", "reject", "stalled"))
        self._scan_lock = threading.Lock()
        self.progress = {"running": False, "done": 0, "total": 0,
                         "kind": "", "started": 0, "last_finished": 0}
        self._stop = threading.Event()
        self._wake = threading.Event()   # 改间隔后唤醒调度循环，使新间隔即时生效(不必等旧 sleep 走完)
        self._thread = None
        self._watchdog_thread = None
        self._watchdog_beat = 0
        self._recover_lock = threading.Lock()
        self._container_lock = threading.Lock()
        self._container_miss = {}     # 集装箱连续未采到次数(二次确认离线，防一次丢包误报)
        self._container_miss_n = 2    # 连续 N 轮(≈N×container_interval)未采到才判离线
        self.notify = None   # 可选回调：扫描完成/告警时推送(WS)

    def wake(self):
        """外部(如改设置后)唤醒调度循环，让其立即重读间隔。"""
        self._wake.set()

    def _ensure_watchdog(self):
        """看门狗自身也要能自愈——它死了就没人检测扫描停摆了。"""
        if self._stop.is_set():
            return
        if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
            print("watchdog: 看门狗线程已死，自动重启")
            # #region agent log
            _dbg("H1", "service.py:_ensure_watchdog", "restart_watchdog", {}, run_id="post-fix")
            # #endregion
            self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="watchdog")
            self._watchdog_thread.start()

    def _force_recover(self, reason):
        """停滞/线程死亡时强制恢复：换新锁、关连接、重启扫描。"""
        if not self._recover_lock.acquire(blocking=False):
            return
        try:
            # #region agent log
            _dbg("FIX", "service.py:_force_recover", "recover_start", {"reason": reason}, run_id="post-fix")
            # #endregion
            print(f"force_recover: {reason}")
            miner_core.close_sessions()
            self._scan_lock = threading.Lock()   # 卡死线程占着旧锁，换新锁让新扫描能跑
            self.progress["running"] = False
            self._wake.set()
            thread_alive = self._thread is not None and self._thread.is_alive()
            beat = self.progress.get("loop_beat", 0)
            beat_stale = bool(beat) and (time.time() - beat > 120)
            if not thread_alive:
                print("force_recover: 重启扫描调度线程")
                # #region agent log
                _dbg("FIX", "service.py:_force_recover", "restart_loop", {}, run_id="post-fix")
                # #endregion
                self._thread = threading.Thread(target=self._loop, daemon=True, name="scan-loop")
                self._thread.start()
            elif beat_stale:
                print("force_recover: 调度线程疑似卡死，发起恢复扫描")
                # #region agent log
                _dbg("FIX", "service.py:_force_recover", "recovery_scan", {"beat_age": int(time.time() - beat)},
                     run_id="post-fix")
                # #endregion
                threading.Thread(target=self._recover_scan_once, daemon=True, name="scan-recovery").start()
            self._ensure_watchdog()
        finally:
            self._recover_lock.release()

    def _recover_scan_once(self):
        try:
            self.scan_full(kind="full")
        except Exception as e:  # noqa: BLE001
            print(f"recover scan error: {e}")

    # ---- 扫描 ----
    def _reconfirm(self, miners):
        """对'上次在线、本次掉线/零算力'的机器二次确认：宽松超时重探(可多轮)，恢复的覆盖回来。"""
        sc = self.cfg["scan"]
        if not sc.get("reconfirm_enabled", True):
            return miners
        prev_online = set(db.online_ips(self.conn))   # 上一次扫描的在线集合
        suspects = [r["ip"] for r in miners if r["ip"] in prev_online
                    and (r["status"] == "offline" or r.get("hr_rt") in (None, 0))]
        if not suspects:
            return miners
        # 大面积掉线=真实事件(交换机/断电)，不重探：避免在网络最脆弱时无限速冲击三层 CoPP
        if len(suspects) > sc.get("reconfirm_max", 800):
            return miners
        cc = dict(sc)
        cc["online_timeout"] = max(sc.get("online_timeout", 0.6), sc.get("reconfirm_online_timeout", 2.0))
        cc["data_timeout"] = max(sc.get("data_timeout", 2.0), sc.get("reconfirm_data_timeout", 5.0))
        cc["liveness_gate"] = False    # 不走判活闸门，给慢响应机器充分时间
        cc["max_pps"] = sc.get("max_pps", 100)   # 保留限速，护住三层 ARP/CoPP(不再置0)
        fixed, remaining = {}, suspects
        for _ in range(max(1, int(sc.get("reconfirm_passes", 1)))):
            again = miner_core.scan(remaining, cc, workers=min(100, len(remaining)))
            amap = {r["ip"]: r for r in again if r.get("device") != "container"}
            fixed.update(amap)
            remaining = [ip for ip, r in amap.items()
                         if r["status"] == "offline" or r.get("hr_rt") in (None, 0)]
            if not remaining:
                break
        return [fixed.get(r["ip"], r) for r in miners]

    def _do_scan(self, kind, ips, workers=None):
        if not self._scan_lock.acquire(blocking=False):
            # #region agent log
            _dbg("H3", "service.py:_do_scan", "lock_busy_skip", {"kind": kind, "ip_count": len(ips)})
            # #endregion
            return None  # 已有扫描在跑
        try:
            # #region agent log
            _dbg("H2", "service.py:_do_scan", "scan_start", {"kind": kind, "ip_count": len(ips)})
            # #endregion
            self.progress.update({"running": True, "done": 0, "total": len(ips),
                                  "kind": kind, "started": int(time.time())})

            def cb(done, total):
                self.progress["done"] = done

            records = miner_core.scan(ips, self.cfg["scan"], progress_cb=cb, workers=workers)
            miners = [r for r in records if r.get("device") != "container"]
            containers = [r for r in records if r.get("device") == "container"]
            # 二次确认：上次在线、本次掉线/零算力的机器，用更宽松超时重探一遍，去掉抖动/慢响应误判
            miners = self._reconfirm(miners)
            # 回填离线机的最后已知身份(型号/SN/矿工名/固件)：掉线机不再只剩光秃秃IP，
            # 仍能按客户名/型号/SN 搜到、列表也认得出是谁的机器
            ident = db.known_identity(self.conn)
            for r in miners:
                if r["status"] != "online":
                    info = ident.get(r["ip"])
                    if info:
                        r["model"] = r.get("model") or info.get("model") or ""
                        r["sn"] = r.get("sn") or info.get("sn") or ""
                        r["worker"] = r.get("worker") or info.get("worker") or ""
                        r["firmware"] = r.get("firmware") or info.get("firmware") or ""
            # —— 自动识别 IP 迁移(改静态等)：同一台机(按SN)已在新IP上线、旧IP掉线 → 旧IP是残影，
            #    自动下架(删名册+清其 offline/zero/reject + 清该网段 segment_down)，免得误报"大面积掉线"。
            #    读不到SN的机器(sn空/N/A)无法自动识别，退回手动"下架移除"。
            # 用"本轮实测、有效、唯一"的在线SN建映射：
            # sn_valid 排除未烧录占位串("no miner sn stored on board")/N/A/过短；唯一性(cnt==1)防占位串或克隆固件撞SN把不同机器误认成一台→误删。
            online_sn_cnt, online_sn_ip = {}, {}
            for r in miners:
                if r["status"] == "online" and miner_core.sn_valid(r.get("sn")):
                    online_sn_cnt[r["sn"]] = online_sn_cnt.get(r["sn"], 0) + 1
                    online_sn_ip[r["sn"]] = r["ip"]
            ghosts = [r["ip"] for r in miners if r["status"] != "online"
                      and miner_core.sn_valid(r.get("sn"))
                      and online_sn_cnt.get(r["sn"]) == 1              # 该SN本轮仅一台在线才敢认作迁移
                      and online_sn_ip[r["sn"]] != r["ip"]]
            migrate_max = self.cfg["scan"].get("migrate_max", 200)     # 熔断：一轮下架过多疑似误判(如占位串撞车)，跳过交人工
            if ghosts and len(ghosts) <= migrate_max:
                try:
                    db.remove_miners(self.conn, ghosts)
                    db.log_commands(self.conn, "system", "auto-migrate",
                                    [{"ip": ip, "ok": True, "msg": "同SN已在新IP上线,自动下架旧IP残影"} for ip in ghosts])
                    gset = set(ghosts)
                    miners = [r for r in miners if r["ip"] not in gset]   # 本轮不落库/不评估这些残影
                    print(f"IP迁移自动下架 {len(ghosts)} 个旧IP残影: {ghosts[:8]}")
                except Exception as e:  # noqa: BLE001
                    print(f"ghost-cleanup error: {e}")
            elif ghosts:
                print(f"IP迁移: 疑似残影 {len(ghosts)} 个超熔断上限 {migrate_max}, 跳过自动下架(交人工核实)")
            # 只落库在线机 + 名册内(曾在线/维修)的离线机；丢弃约1.5万死IP空记录(省存储/写放大)
            # 首轮(名册为空,如全新部署/清库)用 None 走全量，否则离线数恒为0、面板误显示全在线
            roster = set(db.roster_ips(self.conn, self.cfg["scan"].get("roster_retention_days", 7)))
            sid, ts = db.save_scan(self.conn, kind, miners, keep_ips=(roster or None))
            online_recs = [r for r in miners if r["status"] == "online"]   # 记身份需整条记录
            online_ips = {r["ip"] for r in online_recs}
            db.upsert_known_miners(self.conn, online_recs, ts)
            # 扫描+落库已成功 → 立即更新 last_finished(看门狗据此判活)，后续单步失败不影响
            self.progress.update({"last_finished": ts})
            # #region agent log
            _dbg("H5", "service.py:_do_scan", "scan_saved", {"kind": kind, "scan_id": sid, "ts": ts,
                  "online": sum(1 for r in miners if r["status"] == "online")})
            # #endregion

            fired, cfired = [], []
            try:   # 维修机重新上线 → 自动恢复为正常
                back = db.repair_ips(self.conn) & online_ips
                if back:
                    db.set_machine_state(self.conn, list(back), "active")
            except Exception as e:  # noqa: BLE001
                print(f"repair-restore error: {e}")
            try:
                fired = alerts.evaluate(self.conn, sid, miners, self.cfg, kind=kind)
            except Exception as e:  # noqa: BLE001
                print(f"alerts.evaluate error: {e}")
            try:   # 集装箱：记住/发现新箱 + 对采到的箱评估故障；不在此判离线
                detected_ips = {c["ip"] for c in containers}
                db.save_containers(self.conn, ts, containers)
                db.upsert_containers(self.conn, containers, ts)
                # 全网扫描对集装箱"未采到"不可靠：0.4s 判活闸门会误杀响应慢的控制器(查PLC慢)，
                # 全扫负载下更易超时。故箱体在线/离线判定与 cooler_offline 告警统一交给 _container_loop
                # (给足 ≥1.5s 超时、每10s 一次)。这里 known_before 传 detected_ips → 不会误报离线。
                cfired = alerts.evaluate_containers(self.conn, containers, detected_ips, self.cfg)
                db.kick_offline_containers(self.conn, self.cfg["scan"].get("container_offline_kick_sec", 86400))
            except Exception as e:  # noqa: BLE001
                print(f"container eval error: {e}")
            try:
                db.prune(self.conn, self.cfg["db"].get("retention_days", 3))
            except Exception as e:  # noqa: BLE001
                print(f"prune error: {e}")
            if self.notify:
                try:
                    self.notify({"type": "refresh", "fired": len(fired) + len(cfired)})
                except Exception:
                    pass
            return {"scan_id": sid, "miners": len(miners), "containers": len(containers),
                    "online": sum(1 for r in miners if r["status"] == "online"),
                    "alerts": len(fired) + len(cfired)}
        finally:
            self.progress["running"] = False
            self._scan_lock.release()

    def scan_full(self, kind="full"):
        segs, hs, he = load_segments(self.cfg)
        ips = miner_core.gen_ips_seg(segs, hs, he)
        # 全网发现用较低并发，避免与矿机/巡检撞墙
        workers = self.cfg["scan"].get("discovery_workers", 80)
        return self._do_scan(kind, ips, workers=workers)

    def scan_quick(self):
        # 快巡检 = 机器名册(在线 + 最近被限电下线的)，抓上线/下线；集装箱由独立循环刷新
        ips = db.roster_ips(self.conn, self.cfg["scan"].get("roster_retention_days", 7))
        if not ips:
            return self.scan_full(kind="full")  # 没有底库先全扫
        return self._do_scan("quick", ips)

    def scan_containers(self):
        """高频刷新已知集装箱（只打 /cooler）。与全网扫描共用 _scan_lock 互斥，避免并发评估重复推送。"""
        if not self._scan_lock.acquire(blocking=False):
            # #region agent log
            _dbg("H3", "service.py:scan_containers", "lock_busy_skip", {})
            # #endregion
            return  # 全网扫描在跑(它已处理集装箱)，本次跳过
        try:
            # #region agent log
            _dbg("H3", "service.py:scan_containers", "container_refresh_start", {})
            # #endregion
            known = db.known_container_ips(self.conn)
            if not known:
                return
            cs = miner_core.scan_containers(known, self.cfg["scan"])
            ts = int(time.time())
            detected = {c["ip"] for c in cs}
            db.save_containers(self.conn, ts, cs)
            db.upsert_containers(self.conn, cs, ts)
            # 二次确认：连续未采到 N 轮才判离线，防一次查询慢/丢包就误报控制器离线(±10s 抖动)
            miss = self._container_miss
            confirmed_off = []
            for ip in known:
                if ip in detected:
                    miss[ip] = 0
                else:
                    miss[ip] = miss.get(ip, 0) + 1
                    if miss[ip] >= self._container_miss_n:
                        confirmed_off.append(ip)
            for ip in list(miss):   # 清理已踢除的箱
                if ip not in known:
                    miss.pop(ip, None)
            db.mark_containers_offline(self.conn, confirmed_off)
            # known_before 只传 已采到 + 已确认离线 → 未确认(刚漏1轮)的不报 cooler_offline
            cfired = alerts.evaluate_containers(self.conn, cs, detected | set(confirmed_off), self.cfg)
            db.kick_offline_containers(self.conn, self.cfg["scan"].get("container_offline_kick_sec", 86400))
            if cfired and self.notify:
                try:
                    self.notify({"type": "refresh", "fired": len(cfired)})
                except Exception:
                    pass
        finally:
            # #region agent log
            _dbg("H3", "service.py:scan_containers", "container_refresh_end", {})
            # #endregion
            self._scan_lock.release()

    # ---- 后台调度 ----
    def _loop(self):
        self._ensure_watchdog()
        while not self._stop.is_set():
            start = time.monotonic()
            self.progress["loop_beat"] = int(time.time())   # 心跳：每轮更新，看门狗据此判断线程是否活着
            # #region agent log
            _dbg("H1", "service.py:_loop", "loop_tick", {"loop_beat": self.progress["loop_beat"],
                  "last_finished": self.progress.get("last_finished", 0), "running": self.progress.get("running")})
            # #endregion
            try:   # 单轮扫描失败不杀死调度线程(否则监控会永久静默停摆)
                result = self.scan_full(kind="full")
                # #region agent log
                _dbg("H4", "service.py:_loop", "scan_full_done", {"result": result is not None,
                      "scan_id": (result or {}).get("scan_id")})
                # #endregion
            except Exception as e:  # noqa: BLE001
                print(f"scan loop error: {e}")
                # #region agent log
                _dbg("H1", "service.py:_loop", "loop_error", {"error": str(e)})
                # #endregion
            if self._stop.is_set():
                break
            # 固定周期：真实间隔≈scan_interval(扣掉本轮扫描耗时)，而非 interval+扫描时长。
            # 每轮重读间隔→网页改后即时生效；wake() 可立即唤醒(改设置/停止)。整段容错，任何异常都不放倒线程。
            try:
                iv = self.cfg["schedule"].get("scan_interval", self.cfg["schedule"].get("quick_interval", 300))
                wait = max(0.0, float(iv) - (time.monotonic() - start))
                self._wake.wait(wait)
                self._wake.clear()
            except Exception as e:  # noqa: BLE001
                print(f"scan loop wait error: {e}")
                self._stop.wait(60)

    def _watchdog_tick(self, th):
        """检查扫描是否停滞；停滞则报警+自愈，恢复则消警。返回是否停滞。"""
        self._watchdog_beat = int(time.time())
        thread_alive = self._thread is not None and self._thread.is_alive()
        last = self.progress.get("last_finished", 0)
        beat = self.progress.get("loop_beat", 0)
        now = time.time()
        # 停滞判定：①有过成功扫描、当前不在扫、超 th 未完成；或 ②调度线程心跳超 th 未跳(卡死——
        # 即便它把 running 挂着 True 也能识别，避免"卡死既不自愈也不告警"的静默冻结)。
        stale = (bool(last) and not self.progress.get("running") and (now - last > th)) \
            or (bool(beat) and (now - beat > th))
        # #region agent log
        _dbg("H1", "service.py:_watchdog_tick", "watchdog_check", {
            "thread_alive": thread_alive, "stale": stale, "th": th,
            "last_age": round(now - last) if last else None,
            "beat_age": round(now - beat) if beat else None,
            "running": self.progress.get("running")}, run_id="post-fix")
        # #endregion
        if stale:
            self._force_recover("stale_detected")
            if not db.active_alert(self.conn, "__monitor__", "stalled"):
                mins = int((now - last) / 60) if last else int((now - beat) / 60)
                db.raise_alert(self.conn, "__monitor__", "stalled", "crit",
                               f"监控停滞：已 {mins} 分钟无成功扫描，已自动尝试恢复")
                alerts.push_text(self.cfg, "🔴 矿机监控停滞：已自动尝试恢复扫描")
                if self.notify:
                    try:
                        self.notify({"type": "refresh", "fired": 1})
                    except Exception:
                        pass
        else:
            db.resolve_alert(self.conn, "__monitor__", "stalled")
            # 自愈：扫描调度线程若已死，自动重启
            if not self._stop.is_set() and not thread_alive:
                print("watchdog: 扫描调度线程已死，自动重启")
                # #region agent log
                _dbg("H1", "service.py:_watchdog_tick", "restart_loop_thread", {}, run_id="post-fix")
                # #endregion
                self._thread = threading.Thread(target=self._loop, daemon=True, name="scan-loop")
                self._thread.start()
        return stale

    def _watchdog_loop(self):
        sch = self.cfg["schedule"]
        th_min = sch.get("watchdog_minutes", 0) or (sch.get("scan_interval", 300) * 3 / 60)
        th = max(120, th_min * 60)
        while not self._stop.is_set():
            self._watchdog_beat = int(time.time())
            self._stop.wait(60)
            if self._stop.is_set():
                break
            try:   # 看门狗自身也要容错，否则"检测停摆的机制自己先停摆"
                self._watchdog_tick(th)
            except Exception as e:  # noqa: BLE001
                print(f"watchdog error: {e}")
                # #region agent log
                _dbg("H1", "service.py:_watchdog_loop", "watchdog_error", {"error": str(e)}, run_id="post-fix")
                # #endregion

    def _container_loop(self):
        while not self._stop.is_set():
            iv = max(5, int(self.cfg["schedule"].get("container_interval", 10)))  # 每轮重读，即时生效
            self._stop.wait(iv)   # 容器间隔变更最多等一个旧周期(≤10s)，无需 wake
            if self._stop.is_set():
                break
            try:
                self.scan_containers()
            except Exception as e:  # noqa: BLE001
                print(f"Container refresh error: {e}")

    def start_scheduler(self):
        if not self.cfg["schedule"].get("enabled", True):
            return
        if self._thread and self._thread.is_alive():
            self._ensure_watchdog()   # 调度线程在但看门狗可能已死
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scan-loop")
        self._thread.start()
        self._ensure_watchdog()
        threading.Thread(target=self._container_loop, daemon=True, name="container-loop").start()

    def stop(self):
        self._stop.set()
        self._wake.set()   # _loop 现等在 _wake 上，停止时一并唤醒以便立即退出

    def scan_now_async(self, kind="manual"):
        """非阻塞触发一次全网扫描(统一走 full，避免 quick 子集污染统计/对比基准)。"""
        if self.progress["running"]:
            return False
        # 容器刷新可能正持 _scan_lock 但不置 running；先探锁是否空闲，
        # 否则起了线程也会在 _do_scan 抢锁失败白跑，却谎报 started=True
        if self._scan_lock.acquire(blocking=False):
            self._scan_lock.release()
        else:
            return False
        t = threading.Thread(target=self.scan_full, kwargs={"kind": "manual"}, daemon=True)
        t.start()
        return True
