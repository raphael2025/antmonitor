# AntMonitor

<p align="center">
  <strong>蚂蚁矿机场地监控 · Local farm ops for ANTMINER / ANTBOX</strong><br/>
  <sub>局域网批量监控 · 告警 · 运维面板 · 多场地云端总览</sub>
</p>

<p align="center">
  <a href="docs/guide/zh.md">中文</a> ·
  <a href="docs/guide/en.md">English</a> ·
  <a href="docs/guide/ru.md">Русский</a> ·
  <a href="docs/guide/es.md">Español</a> ·
  <a href="docs/guide/de.md">Deutsch</a> ·
  <a href="docs/guide/ar.md">العربية</a>
</p>

<p align="center">
  <a href="https://github.com/raphael2025/antmonitor">Site · 场地端</a> ·
  <a href="https://github.com/raphael2025/antmonitor-cloud">Cloud · 云端</a> ·
  <a href="https://t.me/+W3J9yAypNgpjNTk9">Telegram</a>
</p>

---

## Why AntMonitor

矿场要的不是又一个仪表盘 Demo，而是：**扫得全、报得准、值班省心、误操作有闸**。

| | |
|---|---|
| **看见全场** | 在线台数、总算力、功耗、货架机位、客户/矿工名报表 |
| **及时告警** | 掉线、掉算力、高温、集装箱冷却故障；语音 + Telegram |
| **敢动手** | 定位灯、批量重启（确认弹窗 + 24h/间隔限流）；掉线自动重启可关 |
| **多场地** | 本地运维 + [AntMonitor Cloud](https://github.com/raphael2025/antmonitor-cloud) 汇聚总览 |
| **可扩展** | 只读公共 API；下一步 **Agent Skill + MCP** 智能化巡检 |

面向托管 / 包机场地：一台 Windows 或 Ubuntu 监控机即可。

---

## Quick start

```bash
git clone https://github.com/raphael2025/antmonitor.git
cd antmonitor
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml                   # 改网段、账号后启动
python server.py                                     # → http://<本机或局域网IP>:8800
```

Windows 可用 `run.bat`（崩溃自动拉起）。Ubuntu 建议 systemd 守护。

**选语言看完整「简介 · 部署 · 使用」→** [docs/guide/](docs/guide/README.md)

---

## Features

- **双档扫描**：快巡检名册 + 定时全网发现新机（限速保护三层网络）
- **Web 面板**：总览 / 列表 / 货架 / 告警 / 集装箱 / 客户报表；WebSocket 近实时刷新
- **角色权限**：viewer · ops · admin；弱口令仅提示，不锁运维
- **重启策略**：手动与自动共用限流；顶部可调并发与批间隔；自动重启默认关
- **生命周期**：维修中静音告警、下架移除、限电上下线自动捕捉
- **云端上报**：场地主动推送摘要到 AntMonitor Cloud（NAT 后无需端口映射）
- **安全运维**：矿池白名单策略、审计日志、网页一键更新（git 部署）

详细能力与安全清单见多语言指南与 [SECURITY](docs/SECURITY.md)。

---

## Repos

| 仓库 | 说明 |
|---|---|
| **[antmonitor](https://github.com/raphael2025/antmonitor)** | 场地本地监控（本仓库） |
| **[antmonitor-cloud](https://github.com/raphael2025/antmonitor-cloud)** | 多场地云端总览 |

本仓库为**私有**。设备对接细节不公开文档；需要 OEM / 二次开发请联系定制。

---

## Roadmap

1. **Agent Skill + MCP** — 智能化监控与管理（优先）
2. 更细的访问控制与通道白名单
3. 分组统计、趋势对比、导出报表

---

## Custom & contact

报表对接、权限改造、多场地策略、Agent/MCP 落地等可定制：

| | |
|---|---|
| 微信 WeChat | 搜索 **`raphael-2024`** |
| Telegram | https://t.me/+W3J9yAypNgpjNTk9 |

---

## Docs

| | |
|---|---|
| 多语言入门 | [docs/guide/](docs/guide/README.md) |
| 开发说明 | [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) |
| Agent 取数 API | [docs/EXTERNAL_API.md](docs/EXTERNAL_API.md) |
| 运维安全 | [docs/SECURITY.md](docs/SECURITY.md) |

---

<sub>AntMonitor · Not affiliated with BITMAIN. ANTMINER / ANTBOX are trademarks of their respective owners.</sub>
