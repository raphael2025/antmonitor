<div align="center">

# AntMonitor

### 蚂蚁矿机场地监控

**看清全场 · 及时告警 · 敢动手运维**

面向 BITMAIN **ANTMINER / ANTBOX** 托管与包机场 —  
局域网批量监控，一台电脑就能跑起来。

<br/>

[中文](docs/guide/zh.md) ·
[English](docs/guide/en.md) ·
[Русский](docs/guide/ru.md) ·
[Español](docs/guide/es.md) ·
[Deutsch](docs/guide/de.md) ·
[العربية](docs/guide/ar.md)

<br/>

[🌐 场地端](https://github.com/raphael2025/antmonitor)
·
[☁ 云端总览](https://github.com/raphael2025/antmonitor-cloud)
·
[💬 Telegram](https://t.me/+W3J9yAypNgpjNTk9)
·
微信 `raphael-2024`

</div>

---

## 适合谁

| | |
|:---|:---|
| **托管 / 包机场** | 几百到几千台矿机，要一眼看清在线、算力、掉线 |
| **水冷 / 风冷** | 集装箱冷却状态与矿机一起盯 |
| **多场地老板** | 本地值班 + [云端总览](https://github.com/raphael2025/antmonitor-cloud) 随时扫一眼全网 |

不需要会写代码。Ubuntu / Windows 都是一行命令装好。

---

## 你能得到什么

- **全场一张图** — 在线台数、总算力、功耗、货架机位、客户报表  
- **告警到人** — 掉线 / 零算力 / 高温 / 冷却故障；面板语音 + Telegram  
- **批量运维** — 定位灯、安全限流的批量重启；持续零算力自动重启可开关（仅在线且明确读到零算力才处理，等待15分钟、间隔15分钟重试，最多4次；掉线只告警，不自动重启；成片零算力按网段/全场熔断，维护中的网段或矿池故障时暂停自动重启）
- **多场地** — 本机做运维，[AntMonitor Cloud](https://github.com/raphael2025/antmonitor-cloud) 做老板总览  
- **面板多语言** — 中 / 英 / 俄 / 西 / 德 / 阿，右上角一键切换  

开源可自建。设备对接细节不在公开文档里；特殊需求可定制。

---

## 三分钟上手

不用先 `git clone`，没装 git / Python 也行，一行命令装好：

**Ubuntu（推荐）**

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash
```

自动装依赖、下载代码、建服务并启动，安装时设置 admin 密码（直接回车会生成强密码并显示）。
面板 `http://<监控机IP>:8800`。再跑一次同样的命令 = 管理菜单（更新 / 修复 / 卸载 / 重置账号）。

装的时候顺便接入云端（接入码在云端面板「🔌 场地接入」里复制）：

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- --connect-code 'AMC1-…' --site-name 内蒙一场
```

**Windows 10 / 11**（PowerShell 里执行，不需要管理员）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1 | iex"
```

下载到 `%LOCALAPPDATA%\AntMonitor\antmonitor`，桌面生成「AntMonitor」快捷方式并启动；没有 Python 会自动从
python.org 下载并验证签名后安装（当前用户，不需要管理员），之后可在网页上一键更新。
也可以照旧下载 ZIP 解压后双击 `run.bat`。

**装好以后**：admin 登录 →「⚙ 网段」填矿机网段 → 保存并全网扫描；要接云端就在同一页「☁ 云端上报」粘贴接入码，
点「🔌 测试连接」，下面会一直显示上报状态（最近一次成功 / 失败原因），不用登机器看日志。

GitHub 访问慢可设镜像前缀 `GH_PROXY`；完全离线可把源码 ZIP 拷过去，解压后在目录里 `sudo bash install.sh`。

**大场地防误报**：掉线、整段掉线、零算力都要跨到下一轮巡检再确认才报；高温连续 2 轮才报；全场同时大面积掉线
只报一条「全场掉线」；矿机网页卡住但还在挖矿时改从 4028 端口取数，不再误报掉线。参数见
[中文指南 · 大场地防误报](docs/guide/zh.md#大场地防误报默认已开启)。

完整「简介 · 部署 · 使用」→ [选语言阅读](docs/guide/README.md)

---

## 产品与仓库

| | 开源地址 | 做什么 |
|:---|:---|:---|
| **场地端** | [antmonitor](https://github.com/raphael2025/antmonitor) | 局域网监控、告警、运维（本仓库） |
| **云端** | [antmonitor-cloud](https://github.com/raphael2025/antmonitor-cloud) | 多场地 KPI、卡片、趋势、客户汇总 |

两边互相独立、互相配合：本地保管明细与历史，云端只收摘要总览。

---

## 持续更新

会一直迭代。矿场里踩过的坑、想要的功能，都欢迎说 — 好的意见会尽量排进版本。

**正在规划：** 智能 Agent（Skill + MCP）、多客户管理、更细的权限与通知、报表增强……

---

## 联系与定制

报表对接、多场地策略、权限改造、Agent 落地、OEM 定制：

| | |
|:---|:---|
| **微信** | 搜索 **`raphael-2024`** |
| **Telegram** | [加入群组](https://t.me/+W3J9yAypNgpjNTk9) |
| **场地端** | https://github.com/raphael2025/antmonitor |
| **云端** | https://github.com/raphael2025/antmonitor-cloud |

Issue / PR 同样欢迎。

---

<div align="center">

<sub>AntMonitor · Open source · Built for real farms</sub>

</div>
