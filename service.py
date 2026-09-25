#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控服务：扫描编排 + 后台定时巡检 / 看门狗 / 数据库维护线程。

扫描分两档（这是容量设计的核心，别再合成一档）：
  - quick：只探"名册"内的已知真机(约5千台)，每 schedule.scan_interval(默认300s)一轮。
           抓上线/下线/掉算力/温度，是日常监控的主力。
  - full ：展开全部网段(约1.5万地址)做发现，每 schedule.full_interval(默认3600s)一轮。
           抓新装机/换网段。死 IP 的判活超时是全扫的主要耗时来源。
之前两档被合并成"每5分钟全扫一次"，扫描耗时逼近间隔本身 → 系统长期处于"永远在扫"状态，
既压三层网络，也让 SQLite 的 WAL checkpoint 永远追不上。
"""
import threading
import time

import alerts
import appconfig
import db
import logs
import miner_core

# 向后兼容的再导出：server.py / 老脚本仍从 service 导入这些
from appconfig import (load_config, load_segments, save_segments,      # noqa: F401
                       load_settings, save_settings, apply_settings,
                       SEG_FILE, SETTINGS_FILE)

log = logs.get(__name__)


class MonitorService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = db.init_db(cfg["db"]["path"])
        db.resolve_types_except(self.conn, alerts.MINER_TYPES)
        self._scan_lock = threading.Lock()
        # 集装箱单独一把锁：只保护"写集装箱 + 评估冷却告警"这一小段(全网扫描和 10 秒刷新
        # 都会做，并发会重复报)。以前共用 _scan_lock，一轮巡检一两分钟里漏液数据都不刷新
        self._container_lock = threading.Lock()
        # 世代号：每次强制恢复 +1。被判死的旧扫描线程杀不掉，但它醒来后会发现自己
        # 的世代已作废 → 丢弃结果不落库，避免把十几分钟前的探测当成"最新快照"
        # 覆盖现状(会让刚恢复的机器被标回离线并触发一轮误告警)。
        self._scan_gen = 0
        self.progress = {"running": False, "done": 0, "total": 0, "kind": "",
                         "started": 0, "last_finished": 0, "progress_ts": 0,
                         "loop_beat": 0, "pending": False}
        # 最近一次扫描结果的内存副本：API 直接读它，不必每个请求都从 SQLite
        # 重新拉 5000 行(一个浏览器每30秒会触发3~4次这样的全量查询)。
        self.snapshot = {"scan_id": 0, "ts": 0, "kind": "", "records": []}
        self._snap_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._watchdog_thread = None
        self._maint_thread = None
        self._container_thread = None
        self._recover_lock = threading.Lock()
        self._wd_lock = threading.Lock()
        self._container_lock = threading.Lock()
        self._trigger_lock = threading.Lock()   # 手动扫描触发的判重(防两个请求都说"已启动")
        self._container_miss = {}     # 集装箱连续未采到次数(二次确认离线，防一次丢包误报)
        self._container_miss_n = 2
        self._alert_state = {}        # 跨轮次告警状态(掉算力连续计数、刚重启的机器)
        self._last_full = 0.0         # 上次全网发现完成的 monotonic 时刻
        self._last_checkpoint = 0.0
        self._last_rollup = 0.0
        self._last_vacuum_day = None
        self._last_reader_sweep = 0.0
        self.notify = None            # 可选回调：扫描完成/告警时推送(WS)

    def wake(self):
        """外部(如改设置后)唤醒调度循环，让其立即重读间隔。"""
        self._wake.set()

    # ---- 内存快照 ----
    def _publish(self, scan_id, ts, kind, records):
        with self._snap_lock:
            self.snapshot = {"scan_id": scan_id, "ts": ts, "kind": kind, "records": records}

    def latest(self):
        """返回 (meta, records)。records 为共享只读列表，调用方不得原地修改。"""
        snap = self.snapshot
        if snap["records"]:
            return snap, snap["records"]
        # 内存里还没有(刚重启)→ 回落到数据库
        ls = db.latest_scan(self.conn)
        if not ls:
            return None, []
        recs = db.scan_records(self.conn, ls["scan_id"])
        self._publish(ls["scan_id"], ls["ts"], ls.get("kind") or "", recs)
        return self.snapshot, recs

    def mark_rebooting(self, ips):
        """记下刚下发重启的机器：静默期内掉线不报警(见 alerts.evaluate)。
        批量分批重启要跑几分钟，所以在下发前整批先标记，失败的再用 unmark_rebooting 撤掉。"""
        rb = self._alert_state.setdefault("rebooting", {})
        now = int(time.time())
        for ip in ips:
            rb[ip] = now

    def unmark_rebooting(self, ips):
        rb = self._alert_state.setdefault("rebooting", {})
        for ip in ips:
            rb.pop(ip, None)

    def drop_from_snapshot(self, ips):
        """下架移除后立刻从内存快照剔除，否则要等下一轮扫描列表才更新。"""
        drop = set(ips)
        with self._snap_lock:
            snap = dict(self.snapshot)
            snap["records"] = [r for r in snap["records"] if r["ip"] not in drop]
            self.snapshot = snap

    # ---- 自愈 ----
    def _scheduling_on(self):
        """配置里关掉定时扫描时，自愈逻辑也不该偷偷把扫描线程拉起来。"""
        return bool(self.cfg["schedule"].get("enabled", True)) and not self._stop.is_set()

    def _ensure_watchdog(self):
        """看门狗自身也要能自愈——它死了就没人检测扫描停摆了。"""
        if not self._scheduling_on():
            return
        with self._wd_lock:
            if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
                log.warning("看门狗线程不在，自动启动")
                self._watchdog_thread = threading.Thread(target=self._watchdog_loop,
                                                         daemon=True, name="watchdog")
                self._watchdog_thread.start()

    def _ensure_container_loop(self):
        """集装箱高频刷新线程也要能自愈——它一死，冷却设备的10秒刷新/离线判定/24h踢除
        会永久静默停摆，而扫描、看门狗、面板看起来一切正常，不会有任何告警提示。"""
        if not self._scheduling_on():
            return
        with self._container_lock:
            if self._container_thread is None or not self._container_thread.is_alive():
                log.warning("集装箱刷新线程不在，自动启动")
                self._container_thread = threading.Thread(target=self._container_loop,
                                                          daemon=True, name="container-loop")
                self._container_thread.start()

    def _ensure_scan_thread(self, who):
        """检查+重启扫描调度线程。与 _force_recover 共用 _recover_lock：
        guardian 与 watchdog 是两条独立的60秒周期，若线程恰好死在两次检查之间，
        两边都会判定"已死"各拉起一条 scan-loop。拿不到锁说明另一条路径正在
        恢复/重启，直接交给它，别再新建线程。"""
        if not self._scheduling_on():
            return
        if not self._recover_lock.acquire(blocking=False):
            return
        try:
            if not (self._thread and self._thread.is_alive()):
                log.warning("%s: 扫描调度线程已死，自动重启", who)
                self._thread = threading.Thread(target=self._loop, daemon=True, name="scan-loop")
                self._thread.start()
        finally:
            self._recover_lock.release()

    def _stale_threshold(self):
        """判定"扫描停摆"的秒数阈值。

        空闲时 last_finished 每个巡检间隔刷新一次；扫描进行中看进度心跳(每 200 台刷新，
        全网发现也一样)。所以按巡检间隔推算即可。以前取 full_interval×1.5 —— 把全扫的
        "间隔"当成了"耗时"，默认要 90 分钟才发现巡检卡死。下限 15 分钟：单轮扫描/二次确认
        各有 600 秒整体超时兜底(期间可能没有心跳)，留出余量，别在兜底之前就误判触发强制恢复。"""
        sch = self.cfg["schedule"]
        cfgd = sch.get("watchdog_minutes", 0)
        if cfgd:
            return max(120, float(cfgd) * 60)
        return max(900.0, sch.get("scan_interval", 300) * 3.0)

    def is_stale(self, now=None):
        """扫描中看进度心跳；空闲时看上次完成时间与调度线程心跳。"""
        now = now or time.time()
        th = self._stale_threshold()
        p = self.progress
        if p.get("running"):
            heart = p.get("progress_ts") or p.get("loop_beat") or 0
            return bool(heart) and (now - heart > th)
        last, beat = p.get("last_finished", 0), p.get("loop_beat", 0)
        return (bool(last) and now - last > th) or (bool(beat) and now - beat > th)

    def health_tick(self):
        """供主事件循环调用的轻量守护：不依赖 daemon 线程自身存活。"""
        try:
            if not self._scheduling_on():
                return
            self._ensure_watchdog()
            self._ensure_scan_thread("guardian")
            self._ensure_container_loop()
            if not (self._maint_thread and self._maint_thread.is_alive()):
                self._maint_thread = threading.Thread(target=self._maintenance_loop,
                                                      daemon=True, name="maintenance")
                self._maint_thread.start()
            if self.is_stale():
                self._force_recover("guardian_stale")
        except Exception as e:  # noqa: BLE001
            log.exception("guardian error: %s", e)

    def _force_recover(self, reason):
        """停滞/线程死亡时强制恢复。"""
        if not self._recover_lock.acquire(blocking=False):
            return
        try:
            log.warning("force_recover: %s", reason)
            was_running = bool(self.progress.get("running"))
            # 扫描还在跑时禁止全局 close_sessions（会打飞在途探测→假离线）
            if not was_running:
                miner_core.close_sessions()
            # 换锁让新扫描能进；旧线程 finally 只释放自己拿到的那把(局部引用)。
            # 同时作废旧世代，旧扫描即便苏醒也不会落库。
            self._scan_lock = threading.Lock()
            self._scan_gen += 1
            self.progress["running"] = False
            self._wake.set()
            if not self._scheduling_on():
                return          # 定时扫描已关闭：作废旧世代即可，不重启任何扫描线程
            if not (self._thread and self._thread.is_alive()):
                log.warning("force_recover: 重启扫描调度线程")
                self._thread = threading.Thread(target=self._loop, daemon=True, name="scan-loop")
                self._thread.start()
            else:
                beat = self.progress.get("loop_beat", 0)
                if beat and time.time() - beat > 120:
                    log.warning("force_recover: 调度线程疑似卡死，发起恢复扫描")
                    threading.Thread(target=self._recover_scan_once, daemon=True,
                                     name="scan-recovery").start()
            self._ensure_watchdog()
        finally:
            self._recover_lock.release()

    def _recover_scan_once(self):
        try:
            self.scan_full(kind="full")
        except Exception as e:  # noqa: BLE001
            log.exception("recover scan error: %s", e)

    # ---- 扫描 ----
    def _beat(self, gen=None):
        if gen is not None and gen != self._scan_gen:
            return
        ts = int(time.time())
        self.progress["loop_beat"] = self.progress["progress_ts"] = ts

    def _reconfirm(self, miners, gen=None):
        """对"上次在线、本次掉线/零算力"的机器二次确认：宽松超时重探(可多轮)。"""
        if gen is not None and gen != self._scan_gen:
            return miners
        sc = self.cfg["scan"]
        if not sc.get("reconfirm_enabled", True):
            return miners
        prev_online = set(db.online_ips(self.conn))
        _m, prev_recs = self.latest()
        prev_hr = {r["ip"]: r.get("hr_rt") for r in prev_recs}
        # 零算力/读不到算力只在"上一轮还有算力"时才算疑似：限电休眠、密码不对的一批机器
        # 一直是 0/None，每轮都算疑似会白白重探，数量一多还会顶破下面的熔断
        suspects = [r["ip"] for r in miners if r["ip"] in prev_online
                    and (r["status"] == "offline"
                         or (r.get("hr_rt") in (None, 0) and (prev_hr.get(r["ip"]) or 0) > 0))]
        if not suspects:
            return miners
        # 大面积掉线=真实事件(交换机/断电)，不重探：避免在网络最脆弱时无限速冲击三层 CoPP。
        # 只按"在线→离线"计数，零算力类不算进熔断
        went_off = sum(1 for r in miners if r["ip"] in prev_online and r["status"] == "offline")
        if went_off > sc.get("reconfirm_max", 800):
            log.info("二次确认跳过: %d 台同时掉线超过上限，判定为真实的大面积事件", went_off)
            return miners
        cc = dict(sc)
        cc["online_timeout"] = max(sc.get("online_timeout", 0.6),
                                   sc.get("reconfirm_online_timeout", 2.0))
        cc["data_timeout"] = max(sc.get("data_timeout", 2.0),
                                 sc.get("reconfirm_data_timeout", 5.0))
        cc["liveness_gate"] = False    # 不走判活闸门，给慢响应机器充分时间
        cc["max_pps"] = sc.get("max_pps", 100)   # 保留限速，护住三层 ARP/CoPP
        def rank(r):   # 在线且有算力 > 在线 > 未探测 > 离线
            if r["status"] == "online":
                return 3 if r.get("hr_rt") else 2
            return 1 if r["status"] == "unknown" else 0

        # 各轮结果只升不降：任一轮确认在线就算在线。直接覆盖会让后一轮的瞬时丢包
        # 抹掉前一轮已探到的在线，恰恰把二次确认要保护的慢响应机器误报成掉线
        fixed = {r["ip"]: r for r in miners if r["ip"] in set(suspects)}
        remaining = suspects
        for _ in range(max(1, int(sc.get("reconfirm_passes", 1)))):
            if gen is not None and gen != self._scan_gen:
                return miners
            self._beat(gen)
            again = miner_core.scan(remaining, cc, workers=min(100, len(remaining)))
            amap = {r["ip"]: r for r in again if r.get("device") != "container"}
            for ip, r in amap.items():
                if ip not in fixed or rank(r) > rank(fixed[ip]):
                    fixed[ip] = r
            remaining = [ip for ip, r in amap.items()
                         if r["status"] == "offline" or r.get("hr_rt") in (None, 0)]
            if not remaining:
                break
        return [fixed.get(r["ip"], r) for r in miners]

    def _do_scan(self, kind, ips, workers=None):
        lock = self._scan_lock   # 局部引用：即便 recover 换了 self._scan_lock，也只释放自己那把
        if not lock.acquire(blocking=False):
            return None          # 已有扫描在跑
        gen = self._scan_gen
        try:
            now = int(time.time())
            self.progress.update({"running": True, "done": 0, "total": len(ips),
                                  "kind": kind, "started": now, "progress_ts": now,
                                  "loop_beat": now})
            t0 = time.monotonic()

            def cb(done, total):
                if gen != self._scan_gen:
                    return
                ts = int(time.time())    # 长扫描期间刷新心跳，别被看门狗当卡死
                self.progress["done"] = done
                self.progress["progress_ts"] = ts
                self.progress["loop_beat"] = ts

            scan_cfg = dict(self.cfg["scan"])
            # MAC is a stable fallback identity for firmware that cannot expose
            # an SN.  Fetch it on discovery scans, not every five-minute patrol.
            scan_cfg["collect_identity"] = kind in {"full", "manual"}
            records = miner_core.scan(ips, scan_cfg, progress_cb=cb, workers=workers)
            # 看门狗恢复会递增世代并启动新扫描。旧线程苏醒后必须在任何进度更新、
            # 二次探测或落库前退出，否则会覆盖新扫描的进度/快照。
            if gen != self._scan_gen:
                log.warning("扫描世代 %d 已作废，丢弃本轮 %d 条结果不落库", gen, len(records))
                return None
            self._beat(gen)
            miners = [r for r in records if r.get("device") != "container"]
            containers = [r for r in records if r.get("device") == "container"]
            miners = self._reconfirm(miners, gen)

            if gen != self._scan_gen:
                log.warning("扫描世代 %d 已作废(期间发生强制恢复)，丢弃本轮 %d 条结果不落库",
                            gen, len(miners))
                return None

            # 回填离线机的最后已知身份：掉线机不再只剩光秃秃 IP，仍能按客户名/型号/SN 搜到
            ident = db.known_identity(self.conn)
            for r in miners:
                info = ident.get(r["ip"])
                # 只回填离线机：在线机的 MAC 必须是本轮实测值——有合法 SN 的原厂机不读 MAC，
                # 若回填名册旧值(可能是之前在这个 IP 上的另一台机)，残影判定会把真掉线的
                # 那台当成"已在别处上线"自动下架、告警静音
                if info and r["status"] != "online":
                    r["mac"] = r.get("mac") or info.get("mac") or ""
                    r["model"] = r.get("model") or info.get("model") or ""
                    r["sn"] = r.get("sn") or info.get("sn") or ""
                    r["worker"] = r.get("worker") or info.get("worker") or ""
                    r["firmware"] = r.get("firmware") or info.get("firmware") or ""

            miners = self._drop_migrated_ghosts(miners)

            # 只落库在线机 + 名册内(曾在线/维修)的离线机；丢弃约1.5万死IP空记录。
            # 全新部署的第一轮还没有名册，此时必须落全量，否则离线数恒为0、面板误显示全在线。
            # 判据用"库里有没有过扫描"而不是"名册是否为空"——后者会被 IP 迁移批量下架
            # 清空名册的情况误伤，导致某一轮突然把上万个死地址全写进库。
            roster = set(db.roster_ips(self.conn, self.cfg["scan"].get("roster_retention_days", 7)))
            first_run = db.latest_scan(self.conn) is None
            sid, ts, kept = db.save_scan(self.conn, kind, miners,
                                         keep_ips=(None if first_run else roster))
            # 矿池地址不落库(列表且只用于本轮防篡改检查)，但 kept 是按库列重建的干净记录，
            # 要带回去，否则 alerts 永远看不到 pools、矿池篡改告警形同虚设
            pools_by_ip = {r["ip"]: r["pools"] for r in miners if r.get("pools") is not None}
            for r in kept:
                if r["ip"] in pools_by_ip:
                    r["pools"] = pools_by_ip[r["ip"]]
            self._publish(sid, ts, kind, kept)
            online_recs = [r for r in miners if r["status"] == "online"]
            online_ips = {r["ip"] for r in online_recs}
            db.upsert_known_miners(self.conn, online_recs, ts)
            # 扫描+落库已成功 → 立即更新 last_finished(看门狗据此判活)
            self.progress["last_finished"] = ts

            fired, cfired = [], []
            try:   # 维修机重新上线 → 自动恢复为正常
                back = db.repair_ips(self.conn) & online_ips
                if back:
                    db.set_machine_state(self.conn, list(back), "active")
            except Exception as e:  # noqa: BLE001
                log.warning("repair-restore error: %s", e)
            try:
                fired = alerts.evaluate(self.conn, sid, kept, self.cfg, kind=kind,
                                        state=self._alert_state)
            except Exception as e:  # noqa: BLE001
                log.exception("alerts.evaluate error: %s", e)
            # 集装箱：记住/发现新箱 + 对采到的箱评估故障；不在此判离线
            got = self._container_lock.acquire(timeout=60)   # 等正在跑的 10 秒刷新做完
            try:
                if not got:
                    raise RuntimeError("等集装箱刷新超时，本轮不处理集装箱")
                detected_ips = {c["ip"] for c in containers}
                db.save_containers(self.conn, ts, containers)
                db.upsert_containers(self.conn, containers, ts)
                # 全网扫描对集装箱"未采到"不可靠(判活闸门会误杀查 PLC 慢的控制器)，
                # 离线判定统一交给 _container_loop(给足超时、每10s 一次)。
                cfired = alerts.evaluate_containers(self.conn, containers, detected_ips, self.cfg,
                                                    state=self._alert_state)
                db.kick_offline_containers(
                    self.conn, self.cfg["scan"].get("container_offline_kick_sec", 86400))
            except Exception as e:  # noqa: BLE001
                log.exception("container eval error: %s", e)
            finally:
                if got:
                    self._container_lock.release()

            took = time.monotonic() - t0
            log.info("%s 扫描完成: %d 台在线 / %d 落库 (地址 %d), 集装箱 %d, 新告警 %d, 耗时 %.1fs",
                     kind, len(online_ips), len(kept), len(ips), len(containers),
                     len(fired) + len(cfired), took)
            if took > self.cfg["schedule"].get("scan_interval", 300):
                log.warning("本轮扫描耗时 %.0fs 已超过巡检间隔 %ds —— 系统将长期处于"
                            "持续扫描状态，建议下调网段范围/提高并发/放宽间隔",
                            took, self.cfg["schedule"].get("scan_interval", 300))
            if self.notify:
                try:
                    self.notify({"type": "refresh", "fired": len(fired) + len(cfired)})
                except Exception:  # noqa: BLE001
                    pass
            return {"scan_id": sid, "kind": kind, "miners": len(kept),
                    "containers": len(containers), "online": len(online_ips),
                    "alerts": len(fired) + len(cfired), "took": round(took, 1)}
        finally:
            # 旧世代不能把已经接管的新扫描标成“未运行”。
            if gen == self._scan_gen:
                self.progress["running"] = False
            try:
                lock.release()
            except RuntimeError as e:
                log.warning("_do_scan release error: %s", e)

    def _drop_migrated_ghosts(self, miners):
        """自动识别 IP 迁移(改静态等)：同一台机(按SN)已在新IP上线、旧IP掉线 → 旧IP是残影，
        自动下架，免得误报"大面积掉线"。读不到SN的机器无法自动识别，退回手动"下架移除"。

        唯一性(cnt==1)判断是必需的：未烧录SN的控制板会返回同一个占位串，
        若当唯一身份用会把不同机器认成同一台 → 误删。"""
        online_sn_cnt, online_sn_ip = {}, {}
        online_mac_cnt, online_mac_ip = {}, {}
        for r in miners:
            if r["status"] == "online" and miner_core.sn_valid(r.get("sn")):
                online_sn_cnt[r["sn"]] = online_sn_cnt.get(r["sn"], 0) + 1
                online_sn_ip[r["sn"]] = r["ip"]
            mac = miner_core.normalize_mac(r.get("mac"))
            if r["status"] == "online" and mac:
                online_mac_cnt[mac] = online_mac_cnt.get(mac, 0) + 1
                online_mac_ip[mac] = r["ip"]
        ghosts = []
        for r in miners:
            if r["status"] == "online":
                continue
            if (miner_core.sn_valid(r.get("sn"))
                    and online_sn_cnt.get(r["sn"]) == 1
                    and online_sn_ip[r["sn"]] != r["ip"]):
                ghosts.append(r["ip"])
                continue
            mac = miner_core.normalize_mac(r.get("mac"))
            if mac and online_mac_cnt.get(mac) == 1 and online_mac_ip[mac] != r["ip"]:
                ghosts.append(r["ip"])
        if not ghosts:
            return miners
        migrate_max = self.cfg["scan"].get("migrate_max", 200)   # 熔断：一轮下架过多疑似误判
        if len(ghosts) > migrate_max:
            log.warning("IP迁移: 疑似残影 %d 个超熔断上限 %d, 跳过自动下架(交人工核实)",
                        len(ghosts), migrate_max)
            return miners
        try:
            db.mark_miners_migrated(self.conn, ghosts)
            db.log_commands(self.conn, "system", "auto-migrate",
                            [{"ip": ip, "ok": True, "msg": "同SN已在新IP上线,自动下架旧IP残影"}
                             for ip in ghosts])
            gset = set(ghosts)
            log.info("IP迁移自动下架 %d 个旧IP残影: %s", len(ghosts), ghosts[:8])
            return [r for r in miners if r["ip"] not in gset]
        except Exception as e:  # noqa: BLE001
            log.exception("ghost-cleanup error: %s", e)
            return miners

    def scan_full(self, kind="full"):
        """全网发现：展开所有网段的全部主机号。"""
        segs, hs, he = appconfig.load_segments(self.cfg)
        ips = miner_core.gen_ips_seg(segs, hs, he)
        workers = self.cfg["scan"].get("discovery_workers", 300)
        return self._do_scan(kind, ips, workers=workers)

    def scan_quick(self, kind="quick"):
        """名册巡检：只探已知真机(在线过的 + 维修中的)，抓上线/下线。"""
        ips = db.roster_ips(self.conn, self.cfg["scan"].get("roster_retention_days", 7))
        if not ips:
            return self.scan_full(kind="full")   # 没有底库先全扫
        return self._do_scan(kind, ips, workers=self.cfg["scan"].get("workers", 300))

    def scan_containers(self):
        """高频刷新已知集装箱（只打 /cooler）。用独立的 _container_lock：矿机扫描进行中
        照样刷新(漏液这类数据等不起一两分钟)，只和"全网扫描里处理集装箱那一小段"互斥。"""
        lock = self._container_lock
        if not lock.acquire(blocking=False):
            return   # 上一次刷新或全网扫描的集装箱处理还没完，本次跳过
        try:
            known = db.known_container_ips(self.conn)
            if not known:
                return
            cs = miner_core.scan_containers(known, self.cfg["scan"])
            ts = int(time.time())
            detected = {c["ip"] for c in cs}
            db.save_containers(self.conn, ts, cs)
            db.upsert_containers(self.conn, cs, ts)
            # 二次确认：连续未采到 N 轮才判离线，防一次查询慢/丢包就误报控制器离线
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
            cfired = alerts.evaluate_containers(self.conn, cs, detected | set(confirmed_off),
                                                self.cfg, state=self._alert_state)
            db.kick_offline_containers(
                self.conn, self.cfg["scan"].get("container_offline_kick_sec", 86400))
            if cfired and self.notify:
                try:
                    self.notify({"type": "refresh", "fired": len(cfired)})
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                lock.release()
            except RuntimeError as e:
                log.warning("scan_containers release error: %s", e)

    # ---- 后台调度 ----
    def _loop(self):
        self._ensure_watchdog()
        while not self._stop.is_set():
            start = time.monotonic()
            self.progress["loop_beat"] = int(time.time())   # 心跳，看门狗据此判断线程活着
            skipped = False
            try:   # 单轮扫描失败不杀死调度线程(否则监控会永久静默停摆)
                sch = self.cfg["schedule"]
                full_iv = sch.get("full_interval", 3600)
                need_full = (self._last_full == 0.0) or (start - self._last_full >= full_iv)
                result = self.scan_full("full") if need_full else self.scan_quick()
                skipped = result is None   # 锁被占用(手动扫描在跑)→尽快重试，别空等满间隔
                if result and result.get("kind") == "full":
                    self._last_full = time.monotonic()
            except Exception as e:  # noqa: BLE001
                log.exception("scan loop error: %s", e)
            if self._stop.is_set():
                break
            # 固定周期：真实间隔≈scan_interval(扣掉本轮扫描耗时)。每轮重读间隔→网页改后即时生效。
            try:
                iv = self.cfg["schedule"].get("scan_interval", 300)
                wait = 15.0 if skipped else max(0.0, float(iv) - (time.monotonic() - start))
                self._wake.wait(wait)
                self._wake.clear()
            except Exception as e:  # noqa: BLE001
                log.exception("scan loop wait error: %s", e)
                self._stop.wait(60)

    def _watchdog_loop(self):
        while not self._stop.is_set():
            self._stop.wait(60)
            if self._stop.is_set():
                break
            try:   # 看门狗自身也要容错，否则"检测停摆的机制自己先停摆"
                self._watchdog_tick()
            except Exception as e:  # noqa: BLE001
                log.exception("watchdog error: %s", e)

    def _watchdog_tick(self):
        """检查扫描是否停滞；停滞则报警+自愈，恢复则消警。"""
        now = time.time()
        stale = self.is_stale(now)
        if stale:
            self._force_recover("stale_detected")
            if not db.active_alert(self.conn, "__monitor__", "stalled"):
                last = self.progress.get("last_finished", 0) or self.progress.get("loop_beat", 0)
                mins = int((now - last) / 60) if last else 0
                db.raise_alert(self.conn, "__monitor__", "stalled", "crit",
                               f"监控停滞：已 {mins} 分钟无成功扫描，已自动尝试恢复")
                alerts.push_text(self.cfg, "🔴 矿机监控停滞：已自动尝试恢复扫描")
                if self.notify:
                    try:
                        self.notify({"type": "refresh", "fired": 1})
                    except Exception:  # noqa: BLE001
                        pass
        else:
            db.resolve_alert(self.conn, "__monitor__", "stalled")
            self._ensure_scan_thread("watchdog")
        self._ensure_container_loop()
        return stale

    def _container_loop(self):
        while not self._stop.is_set():
            iv = max(5, int(self.cfg["schedule"].get("container_interval", 10)))
            self._stop.wait(iv)
            if self._stop.is_set():
                break
            try:
                self.scan_containers()
            except Exception as e:  # noqa: BLE001
                log.exception("Container refresh error: %s", e)

    def _maintenance_loop(self):
        """数据库维护：计费归档 → 过期清理 → WAL 截断 → 每日 VACUUM。

        这些以前要么没有(checkpoint/vacuum/rollup)，要么挂在扫描路径上(prune 每轮跑)。
        单独一个低频线程做，既不拖慢扫描，也保证一定会被执行。"""
        while not self._stop.is_set():
            self._stop.wait(60)
            if self._stop.is_set():
                break
            try:
                self._maintenance_tick()
            except Exception as e:  # noqa: BLE001
                log.exception("maintenance error: %s", e)

    def _maintenance_tick(self, now=None):
        now = now or time.monotonic()
        dbc = self.cfg["db"]
        # 只读连接是"每个碰过DB的线程各开一个、用到进程退出才关"(db._r())，FastAPI的
        # sync路由跑在会动态开关线程的线程池里，线程一多、活得越久就攒得越多——
        # 面板被人盯着看、上报/告警轮询越频繁，攒的速度越快，攒够 1024 就把服务打
        # 到句柄耗尽、连上报的新 socket 都开不出来(生产上实测复现过)。定期强制关闭
        # 重来，跟写连接完全独立，不用等扫描空闲，代价只是极小概率撞上某个读请求
        # 正查到一半、报一次可忽略的失败。
        sweep_iv = max(60, int(dbc.get("reader_sweep_minutes", 15)) * 60)
        if now - self._last_reader_sweep >= sweep_iv:
            self._last_reader_sweep = now
            n = db.close_readers()
            if n:
                log.info("已关闭 %d 个只读连接句柄(定期清理，防句柄泄漏)", n)
        if now - self._last_rollup >= 300:          # 每5分钟归档一次已结束的小时
            self._last_rollup = now
            db.rollup_hours(self.conn)
            db.prune(self.conn, dbc.get("retention_days", 3),
                     roster_days=self.cfg["scan"].get("roster_retention_days", 7),
                     rollup_days=dbc.get("rollup_retention_days", 400))
        ckpt_iv = max(60, int(dbc.get("checkpoint_minutes", 10)) * 60)
        # 计时器只在"真的做了"之后才重置：否则赶上扫描被跳过时也算一轮，
        # checkpoint 要再等满一个周期(WAL 曾因此涨到 800MB)、vacuum 直接错过一整天。
        if now - self._last_checkpoint >= ckpt_iv:
            if not self.progress.get("running"):    # 扫描落库期间不去抢写锁
                db.checkpoint(self.conn)
                self._last_checkpoint = now
                main_sz, wal_sz = db.db_size(self.conn)
                if wal_sz > 512 * 1024 * 1024:
                    log.warning("WAL 仍有 %.0f MB —— 可能有长事务未结束，请检查", wal_sz / 1e6)
            else:
                log.info("checkpoint 因扫描进行中被跳过，将于下次 tick 重试")
        vh = int(dbc.get("vacuum_hour", 4))
        today = time.strftime("%Y-%m-%d")
        if vh >= 0 and self._last_vacuum_day != today and int(time.strftime("%H")) == vh:
            if not self.progress.get("running"):
                main_sz, _ = db.db_size(self.conn)
                log.info("开始每日 VACUUM (当前主库 %.0f MB，期间数据库被独占)", main_sz / 1e6)
                db.checkpoint(self.conn)
                db.vacuum(self.conn)
                self._last_vacuum_day = today
                new_sz, _ = db.db_size(self.conn)
                log.info("VACUUM 后主库 %.0f MB (回收 %.0f MB)",
                         new_sz / 1e6, (main_sz - new_sz) / 1e6)
            else:
                # 不标记"今天已做"：下次 tick(60s后)只要还在这个小时内就会再试一次
                log.info("vacuum 因扫描进行中被跳过，将于下次 tick 重试")

    def start_scheduler(self):
        if not self.cfg["schedule"].get("enabled", True):
            log.warning("schedule.enabled=false，后台定时扫描未启动")
            return
        if self._thread and self._thread.is_alive():
            self._ensure_watchdog()
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scan-loop")
        self._thread.start()
        self._ensure_watchdog()
        self._ensure_container_loop()
        self._maint_thread = threading.Thread(target=self._maintenance_loop, daemon=True,
                                              name="maintenance")
        self._maint_thread.start()
        log.info("调度已启动: 巡检 %ds / 全网发现 %ds / 集装箱 %ds",
                 self.cfg["schedule"].get("scan_interval", 300),
                 self.cfg["schedule"].get("full_interval", 3600),
                 self.cfg["schedule"].get("container_interval", 10))

    def stop(self):
        self._stop.set()
        self._wake.set()

    def scan_now_async(self, kind="manual"):
        """非阻塞触发一次扫描。kind: full/manual=全网发现, quick=名册巡检。

        判重放在 _trigger_lock 里并用 pending 标志占位：否则两个并发请求都能通过
        "锁空闲"检查、都返回 started=True，实际只有一个真的跑起来。"""
        want_quick = kind == "quick"
        with self._trigger_lock:
            if self.progress["running"] or self.progress.get("pending"):
                return False
            # 扫描可能刚拿到 _scan_lock 还没置 running，先探一下锁
            if self._scan_lock.acquire(blocking=False):
                self._scan_lock.release()
            else:
                return False
            self.progress["pending"] = True
        threading.Thread(target=self._manual_scan, args=(want_quick,),
                         daemon=True, name="scan-manual").start()
        return True

    def _manual_scan(self, want_quick):
        try:
            if want_quick:
                self.scan_quick(kind="quick")
            else:
                result = self.scan_full(kind="manual")
                if result:
                    self._last_full = time.monotonic()   # 手动全扫也顶一轮发现
        except Exception as e:  # noqa: BLE001
            log.exception("manual scan error: %s", e)
        finally:
            self.progress["pending"] = False
