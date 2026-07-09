# 开发文档（Development Guide）

矿场监控系统（Miner Monitor）——给托管/包机场地用的矿机+水冷集装箱监控平台。
本文面向**维护/二次开发**：架构、数据模型、控制流、线程模型、配置、约定与坑。

- 用户/功能说明见 [../README.md](../README.md)
- 矿机/设备接口（本系统调设备）见 [INTERNAL_API.md](INTERNAL_API.md)
- 外部 Agent 取数接口（别人调本系统）见 [EXTERNAL_API.md](EXTERNAL_API.md)

---

## 1. 设计哲学

- **单进程、零外部依赖**：FastAPI + SQLite + 原生 JS，一条 `python server.py` 起全部。不引 Redis/PG/前端框架。
- **分层、核心无副作用**：`miner_core` 只探测返回结构化数据，不碰 DB/网络副作用之外的状态；CLI / 定时服务 / Web API 三处复用同一核心。
- **保护三层网络（CoPP）**：扫 2 万 IP 的最大风险是对死 IP 的 ARP 冲破三层 500pps 控制平面限制；用令牌桶 `max_pps` + liveness_gate 兜底。
- **故障可发现 > 功能花哨**：告警的准确性（防误判/防漏报/防刷屏）是第一优先。

---

## 2. 架构总览

```
                         ┌─────────────── 浏览器面板 (web/) ───────────────┐
                         │  app.js  ── REST + WebSocket ──┐                 │
                         └────────────────────────────────┼─────────────────┘
 别的 Agent ── /api/public/* (token) ──┐                   │
                                       ▼                   ▼
                                ┌──────────────── server.py (FastAPI) ───────────────┐
                                │  鉴权(auth) · 路由 · 托管前端 · WS 广播             │
                                └───────┬───────────────────────────┬─────────────────┘
                                        │ 读/写                      │ 触发/读进度
                                        ▼                            ▼
                         ┌──────── db.py (SQLite/WAL) ────────┐   service.py (后台线程)
                         │ scans/snapshots/alerts/containers  │   ├ _loop          全网扫描(5min)
                         │ known_miners/command_log           │   ├ _container_loop 集装箱(10s)
                         └────────────────────────────────────┘   └ _watchdog_loop  看门狗
                                        ▲                            │
                                        │ 评估                       │ 调用
                              alerts.py │                            ▼
                              (规则/恢复/Telegram)        miner_core.py (探测核心, 纯数据)
                                                          control.py    (远程命令, 写设备)
                                                                 │
                                                                 ▼
                                                         矿机 / AntBox 集装箱
```

**数据流（一次扫描）**：`service._loop` 每 `scan_interval` → `miner_core.scan(全部IP)` 并发探测
→ `_reconfirm` 二次确认 → `db.save_scan`（只落库真机，见 §6 H7）→ `db.upsert_known_miners`
→ `alerts.evaluate`（产/消告警）→ 集装箱评估/踢除 → `db.prune` → WS 推送 `{type:refresh}`。

---

## 3. 模块参考

| 文件 | 职责 | 关键函数 |
|---|---|---|
| `miner_core.py` | 探测核心（**纯数据，无副作用**） | `probe` `scan` `probe_stock/uniplus/antbox` `fetch_pool` `gen_ips_seg` `rank` `RateLimiter` `tcp_open` |
| `db.py` | SQLite 存储层（共享连接 + `RLock`） | `init_db` `save_scan` `roster_ips` `upsert_known_miners` `raise/resolve_alert` `active_alerts_by_type` `customer_report` `prune` |
| `alerts.py` | 告警规则评估 + 恢复 + Telegram | `evaluate` `evaluate_containers` `_push_batch` |
| `control.py` | 远程命令（写设备，双固件） | `run_batch` `run_one` `_stock` `_uniplus` |
| `auth.py` | 登录 + RBAC + pbkdf2 + 失败限流 | `login` `current` `has_role` `hash_password` `locked` |
| `service.py` | 扫描编排 + 后台调度线程 | `MonitorService` `_do_scan` `_loop` `_container_loop` `_watchdog_loop` `_reconfirm` `scan_full` |
| `server.py` | FastAPI：路由 + 鉴权 + 托管前端 + WS + 外部 API | 各 `api_*` 路由、`_broadcast_threadsafe`、`/api/public/*` |
| `web/` | 原生 JS 面板（`index.html`/`style.css`/`app.js` + Chart.js） | — |
| `scan_miners.py` | 命令行扫描器（薄封装复用 core） | — |
| `config.yaml` | 主配置（启动加载） | — |
| `segments.json` / `<settings>.json` | 网页可改的网段 / 运行时参数（覆盖 config） | — |

---

## 4. 数据模型（SQLite，WAL）

连接：`sqlite3.connect(path, check_same_thread=False)` 单共享连接 + `db._lock = RLock()`（读写都加锁）。

| 表 | 主键 | 用途 | 关键列 |
|---|---|---|---|
| `scans` | `scan_id` | 每次扫描一行（汇总） | `ts, kind(full\|quick\|manual), total, online, offline, total_hr`；`idx_scans_ts` |
| `snapshots` | `(scan_id, ip)` | 每次扫描每台机一行 | `status, firmware, model, sn, hr_rt, hr_avg, power, temp, eff, uptime, worker, accepted, rejected, stale, note`；`idx_snap_ip` |
| `alerts` | `id` | 告警（含已恢复） | `ts, ip, type, severity, detail, resolved, resolved_ts, ack_by, ack_at`；`idx_alert_active(ip,type,resolved)` |
| `known_miners` | `ip` | 名册（roster） | `last_online, state(active\|repair)` |
| `containers` | `ip` | 集装箱当前态（持久记忆） | 水温/压力/泵/故障/功耗/箱内矿机数 + `online, last_online` |
| `container_snaps` | `(ts, ip)` | 集装箱历史 | 同上快照；`idx_csnap_ip` |
| `command_log` | `id` | 命令审计 | `ts, user, action, ip, ok, msg` |

> **统一记录结构**（`miner_core._blank`）：DB 列名、前端字段、core 输出三者**必须一致**。
> 曾因长短名不一致（`hashrate_rt_th` vs `hr_rt`）出过 bug。新增字段三处同步改。

**告警类型**：`offline` / `zero` / `reject` / `segment_down` / `stalled` / `cooler:<flag>` / `cooler_offline`。

---

## 5. 控制流详解

### 5.1 扫描周期 `service._do_scan`
1. `_scan_lock.acquire(blocking=False)`——拿不到锁（已有扫描或集装箱循环在跑）直接 `return None`。
2. `miner_core.scan(ips, ...)`：线程池并发 `probe`，`RateLimiter` 限 `max_pps`。
3. `_reconfirm(miners)`：对"上次在线、本次掉线/零算力"的机器用宽松超时（连接2s/数据5s、关闭判活闸门、**保留 max_pps**）重探，恢复的覆盖回来——治网络抖动误判。`reconfirm_max` 上限防大面积掉线时无限速冲击三层。
4. `save_scan(keep_ips=roster)`：只落库在线机 + 名册内机（见 §6）。
5. `upsert_known_miners`：在线机写回名册。
6. `last_finished` 立即更新（看门狗据此判活），后续各步包 try/except 不互相影响。
7. `alerts.evaluate` / 集装箱评估 / `kick_offline_containers` / `prune`。
8. WS 推送。

### 5.2 告警状态机 `alerts.evaluate`
- 对比**同类**上一次扫描（`prev_scan_id(kind)`，避免 quick/full 混排污染基准）。
- **segment_down**：基于**当前绝对离线率**（不是"本轮新跌落"），分母只数 `roster` 内真机；离线率回落→恢复，但**只对"本轮有样本(seg_total>0)"的段判恢复**（整段掉出名册时保持告警，防长期断电被误恢复）。
- **offline**：维修中 / 被网段事件覆盖 → 不报；否则"上次在线本次离线"→ crit。
- **zero**：`hr_rt==0` 才算（`None`=读不到，不动）；维修中 / `uptime<grace`（刚开机升频）→ 不报。
- **reject**：只在 accepted/rejected 非空时算，拒绝率 ≥ 阈值 → warn。
- **cooldown**：刚恢复不久（`< cooldown`）的同机同类抖动不再报（`db.recent_alert`）。
- **恢复**用 `db.active_alerts_by_type`（**无 limit**，避免大面积告警时早期告警被 `list_alerts` 的 200 条截断而永不恢复）。

### 5.3 集装箱循环 `_container_loop`（10s）
独立于矿机扫描，只打 `/cooler`。与全网扫描**共用 `_scan_lock` 互斥**（全扫在跑时本轮跳过，全扫已处理集装箱）。`evaluate_containers` 做故障位/压力阈值告警 + 箱体离线判定 + 恢复消警。

### 5.4 看门狗 `_watchdog_loop`（60s）
超过 `watchdog_minutes`（0=`scan_interval×3`）无成功扫描 → `stalled` crit 告警 + Telegram。看门狗自身 try/except（"检测停摆的机制不能自己先停摆"）。

---

## 6. 关键设计决策

- **H7：死 IP 不落库**。全扫 2 万 IP 里约 1.5 万是从未是矿机的死 IP；`save_scan(keep_ips=roster)` 只存"在线机 + 名册内（曾在线/维修）的离线机"，每次约 5 千行而非 2 万行（省 74% 写放大）。`scans.total/online/offline` 随之表示**真实机器**口径（前端"总数/掉线"更有意义）。首轮名册为空时退化为全量落库（否则 offline 恒 0）。
- **机器身份按 IP 而非 SN**（已知局限，**人工处理，不改代码**）：机器换 IP（动态→静态）时旧 IP 在名册滞留 7 天成"幽灵"，可能误触发 segment_down。决定：运维对旧网段离线机点"下架移除"即可，或等 7 天名册过期。
- **RBAC**：`viewer<ops<admin`。读=viewer，写（命令/扫描/改设置/下架）=ops，审计/改网段=admin。**关 auth=匿名只读**（viewer），写操作仍需开 auth 登录（不是"全员 admin"）。
- **CoPP 限速**：`max_pps` 令牌桶是核心保护；提高并发≠更快，反而更易冲破 ARP 限制（要慢扫就低并发）。

---

## 7. 配置参考

三处配置，优先级 **运行时 settings > segments.json > config.yaml**：

| 来源 | 内容 | 谁改 |
|---|---|---|
| `config.yaml` | 全部默认（扫描/告警/控制/鉴权/db/server/telegram/public_api） | 手工，启动加载 |
| `segments.json` | 扫描网段 + 主机号范围 | 网页「⚙网段」(admin)，即时生效 |
| 运行时 settings（`service.load_settings`） | `scan_interval`/`max_pps`/`container_interval` 等 | 网页「设置」，`apply_settings` 原地改 CFG + `SVC.wake()` 即时生效 |

**`config.yaml` 关键段**（详见文件内中文注释）：
- `scan`：`online_timeout`/`data_timeout`/`max_pps`/`liveness_gate`/`reconfirm_*`/`roster_retention_days`/`passwords`。
- `schedule`：`scan_interval`(默认300)/`container_interval`(10)/`watchdog_minutes`。
- `alerts`：`cooldown`/`zero_grace_sec`/`reject_pct`/`segment_down_ratio/min`/`container_faults_ignore`/压力阈值。
- `control`：`enabled`(总开关)/`uniplus_password`/`timeout`/`max_batch`(批量上限)。
- `auth`：`enabled`/`secure_cookie`/`users`(password 用 `python auth.py 新密码` 生成的 pbkdf2$ 哈希)。
- `db`：`path`/`retention_days`(默认3，影响客户报表最长周期)。
- `public_api`：`token`(外部 Agent API 共享密钥，空=该 API 关闭/404)。

---

## 8. 线程与并发模型

- FastAPI **同步端点**跑在 uvicorn 线程池；后台 3 个 daemon 线程（`_loop`/`_container_loop`/`_watchdog_loop`）。
- DB 单连接 + `RLock`（可重入，读函数可嵌套调用）；写用 `with _lock, conn:`。
- `_scan_lock`(普通 Lock，非重入)：保证同一时刻只有一类扫描在发包（护 CoPP + 防并发评估重复推送）。
- WS 广播：后台线程经 `asyncio.run_coroutine_threadsafe` 投递到主事件循环；`done_callback` 清理死连接。
- 会话/限流字典在 `auth._slock` 下；登录失败按来源 IP 限流（8 次锁 5 分钟）。

---

## 9. 运行与开发

```bash
pip install -r requirements.txt        # requests fastapi uvicorn PyYAML
python server.py                       # 面板+后台巡检，默认 0.0.0.0:8800
# 改密码哈希：
python auth.py 新密码                   # 输出 pbkdf2$... 填到 config.yaml
# 不依赖服务的 CLI：
python scan_miners.py --lo 100 --hi 160
```

- **端口**：`config.yaml > server.port`（默认 8800）。
- **重启生效范围**：改 `config.yaml` 需重启；改网段/`scan_interval`/`max_pps`/`container_interval` 网页即时生效。
- **进程守护**：当前无自启；生产建议交 systemd / NSSM（`stalled` 告警可兜底发现进程死亡）。

---

## 10. 约定与坑（务必遵守）

1. **三处字段名一致**：core `_blank` / DB 列 / 前端字段。改一个改三个。
2. **算力千进制**：UI 按 1000TH=1PH=…自动选单位（`fmtHash`）；DB/接口内部一律存原始 **TH** 数值。
3. **前端 XSS**：所有设备/矿池返回串经 `app.js > esc()` 转义后再 innerHTML（model/worker/sn/note/detail/故障label）。
4. **告警恢复别用 `list_alerts`**（有 200 条截断）；按类型用 `active_alerts_by_type`。
5. **save_scan 别把死 IP 落库**：用 `keep_ips=roster`（见 H7）。
6. **PowerShell 控制台 GBK** 会乱码中文；调试用 `PYTHONUTF8=1 python ...`。
7. **改设置后** 记得 `SVC.wake()` 才能即时生效。
8. 已做 3 轮深度审计修复约 57 项；改动前优先看本文与 README，别重复造已修过的坑。
