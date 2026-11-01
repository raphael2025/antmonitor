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

不需要会写代码。Ubuntu / Windows 均提供 GitHub 和 Gitee 安装入口。

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

Ubuntu 不用先 `git clone`，也不用预装 Git / Python；按网络情况选择 GitHub 或 Gitee，执行一条安装命令即可。

### Ubuntu（推荐）

**GitHub：**

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- --source github
```

**Gitee（国内下载）：**

```bash
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- --source gitee
```

Gitee 命令会自动安装 Git，并从 Gitee 拉取源码；后续网页更新使用同一仓库来源。

自动装依赖、下载代码、建服务并启动，安装时设置 admin 密码（直接回车会生成强密码并显示）。
默认安装目录 `/opt/antmonitor`，服务名 `antmonitor`，面板端口 `8800`。
已安装后再次运行安装入口、不指定 `update` 等动作，有交互终端时会进入管理菜单（更新 / 修复 / 卸载 / 重置账号）。

#### Ubuntu 没有桌面，怎样打开面板？

**Ubuntu Server 不需要安装桌面或浏览器。** AntMonitor 会启动 Web 服务；Linux 上不会自动弹出浏览器，
请用同一局域网内另一台电脑的浏览器配置：

1. 在 Ubuntu 终端执行 `hostname -I`，找到另一台电脑能访问的局域网 IP（可能显示多个地址）。
2. 在 Windows / Mac / 其他电脑的浏览器打开 `http://<Ubuntu监控机IP>:8800`，
   例如 `http://192.168.1.50:8800`，替换成你自己的 Ubuntu IP。
3. 用 **admin** 登录 → 右上「⚙ 网段」→「☁ 云端上报」。
4. 从**云端面板「🔌 场地接入」**复制接入码 → 回到本地面板粘贴并点「填入」→ 填场地名、选择类型、勾选启用
   →「🔌 测试连接」→ **保存**。测试连接不会保存配置；保存后查看下方最近上报成功时间或失败原因。

**只有 SSH 能访问 Ubuntu？** 在有浏览器的电脑上运行（把用户名和 IP 换成实际 SSH 登录信息）：

```bash
ssh -N -L 18800:127.0.0.1:8800 用户名@Ubuntu监控机IP
```

保持 SSH 窗口打开，在这台电脑的浏览器访问 `http://127.0.0.1:18800`。
以上地址使用默认 HTTP / 8800 配置；如果改过面板端口或启用了 HTTPS，按实际配置调整。

#### 通过终端接入云端

接入码从**云端面板「🔌 场地接入」**复制。`--connect-code -` 表示运行后在交互终端中隐藏输入，
不要把包含 token 的接入码直接写进命令。

**首次安装时接入云端，按下载源选一条：**

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- --source github --connect-code - --site-name '你的场地名' --site-type hydro

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- --source gitee --connect-code - --site-name '你的场地名' --site-type hydro
```

**已经安装后接入或更换云端：**当前脚本通过 `update` 处理接入码，
**会同时更新代码和依赖、保存云端配置并重启服务**。只想修改配置时，使用上面的 Web 面板。

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- update --source github --connect-code - --site-name '你的场地名' --site-type hydro

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- update --source gitee --connect-code - --site-name '你的场地名' --site-type hydro
```

把场地名换成实际名称；`hydro` 为水冷，风冷用 `air`，混合用 `mixed`。
如果原来使用自定义安装目录，还需补 `--dir '/实际安装目录'`。
接入后核对具体连接测试结果及云端是否出现本场地；详细排查见
[中文指南 · Ubuntu 无桌面访问与云端配置](docs/guide/zh.md#ubuntu-无桌面访问与云端配置)。

### Windows 10 / 11

在 PowerShell 里执行，不需要管理员。按下载源选一条：

**GitHub（无需提前安装 Git）：**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1 | iex"
```

**Gitee（系统已安装 Git for Windows，且 `git --version` 可用）：**

```powershell
& ([scriptblock]::Create((irm 'https://gitee.com/raphael01/antmonitor/raw/main/install.ps1'))) -Source gitee
```

若还没安装 Git，先安装 Git for Windows 并重新打开 PowerShell，再执行 Gitee 命令。
下载到 `%LOCALAPPDATA%\AntMonitor\antmonitor`，桌面生成「AntMonitor」快捷方式并启动；没有 Python 会自动从
python.org 下载并验证签名后安装（当前用户，不需要管理员），之后可在网页上一键更新。
Windows 已有安装再次运行脚本只会启动程序，保留原来的更新源；换源说明见[中文指南](docs/guide/zh.md#windows一键安装)。

### 后续更新与换源

Ubuntu 更新代码、依赖并重启，按希望使用的来源选一条：

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- update --source github

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- update --source gitee
```

Ubuntu 显式传入 `--source` 可在两个官方仓库之间切换更新源；不传时沿用已有 `origin`。
自定义仓库不会被自动覆盖。自定义安装目录仍需追加 `--dir '/实际安装目录'`。
Windows 日常从网页更新；GitHub / Gitee 仓库是否已有新版本，取决于发布者的同步情况。
详细步骤见[中文指南 · Ubuntu 更新与选择下载源](docs/guide/zh.md#ubuntu-更新与选择下载源)。

### 装好以后

admin 登录 →「⚙ 网段」填矿机网段 → 保存并全网扫描。接云端就在同一页「☁ 云端上报」粘贴接入码并点「填入」，
填场地名、选择类型、勾选启用 →「🔌 测试连接」→ **保存**。保存后查看下方上报状态（最近一次成功 / 失败原因）。

GitHub 访问慢可使用上面的 Gitee 一键命令，或设置镜像前缀 `GH_PROXY`；也可把源码 ZIP 拷过去，解压后在目录里
`sudo bash install.sh`（无 `.git` 的源码包不保留 Git 更新来源；Python 依赖仍需能访问 PyPI 或国内镜像）。

**大场地防误报**：发现掉线或零算力先等 20 秒用更长超时重探一遍（二次确认），仍不行当轮就报；整段掉线不重探、直接报整段掉线；单台判活最多等 3 秒；高温连续 2 轮才报；全场同时大面积掉线
只报一条「全场掉线」；矿机网页卡住但还在挖矿时改从 4028 端口取数，不再误报掉线。参数见
[中文指南 · 大场地防误报](docs/guide/zh.md#大场地防误报默认已开启)。

完整「简介 · 部署 · 使用」→ [选语言阅读](docs/guide/README.md)

---

## 产品与仓库

| | GitHub | Gitee | 做什么 |
|:---|:---|:---|:---|
| **场地端** | [antmonitor](https://github.com/raphael2025/antmonitor) | [antmonitor](https://gitee.com/raphael01/antmonitor) | 局域网监控、告警、运维（本仓库） |
| **云端** | [antmonitor-cloud](https://github.com/raphael2025/antmonitor-cloud) | [antmonitor-cloud](https://gitee.com/raphael01/antmonitor-cloud) | 多场地 KPI、卡片、趋势、客户汇总 |

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
