# 内部 API 文档（矿机 / 设备接口）

> **范围**：本文档描述本系统**向矿机/集装箱设备发起**的接口调用，即"内部 API"——
> 这些是设备固件自带的 HTTP/TCP 接口，由 `miner_core.py`（读）和 `control.py`（写）消费。
> 给**别的 Agent 调用本系统取数**的接口见 [EXTERNAL_API.md](EXTERNAL_API.md)。
>
> 所有接口均为**局域网内网明文 HTTP**。设备 IP 形如 `172.16.<段>.<主机>`。
> 实测机型：S19 XP+ Hydro（原厂 / UniPlusOS 两种固件）+ AntBox 水冷集装箱控制器。

---

## 0. 设备识别与探测顺序

`miner_core.probe(ip, cfg)` 对单个 IP 的探测流程：

```
1. liveness_gate（可选）：TCP 连 80 端口，gate_timeout(默认0.4s) 内连不上 → 判离线，不再探测
2. probe_stock(ip)      → 原厂 Bitmain？   命中返回 firmware=stock
3. probe_uniplus(ip)    → 第三方 UniPlusOS？命中返回 firmware=uniplus
4. probe_antbox(ip)     → AntBox 集装箱？  命中返回 device=container
5. 都不命中             → 离线 _blank(ip,"offline")
6. 在线机补取 fetch_pool(ip)（cgminer 4028）→ 矿工名 + 份额
```

三类设备**都开 80 端口**，故用 80 做通用判活（6060 只对原厂有效）。

| 设备类型 | 判定依据 | `firmware`/`device` |
|---|---|---|
| 原厂 Bitmain | `6060/get_sn` 返回 200 | `firmware=stock` |
| 第三方 UniPlusOS | `/api/v1/summary` 含 `miner` 对象 | `firmware=uniplus` |
| AntBox 集装箱 | `/cooler?operation=coolerState` 返回 `ok=true` | `device=container` |

---

## 1. 原厂 Bitmain 固件（lighttpd + Digest 认证）

**认证**：HTTP Digest，默认 `root` / `root`（可在 `config.yaml > scan.passwords` 配多组依次尝试）。
例外：`6060/get_sn` 免认证。

### 1.1 判活 + 读 SN（探活，最快，免认证）

```
GET http://<ip>:6060/get_sn
```
- 自定义文本协议，**命令必须是 `get_sn`**（`sn`/`getsn` 返回 `error 6060 cmd`）。
- 返回：纯文本 SN（明文）。返回非 200 或含 `error`/空 → SN 记为 `N/A`（仍判在线）。
- 用途：本系统用它做原厂判活 + 取 SN。超时 `scan.online_timeout`（默认 0.5s）。

### 1.2 读算力 / 温度 / 功耗（监控主数据）

```
GET http://<ip>/cgi-bin/stats.cgi      （Digest）
```
返回 JSON。本系统取用字段（见 `miner_core.probe_stock`）：

| 设备字段 | 系统字段 | 说明 |
|---|---|---|
| `INFO.type` | `model` | 机型 |
| `STATS[0].rate_5s` | `hr_rt` | 实时算力（GH/s → 系统转 TH/s） |
| `STATS[0].rate_avg` | `hr_avg` | 平均算力（GH/s → TH/s） |
| `STATS[0].watt` | `power` | 功耗 W |
| `STATS[0].jt` | `eff` | 能效 J/TH |
| `STATS[0].elapsed` | `uptime` | 运行时长（秒） |
| `STATS[0].chain[].temp_chip[]` | `temp` | 各链芯片温，取最大值 |

> `stats.cgi` 比 `summary.cgi` 多出 watt / temp_chip / jt / elapsed，故监控用它。
> `summary.cgi` 仅 `SUMMARY[0].rate_5s/rate_avg`，作为备用。

### 1.3 读 MAC / 网络配置（机器身份，按需）

```
GET http://<ip>/cgi-bin/get_system_info.cgi    （Digest）
```
一次返回 `macaddr` + `nettype`(Static/DHCP) + `minertype`(机型) + ip/掩码/网关。
另有 `get_network_info.cgi`（含 `macaddr` + `conf_*` 当前/待生效配置）。
> 6060/get_sn **不含** MAC。实测：139.105 → `macaddr=D6:1A:58:2E:22:3C, nettype=Static`。

### 1.4 其他原厂只读接口（未全部接入）

`pools.cgi`（矿池状态）、`get_miner_conf.cgi`（矿池配置，密码明文）、`chart.cgi`（历史图）。

### 1.5 写操作（远程命令，`control.py > _stock`）

| 命令 | 接口 | 方法/Body | 返回判定 |
|---|---|---|---|
| 重启 | `POST /cgi-bin/reboot.cgi` | POST 空 | 200=ok |
| 定位灯 | `POST /cgi-bin/blink.cgi` | `{"blink":"true"\|"false"}` | 200=ok（状态查 `get_blink_status.cgi`） |
| 换矿池 | `POST /cgi-bin/set_miner_conf.cgi` | 先 GET `get_miner_conf.cgi` 取完整 conf，替换 `conf["pools"]` 再整体 POST | 200=ok |

> ⚠️ 这些 cgi **对 GET 返回 200 但不执行**，必须 POST。密码用 `scan.passwords`（Digest）。

---

## 2. 第三方 UniPlusOS 固件（SPA + REST）

**认证**：读接口**免认证**；写操作需先解锁（见下）。**不开 6060 端口**。

### 2.1 读算力（监控主数据）

```
GET http://<ip>/api/v1/summary     （免认证）
```
返回 JSON，本系统取用（见 `miner_core.probe_uniplus`，命中 `miner` 对象才算 uniplus）：

| 设备字段 | 系统字段 | 说明 |
|---|---|---|
| `miner.miner_type` | `model` | 机型 |
| `miner.hr_realtime` | `hr_rt` | 实时算力（GH/s → TH/s） |
| `miner.hr_average` | `hr_avg` | 平均算力（GH/s → TH/s） |
| `miner.power_consumption` | `power` | 功耗 W |
| `miner.power_efficiency` | `eff` | 能效 J/TH |
| `miner.chip_temp.max` | `temp` | 芯片最高温 |
| `miner.miner_status.miner_state_time` | `uptime` | 运行时长（秒） |

### 2.2 读 MAC / 网络（机器身份，按需）

```
GET http://<ip>/api/v1/info        （免认证）
```
含 `network_status.mac` + `network_status.dhcp`(bool)。实测：119.21 → `mac=10:0A:41:96:B6:F2, dhcp=false`。

### 2.3 其他只读接口

`/status`、`/metrics`(历史)、`/chains`(逐芯片)、`/chains/factory-info`、`/perf-summary`(超频预设)。
> ⚠️ HTTP summary 会把矿工名打码成 `*****`；取明文矿工名走 4028（见 §4）。

### 2.4 写操作（远程命令，`control.py > _uniplus`）

| 命令 | 接口 | 解锁 | 说明 |
|---|---|---|---|
| 定位灯 | `POST /api/v1/find-miner` `{"find":bool}` | **免解锁** | 返回 `{"on":bool}` |
| 解锁 | `POST /api/v1/unlock` `{"pw":<密码>}` | — | 字段名是 `pw`；session/cookie 维持解锁态 |
| 重启 | `POST /api/v1/system/reboot` | 需先解锁 | 401=需解锁密码 |
| 换矿池 | `POST /api/v1/settings` `{"pools":[...]}` | 需先解锁 | 401=需解锁密码 |

> 解锁密码配 `config.yaml > control.uniplus_password`。

---

## 3. AntBox 水冷集装箱控制器（端口 80，免认证）

页面 title=AntBox，作者 meta=蚂蚁矿机AntBox。控制器查 PLC 响应较慢，给 ≥1.5s 超时。

### 3.1 冷却状态（监控主数据）

```
GET http://<ip>/cooler?operation=coolerState     （免认证）
```
返回 `{ok, method:"coolerState", params:{...}}`。本系统取用（见 `miner_core.probe_antbox`）：

| 设备字段 (`params.*`) | 系统字段 | 说明 |
|---|---|---|
| `supply_liquid_temp` / `return_liquid_temp` | `supply_temp` / `return_temp` | 进/出水温 |
| `supply_liquid_pressure` / `return_liquid_pressure` | `supply_pressure` / `return_pressure` | 供/回液压力 MPa |
| `supply_liquid_flow` | `flow` | 供液流量 |
| `antbox_internal_temp` / `antbox_internal_humidity` | `internal_temp` / `internal_humidity` | 箱内温/湿 |
| `colding_tower_inlet_temp` | `tower_inlet_temp` | 冷却塔进水温 |
| `supply_liquid_set_temp` | `set_temp` | 设定温 |
| `distribution_box1_power` / `distribution_box2_power` | `power1` / `power2` | 两路配电功率 W（总功耗=两者之和） |
| `circulating_pump`/`spray_pump`/`fan1`/`fan2`/`cooling_tower_fan1..3` | `pumps{}` | 泵/风扇开关 |
| （20+ 故障位，见下） | `faults[]` | 冷却故障 |

> ⚠️ `power_a/b_distribution` 是累计 kWh，不是功率，**不用**；功率用 `distribution_boxN_power`(实时 W)。

**故障位**（`params.<flag>` 为真即故障，`miner_core.ANTBOX_FAULTS`）：

| flag | 中文 | 级别 |
|---|---|---|
| `leakage_fault` | 漏液 | crit |
| `freezing_alarm` | 冻结 | crit |
| `power_fault` / `power_relay_fault` | 电源/继电器故障 | crit |
| `phasefailure` | 断相 | crit |
| `supply_liquid_temp_too_high` | 供液温过高 | crit |
| `liquid_level_low` | 液位低 | crit |
| `circulating_pump_fault` | 循环泵故障 | crit |
| `second_pump1_fault` / `second_pump2_fault` | 二级泵故障 | crit |
| `disconnect` | 控制器失联 | crit |
| `supply_liquid_temp_high` | 供液温偏高 | warn |
| `liquid_level_high` | 液位高 | warn |
| `cooling_tower_liquid_level_low` | 冷却塔液位低 | warn |
| `supply_liquid_flow_low` | 供液流量低 | warn |
| `supply_liquid_pressure_high` | 供液压力高 | warn |
| `return_liquid_pressure_low` | 回液压力低 | warn |
| `spray_pump_fault` / `fluid_infusion_pump_fault` | 喷淋泵/补液泵故障 | warn |
| `fan1_fault`/`fan2_fault`/`fan_fault` | 风扇故障 | warn |
| `cooling_tower_fan1..3_fault` | 冷却塔风扇故障 | warn |
| `supply_liquid_temp_fault`/`return_liquid_temp_fault` | 温传感器故障 | warn |

> 哪些故障位**不报警**由 `config.yaml > alerts.container_faults_ignore` 控制（仍在卡片显示）。

### 3.2 箱内矿机信息（best-effort）

```
GET http://<ip>/cooler?operation=minerInfo      （免认证）
```
取 `params.miner_num`（箱内矿机数）、`chip_max_temp`、`miner_info`(成员字典，键为矿机 IP)。

### 3.3 其他接口

`sensorData`（温湿度/三相电表/烟感/防雷）；历史 op（`supplyTempHistory` 等）返回 null 不可用→系统自存历史。
写操作（`setTemp`/`pid`/泵控走 `xmlRequest`；升级 `POST /upgrade/*`）**敏感，本系统未接入**。

---

## 4. cgminer 4028 端口（矿工名 + 份额，跨固件通用）

```
TCP <ip>:4028  发送 {"command":"pools"}\n      （明文，免认证）
```
两种固件**都通且明文**（第三方 HTTP 会把 User 打码，4028 不会）。见 `miner_core.fetch_pool`：

| POOLS[] 字段 | 系统字段 | 说明 |
|---|---|---|
| `User` | `worker` | 矿工名，**取第一个 `Status=Alive`(优先 Priority 最小) 池**，去掉首个 `.` 后缀 |
| `Accepted` | `accepted` | 接受份额（**只累计 Alive 池**，避免备用池历史拒绝抬高拒绝率） |
| `Rejected` | `rejected` | 拒绝份额（同上） |
| `Stale` | `stale` | 陈旧份额 |

> 响应可能以 `\x00` 结尾。本系统按 **JSON 完整性**判定收齐（不凭末字符 `}` 提前 break，避免多池响应被截断）。
> 4028 也能取 stats（算力/型号），是 AntBox 之外的通用兜底方案；可在 `scan.fetch_worker` 关闭整个 4028 取数。

---

## 5. 超时与负载（CoPP 保护）

| 参数 | 默认 | 作用 |
|---|---|---|
| `scan.online_timeout` | 0.5s | 判活/取 SN 超时 |
| `scan.data_timeout` | 2.0s | 取算力数据超时 |
| `scan.gate_timeout` | 0.4s | liveness_gate 连 80 超时 |
| `scan.max_pps` | 100 | **令牌桶限速**：每秒新建连接(≈死IP的ARP速率)上限，护三层 500pps CoPP（5 倍余量） |
| AntBox 探测 | ≥1.5s | 控制器查 PLC 慢，单独放宽 |

> 数据流量（读算力/水温）走硬件转发的**数据平面，不占 CoPP**，无需限速；
> 真正压三层控制平面的是对死 IP 的 ARP（由 `max_pps` 令牌桶限制）。
