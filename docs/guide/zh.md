# AntMonitor · 中文

[中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

**场地端** https://github.com/raphael2025/antmonitor · **云端** https://github.com/raphael2025/antmonitor-cloud

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

**环境**：Ubuntu 20.04+ 或 Windows 10/11；监控机能访问矿机网段。不需要提前装 git / Python。

### Ubuntu：一行命令装好（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash
```

自动完成：装依赖 → 下载代码（有 git 用 git，没有就下源码包）→ 建虚拟环境 → 生成配置 → 设置 admin 密码
（终端里输入；直接回车会自动生成强密码并在最后显示）→ 注册 systemd 服务 `antmonitor` 并启动。
默认目录 `/opt/antmonitor`，面板 `http://<监控机IP>:8800`。

**安装时顺便接入云端**（接入码在云端面板右上「🔌 场地接入」里复制；`-` 表示运行后按提示粘贴，接入码含 token，不写在命令里就不会被 `ps`/命令历史看到）：

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- --connect-code - --site-name 内蒙一场
```

**再次执行不带参数的安装命令**：出管理菜单（更新 / 修复 / 卸载 / 重置用户名和密码）；没有终端（自动化）时直接更新。
也可直接指定：`… | sudo bash -s -- update`（或 `repair` / `uninstall` / `reset-user`）。

GitHub 访问慢：`curl -fsSL <镜像前缀>/https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo GH_PROXY=<镜像前缀>/ bash`
（镜像能看到并篡改下载内容，只用信得过的）。连不上 GitHub：把源码 ZIP 拷到监控机，解压后在目录里
`sudo bash install.sh`，直接用这份源码安装（Python 依赖仍需能访问 PyPI 或国内镜像）。

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
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh |
  sudo bash -s -- update \
    --connect-code - \
    --site-name '你的场地名' \
    --site-type hydro
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

### Windows：一行命令装好

在 PowerShell（不需要管理员）里执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1 | iex"
```

下载源码到 `%LOCALAPPDATA%\AntMonitor\antmonitor`、在桌面建「AntMonitor」快捷方式并启动。首次启动自动：
没有 Python 就从 python.org 下载并验证签名后按当前用户安装（不需要管理员、不改 PATH）→ 建 `.venv` 装依赖 →
生成配置 → 提示设置 admin 密码 → 转成 git 部署（没装 Git 会下载校验过的便携 Git），之后在网页上一键更新。
以后双击桌面快捷方式启动；窗口里按 Ctrl+C 停止。也可以照旧下载 ZIP 解压后双击 `run.bat`。

首次安装、依赖变化时需要联网；Python 安装日志在 `%LOCALAPPDATA%\AntMonitor\python-install.log`。
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
