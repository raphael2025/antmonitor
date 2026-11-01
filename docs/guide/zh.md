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

直接运行上面的 GitHub 或 Gitee 一键安装命令。设置 admin 密码后，脚本会提示：
**「粘贴云端接入码（AMC1-…，输入不显示，回车跳过）」**。
从云端面板右上「🔌 场地接入」复制接入码，粘贴并回车即可。新接入码自动带入场地名和类型；
旧码缺少这些信息时会提示补填。暂时不接云端就回车跳过，以后可从管理菜单或 Web 面板补接。

需要显式要求输入接入码时，仍可使用 `--connect-code -`，例如：

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- --source github --connect-code -

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- --source gitee --connect-code -
```

接入码含 token，不要把真实接入码直接写进命令。`--site-name` 和 `--site-type air|hydro|mixed`
仍可用于明确指定名称与类型；普通交互安装无需记这些参数。
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
用 **admin** 登录，点击顶部独立的 **「☁ 连接云端」**，按下方[装好以后](#装好以后)的步骤粘贴接入码。

如果另一台电脑已经可以通过 SSH 登录 Ubuntu，也可以在**有浏览器的那台电脑**的终端
（Windows 可用 PowerShell）运行：

```bash
ssh -N -L 18800:127.0.0.1:8800 用户名@Ubuntu监控机IP
```

替换为实际 SSH 登录信息，保持窗口打开，再用同一台电脑的浏览器访问 `http://127.0.0.1:18800`。
`18800` 是浏览器所在电脑的转发端口，`8800` 是 Ubuntu 上的面板端口。
以上示例按默认 HTTP / 8800 配置编写；如果改过面板端口或启用了 HTTPS，按实际配置调整。

#### 已经安装后，用终端设置云端接入

先在云端面板「🔌 场地接入」复制接入码。重跑原来的一键安装命令，选择菜单
**6「连接 / 更换云端」**，粘贴接入码并回车即可。也可以用下面的固定入口直接进入接入提示，
适用于**已经安装**的 Ubuntu，包括之前安装的旧版本：

```bash
# GitHub
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- connect

# Gitee
curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/install.sh | sudo bash -s -- connect
```

- 运行后在交互终端里粘贴接入码，输入不显示；不要把真实接入码写进命令。
- 新码自动带入名称与类型；旧码会提示补填，并默认保留已有名称、类型和场地 ID。
- 默认目录为 `/opt/antmonitor`；自定义安装目录需补 `--dir '/实际安装目录'`。

`connect`（别名 `configure-cloud`）只测试连接、保存云端配置并重启现有服务，
不会更新代码、安装依赖、修改 Git 的 `origin` 或重写服务定义。
旧的 `update --connect-code -` 用法仍兼容，但单独接云端无需更新程序。

格式或连接测试失败时，原配置保持不变；终端会显示原因，可重新粘贴或回车跳过。
通过验证后，接入信息写入安装目录的 `settings.json`，其中 `cloud` 的对应字段会覆盖 `config.yaml`。
其余设置会保留；新码包含场地 ID 时使用该 ID，旧码未包含时保留原 ID。
因此只修改 `config.yaml` 可能不生效；不要用新的 JSON 整份覆盖已有 `settings.json`。

终端会分别说明验证、保存和服务启动结果。若服务重启失败，会明确提示「配置已保存、服务未启动」，
按下方命令检查服务后处理。连接测试只验证地址、证书与 token；真实监控数据需等本地首轮扫描完成后才上报。
在本地「☁ 连接云端」窗口可持续查看状态，再到云端确认场地出现。

#### 面板打不开或上报失败

在 Ubuntu 终端查看服务、监听端口和近期日志：

```bash
sudo systemctl status antmonitor --no-pager
sudo ss -lntp 'sport = :8800'
sudo journalctl -u antmonitor -n 50 --no-pager
```

服务未启动时先根据日志定位；本机有监听、另一台电脑仍打不开时，检查 IP、端口和网络是否可达，
或使用上面的 SSH 转发。若使用自定义端口，请把检查命令中的 `8800` 换成实际端口。
网页里的「连接云端」会依次检查接入信息、测试地址 / 证书 / token，成功后才保存并启用。
验证或测试失败时不改变原配置，并保留输入方便重试。若保存请求的响应中断，页面会提示无法确认保存结果，
请先查看下方当前状态再重试。上报由本地主动发往云端，无需为此给场地端配置公网端口映射。

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

<a id="windows-web-update-recovery"></a>

### Windows 网页更新恢复

如果 `run.bat` 已能启动监控，但网页显示「找不到 git」，或者控制台显示安装 MinGit 时
`WinError 5 / 拒绝访问`，可用下面的命令修复更新能力。重新运行普通安装命令只会启动旧程序，不能修复旧启动器。

1. 在原 AntMonitor 控制台按 **Ctrl+C**，等待退出；这会暂时停止监控，不会重启矿机。
2. 用平时启动监控的同一个 Windows 用户打开 **PowerShell**，不需要管理员，按网络情况选择一条：

```powershell
# GitHub
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/raphael2025/antmonitor/main/repair-web-updates.ps1')))

# Gitee
& ([scriptblock]::Create((irm 'https://gitee.com/raphael01/antmonitor/raw/main/repair-web-updates.ps1'))) -Source gitee
```

3. 脚本会显示所选安装目录。如果不在默认目录，先进入原目录再执行，或在命令末尾添加
   `-Directory 'D:\我的场地\AntMonitor'`。没有找到安装时，会提示粘贴含 `run.bat` 和 `server.py` 的原文件夹路径。
   已设置 `ANTMONITOR_HOME` 时优先使用该目录；不要选择另一个新建空目录。
4. 看到「已恢复 Git 和仓库信息」后，脚本会打开新的监控窗口。登录网页，点击右上角 **「更新」** 安装最新版本。
   原来已运行的进程不会自动获得新 Git 路径，所以需要上述关闭、重新启动步骤。

恢复过程把辅助程序放到临时目录，不覆盖原项目文件，不重装依赖，也不修改系统 PATH。
`config.yaml`、`settings.json` 和历史数据库保留；源码 ZIP 会先匹配它原来的历史提交，之后由网页执行正常更新。
如果程序源码被手动修改，或目录中有会阻挡更新的额外文件，脚本会列出原因并停止，不会删除文件或强制覆盖。
请保存本地改动后再处理；不要删除配置和数据库来绕过提示。

**下载源说明：** `-Source gitee` 从 Gitee 下载恢复程序，没有 `.git` 的源码 ZIP 会以 Gitee 为更新源；
已有 Git 仓库的 `origin` 始终保留，不会借修复切换来源。需要自动安装便携 Git 时，两种恢复入口都使用
Git for Windows 的官方 GitHub 发布包，并验证固定 SHA256；Gitee 入口不会代理这个下载。
若电脑无法访问该发布地址，可先安装可用的 Git for Windows、重新打开 PowerShell，再运行恢复命令。

管理员排查时可以添加 `-NoStart` 只修复而不启动。旧启动器可能还不认识新缓存位置，之后应再次运行不带
`-NoStart` 的同一命令启动，再完成网页更新。以 Windows 服务运行的部署需使用该服务的运行用户处理 Git 路径并重启服务。

### 装好以后

1. 浏览器打开面板，用 admin 登录 → 右上「⚙ 网段」填矿机网段 → 保存并全网扫描。
2. 接入云端：从云端面板「🔌 场地接入」复制**接入码**，回到本地顶部独立的 **「☁ 连接云端」** 窗口粘贴。
   新码会自动显示场地名、风冷 / 水冷 / 混合类型，核对后点一次 **「连接云端」**，程序自动测试、保存并启用上报。
   旧码仍可使用，缺少名称时补填一次；已有场地信息会保留，可直接编辑。连接过程不会改变网段或扫描设置。
3. 连接窗口持续显示真实状态：**未连接 → 测试中 → 已保存等待首扫 / 首次上报 → 上报成功**。
   本地尚未扫描时，设置矿机网段并开始全网扫描，完成后自动上报；网络或云端异常会显示失败原因并自动重试。
   配置保存成功不代表已上传数据，以最近一次上报成功时间为准。窗口打开时每 3 秒刷新，关闭后顶部状态每 30 秒刷新。
4. 没有接入码时，展开 **「高级设置 / 手动填写」** 输入地址、token、证书指纹，再点击同一个连接按钮。
   云端使用自签名证书时需要正确的证书指纹，可随接入码自动填入；公共 CA 签发且校验正常的证书可留空。
   需要暂时停止时，点高级设置内的 **「暂停当前上报」**，连接信息会保留；再点「连接云端」即可恢复。
5. 上报 token 保存后不再明文显示（只提示已填写，较长 token 显示末 4 位）；云端地址和证书指纹未改变时，
   输入框留空保存会沿用原 token。更换地址或指纹后，需要重新填写 token 或粘贴新的接入码。

### 日间与夜间主题

登录页和面板顶部提供 **跟随系统 / 按时间自动 / 日间 / 夜间** 四种选择，浏览器会记住偏好。
「跟随系统」跟随电脑或手机的外观设置；「按时间自动」按**浏览设备的本地时间**，
07:00（含）至 19:00（不含）使用日间主题，其余时间使用夜间主题，无需修改服务器时间或配置。

### 客户算力怎么看

客户／矿工名下的**实时算力**是最新一轮扫描中各台机器实际算力的合计；**近 24 小时平均算力**按这段时间内的记录和持续时间计算。
一台机器全天保持 293 TH/s，平均仍为 293 TH/s；100 台都保持这个水平，客户总平均为 29,300 TH/s，也就是 29.3 PH/s。
如果一台机器运行 12 小时为 293 TH/s，另外 12 小时确认离线，全天平均就是 146.5 TH/s。
机器标称 293T 不代表它每时每刻都达到这个数，因此不能用标称算力乘台数代替实测统计。

监控刚安装、记录不足或中途断档时，页面会提示覆盖不足；只有已观测时段的结果，不能当成完整一天的数据。
无法读取的数据与确认离线不同，缺失指标显示未知；实时数据还要结合采样时间看是否已经过期。
选择其他报表周期时，平均值也对应所选周期。矿机列表原来的「平均算力」由矿机固件提供，其时间窗口不保证是 24 小时。

累计交付算力仍保留在周期统计与 CSV 中，单位为 **TH·h**。例如 293 TH/s 持续 24 小时累计 7,032 TH·h，
这是随时间累加的量；实时和平均算力使用 **TH/s、PH/s、EH/s**，其中 1 PH/s = 1,000 TH/s。

需要在云端查看客户实时和平均算力时，场地端与云端都应更新；旧场地端没有上报的新指标显示未知，不会补成零。
默认实时客户数据随摘要每 60 秒上报，平均报表每 600 秒更新，页面刷新不等于产生了新的矿机采样。

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
