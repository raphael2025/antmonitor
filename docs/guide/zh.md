# AntMonitor · 中文

[中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

**场地端** https://github.com/raphael2025/antmonitor · **云端** https://github.com/raphael2025/antmonitor-cloud

---

## 简介

AntMonitor 是矿场**局域网**批量监控与运维系统：看清全场算力与状态，告警及时，远程操作有确认和限流。

- 总览 / 列表 / 货架机位 / 告警 / 集装箱 / 客户报表
- 定时巡检；掉线、掉算力、高温、冷却故障可语音与 Telegram
- 定位灯、批量重启（限流）；可选掉线自动重启（默认关）
- 多场地推送到 AntMonitor Cloud；规划 Agent Skill + MCP

适合托管 / 包机。一台 Windows 或 Ubuntu 监控机即可。

---

## 部署

**环境**：Python 3.8+；能访问矿机网段。

**Ubuntu 一键（推荐）**：在仓库根目录执行 `sudo bash install.sh`  
菜单可选语言，支持：首次安装 · 更新 · 修复 · 卸载 · 重置用户名和密码。默认目录 `/opt/antmonitor`，服务名 `antmonitor`。

```bash
# 也可手动
git clone https://github.com/raphael2025/antmonitor.git && cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml    # 改网段、登录账号
python server.py                      # http://<IP>:8800
```

**Windows**：

1. 在 Windows 10 / 11 将项目 ZIP 完整解压到本地可写目录（也可 `git clone`），双击 `run.bat`。
2. 没有可用 Python 时，自动从 python.org 下载并验证官方安装包的签名，按当前用户安装到
   `%LOCALAPPDATA%\AntMonitor\Python313`。不需要管理员权限，也不用修改 PATH 或重新打开窗口。
   已有可用的标准 Python 3.8–3.14 会复用；没有加进 PATH 的常规 Python 安装也会通过注册表检测。
3. 自动创建 `.venv`、安装 `requirements.txt` 中全部依赖、生成配置，按提示设置 admin 密码。
4. 登录面板并配置矿机网段；已有的配置和密码不会被覆盖。

首次安装、依赖变化或依赖损坏时需要联网；正常重复启动不会每次下载依赖。
暂不使用 Python 3.15+ 或自由线程版，依赖只安装预编译 wheel，避免要求用户安装 C++ 编译工具。
曾使用不兼容版本的 `.venv` 会自动保留到 `.venv.incompatible-*` 并重新创建；配置、密码和数据库不会被覆盖。
启动失败时窗口保留报错，修复后再次双击即可。按 Ctrl+C 停止服务，不会触发自动重启。
Python 安装失败时，可查看 `%LOCALAPPDATA%\AntMonitor\python-install.log`；网络无法访问
python.org 或 PyPI 时需要恢复网络再试，或手动安装 Python 和依赖。
需要手动设置密码时，在项目目录的 CMD 执行 `.venv\Scripts\python.exe auth.py passwd admin`。
ZIP 部署支持本地监控；网页更新需要 Git 和 git clone 的工作目录。
Windows 启动守护代码更新后，关闭旧启动窗口再双击即可使用新的守护逻辑。
安全清单：[SECURITY](../SECURITY.md)。

打开面板后用 admin 配置网段并触发一次全网发现。

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
