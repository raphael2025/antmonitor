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

GitHub / Gitee 是下载代码的网站，无需注册账号或安装它们的客户端。Ubuntu 和 Windows 都会自动准备 Git / Python；
按网络情况选择下面的一条命令即可。Ubuntu 命令需要 `curl`；系统没有时先执行 `sudo apt-get update && sudo apt-get install -y curl ca-certificates`。

### Ubuntu（推荐）

**GitHub：**

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- --source github
```

**Gitee（国内下载）：**

```bash
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- --source gitee
```

<details><summary>不确定哪个网站能访问？自动选择 GitHub / Gitee</summary>

下面是一条命令：先尝试 GitHub，下载失败再尝试 Gitee；完整下载成功后才执行安装脚本一次。
安装器也会自动选择可用的源码来源，不会在安装已开始后重复运行另一份安装程序。

```bash
( f=$(mktemp) && trap 'rm -f "$f"' EXIT && { curl -fsSL --connect-timeout 10 --max-time 60 https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh -o "$f" || curl -fsSL --connect-timeout 10 --max-time 60 https://gitee.com/raphael01/antmonitor/raw/main/install.sh -o "$f"; } && sudo bash "$f" --source auto )
```

</details>

Gitee 命令会自动安装 Git，并从 Gitee 拉取源码；后续网页更新使用同一仓库来源。

自动装依赖、下载代码、建服务并启动，安装时设置 admin 密码（直接回车会生成强密码并显示），
随后可直接粘贴云端接入码，暂时不接云端就按回车跳过。
默认安装目录 `/opt/antmonitor`，服务名 `antmonitor`，面板端口 `8800`。
已安装后再次运行同一安装入口，有交互终端时会进入管理菜单；选 **6「连接 / 更换云端」** 即可补接云端。

#### Ubuntu 没有桌面，怎样打开面板？

**Ubuntu Server 不需要安装桌面或浏览器。** AntMonitor 会启动 Web 服务；Linux 上不会自动弹出浏览器，
请用同一局域网内另一台电脑的浏览器配置：

1. 在 Ubuntu 终端执行 `hostname -I`，找到另一台电脑能访问的局域网 IP（可能显示多个地址）。
2. 在 Windows / Mac / 其他电脑的浏览器打开 `http://<Ubuntu监控机IP>:8800`，
   例如 `http://192.168.1.50:8800`，替换成你自己的 Ubuntu IP。
3. 用 **admin** 登录 → 顶部 **「☁ 连接云端」**（未连接时会突出显示）。
4. 云端「🔌 场地接入」点 **「生成并复制接入信息」** → 本地端点 **「粘贴接入信息并连接」**。
   局域网 HTTP 页面若提示不能自动读剪贴板，直接按 **Ctrl+V** 即可继续连接。地址、token、场地名与类型自动带入；旧码缺名称时补填一次。
   程序自动测试、保存并启用上报，验证失败时保留原配置和输入。
   下方状态会持续更新；显示「等待首轮扫描」时，在「⚙ 网段」设置矿机网段并开始全网扫描，完成后自动上报。

**只有 SSH 能访问 Ubuntu？** 在有浏览器的电脑上运行（把用户名和 IP 换成实际 SSH 登录信息）：

```bash
ssh -N -L 18800:127.0.0.1:8800 用户名@Ubuntu监控机IP
```

保持 SSH 窗口打开，在这台电脑的浏览器访问 `http://127.0.0.1:18800`。
以上地址使用默认 HTTP / 8800 配置；如果改过面板端口或启用了 HTTPS，按实际配置调整。

#### 通过终端接入云端

从**云端面板「🔌 场地接入」**复制接入码。首次运行上方一键安装命令时，按提示粘贴即可，输入不会显示；
新码自带场地名和类型，旧码会提示补填。暂时没有接入码，可以回车跳过。

**已经安装后接入或更换云端：**重跑同一条安装命令，选菜单 **6「连接 / 更换云端」**。
也可以按脚本下载源选一条，直接进入接入提示：

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- connect

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- connect
```

`connect` 只验证连接、保存云端配置并重启现有服务，不更新代码或依赖，也不改变原来的更新源。
接入码格式或连接测试失败时保留原配置，可立即重试。
如果原来使用自定义安装目录，还需补 `--dir '/实际安装目录'`。
接入后核对具体连接测试结果及云端是否出现本场地；详细排查见
[中文指南 · Ubuntu 无桌面访问与云端配置](docs/guide/zh.md#ubuntu-无桌面访问与云端配置)。

已经更新到当前版本，也可在安装目录直接运行 `sudo bash install.sh connect --dir .`，无需再次下载脚本。
**Windows 的同一份接入信息**可在本地网页使用，也可双击安装目录内的 `connect-cloud.bat` 后粘贴。
命令界面输入不显示；测试通过后才保存。如果原 Windows 监控仍在运行，按提示关闭原窗口并重新运行 `run.bat`；
端口空闲时会自动打开监控。程序保存配置和首次真实上报是两个步骤，以面板的上报成功时间为准。

### Windows 10 / 11

在 PowerShell 里执行，不需要管理员。按下载源选一条：

**GitHub：**

```powershell
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1'))) -Source github
```

**Gitee（GitHub 无法访问时使用）：**

```powershell
& ([scriptblock]::Create((irm 'https://gitee.com/raphael01/antmonitor/raw/main/install.ps1'))) -Source gitee
```

两种方式均无需预装 Git / Python。缺少 Git 时先自动准备便携 Git；Gitee 优先使用经过固定 SHA256 校验的 SourceForge 镜像，
不需要先成功访问 GitHub。下载到 `%LOCALAPPDATA%\AntMonitor\antmonitor`，桌面生成「AntMonitor」快捷方式并启动；
缺少 Python 时从 python.org 下载并验证签名后安装（当前用户，不需要管理员），之后可在网页上一键更新。
Windows 已有安装再次运行脚本只会启动程序，保留原来的更新源；换源说明见[中文指南](docs/guide/zh.md#windows一键安装)。

<details><summary>Windows 自动选择 GitHub / Gitee</summary>

复制这一整段到 PowerShell 执行。先完整下载脚本，再执行一次；不会在安装失败后重新执行另一份脚本。

```powershell
$s=$null; foreach($u in @('https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1','https://gitee.com/raphael01/antmonitor/raw/main/install.ps1')) { try { $s=Invoke-RestMethod -Uri $u -TimeoutSec 60 -ErrorAction Stop; break } catch { Write-Host ('Download unavailable: '+([uri]$u).Host) } }; if([string]::IsNullOrWhiteSpace($s)) { throw 'GitHub/Gitee unavailable; use downloaded source.' }; & ([scriptblock]::Create($s)) -Source auto
```

</details>

<a id="windows-web-update-recovery"></a>

### Windows 网页提示找不到 Git：一键恢复

适用于已能正常监控，但网页更新提示「找不到 git」，或启动时安装便携 Git 出现 `WinError 5 / 拒绝访问` 的旧版本。
**先在原 AntMonitor 控制台按 Ctrl+C，等程序停止，再用同一 Windows 用户打开 PowerShell，选一条执行：**

```powershell
# GitHub
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/raphael2025/antmonitor/main/repair-web-updates.ps1')))

# Gitee
& ([scriptblock]::Create((irm 'https://gitee.com/raphael01/antmonitor/raw/main/repair-web-updates.ps1'))) -Source gitee
```

脚本会寻找当前目录或默认安装目录；找不到时提示粘贴含有 `run.bat` 的原文件夹路径。
恢复成功会自动启动新窗口，随后登录网页点击「更新」，安装最新版本。
脚本保留原源码、配置和数据库；旧 ZIP 会匹配它原来的发布版本，已有 Git 仓库保留原 `origin`。
Gitee 选择恢复脚本和无 `.git` 时的仓库来源；缺少 Git 时优先使用 SourceForge 上与官方文件 SHA256 一致的 MinGit，
镜像失败才尝试官方 GitHub。也可通过 `MINER_MINGIT_ZIP` 指定本地固定版本安装包。
自定义目录、网络失败或本地改动的处理见[中文指南 · Windows 网页更新恢复](docs/guide/zh.md#windows-web-update-recovery)。

### 后续更新与换源

Ubuntu 更新代码、依赖并重启，按希望使用的来源选一条：

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- update --source github

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- update --source gitee
```

Ubuntu 显式传入 `--source github` / `--source gitee` 可在两个官方仓库之间切换更新源；不传或 `--source auto` 时保留已有 `origin`。
默认自动模式下，官方来源网络失败时可尝试另一平台的同分支，只接受快进更新，不回退到旧镜像版本。
自定义仓库不会被自动覆盖。自定义安装目录仍需追加 `--dir '/实际安装目录'`。
Windows 日常从网页更新；GitHub / Gitee 仓库是否已有新版本，取决于发布者的同步情况。
详细步骤见[中文指南 · Ubuntu 更新与选择下载源](docs/guide/zh.md#ubuntu-更新与选择下载源)。

### 装好以后

admin 登录 →「⚙ 网段」填矿机网段 → 保存并全网扫描。
接云端用顶部 **「☁ 连接云端」**：先在云端复制完整接入信息，再点本地 **「粘贴接入信息并连接」**。
浏览器不允许自动读取时，按提示 **Ctrl+V** 即可继续。测试通过后自动保存并启用，持续显示等待扫描、上报成功或失败重试。
手动填写地址、token、证书指纹，以及暂停上报，都在连接窗口的「高级设置」中。

登录页和顶部都能切换 **跟随系统 / 按时间自动 / 日间 / 夜间**，浏览器会记住选择。
按时间自动使用浏览设备的本地时间：07:00–19:00 为日间，其余为夜间，无需设置服务器。

### GitHub 和 Gitee 都打不开

在能联网的电脑下载完整源码 ZIP，拷到监控机解压，然后使用本地安装入口：

| 系统 | 在解压后的源码目录执行 |
|---|---|
| Ubuntu | `sudo bash install.sh --dir /opt/antmonitor` |
| Windows PowerShell | `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1` |

这条路径不需要访问 GitHub / Gitee 下载源码。**源码包不等于完整离线安装包**：系统包、Python、Python 依赖仍需可用下载源，
或事先拷入对应系统和版本的安装包 / wheels。Windows 支持本地签名 Python 安装器和校验过的 MinGit ZIP，
两系统支持 `offline/wheels` 依赖目录，具体变量和操作见[中文指南 · 无法访问代码托管平台](docs/guide/zh.md#无法访问代码托管平台)。

**大场地防误报**：发现掉线或零算力先等 20 秒用更长超时重探一遍（二次确认），仍不行当轮就报；整段掉线不重探、直接报整段掉线；单台判活最多等 3 秒；高温连续 2 轮才报；全场同时大面积掉线
只报一条「全场掉线」；矿机网页卡住但还在挖矿时改从 4028 端口取数，不再误报掉线。参数见
[中文指南 · 大场地防误报](docs/guide/zh.md#大场地防误报默认已开启)。

完整「简介 · 部署 · 使用」→ [选语言阅读](docs/guide/README.md)

### 客户实时算力与 24 小时平均

客户实时算力是各台矿机**当前实测算力之和**；近 24 小时平均按运行记录和持续时间计算，确认离线的时段计为零。
一台机器全天稳定在 **293 TH/s**，24 小时平均仍为 **293 TH/s**；100 台均如此时，客户合计为 **29.3 PH/s**。
`台数 × 标称算力` 只能表示理论值，实际停机、降频和算力波动都会影响平均值。

历史不足或监控断档会提示覆盖不足；没有数据显示未知，不当成零。矿机列表原有的「平均算力」来自固件，周期不保证是 24 小时。
累计交付 `TH·h` 另用于周期统计与 CSV：293 TH/s 持续一天是 7,032 TH·h，不能把这个数当成平均算力。
云端同时查看这些指标时，请更新**场地端和云端**；默认实时客户数据随场地摘要每 60 秒上报，平均报表每 600 秒更新。
详细口径见[中文指南 · 客户算力怎么看](docs/guide/zh.md#客户算力怎么看)。

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
