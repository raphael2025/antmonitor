# 运维迭代进度（feat/ops-iteration-2026）

> **不合并 main / master，等用户验收后再合。**  
> 本地分支：`antmonitor` → `feat/ops-iteration-2026`  
> 云端分支：`antmonitor-cloud` → `feat/ops-iteration-2026`

## 目标顺序

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 点开矿机网页（stock Digest / UniPlus `pwd=admin`）+ 固件指纹修正 | 进行中 |
| M2 | 限电/计划停机运维模式 + 告警降噪 | 待做 |
| M3 | 水冷漏液默认告警 + 集装箱停更看门狗 | 待做 |
| M4 | 批量操作安全（跨客户/维修确认/勾选锁定） | 待做 |
| M5 | 云端：场地健康语义拆分 + 通知异步合并 | 待做 |
| M6 | 客户台账 / 日汇总对账 | 待做 |

## 现场结论（yunhai 2026-11-13）

- 172.16.119/120/122 以 UniPlus 为主；125/127/129/135 以原厂为主
- UniPlus：`POST /cgi-bin/login.cgi` body=`pwd=...`，token → sessionStorage + cookie
- `/dashboard` HTML 登录前后相同，态在浏览器本地
- 库内常把 UniPlus 误标 stock（6060 先命中）

## 续跑入口

从本文件「目标顺序」里第一个非完成项继续；合并前必须用户明确验收通过。
