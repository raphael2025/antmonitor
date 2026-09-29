# AntMonitor · 中文指南

**语言：** [中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

仓库：https://github.com/raphael2025/antmonitor  
云端总览：https://github.com/raphael2025/cloud-overview

---

## 1. 简介

**AntMonitor（蚂蚁矿机监控）** 是面向矿场局域网的 **比特大陆蚂蚁矿机（ANTMINER）** / **ANTBOX** 批量监控与运维系统。

能做什么：

- 扫描局域网矿机与水冷箱，读算力 / 温度 / 功耗 / 矿工名
- Web 面板：总览、列表、货架机位、告警、客户报表
- 定时巡检 + 掉线 / 高温 / 掉算力 / 集装箱故障告警（可语音、Telegram）
- 远程：定位灯、重启（有 24h/间隔限流；可选掉线自动重启，默认关）
- 多场地可上报到云端总览；后续规划 **Agent Skill + MCP** 智能化运维

适合：托管 / 包机场地，Windows 或 Ubuntu 上一台监控机即可。

---

## 2. 怎么部署

### 2.1 环境

- Python 3.8+
- 与矿机同网或能路由到矿机网段（不要经会压垮的防火墙乱扫）
- 推荐：独立小主机或 Ubuntu 服务器 + systemd

### 2.2 安装启动（开发 / 试用）

```bash
git clone https://github.com/raphael2025/antmonitor.git
cd antmonitor
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 按场地改网段、账号、密码
python server.py                     # 默认 http://0.0.0.0:8800
```

Windows 也可用 **`run.bat`**（崩溃自动拉起）。

### 2.3 必改配置（`config.yaml`）

1. **扫描网段**：`scan.segments` 或网页「⚙ 网段」（admin）写到 `segments.json`
2. **登录账号**：`auth.users`（用 `python auth.py 新密码` 生成哈希）
3. **矿机密码**：`scan.passwords`（原厂 Digest，默认常为 root/root）
4. 可选：`telegram` 告警、`cloud` 上报云端、`control.pool_allowlist` 矿池白名单

安全清单见 [docs/SECURITY.md](../SECURITY.md)。

### 2.4 Ubuntu systemd 示例

```ini
[Unit]
Description=AntMonitor
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/antmonitor
ExecStart=/opt/antmonitor/.venv/bin/python server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now antmonitor
```

面板：本机 `http://127.0.0.1:8800`，局域网用日志里打印的 IP。

---

## 3. 怎么用

### 3.1 登录与角色

| 角色 | 能力 |
|---|---|
| viewer | 只看 |
| ops | 看 + 扫描 + 远程命令 |
| admin | 全部（网段、设置、审计、更新） |

弱口令只会**提示**改密，不锁权限；建议尽快改掉。

### 3.2 日常操作

1. 打开面板 → 看总览在线数 / 算力 / 告警
2. admin：⚙ 网段 → 填矿机网段 →「保存并全网扫描」发现新机
3. 之后默认约每 5 分钟**快巡检**名册；约每小时**全网发现**
4. 告警：可开右上「语音告警」；可选 Telegram
5. 勾选矿机 → 定位灯 / 重启（破坏性操作要二次确认）
6. 顶部可开关「掉线自动重启」、改批间隔与并发（admin 保存；默认关）
7. 维修中 / 下架：命令栏标记，维修机不报掉线

### 3.3 注意

- 重启有限流：每 IP 滚动 24h ≤4 次成功，间隔 ≥15 分钟
- 换矿池面板入口已去掉；API 仍可用，须配置矿池白名单
- 算力显示千进制：1000 TH = 1 PH

### 3.4 定制与支持

特殊功能（报表、对接、权限、多场地策略、Agent/MCP 等）可定制：

- 微信搜索 **`raphael-2024`**
- Telegram：https://t.me/+W3J9yAypNgpjNTk9

更细的开发 / API 文档：[DEVELOPMENT](../DEVELOPMENT.md) · [INTERNAL_API](../INTERNAL_API.md) · [EXTERNAL_API](../EXTERNAL_API.md)
