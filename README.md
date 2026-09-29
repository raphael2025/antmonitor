# 矿机监控系统 (Miner Monitor)

扫描局域网矿机、自动识别固件、读取算力，提供 **Web 面板 + 定时巡检 + 主动告警**。
已提供 **Agent 公共 API**（`/api/public/*`，Token 鉴权、只读）供 hermes 等 AI agent 取数。

| 仓库 | 地址 | 职责 |
|---|---|---|
| **本仓库（场地本地）** | https://github.com/raphael2025/miner-monitor | 内网扫描 / 运维面板 / 告警 / 远程命令 |
| **云端总览（多场地）** | https://github.com/raphael2025/cloud-overview | 云服务器汇聚各场地摘要看板 |

> 📚 **文档**：[开发文档](docs/DEVELOPMENT.md) ｜ [内部API-矿机接口](docs/INTERNAL_API.md) ｜
> [外部API-Agent取数接口](docs/EXTERNAL_API.md)（详细） ｜ **[运维安全手册](docs/SECURITY.md)（上线前必读）**

> 算力按**千进制**显示：1000 TH = 1 PH，1000 PH = 1 EH（总算力/客户/趋势自动选单位；
> 单机一般仍为 TH）。CSV/接口内部仍存原始 TH 数值。

## 支持的设备 / 接口

| 设备 | 探测 | 数据来源 | 认证 |
|---|---|---|---|
| 原厂 Bitmain 矿机 | `GET http://IP:6060/get_sn`（明文 SN，快） | `GET /cgi-bin/stats.cgi` | Digest（默认 root/root，可配多组） |
| 第三方 UniPlusOS 矿机 | `GET http://IP/api/v1/summary` | 同左 | 免认证 |
| **AntBox 水冷集装箱** | `GET http://IP/cooler?operation=coolerState` | 同左 + `minerInfo` | 免认证 |

> 扫描时按 矿机→AntBox 顺序探测，与矿机**同网段自动发现**集装箱（控制器响应慢，给更宽超时）。
> 集装箱**记忆持久化**（`containers` 表）+ **独立高频刷新**：全量扫描(每天)发现新箱；已知箱由专门的
> 循环每 `schedule.container_interval`(默认10s)只打 `/cooler` 刷新（50箱@10s≈123kbps，带宽可忽略，
> 与矿机5min巡检解耦）；离线超 `scan.container_offline_kick_sec`(默认24h) 自动踢除。

## 集装箱 (AntBox) 监控

读取每个水冷集装箱控制器：**进/出水温、ΔT、供回液压力、流量、箱内温湿度、冷却塔进水温、
设定温度、总功耗(两路配电之和，卡片头部 ⚡ 显示 kW/MW)、泵/风扇状态、箱内矿机数/芯片温**。面板「🧊 集装箱」面板按箱卡片展示，
点击看进/出水/箱内温历史曲线。**冷却故障告警**（漏液/泵故障/断流/水温过高/液位/冻结/断相…，
共 20+ 故障位）→ 告警 + 语音 + 推送；箱体控制器离线 → `cooler_offline`。矿机↔箱体：
箱体 `minerInfo` 汇报其箱内矿机数/成员（populated 时关联）。集装箱随快巡检高频更新。

**在线规则**：任一探测在 `online_timeout`（默认 0.5s）内有响应即在线；
6060 连得上但读不到 SN（返回 error/空）也算在线，SN 记为 `N/A`。

## 项目结构

```
miner_core.py     扫描核心（探测/并发/排名/基线），无副作用，纯数据
db.py             SQLite：扫描快照 + 告警 + 计费聚合 + 命令审计；也是运维CLI(见下)
alerts.py         告警规则评估 + 推送（批量写库，一轮评估只 2 读 2 写）
control.py        远程命令层（重启/定位灯/换矿池，原厂+第三方双路）
auth.py           登录 + 角色权限（admin/ops/viewer）
appconfig.py      配置装载/默认值/校验 + settings.json、segments.json 读写
logs.py           统一日志（控制台 + 轮转文件）
service.py        扫描编排 + 定时巡检/看门狗/数据库维护线程
server.py         FastAPI：JSON API + 托管前端 + 鉴权
web/              前端面板（index.html / style.css / app.js，Chart.js 趋势图）
scan_miners.py    命令行扫描器（薄封装，复用 miner_core）
backup_db.py      SQLite 在线热备份 + 完整性校验 + 滚动保留
tests/            pytest：告警状态机 / 计费 / 扫描编排（`python -m pytest tests/ -q`）
config.yaml       配置（不入 git，模板见 config.example.yaml）
```

### 数据库运维 CLI

```bash
python db.py stats        # 主库/WAL 大小、报表可覆盖小时数
python db.py checkpoint   # 立即把 WAL 合并回主库并截断
python db.py rollup       # 立即补跑计费小时聚合
python db.py vacuum       # 回收删除留下的空页（独占数据库，放低峰期）
```
> 服务运行时这三件事由后台维护线程按 `db.checkpoint_minutes` / `db.vacuum_hour` 自动做，
> CLI 只是需要手动介入时用。

## 登录与权限（RBAC）

`config.yaml` 的 `auth` 段配置用户与角色（`enabled:false` 则免登录）：
- **viewer**：只读监控（隐藏所有写操作入口）
- **ops**：监控 + 远程命令 + 触发扫描
- **admin**：全部（含命令审计日志）

会话基于 Cookie 令牌（内存存储，重启需重新登录；12 小时不活动自动登出）。

- **改密码**：右上「🔑 改密码」（即时生效），或在监控电脑上 `python auth.py passwd 用户名`
  （自动写哈希进 config.yaml，**重启服务后生效**，重启前旧密码仍可用）。
- **弱密码**（默认密码、少于 8 位、少于 4 种不同字符、或与用户名相同）：登录后只**提示**尽快改密，
  **不锁权限**——局域网/Ubuntu 服务器部署时远程也能正常运维、也能改密码（可关掉提示继续用）。
- 用域名（而不是 IP）访问面板时，需把域名加到 `server.allowed_hosts`（防 DNS 重绑定）。
- 完整的安全配置（矿池白名单、交换机 ACL、防火墙、HTTPS）见 [运维安全手册](docs/SECURITY.md)。

## 远程命令与批量操作

表格勾选矿机（或「选中当前筛选」），用命令栏批量执行：

| 命令 | 原厂 Bitmain | 第三方 UniPlusOS | 面板入口 | 破坏性 |
|---|---|---|---|---|
| 💡 定位灯 | `blink.cgi` | `find-miner`（免解锁） | 有 | 否 |
| ⟳ 重启 | `reboot.cgi` | `system/reboot`（需解锁） | 有 | 是·二次确认 |
| ⚙ 换矿池 | `set_miner_conf.cgi` | `settings`（需解锁） | **无**（仅 API） | 是·二次确认 |

**原厂重启说明**：`GET /cgi-bin/reboot.cgi`（与原厂网页一致，个别固件回 405 时改用 POST）。
矿机收到后常常不回响应就断开（已经在重启），这种情况按「已下发」算成功，不会补发。
下发后进入 `control.reboot_grace_sec`（默认 600 秒）**静默期**：期间掉线不报警、不算进网段掉线比例；
过了静默期仍不在线 → 报「重启后 N 分钟仍未上线」。刚开机的零算力/掉算力本来就有 `zero_grace_sec` 宽限。

**重启限流（手动 + 自动共用）**：同一 IP 在滚动 **24 小时内最多 4 次成功重启**
（`control.reboot_max_per_day`，只计 `command_log` 里 `ok=1`），且距上次成功至少
**15 分钟**（`control.reboot_min_interval_sec=900`）。超限则跳过并写审计，不打断整批。

**掉线自动重启**：面板顶部「掉线自动重启」开关（`control.reboot_enabled`，**默认关**）。
打开后，每轮扫描评估到名册内掉线、且不在维修 / 重启静默期 / 整段掉线静音内时，自动下发重启
（审计用户 `system@auto-reboot`）。开关**只挡自动**；手动重启始终可用，但仍走同一套限流。
顶部还可改每批并发（`reboot_concurrency`）与批间隔秒数（`reboot_delay_sec`），保存走
`/api/settings`（admin），热生效。

**换矿池**：面板已去掉「换矿池」按钮；`POST /api/command` `action=set_pools` 仍保留。
必须在 `config.yaml` 配 `control.pool_allowlist`（如 `["f2pool.com"]`，子域名自动放行），
未配置则 API 也拒绝换池。扫描时发现矿机上配了白名单外的矿池（含备用池）→ 严重告警
「矿池不在白名单（疑似被篡改）」。重启/换池/被拒的换池尝试都推 Telegram。

破坏性命令弹窗强制二次确认；所有命令写入 `command_log` 审计表（admin 可查 `/api/commands`）。
第三方解锁密码配 `control.uniplus_password`，原厂密码复用 `scan.passwords`。

## 矿工名分组（吸收自旧 antbox）

扫描时对在线机额外查 **cgminer 4028 端口的 `pools` 命令**取矿工名（矿池 User，明文、
跨固件通用——第三方 HTTP 接口会把 User 打码成 `*****`，4028 不会）。面板「矿工名」标签页
按矿工名/客户聚合：总台数、总算力、机型分布。可在 `scan.fetch_worker` 关闭。

## 实时推送（WebSocket）

面板通过 `/ws` 长连接接收推送：每次扫描完成/告警触发，后台线程即时推送，前端秒级刷新
（轮询降级为 30s 兜底）。WS 同样校验登录 Cookie。

## 货架/机位视图

按 /24 网段自动聚合成「货架」，最后一段为机位号，无需手工映射。
每个机位按状态着色（正常/低算力/过温/无算力/离线·空位），异常一眼可见，
点击机位直达单机详情。面板「全部矿机」右上「列表 / 货架」切换。

## 快速开始

```bash
pip install -r requirements.txt
python server.py            # 启动面板 + 后台定时巡检（Windows 推荐双击 run.bat，带崩溃自动拉起）
```

- **Windows 下启动后自动打开本机浏览器**（`server.open_browser: false` 可关；run.bat 守护重启/自动更新后不会重复弹窗）。
- **不知道本机 IP？** 看启动日志开头的「面板地址」几行：本机用 `http://127.0.0.1:8800`，
  局域网其它电脑用日志里列出的 `http://<局域网IP>:8800`。

启动后按 `config.yaml` 节奏运行：
- **巡检** `schedule.scan_interval`（默认 300s=5分钟）：每 5 分钟探一遍名册内的已知矿机，
  掉线/限电上下线/掉算力都在 5 分钟内捕捉。
- **全网发现** `schedule.full_interval`（默认 3600s=1小时）：展开全部网段找新装的机器。
  两种扫描的并发都受 `scan.max_pps`(默认 **300**) 限速，保护三层 CoPP（详见下文「扫描性能与负载控制」）。
  全网发现整体超时下限 **15 分钟**（`max(900, 地址数/pps×3)`），避免大网段扫到一半被裁掉漏新机。
- **集装箱刷新** `schedule.container_interval`（默认 10s）：独立高频刷新冷却数据。

> 60 段(~1.5万IP)实测：每次全扫 ~150s，ARP≈100/s（你的 500 CoPP 有余量），带宽峰值 ~26 Mbps(数据平面，不占CoPP)。

## 命令行（不依赖服务）

```bash
python scan_miners.py --lo 100 --hi 160        # 扫描出排名 + CSV
python scan_miners.py --report miners_xxx.csv  # 只从 CSV 出报告
```

## Web API（前端用，也是未来 MCP/HTTP 取数的基础）

| 接口 | 说明 |
|---|---|
| `GET /api/summary` | 总览：在线/总/平均算力、固件分布、基线、Top/Bottom、总功耗、平均能效、告警数 |
| `GET /api/miners?status=&fw=&q=&sort=&order=` | 矿机列表（含功耗/能效/温度/运行时长，可筛选/搜索/排序） |
| `GET /api/miner/{ip}` | 单机当前状态 + 历史曲线 |
| `GET /api/racks` | 货架视图数据（按 /24 聚合，机位状态着色） |
| `GET /api/workers` | 矿工名分组（总台数/总算力/机型分布） |
| `GET /api/containers` · `/api/container/{ip}` | 集装箱列表 / 单箱详情+水温历史 |
| `WS /ws` | 实时推送（扫描完成/告警），需登录 Cookie |
| `GET /api/alerts?active=true` | 告警列表 |
| `GET /api/trend?points=288` | 总算力历史趋势 |
| `GET /api/segments` · `POST /api/segments` | 读取 / 保存扫描网段（保存需 admin） |
| `POST /api/scan?kind=full\|quick` | 手动触发扫描（需 ops+） |
| `POST /api/command` | 远程命令 `{ips,action,params}`（需 ops+） |
| `GET /api/commands` | 命令审计日志（需 admin） |
| `POST /api/login` · `/api/logout` · `GET /api/me` | 登录 / 登出 / 当前用户 |
| `GET /api/progress` | 当前扫描进度 |

> 读接口需 viewer+，写接口需 ops+，审计需 admin。未登录返回 401，权限不足返回 403。

## 网段设置 / 按网段看算力

- 网页右上「⚙ 网段」（仅 admin）：每行一个网段，设主机号范围，存 `segments.json`，可「保存并全网扫描」。
- 「全部矿机」筛选栏有 **网段下拉**：选某网段即在右侧看该段 **在线数 / 算力 / 功耗**；货架视图每段也直接显示算力。

## 机器生命周期（限电 / 维修 / 下架）

- 限电拉闸下线、来电恢复上线，靠每 5 分钟的巡检自动捕捉。
- **维修中**：选中机器→「🔧维修中」。不再报离线告警、列表显示"维修中"标签，修好重新上线**自动恢复正常**。
- **下架移除**：选中→「🗑下架移除」从名册删除（不再告警）；若重装上线，下次全网扫描当新机重新纳入。

## 扫描性能与负载控制（针对三层 500pps CoPP）

**两档扫描（这是容量设计的核心，别再合成一档）：**

| | 巡检 quick | 全网发现 full |
|---|---|---|
| 目标 | 名册内已知真机（约5千台） | 展开全部网段（约1.5万地址） |
| 间隔 | `schedule.scan_interval`（默认300s） | `schedule.full_interval`（默认3600s） |
| 抓什么 | 上线/掉线/掉算力/高温，日常主力 | 新装机/新网段 |

死 IP 的判活超时是全扫耗时的主要来源。若把两档合成「每5分钟全扫一次」，扫描耗时会
逼近间隔本身，系统长期处于「永远在扫」状态：既压三层网络，也让 SQLite 的 WAL
checkpoint 永远追不上（本项目曾因此把 WAL 涨到 800MB）。单轮耗时超过巡检间隔时日志会告警。

- **快速判活闸门**（`scan.liveness_gate`）：先 1 次 TCP 连 80 判活（原厂/第三方/AntBox 三类都开80），
  死 IP 直接判离线，不再白跑 3 个探测。（6060 只对原厂有效，故用 80 通用判活。）
  `gate_timeout` 必须明显大于到矿机的 RTT，否则整网假离线。
- **ARP 限速**（`scan.max_pps`，默认 **300**）：令牌桶限制每秒新建连接(≈死IP的 ARP 速率)，
  护住三层的 ARP/控制平面(CoPP)限制。`discovery_workers` 是并发上限，实际由 max_pps 节流。
  网页「网段」设置也可改，写入 `settings.json` 热生效；合法范围 1~500。
- 数据流量（读矿机/箱子）是硬件转发的数据平面，**不占 CoPP**，无需限制。

## 告警确认与防误判

- **二次确认**：某机「上次在线、本次掉线/零算力」时，立刻用更宽松超时（连接2s/数据5s、关判活闸门）
  单独重探一遍，仍失败才报警——避免网络抖动/响应慢造成的误判（只重探掉下来的真机，成本极低）。
- **告警时间戳**：每条告警显示首次出现时间 + 相对时间（如「28分钟前」），老的没处理一眼可见。
- **确认按钮**：矿机告警可点「确认」标记处理中（显示"✓ 用户 已确认"、行变灰），多人值班不重复跑；
  集装箱告警不提供确认（修好自动消失）。告警在条件恢复时仍自动消除。
- **语音只报台数**：出现新告警时播报「掉线 N 台，零算力 N 台，集装箱故障 N 处」，不念 IP。

## 告警规则

- **掉线** offline：上次在线、本次超时 → crit
- **零算力** zero：在线但实时算力**明确为 0** → warn。
  注意：算力读不到(null，接口超时/密码错)**不算**零算力，既不报也不清——
  否则一次接口抖动就误报一片。面板对应显示「无数据」而不是红色的 0。
- **掉算力** low_hashrate：算力低于**同机型在线机中位数**的 `low_hashrate_ratio`(默认70%)，
  且连续 `low_hashrate_rounds`(默认2) 轮成立 → warn。矿场最常见的故障形态
  （算力板/风扇坏一块，机器还"在线且有算力"），只看零算力是完全静默的。
- **高温** overheat：芯片温 ≥ `overheat_c`(默认95℃) → crit，回落到 `overheat_clear_c`(90℃)
  以下才消警（滞回，防临界值反复刷屏）
- **拒绝率** reject：拒绝率 ≥ `alerts.reject_pct`(默认5%) → warn（矿池健康，4028 取 accepted/rejected/stale）
- **网段事件** segment_down：某段「已知真机」掉线比例 ≥ `segment_down_ratio`(0.6) 且数量 ≥ `segment_down_min`(5)
  → **合并成一条事件**（疑似交换机/断电），并抑制该段的单条掉线/零算力，防刷屏
- **监控停滞** stalled（看门狗）：超过 `schedule.watchdog_minutes`(0=按扫描间隔自动推算) 无成功扫描
  → crit，防止扫描进程悄悄死掉。建议进程再交 systemd/NSSM 守护。

> 同机同类告警恢复后自动 resolve；标记「维修中」的机器完全静音；货架配色：正常(绿)/零算力(黄)/离线(红)/空位(灰)。

## 客户报表（内部对账）

「矿工名」标签页选周期（当前/24h/3天/7天/30天/90天）：当前=机型分布；周期=按客户出
**可用率% / 交付算力 TH·h / 耗电 kWh**（停机正确归属到该机已知矿工名）。用于托管对账。

**长周期怎么做到的**：明细快照按 `db.retention_days`(默认3天)清理，但每小时会把
「每客户的交付算力/耗电/在线样本」归档进 `worker_hourly` 表，保留 `db.rollup_retention_days`
(默认400天)。报表 = 已归档小时 + 当前未归档小时拼接，所以明细删了月度对账照样算得出。
一天只增几百行。清理逻辑有硬保证：**删除线永远不越过归档进度**，没归档的明细一条都不删。

> 计量口径：单次扫描代表「到下一次扫描」的时长，且单次最多计 15 分钟——
> 监控自己停摆时不会把停机时间按最后一次读数算成交付算力。

## 告警通道

- **浏览器语音告警**：面板右上「🔇 语音告警」按钮开启（点击即解锁音频）。出现新告警时：
  掉线→蜂鸣 + 中文语音播报 IP（"警告，N 台矿机掉线，172 点 16 点…"）+ 桌面通知；
  过温/严重→蜂鸣 + 播报；恢复→提示音。标题栏显示活跃告警数 `(N)`。偏好存 localStorage。
- **Telegram 推送**：在 `config.yaml` 填 `telegram.bot_token` 与 `chat_id`，`enabled: true` 即开启。

## 云端总览上报（多场地汇聚）

配好 `config.yaml > cloud` 段后，本地每分钟向**云端总览**主动推送场地摘要
（在线/算力/功耗/告警/集装箱，几KB/次）+ 每10分钟推客户报表。

- **云端项目**：https://github.com/raphael2025/cloud-overview  
  （多场地汇聚看板，部署在云服务器；本地本仓库负责运维与完整历史。）
- 方向是本地→云端，**场地在 NAT 后面不需要任何端口映射**；第一次上报云端自动注册本场地。
- 断网自动中断（云端显示"未收到上报"），恢复自动续传；上报线程独立，不影响扫描/告警。
- 公网云端地址须用 **https**（明文 HTTP 只放行内网调试地址）。

**场地身份 = 唯一 site_id**：首次启动自动生成存 `cloud_site_id.txt`（启动日志会打印），
云端按它识别本场地——`site_name` 随时可改不丢数据。
⚠ 整目录克隆部署到新场地时必须删掉 `cloud_site_id.txt`（自动重新生成），否则两场地同ID互相覆盖。

```yaml
cloud:
  enabled: true
  url: "https://overview.example.com"   # 云端总览地址（见 cloud-overview 部署文档）
  token: "<云端 config.yaml > ingest.token>"
  site_name: "内蒙一场"                  # 云端显示名(全网唯一)
  site_type: air                         # air=风冷 | hydro=水冷
```

## 版本更新（git 更新器）

以 git clone 方式部署后，可检测远端新版本并更新（配置/数据被 .gitignore 保护，更新永不触碰）：

- **网页一键更新**（admin）：右上「⬆ 版本」按钮，有新版本时变绿显示「有新版本(N)」（每 30 分钟自查，
  服务端缓存 10 分钟）。点开看更新内容 → 「立即更新并重启」→ 拉取 + 自检（不通过自动回滚、不重启）→ 重启，
  页面等服务回来后自动刷新（会话在内存里，需重新登录）。
  重启方式自动判断：run.bat / NSSM 等 Windows 服务 / systemd 下退出码 42 交给守护拉起；
  直接 `python server.py` 启动（没有守护）时程序自己拉起新进程，不会"点完更新监控就没了"。
  注意：目录里有未提交的改动或多出的未跟踪文件时会拒绝更新（防止覆盖现场手改）。
- 自动：`config.yaml > update.auto: true`，每小时自查，有新版本自动拉取+编译自检+重启
  （需 NSSM 守护或用 `run.bat` 启动——退出后 3 秒自动重新拉起）
- 手动：机器上跑 `python updater.py check / apply`（apply 后重启服务），
  或 API `GET /api/update/check`、`POST /api/update/apply`（admin，apply 自动重启）
- 语法错误的坏版本会被编译自检拦下并**自动回滚**；云端侧发布/场地接入见
  [cloud-overview/docs/DEPLOY.md](https://github.com/raphael2025/cloud-overview/blob/master/docs/DEPLOY.md)

## 后续规划（功能定稿后再做）

1. **MCP server**：把 summary/top/bottom/miner/alerts 暴露成 tool，供 hermes/openclaw 调用。
2. **访问控制**：API token + Telegram chat_id 白名单（算力/SN/拓扑属敏感数据）。
3. 机型/网段分组统计、历史趋势对比、导出报表。

## 依赖

Python 3.8+：`requests fastapi uvicorn websockets PyYAML`（运行必需）、`httpx`（跑测试）、
`openpyxl`（仅 `antfleet/` 改 IP 工具用）。一律以 requirements.txt 为准：`pip install -r requirements.txt`
