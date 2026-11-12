# 运维迭代进度（feat/ops-iteration-2026）

> **不合并 main / master，等用户验收后再合。**  
> 本地分支：`antmonitor` → `feat/ops-iteration-2026`  
> 云端分支：`antmonitor-cloud` → `feat/ops-iteration-2026`

## 目标顺序

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 点开矿机网页（stock Digest / UniPlus `pwd=admin`）+ 固件指纹修正 | ✅ 完成 |
| M2 | 限电/计划停机运维模式 + 告警降噪 | ✅ 完成 |
| M3 | 水冷漏液默认告警 + 集装箱停更看门狗 | ✅ 完成 |
| M4 | 批量操作安全（跨客户/维修确认/勾选锁定） | ✅ 完成 |
| M5 | 云端：场地健康语义拆分 + 通知异步合并 | ✅ 完成 |
| M6 | 客户台账 / 日汇总对账 | ✅ 完成 |

## 验收要点（合主分支前）

- 本地：漏液不可忽略、集装箱停更、限电标记、勾选锁定、跨客户换池闸门
- 本地：`GET /api/reports/customers/daily`；SN→客户台账；维修/限电不计客户停机
- 云端：`site_down` / `stalled` / `unreachable` 文案区分；`merge_window_sec` 合并通知
- 云端：`GET /api/customers/daily`；ingest 可收 `customers_daily`

## 续跑入口

验收通过后由用户明确指令再合并 main/master。
