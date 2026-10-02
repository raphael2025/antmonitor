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

不需要会写代码。Ubuntu 一键安装，或 Windows 双击启动。

---

## 你能得到什么

- **全场一张图** — 在线台数、总算力、功耗、货架机位、客户报表  
- **告警到人** — 掉线 / 零算力 / 高温 / 冷却故障；面板语音 + Telegram  
- **批量运维** — 定位灯、安全限流的批量重启；持续零算力自动重启可开关（仅在线且明确读到零算力才处理，等待15分钟、间隔15分钟重试，最多4次；掉线只告警，不自动重启）
- **多场地** — 本机做运维，[AntMonitor Cloud](https://github.com/raphael2025/antmonitor-cloud) 做老板总览  
- **面板多语言** — 中 / 英 / 俄 / 西 / 德 / 阿，右上角一键切换  

开源可自建。设备对接细节不在公开文档里；特殊需求可定制。

---

## 三分钟上手

**Ubuntu（推荐）**

```bash
sudo bash install.sh
```

选语言 → 首次安装 / 更新 / 修复 / 卸载 / 重置账号。

**或手动**

```bash
git clone https://github.com/raphael2025/antmonitor.git
cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 填网段与账号
python server.py                     # 浏览器打开 :8800
```

Windows 用 `run.bat` 即可。

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
