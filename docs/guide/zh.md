# AntMonitor · 中文

[中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

| 项目 | GitHub | Gitee |
|---|---|---|
| 场地端 | [antmonitor](https://github.com/raphael2025/antmonitor) | [antmonitor](https://gitee.com/raphael01/antmonitor) |
| 云端 | [antmonitor-cloud](https://github.com/raphael2025/antmonitor-cloud) | [antmonitor-cloud](https://gitee.com/raphael01/antmonitor-cloud) |

---

## 简介

AntMonitor 是矿场**局域网**批量监控与运维系统：看清全场算力与状态，告警及时，远程操作有确认和限流。

- 总览 / 列表 / 货架机位 / 告警 / 集装箱 / 客户报表
- 定时巡检；掉线、掉算力、高温、冷却故障可语音与 Telegram
- 定位灯、批量重启（限流）；可选零算力自动重启（只处理在线且明确读到零算力的机器；掉线只告警，默认关闭）
- 多场地推送到 AntMonitor Cloud；规划 Agent Skill + MCP

适合托管 / 包机。一台 Windows 或 Ubuntu 监控机即可。

---

## 部署

**环境**：Ubuntu 20.04+ 或 Windows 10/11；监控机能访问矿机网段。
Ubuntu 的 GitHub / Gitee 安装命令都会自动准备 Git / Python；Windows Gitee 方式需要系统先装好 Git for Windows。

### Ubuntu：一键安装

选择一个可访问的下载源，执行一条命令即可，无需手动克隆仓库。

**GitHub：**

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- --source github
```

**Gitee（国内下载）：**

```bash
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- --source gitee
```

自动完成：装依赖 → 从所选平台下载代码 → 建虚拟环境 → 生成配置 → 设置 admin 密码
（终端里输入；直接回车会自动生成强密码并在最后显示）→ 注册 systemd 服务 `antmonitor` 并启动。
默认目录 `/opt/antmonitor`，面板 `http://<监控机IP>:8800`，生产分支为 `main`。
Gitee 模式自动安装 Git 并克隆仓库；如果 Git 安装或源码拉取失败，会停止并显示错误，请处理后重试。

#### 安装时顺便接入云端

接入码从云端面板右上「🔌 场地接入」复制。`--connect-code -` 表示运行后在终端隐藏输入，
接入码含 token，不要把真实接入码直接写在命令中。按下载源选一条：

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- --source github --connect-code - --site-name '你的场地名' --site-type hydro

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- --source gitee --connect-code - --site-name '你的场地名' --site-type hydro
```

替换场地名；`hydro` 为水冷，风冷用 `air`，混合用 `mixed`。
自定义安装目录需追加 `--dir '/实际安装目录'`，后续操作沿用该目录。
无桌面 Ubuntu 的浏览器访问与上报结果核对见[下方说明](#ubuntu-无桌面访问与云端配置)。

### Ubuntu 更新与选择下载源

**已经安装后更新**，按希望使用的来源选一条。`update` 会更新代码与依赖，并重启服务：

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- update --source github

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- update --source gitee
```

已有官方 Git 部署时，显式 `--source github` / `--source gitee` 会将 `origin` 切换到对应官方仓库，
网页「⬆ 版本」也会从该来源更新。原来不带 `--source` 的命令仍可用：新装默认 GitHub，已有安装保留 `origin`；自维护仓库或 fork 不会被自动改写，
使用这类仓库时请省略 `--source`，沿用原来源。两个平台的分支与提交以仓库实际发布内容为准。

自定义安装目录仍需补 `--dir '/实际安装目录'`。使用安装目录的属主用户核对来源：

```bash
git -C /opt/antmonitor remote get-url origin
```

**再次运行安装入口且不指定 `update` 等动作**：已安装且有交互终端时进入管理菜单，
可选更新 / 修复 / 卸载 / 重置用户名和密码；没有交互终端时直接更新。
也可直接传入动作，例如需要重置账号时，使用同一个 Gitee 入口：

```bash
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- reset-user --source gitee
```

GitHub 访问慢时可选 Gitee 命令，或使用可信的 `GH_PROXY` 镜像前缀。
系统软件源、Python 的 pip 依赖仍需联网。可选的手动源码安装见文末开发/调试段落。

### Ubuntu 无桌面访问与云端配置

Ubuntu Server 没有桌面也能使用完整面板。AntMonitor 默认监听 `0.0.0.0:8800`；
Linux 上只打印访问地址，不会自动打开浏览器。默认安装后按以下方式操作。

#### 从另一台电脑打开面板

在 Ubuntu 终端查看 IP：

```bash
hostname -I
```

输出可能包含多个地址，选择另一台电脑能访问的局域网 IP。在那台电脑的浏览器打开
`http://<Ubuntu监控机IP>:8800`（例如 `http://192.168.1.50:8800`，请替换 IP）。
用 **admin** 登录，按下方[装好以后](#装好以后)的步骤进入「⚙ 网段 → ☁ 云端上报」。

如果另一台电脑已经可以通过 SSH 登录 Ubuntu，也可以在**有浏览器的那台电脑**的终端
（Windows 可用 PowerShell）运行：

```bash
ssh -N -L 18800:127.0.0.1:8800 用户名@Ubuntu监控机IP
```

替换为实际 SSH 登录信息，保持窗口打开，再用同一台电脑的浏览器访问 `http://127.0.0.1:18800`。
`18800` 是浏览器所在电脑的转发端口，`8800` 是 Ubuntu 上的面板端口。
以上示例按默认 HTTP / 8800 配置编写；如果改过面板端口或启用了 HTTPS，按实际配置调整。

#### 已经安装后，用终端设置云端接入

先在云端面板「🔌 场地接入」复制接入码。以下命令适用于**已经安装**的 Ubuntu：
**它会更新代码和依赖、保存云端配置并重启服务**。如果只需更改上报设置，使用 Web 面板即可。

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- update --source github --connect-code - --site-name '你的场地名' --site-type hydro

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- update --source gitee --connect-code - --site-name '你的场地名' --site-type hydro
```

- `--connect-code -`：运行后在交互终端里粘贴接入码，输入不显示；不要把真实接入码写进命令。
- `--site-name`：本场地在云端显示的名称，请替换示例。
- `--site-type`：`air` 风冷、`hydro` 水冷、`mixed` 混合，按实际类型选择。
- 默认目录为 `/opt/antmonitor`；自定义安装目录需补 `--dir '/实际安装目录'`。

当前管理菜单没有独立的「配置云端」选项；`update` 会处理接入码，`repair` 不会。
接入码会保存到安装目录的 `settings.json`，其中 `cloud` 的对应字段会覆盖 `config.yaml`。
因此只修改 `config.yaml` 可能不生效；不要用新的 JSON 整份覆盖已有 `settings.json`。

终端会输出连接测试结果。若显示 `!` 或连接失败原因，配置可能已保存，但连接尚未成功；
不要仅凭「已接入云端」的提示前缀判断成功。修正地址、接入码或网络后重试，
并在本地面板确认最近上报成功时间、在云端确认本场地出现。

#### 面板打不开或上报失败

在 Ubuntu 终端查看服务、监听端口和近期日志：

```bash
sudo systemctl status antmonitor --no-pager
sudo ss -lntp 'sport = :8800'
sudo journalctl -u antmonitor -n 50 --no-pager
```

服务未启动时先根据日志定位；本机有监听、另一台电脑仍打不开时，检查 IP、端口和网络是否可达，
或使用上面的 SSH 转发。若使用自定义端口，请把检查命令中的 `8800` 换成实际端口。
网页里的「🔌 测试连接」检查云端地址、证书与 token，**不会保存配置**；修改后需要点击「保存」，
再查看下方上报状态。上报由本地主动发往云端，无需为此给场地端配置公网端口映射。

### Windows：一键安装

在 PowerShell（不需要管理员）里执行，按下载源选一条：

**GitHub（无需提前安装 Git）：**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1 | iex"
```

**Gitee（系统先安装 Git for Windows，且 `git --version` 可用）：**

```powershell
& ([scriptblock]::Create((irm 'https://gitee.com/raphael01/antmonitor/raw/main/install.ps1'))) -Source gitee
```

Gitee 方式会自动克隆源码，无需自己执行 `git clone`。如果提示找不到 Git，请安装 Git for Windows，
重新打开 PowerShell 后重试；Gitee 模式不会自动下载 Git，也不使用仓库 ZIP 下载页。

两种方式默认安装到 `%LOCALAPPDATA%\AntMonitor\antmonitor`，在桌面创建「AntMonitor」快捷方式并启动。
没有 Python 时会从 python.org 下载并验证签名后按当前用户安装，再创建虚拟环境、安装依赖并提示设置 admin 密码。
GitHub ZIP 安装会自动准备 Git 部署以支持网页更新；Gitee 安装直接保留克隆仓库的 `origin`。
以后双击桌面快捷方式启动，窗口里按 Ctrl+C 停止；日常版本更新使用网页入口。

**已有 Windows 安装**：再次运行任一安装脚本只会启动已有程序，`-Source gitee` 不会改变已有 `origin`。
若需把默认官方 GitHub 部署改为 Gitee，先在 PowerShell 进入实际安装目录，检查本地改动与来源；
确认是官方仓库并已保存本地改动后，再执行后两条命令：

```powershell
Set-Location "$env:LOCALAPPDATA\AntMonitor\antmonitor"
git status --short
git remote get-url origin
git remote set-url origin https://gitee.com/raphael01/antmonitor.git
git remote get-url origin
```

使用自维护仓库或 fork 时保留维护者指定的来源。没有 `.git` 的目录不能直接用上述 Git 换源命令。
首次安装、依赖变化时仍需能访问 Python 与 pip 下载源；Python 安装日志在 `%LOCALAPPDATA%\AntMonitor\python-install.log`。
手动改密码：在项目目录 CMD 执行 `.venv\Scripts\python.exe auth.py passwd admin`。

### 装好以后

1. 浏览器打开面板，用 admin 登录 → 右上「⚙ 网段」填矿机网段 → 保存并全网扫描。
2. 接入云端：从云端面板「🔌 场地接入」复制**接入码**，回到本地同一页「☁ 云端上报」粘贴并点「填入」
   → 填场地名、选择风冷 / 水冷 / 混合、勾选启用 →「🔌 测试连接」→ **保存**。
   测试连接只检查地址 / 证书 / token，**不会保存配置**；保存后下方显示上报状态（最近一次成功时间或失败原因）。
   云端使用自签名证书（例如 `https://IP:端口`）时需要填写**证书指纹**，可随接入码一并填入。
3. 上报 token 保存后不再明文显示（只提示已填写，较长 token 显示末 4 位）；云端地址和证书指纹未改变时，
   输入框留空保存会沿用原 token。更换地址或指纹后，需要重新填写 token 或粘贴新的接入码。

<details><summary>手动安装（开发/调试）</summary>

需要手动管理源码时，下面的克隆地址也可替换为 `https://gitee.com/raphael01/antmonitor.git`。

```bash
git clone https://github.com/raphael2025/antmonitor.git && cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml    # 改网段、登录账号
python auth.py passwd admin           # 设置 admin 密码
python server.py                      # http://<IP>:8800
```
</details>

安全清单：[SECURITY](../SECURITY.md)。

### 大场地防误报（默认已开启）

交换机重启、STP 收敛、ARP 表刷新、矿机网页服务卡顿、换矿池瞬间读到 0 算力，通常一两分钟就过去。
现在的做法：扫描发现掉线或零算力 → 等 20 秒用更长超时重探（**二次确认**）→ 仍不行**当轮就报**；
**整段掉线**（交换机/断电）不重探，当轮直接报一条整段掉线。`config.yaml > alerts` 可调：

| 项 | 默认 | 含义 |
|---|---|---|
| `offline_confirm_sec` | 0 | 额外的跨轮次确认：二次确认后还要持续这么久才报。交换机经常闪断的场地可设 240（≈下一轮巡检，告警晚约 5 分钟） |
| `zero_confirm_sec` | 0 | 同上，针对零算力 |
| `overheat_rounds` | 2 | 连续 N 轮超温才报；0℃/负数/超过 150℃ 的读数视为坏传感器直接丢弃 |
| `unresponsive_rounds` | 3 | 端口连得上但接口一直读不到（控制板卡死）连续 N 轮才按掉线报 |
| `site_down_ratio` / `site_down_min` | 0.5 / 50 | 全场过半已知机器同时掉线 → 只报一条「全场大面积掉线」，不逐段逐台刷屏 |
| `container_offline_rounds` | 3 | 集装箱控制器连续 N 轮（每轮约 10 秒）没采到才判离线 |
| `scan.reconfirm_delay_sec` | 20 | 二次确认每轮重探前先等几秒，避开同一波网络抖动 |
| `scan.online_timeout` / `gate_timeout` | 3 | 单台判活/在线判定最多等 3 秒（低于 3 按 3）：扫描慢一点，但慢响应的矿机不会被当成掉线 |

另外：矿机网页（lighttpd/CGI）卡住但挖矿程序还在跑时，改从 4028 端口取算力，不再当成掉线。

---

## 使用

| 角色 | 能力 |
|---|---|
| viewer | 只看 |
| ops | 扫描 + 远程命令 |
| admin | 网段 / 设置 / 审计 / 更新 |

日常：看总览与告警 → 勾选机器定位灯或重启 → 维修中可静音告警。  
重启限流：每 IP 24h 内成功次数与间隔有上限。弱口令仅提示改密。

---

## 定制

项目持续迭代；欢迎提意见。规划中含 **多客户管理**、Agent Skill + MCP 等。

微信 **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9

开发说明 [DEVELOPMENT](../DEVELOPMENT.md) · Agent 取数 [EXTERNAL_API](../EXTERNAL_API.md)

## 面板语言 / Panel language

登录页与顶栏可选语言；选择会保存在浏览器本地。
