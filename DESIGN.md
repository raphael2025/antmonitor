# 矿场托管运维平台 — 详细设计文档

> 目标场景：**托管/包机（多客户）· 多场地 · 万台以上**。
> 现有单进程 + SQLite 版本（见 README.md）将演进为分布式平台。本文为重构设计依据。

---

## 1. 背景、目标与非目标

**现状**：单进程 FastAPI（扫描线程 + SQLite + Web + WS），适合单场 ≤2000 台。
**问题**：① 跨 WAN 无法集中扫万台；② SQLite 扛不住千万级/日时序写入；③ 无 SN 资产模型、无多租户、无 SLA 报表；④ 采集与 Web 同进程、无自监控。

**目标**
- 支撑 **多场地、≥1 万台**（设计上限 5 万）稳定采集与展示。
- **托管对账**：按客户出可用率、交付算力(TH·h)、功耗(kWh)、停机事件。
- **多租户**：客户登录只读自己机群 + 自助报表；运维分场地/角色作业。
- **稳态运维**：网段级故障去重、告警认领/升级、维护模式、自监控看门狗。

**非目标（本期不做）**
- 自建 stratum 代理（份额级实时）——列为远期。
- 利润/币价计算、电力市场竞价——后续模块。

---

## 2. 总体架构

```
   场地A             场地B             场地C
 ┌────────┐       ┌────────┐       ┌────────┐
 │采集器Agent│      │采集器Agent│      │采集器Agent│
 │ 本地扫描  │      │ 断网缓存  │      │ 远程控制  │
 └───┬────┘       └───┬────┘       └───┬────┘
     │  HTTPS + Collector-Key  批量上报 / 心跳
     └───────────────┼───────────────┘
            ┌─────────▼──────────┐
            │   中心服务 (无状态)   │  FastAPI ×N, nginx 负载
            │  Ingest / Ops / Portal API + WS │
            └────┬───────────┬────────┬───┘
        ┌────────▼──┐  ┌─────▼─────┐ ┌▼─────────┐
        │ PostgreSQL │  │TimescaleDB│ │  Redis    │
        │ 资产/客户/  │  │ 指标时序   │ │ WS pub/sub│
        │ 告警/工单/  │  │ +连续聚合  │ │ +会话/限速│
        │ SLA/用户    │  └───────────┘ └──────────┘
        └────────────┘
```

**组件职责**
| 组件 | 职责 | 由现有代码演化 |
|---|---|---|
| 采集器 Agent | 本地扫描(4028/HTTP)、取算力/矿工名/拒绝率、执行远程命令、断网缓存重传、心跳 | `miner_core` + `control` |
| 中心 Ingest API | 接收上报、Upsert 资产、写时序、跑告警/事件、worker→客户映射 | 新增 |
| 中心 Ops/Portal API + Web | 给运维/客户的查询、报表、操作、WS 推送 | `server`/`auth`/`alerts`/`web` |
| PostgreSQL | 资产/客户/位置/告警/工单/SLA/用户（关系型） | 由 SQLite 迁移 |
| TimescaleDB | 指标时序 + 连续聚合（降采样） | 新增（可与 PG 同实例） |
| Redis | 多实例 WS 广播、会话、限速 | 新增 |

> Timescale 是 Postgres 扩展，可与关系库**同一实例**，运维只多一个 Redis。

---

## 3. 数据模型

### 3.1 关系库（PostgreSQL）

```sql
-- 场地与采集器
sites(id, name, location, timezone, created_at)
collectors(id, site_id→sites, name, api_key_hash, version,
           last_heartbeat, status)               -- 在线/离线
segments(id, site_id→sites, prefix, host_start, host_end, enabled)

-- 物理位置（用于派工定位）
locations(id, site_id→sites, building, room, row, rack, position)

-- 客户与归属映射
customers(id, name, contact, contract_no, created_at, active)
worker_patterns(id, customer_id→customers, pattern, kind)  -- prefix|regex，矿池worker→客户

-- 资产：以 SN 为主键（IP 会变，SN 稳定）
machines(
  sn PK, site_id→sites, customer_id→customers NULL, location_id→locations NULL,
  model, firmware, current_ip, rate_nominal_t,
  first_seen, last_seen, status,                 -- online|offline|zero
  decommissioned_at NULL
)

-- 告警与事件（事件用于网段/场地级去重聚合）
incidents(id, site_id, scope, type, summary, severity,
          opened_at, resolved_at, affected_count) -- scope: machine|segment|site|power
alerts(id, sn→machines, incident_id→incidents NULL, type, severity,
       opened_at, ack_by→users NULL, ack_at, resolved_at, escalated_at)
tickets(id, incident_id NULL, sn NULL, assignee→users NULL,
        status, notes, created_at, closed_at)     -- 工单/检修

-- 用户与权限（含客户门户用户）
users(id, username, password_hash, role,           -- admin|ops|viewer|customer
      customer_id→customers NULL,                  -- 客户用户绑定其归属
      created_at, last_login, active)

-- 运维与配置
command_log(id, ts, user_id, action, sn, ip, ok, msg, approved_by NULL)
alert_rules(id, type, threshold, severity, enabled, scope)
notify_channels(id, kind, config_json, enabled)    -- telegram|email|webhook|voice
maintenance_windows(id, scope, target, start_at, end_at, by_user) -- 静音
sla_contracts(id, customer_id, uptime_target, power_rate, hosting_rate, period)
```

### 3.2 时序库（TimescaleDB hypertable）

```sql
-- 原始指标（每次扫描每台一行）
CREATE TABLE metrics (
  time TIMESTAMPTZ NOT NULL,
  sn   TEXT NOT NULL,
  status SMALLINT,           -- 0离线 1正常 2零算力
  hr_rt REAL, hr_avg REAL,   -- TH/s
  power INT, temp SMALLINT, eff REAL, uptime INT,
  accepted BIGINT, rejected BIGINT, stale BIGINT   -- 矿池健康
);
SELECT create_hypertable('metrics','time');
-- 连续聚合：5分钟、1小时（SLA 报表查聚合表，原始仅留 7-14 天）
CREATE MATERIALIZED VIEW metrics_5m  WITH (timescaledb.continuous) AS ...
CREATE MATERIALIZED VIEW metrics_1h  WITH (timescaledb.continuous) AS ...
-- 保留策略：raw 14天，5m 90天，1h 2年
```

> SN 作为时序主维度；机型/客户/位置等维度通过 `machines` join，避免在时序里冗余高基数标签。

---

## 4. 采集与上报协议

**采集节奏**（Agent 本地）
- 快巡检：每 2-5 分钟扫**已知在线 SN/IP**，抓 掉线/零算力/拒绝率。
- 全网发现：每日/手动，扫本场全部网段，发现新机/移机。
- 取数：4028 `stats`(算力/型号/温/功耗) + `pools`(accepted/rejected/stale/worker)；原厂可回退 HTTP。

**上报**（Agent → 中心）
```
POST /api/ingest/scan        Header: X-Collector-Key: <key>
{
  "collector":"siteA-01","site":"A","ts":1782660000,"kind":"quick",
  "results":[
    {"ip":"172.16.119.21","sn":"HQDZ...","status":"online","firmware":"uniplus",
     "model":"S19 XP+ Hydro","hr_rt":336.2,"hr_avg":335.1,"power":5958,"temp":71,
     "eff":17.78,"uptime":84703,"worker":"ass19xphyd450",
     "accepted":12284,"rejected":0,"stale":0}
  ]
}
POST /api/ingest/heartbeat   {"collector":..., "ts":..., "version":...}
```
- **认证**：每采集器一把 Key（中心存 hash），TLS 必须。
- **断网缓存**：中心不可达时 Agent 落本地队列文件，恢复后按原始 ts 重放（保证时序不丢洞）。
- **幂等**：(collector, ts, sn) 去重，重放安全。

**中心 Ingest 处理流水**
1. Upsert `machines`（按 SN）：更新 ip/model/fw/last_seen/status/location。
2. 批量写 `metrics`。
3. worker→`customers` 映射（按 worker_patterns）。
4. 跑告警/事件评估（见 §5）。
5. 触发 WS 推送（经 Redis pub/sub 广播到所有 Web 实例）。

---

## 5. 核心规则与算法

### 5.1 机器状态（保持精简两类告警）
- `offline`：上次在线、本次未响应。
- `zero`：在线但 hr_rt==0（含读不到）。
- 正常：hr_rt>0。

### 5.2 网段/场地级故障去重（关键）
> 一个交换机/回路挂掉，不该刷 254 条掉线。
- 单次扫描某网段「原在线」机器掉线比例 ≥ 阈值(如 60%) → 建一个 `incident(scope=segment)`，把这些机器的 offline 告警挂到该事件下、**合并成一条**通知。
- 整场地多网段同时大面积掉线 → 升级为 `incident(scope=site)`（疑似断网/断电）。
- 恢复时统一关闭事件。

### 5.3 SLA / 对账计算
- **可用率%** = 在线时长 / 周期总时长（按 `metrics_1h` 的 status 采样积分，或状态变迁日志）。
- **交付算力 TH·h** = Σ(hr_rt × 区间时长)，按客户聚合。
- **耗电 kWh** = Σ(power × 区间时长)，用于电费转售计量。
- 周期：日/月；输出每客户对账单（可用率达标与否、交付量、耗电）。

### 5.4 告警生命周期
`active → acknowledged(认领人/时间) → resolved`；未认领超 X 分钟 → `escalated`（升级渠道/电话）。维护窗口内的目标静音。

---

## 6. API 设计（分三类）

**Ingest API（采集器，Key 鉴权）**
`POST /api/ingest/scan` · `POST /api/ingest/heartbeat`

**Ops/Admin API（员工，Cookie+RBAC）**
- 监控：`/api/summary` `/api/miners` `/api/machine/{sn}` `/api/racks` `/api/workers` `/api/trend`
- 告警/事件：`/api/alerts` `/api/incidents` `POST /api/alerts/{id}/ack|resolve` `/api/tickets`
- 运维：`POST /api/command`（重启/定位/换池，下发到对应 Agent）`/api/commands`(审计) `/api/maintenance`
- 资产/配置：`/api/machines` `/api/customers` `/api/sites` `/api/collectors` `/api/segments` `/api/users`
- 报表：`/api/reports/uptime` `/api/reports/delivered` `/api/reports/power?customer=&period=`

**Customer Portal API（客户，作用域隔离）**
- `/api/portal/summary` `/api/portal/machines` `/api/portal/reports`
  —— 一律强制 `WHERE customer_id = 当前用户.customer_id`，**无任何控制接口**。

**实时**：`WS /ws`（员工）/ `WS /portal/ws`（客户）；多实例经 Redis 广播。

---

## 7. 多租户与权限

| 角色 | 范围 |
|---|---|
| admin | 全部 + 用户/场地/规则配置 |
| ops | 监控 + 远程命令 + 扫描（可限定场地） |
| viewer | 内部只读全场 |
| customer | **仅自己 customer_id 的机器**，只读 + 报表，无控制 |

- 客户数据隔离在 API 层强制过滤；门户独立前端，避免误暴露内部数据。
- 密码 **bcrypt/argon2 哈希**；会话存 Redis（重启不掉线）；登录限速。

---

## 8. 远程控制与安全

- 控制命令由中心下发到**目标机所在 Agent** 执行（中心不直连万台）。
- 破坏性命令（重启/换池）：ops+ 才能发、强制二次确认、**全程审计**，可选「批量>N 台需 admin 审批」。
- 客户角色**永不**获得控制能力。
- 传输全 TLS；采集器 Key、矿机密码、Telegram token 等放环境变量/密钥库，不入库明文。
- 网络分段：采集器在矿机内网，中心在管理网，最小放行。

---

## 9. 前端 / 信息架构

顶部**全局场地选择器**；左侧导航：

| 页面 | 受众 | 内容 |
|---|---|---|
| NOC 总览 | 值班/老板 | 集团+分场地：总算力 vs 应有、在线率、告警分级、问题流（大屏） |
| 告警/事件 | 运维 | 网段级聚合事件、确认/认领/升级、历史 |
| 矿机 | 运维 | 表格(SN视角)、保存筛选、批量操作 |
| 货架/机位 | 现场工 | 场地→楼栋→排→架→位 定位、点灯 |
| 客户 | 内部 | 每客户机群、可用率、交付算力、对账单 |
| 运维操作 | ops | 批量/固件/维护模式/命令审计 |
| 报表 | 管理 | 日/月可用率、交付、耗电、导出 |
| 设置 | admin | 场地/网段/采集器、用户角色、告警规则与渠道 |
| **客户门户** | 客户 | 独立精简、仅自己机群与报表 |

移动端：现场工精简视图（搜 SN/IP → 看故障 → 点定位灯）。

---

## 10. 通知与告警渠道
Telegram / 浏览器语音 / 桌面通知（已有）+ 邮件 / Webhook(钉钉飞书) / 关键级电话。
路由按场地/严重级/值班表；维护窗口静音；升级策略可配。

---

## 11. 可观测性 / 自监控（别让监控自己悄悄死）
- 采集器**心跳**：超 N 分钟无心跳 → 该场地「采集中断」告警。
- 中心**死人开关**：超 N 分钟无任何成功 ingest → 独立通道告警。
- 进程交 systemd / NSSM(Windows) 守护、自动重启。
- 暴露自身 metrics（扫描时延、入库延迟、队列积压）。

---

## 12. 技术选型与理由
| 选型 | 理由 |
|---|---|
| PostgreSQL + TimescaleDB | 一个库搞定关系+时序；连续聚合自动降采样，SLA 报表直接查；生态成熟 |
| FastAPI（沿用） | 已有代码可复用；异步、WS、依赖注入做 RBAC 顺手 |
| Redis | 多 Web 实例 WS 广播、会话、限速 |
| nginx | TLS 终止 + 负载 + 静态 |
| 采集器 Python（沿用 miner_core/control） | 复用现有扫描/控制逻辑，单文件可打包部署到各场地 |
| 备选 | 超高基数/更大规模时序可换 VictoriaMetrics；队列可上 NATS |

---

## 13. 容量估算（1 万台，扫描间隔 3 分钟）
- 原始指标：1e4 × 480/天 ≈ **480 万行/天**；保留 14 天 ≈ 6700 万行（Timescale 轻松）。
- 5 分钟聚合保留 90 天、1 小时聚合保留 2 年（1e4×24×365 ≈ 8760 万行/年）。
- 关系库：万级机器、百级客户、告警/工单——**负载可忽略**。
- 单中心实例（8C/32G + SSD）+ 1 Redis 可起步；Web 按需横向扩。

---

## 14. 迁移路线（复用现有代码，分阶段灰度）
1. **采集器化**：抽 `miner_core`+`control` 为独立 Agent（本地扫+上报+心跳+断网缓存）；中心加 Ingest API。先单场地打通 Agent→中心链路（双写 SQLite 验证）。
2. **换库 + 资产模型**：上 PostgreSQL/Timescale，建 SN资产/场地/客户/位置 表；worker→客户映射。
3. **托管化**：客户作用域 RBAC + 客户门户 + SLA/对账报表。
4. **稳态**：网段级事件去重 + 告警认领/升级 + 维护模式 + 看门狗自监控。
5. **多场地铺开**：多采集器、场地切换、权限按场地隔离。
6. **远期**：stratum 代理（份额级实时）、功率调度/限电、利润核算。

---

## 15. 风险与取舍
- **SN 不可读的机器**：少数固件 4028/HTTP 取不到 SN → 用 (site+mac) 兜底主键，台账标注待补。
- **worker→客户映射维护成本**：靠 pattern 自动归类 + 人工兜底未匹配项。
- **写库峰值**：全网发现与快巡检错峰；ingest 批量 + COPY 入库。
- **Timescale 运维**：连续聚合/保留策略需正确配置，否则磁盘膨胀。
- **控制权限面**：万台批量控制风险大，务必审批+审计+限速。
