# 外部 API 文档（供别的 Agent / 系统调用取数）

> **范围**：本文档描述**别的程序调用本监控系统取数**的 HTTP API，分两类访问方式：
> 1. **Agent 公共 API**（`/api/public/*`）——**给 AI Agent（Hermes 等）/外部脚本用**，Bearer Token 鉴权、**只读**、稳定字段、带单位后缀。**本文档重点。**
> 2. **会话 REST API**（其余 `/api/*`）——前端面板/运维用，Cookie + RBAC 鉴权，含写操作。
>
> 本系统**调矿机/集装箱设备**的接口（内部 API）见 [INTERNAL_API.md](INTERNAL_API.md)。
> 基址默认 `http://<服务器IP>:8800`。

---

## 一、Agent 公共 API（`/api/public/*`）— 重点

为外部 Agent 取数设计：**统一 Token、纯只读、稳定 schema、字段带单位后缀（`_ths`/`_w`/`_c`/`_s`）对 LLM 友好**。

### 鉴权

- 在 `config.yaml > public_api.token` 配置一个共享密钥（已配置即启用；**留空则整组端点返回 404**，fail-closed，不暴露存在性）。
- 调用方二选一传 Token：
  - **请求头（推荐）**：`Authorization: Bearer <token>`
  - **查询参数**：`?token=<token>`（方便浏览器/快速测试；⚠️ 会进访问日志，生产优先用请求头）
- Token 错误 → `401`；未配置 Token → `404`。

```bash
TOKEN=962b51b17a...                       # config.yaml > public_api.token
BASE=http://172.16.x.x:8800
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/public/summary
```

### 通用约定

- 所有响应是 JSON，含 `ok`(bool) 与 `ts`(本次响应的 Unix 秒)。出错时 `summary` 返回 `{ok:false,error}`（不抛 500）。
- 算力单位 **TH/s**（字段 `*_ths`），功率 **W**（`*_w`），温度 **℃**（`*_c`），时长 **秒**（`*_s`）。
- `/summary` 有 **10 秒进程内缓存**（`cached:true/false`），高频轮询不击穿 DB；其余端点实时查（WAL 读不阻塞扫描）。
- **能力边界**：公共 API **永远只读**（全是 GET，无任何控制/写操作）。远程重启/换矿池/下架等破坏性动作**不对 Agent 开放**，必须走人工审核的会话 API。

---

### 1. `GET /api/public/health` — 监控自身心跳

让 Agent 判断**监控系统本身是否还活着**（别被陈旧数据骗了报平安）。

**响应**
```json
{
  "ok": true, "ts": 1782753412,
  "scanning": false,              // 是否正在扫描
  "last_scan_ts": 1782753100,     // 上次成功完成扫描的时间戳(null=尚未完成首扫)
  "last_scan_age_s": 312,         // 距上次成功扫描多少秒(用它判断扫描是否停滞)
  "active_alerts": 7              // 当前活跃告警数
}
```
> 建议 Agent：`last_scan_age_s` 远超 `scan_interval`(默认300) 即视为监控停滞，需告警人工介入。

---

### 2. `GET /api/public/summary` — 全场总览（最常用）

**查询参数**：`?lite=1` 省略 `miners[]` 全量明细（其余统计字段不变）。
供云端总览等**每分钟轮询**的调用方省带宽（几千台时 ~750KB → 几KB）；旧版服务端忽略该参数（返回全量），向后兼容。

**响应**（节选关键字段）
```json
{
  "ok": true, "ts": 1782753412, "cached": false,
  "scanned": true, "scan_id": 142, "scan_ts": 1782753100, "scan_kind": "full",
  "total": 5180, "online": 3298, "offline": 1882,    // 真实机器口径(非扫描地址数)
  "with_hashrate": 3290, "no_hashrate": 8,
  "total_hashrate_ths": 1013328.0,                   // 总算力 TH/s
  "avg_hashrate_ths": 308.0,
  "total_power_kw": 18103.6,                         // 总功耗 kW
  "active_alerts": 7,
  "containers": 30, "containers_faulty": 2, "containers_offline": 1,
  "progress": {"running": false, "done": 0, "total": 0, "kind": "full", "last_finished": 1782753100},
  "miners": [ {"ip":"172.16.119.1","model":"...","firmware":"uniplus","status":"online",
               "hashrate_ths":328.68,"temp_c":72,"power_w":5876,"worker":"Asuna666"}, ... ]
}
```
> `miners[]` 是**精简全量列表**（每机 8 字段，低开销）。要更详细字段或筛选用 `/api/public/miners`。
> 未扫描时 `scanned:false`，统计字段为 0。

---

### 3. `GET /api/public/miners` — 矿机列表（可筛选）

**查询参数**（全部可选，可组合）

| 参数 | 说明 | 示例 |
|---|---|---|
| `status` | `online` / `offline` | `?status=online` |
| `fw` | 固件 `stock` / `uniplus` | `?fw=uniplus` |
| `seg` | 网段前三段精确匹配 | `?seg=172.16.119` |
| `q` | IP / SN / 矿工名 模糊匹配 | `?q=Asuna` |
| `limit` | 返回上限（默认 50000=全量） | `?limit=500` |

**响应**
```json
{
  "ok": true, "ts": 1782753412,
  "count": 1820, "online": 1820,
  "total_hashrate_ths": 598234.5, "total_power_w": 10693120,
  "miners": [ {
    "ip": "172.16.119.1", "status": "online", "firmware": "uniplus",
    "model": "Antminer S19 XP+ Hydro (Uniplus 1.1.5)", "sn": "", "worker": "Asuna666",
    "hashrate_ths": 328.68, "hashrate_avg_ths": 330.37, "power_w": 5876, "temp_c": 72,
    "efficiency_jth": 17.79, "uptime_s": 181033, "accepted": 27934, "rejected": 2
  }, ... ]
}
```
> ⚠️ 全场约 5 千台，无过滤拉全量会很大。给 LLM 用时建议加 `status=offline` 或 `seg=` 缩小，或只用 `/summary`。

---

### 4. `GET /api/public/miner/{ip}` — 单机详情 + 历史

**响应**
```json
{
  "ok": true, "ts": 1782753412, "found": true,
  "miner": { ...同上单机字段... },
  "history": [ {"ts":1782750000,"status":"online","hashrate_ths":328.7,"temp_c":71,"power_w":5870}, ... ]
}
```
> 不存在该 IP → `{"ok":true,"found":false,"ip":"..."}`。history 最多 200 点。

---

### 5. `GET /api/public/alerts` — 告警列表

**查询参数**：`?active=true`（默认，仅活跃）/ `?active=false`（含已恢复）。

**响应**
```json
{
  "ok": true, "ts": 1782753412, "count": 7,
  "alerts": [ {
    "id": 2891, "ts": 1782752000, "ip": "172.16.120.45",
    "type": "offline", "severity": "crit", "detail": "172.16.120.45 掉线",
    "resolved": false, "resolved_ts": null, "ack_by": null
  }, ... ]
}
```
**告警类型 `type`**：`offline`(掉线) / `zero`(零算力) / `reject`(拒绝率高) / `segment_down`(整段掉线) / `stalled`(监控停滞) / `cooler:<flag>`(集装箱故障) / `cooler_offline`(箱体离线)。
**级别 `severity`**：`crit` / `warn` / `info`。

---

### 6. `GET /api/public/containers` — 水冷集装箱

**响应**
```json
{
  "ok": true, "ts": 1782753412, "count": 30, "faulty": 2, "offline": 1,
  "containers": [ {
    "ip": "172.16.119.250", "online": 1,
    "supply_temp": 38.2, "return_temp": 45.1, "supply_pressure": 0.35, "return_pressure": 0.12,
    "flow": 120.5, "internal_temp": 41.0, "internal_humidity": 35,
    "set_temp": 40, "power1": 512000, "power2": 530000,
    "miner_num": 171, "chip_max_temp": 78,
    "pumps": {"循环泵": true, "喷淋泵": false, "风扇1": true, ...},
    "faults": [ {"flag":"supply_liquid_pressure_high","label":"供液压力高","sev":"warn"} ]
  }, ... ]
}
```
> `faults[]` 为该箱当前故障位；总功耗 = `power1 + power2`（W）。字段含义见 [INTERNAL_API.md §3](INTERNAL_API.md)。

---

### 7. `GET /api/public/customers` — 按客户报表（对账）

**查询参数**：`?hours=24`（统计周期小时数，默认 24）。

**响应**
```json
{
  "ok": true, "ts": 1782753412,
  "hours": 168, "covered_hours": 72, "truncated": true,   // 周期超快照保留期被截断
  "customers": [ {
    "worker": "Asuna666", "machines": 320,
    "uptime_pct": 99.2, "delivered_th_h": 2360160.0, "power_kwh": 423360.0
  }, ... ]
}
```
> ⚠️ `truncated:true` 表示请求周期超过 `db.retention_days`(默认3天)，实际只统计了 `covered_hours`——做月度结算前需调大保留期或接计费聚合表（见路线图）。

---

### Agent 接入建议

- **探活**：先 `GET /health`，`last_scan_age_s` 正常再信任数据。
- **播报/工单**：轮询 `GET /alerts?active=true`，按 `type/severity` 分类。
- **客户问询**：`GET /customers?hours=24`、`GET /miners?q=<客户名>`。
- **节流**：`/summary` 已 10s 缓存；其余端点建议 Agent 侧 ≥10s 轮询一次即可。
- **不要**期望任何写/控制能力——公共 API 只读；控制需人工走会话 API。
- **MCP 封装**（规划中）：未来提供 MCP server 把 `get_summary`/`list_alerts`/`get_customer_report`/`get_miner`/`list_containers` 暴露成 tool，Token 由 MCP 配置注入、不暴露给 LLM。

---

## 二、会话 REST API（前端 / 运维，Cookie + RBAC）

供 Web 面板与运维脚本使用。**鉴权**：先 `POST /api/login` 拿 Cookie 令牌（`mm_token`，HttpOnly），后续请求带 Cookie。
**角色**：`viewer`(只读) < `ops`(+命令/扫描) < `admin`(+审计/改网段/改设置)。未登录 `401`，权限不足 `403`。关闭 auth 时匿名按 `viewer`。

### 鉴权

| 接口 | 方法 | 角色 | 说明 |
|---|---|---|---|
| `/api/login` | POST | — | `{username,password}` → 设 Cookie。失败 401；同源 IP 连续失败 8 次锁 5 分钟(429) |
| `/api/logout` | POST | — | 注销 |
| `/api/me` | GET | — | 当前用户 `{authenticated,user,role,auth_enabled}` |

### 读取（viewer+）

| 接口 | 说明 |
|---|---|
| `GET /api/summary` | 全场总览（在线/总/算力/固件分布/功耗/能效/告警数/集装箱） |
| `GET /api/miners?status=&fw=&seg=&q=&sort=&order=&limit=` | 矿机列表（含全部内部字段，可筛选/排序，sort 字段白名单） |
| `GET /api/miner/{ip}` | 单机当前 + 历史曲线 |
| `GET /api/racks` | 货架视图（按 /24 聚合，机位状态着色） |
| `GET /api/workers` | 按矿工名聚合（台数/算力/机型分布） |
| `GET /api/reports/customers?hours=&format=` | 客户报表；`format=csv` 导出 UTF-8 BOM 的 CSV（对账存档） |
| `GET /api/containers` · `GET /api/container/{ip}` | 集装箱列表 / 单箱详情+水温历史 |
| `GET /api/alerts?active=` | 告警列表 |
| `GET /api/trend?points=288` | 总算力历史趋势 |
| `GET /api/progress` | 当前扫描进度 |
| `GET /api/segments` · `GET /api/settings` | 当前网段 / 运行参数 |
| `WS /ws` | WebSocket 实时推送（扫描完成/告警 → `{type:"refresh"}`）；握手校验 Cookie，会话失效后端 `close(1008)` |

### 写操作（ops+）

| 接口 | 角色 | 说明 |
|---|---|---|
| `POST /api/scan?kind=full\|quick` | ops | 手动触发扫描（已有扫描在跑则返回 `started:false`） |
| `POST /api/containers/scan` | ops | 手动刷新集装箱 |
| `POST /api/command` | ops | 远程命令 `{ips,action:"reboot\|locate\|set_pools",params}`；批量上限 `control.max_batch`(默认1000)；set_pools 校验 url(stratum+tcp/ssl)+user |
| `POST /api/machine-state` | ops | 维修/取消/下架 `{ips,action:"repair\|active\|remove"}` |
| `POST /api/alerts/ack` | ops | 确认矿机告警 `{id}`（集装箱告警不可确认 → 400） |

### 管理（admin）

| 接口 | 说明 |
|---|---|
| `GET /api/commands?limit=` | 命令审计日志 |
| `POST /api/settings` | 改 `scan_interval`/`max_pps`/`container_interval`（即时生效，非数字→400） |
| `POST /api/segments` | 改扫描网段（`{segments,host_start,host_end}`） |

### 错误码

| 码 | 含义 |
|---|---|
| 200 | 成功 |
| 400 | 参数非法（缺字段/非数字/url 非法/集装箱告警不可确认 等） |
| 401 | 未登录 / Token 错误 |
| 403 | 已登录但权限不足 / 控制功能未启用 |
| 404 | 资源不存在 / 公共 API 未配置 Token |
| 429 | 登录失败次数过多（锁定中） |

---

## 三、安全与上线提示

- 公共 Token 能拉全场算力/SN/矿工名/拓扑（=客户资产清单），属敏感数据：生产建议 Token 从**环境变量**注入而非明文写 config，并只用 `Authorization` 头（不用 `?token=`）。
- 当前为内网 HTTP 明文；上线建议前置 nginx/caddy 做 TLS 并设 `auth.secure_cookie=true`。
- 默认弱口令（admin888 等）启动时会打印安全警告，务必用 `python auth.py 新口令` 改成强口令。
