#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git 检测/更新器（云端与场地端同一份文件，自我更新自己所在目录的仓库）。

- check():  git fetch 后对比本地/远端，返回落后多少个提交 + 更新内容
- apply():  仅快进合并(ff-only) → 自检(全部 *.py 编译 + 子进程 import server)，
            失败自动回滚 → 重启(见 restart_mode：有守护就退出码 42 交给守护拉起，
            没有守护就自己拉起新进程，避免"点了更新，监控就再也不回来")
            全程持一把互斥锁，并发调用直接被拒绝(不排队)
- start_auto(cfg): 后台线程按 check_interval 轮询，检测到新版本自动 apply
- CLI: python updater.py check | apply   (CLI 的 apply 不重启进程，需手动重启服务)

约定：
- 部署机以 git clone 方式部署，config.yaml/数据库等本机文件已被 .gitignore 排除，
  更新永远不会碰配置和数据。
- 生产建议跟踪专用 release 分支(update.branch)，开发分支随便推不影响线上。
- Docker 部署不用本更新器（重建镜像），见 docs/DEPLOY.md。
"""
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import logs

log = logs.get(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))

# 更新状态(已 .gitignore)：{"bad": [自检没过的远端提交], "pending": {"prev","to","attempts"}}
STATE_FILE = os.path.join(BASE, ".update_state.json")
# 新版本连续启动这么多次都没能稳定运行 60 秒 → 自动回滚到上一版
MAX_BOOT_ATTEMPTS = 3
HEALTHY_AFTER_SEC = 60

# 更新互斥：apply() 全程串行。两次 POST /api/update/apply 撞车(人为双击、或自动检测
# 线程与手动调用同时触发)会让两串 git fetch/merge/reset 打在同一个 .git 索引上，
# 轻则 index.lock 报错，重则一个线程正在 reset --hard 回滚、另一个线程正在编译/导入
# 同一份工作区(TOCTOU)，自检结果不可信。取不到锁就直接拒绝，绝不排队等待。
_apply_lock = threading.Lock()

# 自检子进程超时(秒)。冷启动 import fastapi 一套在忙碌的 Windows 机器上可能要十几秒，
# 留足余量：超时会被判定为自检失败并回滚，宁可宽松也别误伤好版本。
SELFCHECK_TIMEOUT = 60


def restart_mode():
    """更新后怎么重启。

    "guardian": 以退出码 42 退出，由守护进程用新代码拉起——
                run.bat(设置 MINER_GUARDIAN)、Windows 服务(NSSM 等，进程在 session 0)、
                systemd(有 INVOCATION_ID)。这些情况下自己再拉一个进程会和守护拉起的抢端口。
    "self":     直接 `python server.py` 手动启动、没有任何守护：退出就再也不回来了，
                所以自己先拉起一个新进程(等旧进程放开端口再监听)，再退出。

    环境变量 MINER_GUARDIAN 显式指定时以它为准：1=有守护，0=没有(例如用任务计划程序以
    SYSTEM 身份直接跑 python server.py——也在 session 0，但任务计划不会因退出码 42 重启)。
    """
    g = os.environ.get("MINER_GUARDIAN")
    if g == "0":
        return "self"
    if g:
        return "guardian"
    if os.name == "nt":
        try:
            import ctypes
            sid = ctypes.c_ulong()
            if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)) \
                    and sid.value == 0:
                return "guardian"
        except Exception:  # noqa: BLE001
            pass
    elif os.environ.get("INVOCATION_ID"):
        return "guardian"
    return "self"


def _restart():
    mode = restart_mode()
    log.warning("更新完成，重启方式: %s(%s)", mode,
                "退出码42交给守护拉起" if mode == "guardian" else "自己拉起新进程")
    if mode == "guardian":
        os._exit(42)
    try:
        env = dict(os.environ, MINER_NO_BROWSER="1", MINER_WAIT_PORT_FREE="1")
        subprocess.Popen([sys.executable, os.path.join(BASE, "server.py")],
                         cwd=os.getcwd(), env=env)
    except Exception as e:  # noqa: BLE001
        # 拉不起新进程就别退出：新代码已在磁盘上，旧进程继续服务，人工重启即可生效
        log.error("自动重启失败(新版本已就位，请手动重启服务生效): %s", e)
        return
    os._exit(0)


# 绝不让 git 等人输入：凭据过期时 git 会在控制台问用户名/弹凭据窗口，服务进程里没人答，
# 卡住的 git-remote-https 孙进程还会让 subprocess 的 timeout 失效(Windows 上只杀得掉 git.exe)，
# 更新锁永久不释放。低速阈值让半断的网络 30 秒内失败而不是挂死。
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
            "GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "30",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=15"}


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("写更新状态失败: %s", e)


def _mark_bad(sha):
    st = _load_state()
    bad = [b for b in st.get("bad", []) if b != sha][-19:]
    st["bad"] = bad + [sha]
    _save_state(st)


def _set_pending(prev, to):
    st = _load_state()
    st["pending"] = {"prev": prev, "to": to, "attempts": 0, "ts": int(time.time())}
    _save_state(st)


def boot_guard():
    """服务启动最开始调用(在 import 其余业务模块之前，模块级崩溃也算一次失败启动)。

    刚更新过(有 pending)：记一次启动尝试；连续 MAX_BOOT_ATTEMPTS 次都没撑到 mark_healthy()，
    说明新版本一启动就崩(自检只 import 了一遍，挡不住读真实配置/真实库才出的错)：
    git reset 回上一版、把这个提交记为坏版本，然后重启——守护(run.bat/NSSM)会用旧代码拉起。
    赶在 run.bat 连续 5 次快速失败熔断之前完成。"""
    st = _load_state()
    p = st.get("pending")
    if not isinstance(p, dict) or not p.get("prev"):
        return
    p["attempts"] = int(p.get("attempts", 0)) + 1
    if p["attempts"] <= MAX_BOOT_ATTEMPTS:
        _save_state(st)
        return
    msg = (f"新版本 {str(p.get('to'))[:10]} 连续 {MAX_BOOT_ATTEMPTS} 次启动失败，"
           f"自动回滚到 {str(p['prev'])[:10]}")
    try:
        sys.stderr.write(msg + "\n")
        log.error("%s", msg)
    except Exception:  # noqa: BLE001
        pass
    try:
        _git("reset", "--hard", p["prev"])
    except Exception as e:  # noqa: BLE001
        log.error("自动回滚失败，需要人工处理: git reset --hard %s (%s)", p["prev"], e)
        return
    st.pop("pending", None)
    _save_state(st)
    if p.get("to"):
        _mark_bad(p["to"])
    _restart()


def mark_healthy():
    """新版本稳定运行 HEALTHY_AFTER_SEC 秒后调用：更新确认成功，清掉 pending。"""
    st = _load_state()
    if st.pop("pending", None) is not None:
        _save_state(st)
        log.info("新版本已稳定运行，更新确认完成")


def _git(*args, timeout=60):
    r = subprocess.run(["git", "-c", "i18n.logOutputEncoding=utf-8", *args], cwd=BASE,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env=dict(os.environ, **_GIT_ENV),
                       timeout=timeout, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return (r.stdout or "").strip()


def check(branch=None):
    """返回 {git, branch, local, remote, behind, changes[], checked_ts} 或 {git:False/error}。"""
    try:
        _git("rev-parse", "--git-dir", timeout=15)
    except FileNotFoundError:
        return {"git": False, "error": "找不到 git 命令：请安装 Git 并确认它在系统 PATH 里"
                                      "(以服务方式运行时要装成所有用户可用)"}
    except Exception as e:  # noqa: BLE001
        # 不一定是"不是仓库"：服务账号与 clone 账号不同会报 dubious ownership 等
        return {"git": False, "error": f"git 无法读取本目录: {str(e)[:200]}"}
    try:
        cur = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch and cur != "HEAD" and cur != branch:
            return {"git": True, "error": f"配置跟踪分支 {branch}，但本地当前在 {cur} 分支；"
                                          f"请先 git checkout {branch} 或改 update.branch"}
        branch = branch or cur
        if branch == "HEAD":   # detached HEAD(按 tag/提交部署)：别悄悄跟到远端默认分支
            return {"git": True, "error": "当前不在任何分支上(detached HEAD)，"
                                          "请在 config.yaml 配置 update.branch 或先 git checkout 分支"}
        _git("fetch", "--quiet", "origin", timeout=120)
        local = _git("rev-parse", "HEAD")
        remote = _git("rev-parse", f"origin/{branch}")
        behind = int(_git("rev-list", "--count", f"HEAD..origin/{branch}") or 0)
        changes = (_git("log", "--oneline", f"HEAD..origin/{branch}", "-10").splitlines()
                   if behind else [])
        return {"git": True, "branch": branch, "local": local[:10], "remote": remote[:10],
                "behind": behind, "changes": changes, "checked_ts": int(time.time()),
                "remote_full": remote, "known_bad": remote in _load_state().get("bad", [])}
    except Exception as e:
        return {"git": True, "error": str(e)[:300]}


def _import_check():
    """子进程真正 import 一遍主入口 server，返回 "" 通过 / 失败原因字符串。

    为什么必须是子进程而不是 importlib.import_module：
      1) 当前进程早就 import 过**旧版本**的这些模块，同进程重导会命中模块缓存，
         新版本的模块级错误根本不会暴露(误判为通过)；
      2) server 顶层有副作用——load_config / logs.setup / MonitorService(cfg)
         (后者会 db.init_db 建表+ALTER 迁移)，在 updater 进程里跑会污染正在
         运行的实例。

    为什么这样是安全的：用 MINER_CONFIG 指向一份临时沙箱配置(内存库 + 临时日志文件 +
    关闭调度)，与 tests/test_server_api.py 导入 server 的做法完全一致——绝不碰生产
    数据库、生产日志和真实设备。server 顶层没有 uvicorn.run/端口绑定(启动代码都在
    if __name__ == "__main__" 里)，单纯 import 不会把 HTTP 服务跑起来。
    """
    if not os.path.exists(os.path.join(BASE, "server.py")):
        return ""      # 云端等没有 server.py 的部署：跳过，只保留编译自检，别误报
    tmp = tempfile.mkdtemp(prefix="update-selfcheck-")
    try:
        cfg_path = os.path.join(tmp, "selfcheck.yaml")
        log_path = os.path.join(tmp, "selfcheck.log").replace("\\", "/")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write('db:\n  path: ":memory:"\n'
                    'schedule:\n  enabled: false\n'
                    f'logging:\n  file: "{log_path}"\n  level: "ERROR"\n')
        env = dict(os.environ, MINER_CONFIG=cfg_path, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-c", "import server"], cwd=BASE, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=SELFCHECK_TIMEOUT)
        if r.returncode != 0:
            detail = ((r.stderr or "") + (r.stdout or "")).strip()
            return f"import server 失败(退出码 {r.returncode}): {detail[-300:] or '无输出'}"
        return ""
    except subprocess.TimeoutExpired:
        return f"import server 超时(>{SELFCHECK_TIMEOUT}s)，疑似顶层卡死"
    except Exception as e:   # noqa: BLE001  自检本身出错也按不通过处理(宁可回滚)
        return f"导入自检无法执行: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _selfcheck():
    """新代码自检，返回 "" 通过 / 失败原因。两道防线，任一不过都回滚。

    1) 逐个 py_compile：便宜，挡纯语法错误。
    2) 子进程 import server：server 会连带 import appconfig/db/service/alerts/
       miner_core/auth/control/logs/cloud_report 等几乎全部业务模块，一次性验证
       "依赖装齐了、模块都能被正确导入"，挡住 py_compile 挡不住的依赖缺失
       (新版 requirements.txt 加了包但没 pip install)和模块级初始化错误。
       仍挡不住只在深层运行时才触发的逻辑 bug——那已超出更新器能做的范围。
    """
    try:
        for rel in _git("ls-files", "*.py").splitlines():
            py_compile.compile(os.path.join(BASE, rel), doraise=True)
    except Exception as e:  # noqa: BLE001
        return f"编译失败: {e}"
    return _import_check()


def apply(branch=None, restart=True):
    """更新到远端最新(带互斥锁，同一时刻只允许一个更新任务)。"""
    if not _apply_lock.acquire(blocking=False):
        return {"ok": False, "msg": "已有更新任务正在执行，请稍后重试"}
    release = True
    try:
        r = _apply(branch, restart)
        # 已排定退出(1.5秒后 os._exit)：这段窗口期继续持锁，挡住"进程正要死"时
        # 又开一次 git 操作把仓库停在半路的情况。进程退出后锁自然随进程消失。
        release = not r.get("restarting")
        return r
    finally:
        if release:
            _apply_lock.release()


def _apply(branch=None, restart=True):
    """更新到远端最新：ff-only 合并 → 自检(失败回滚) → 退出进程交给守护重启。"""
    try:
        dirty = _git("status", "--porcelain", "--untracked-files=normal", timeout=15)
    except Exception as e:
        return {"ok": False, "msg": f"无法检查工作区状态: {e}"}
    if dirty:
        return {"ok": False, "msg": "工作区有未提交改动，已拒绝自动更新；请先提交或妥善保存改动"}
    st = check(branch)
    if st.get("error") or not st.get("behind"):
        return {"ok": False, "msg": st.get("error") or "已是最新版本", "check": st}
    if st.get("known_bad"):   # 同一个坏提交不再反复"合并→自检失败→回滚"(每小时一次，期间磁盘上是坏代码)
        return {"ok": False, "msg": f"远端版本 {st['remote']} 之前自检未通过或启动失败已回滚，等待新的提交",
                "check": st}
    prev = _git("rev-parse", "HEAD")
    try:
        _git("merge", "--ff-only", f"origin/{st['branch']}")
    except Exception as e:
        return {"ok": False, "msg": f"合并失败(本地有改动或分叉?): {e}"}
    err = _selfcheck()   # 自检不过就立刻回滚，绝不带病重启
    if err:
        _mark_bad(st.get("remote_full") or "")
        for attempt in (1, 2):
            try:
                _git("reset", "--hard", prev)
                break
            except Exception as e:  # noqa: BLE001 - 文件被占用/index.lock 时再试一次
                if attempt == 2:
                    log.error("新代码自检失败且回滚失败，磁盘上是坏版本，下次重启会加载它！"
                              "请人工执行 git reset --hard %s (%s)", prev, e)
                    return {"ok": False, "msg": f"新代码自检失败，且回滚失败(需人工执行 git reset "
                                                f"--hard {prev[:10]}): {e}"[:300]}
                time.sleep(2)
        return {"ok": False, "msg": f"新代码自检失败，已回滚到 {prev[:10]}: {err}"[:300]}
    new = _git("rev-parse", "HEAD")
    if restart:
        _set_pending(prev, new)                  # 新版本若一启动就崩，boot_guard 会自动回滚
        threading.Timer(1.5, _restart).start()   # 等响应发出去再退
    return {"ok": True, "from": prev[:10], "to": new[:10],
            "changes": st.get("changes"), "restarting": restart,
            "restart_mode": restart_mode() if restart else ""}


def start_auto(ucfg, on_event=None):
    """自动更新线程。ucfg: config 的 update 段 {auto, branch, check_interval}。
    启动后先等一个完整周期再首查(防更新→重启→立刻又查的循环风暴)。"""
    if not (ucfg or {}).get("auto"):
        return None
    interval = max(300, int(ucfg.get("check_interval", 3600)))
    branch = ucfg.get("branch") or None

    def _say(msg):
        try:
            if on_event:
                on_event(msg)
            else:
                log.info("%s", msg)
        except Exception:  # noqa: BLE001
            pass

    def _loop():
        while True:
            time.sleep(interval)
            try:
                st = check(branch)
                if st.get("error"):
                    log.warning("自动更新检查失败: %s", st["error"])
                elif st.get("behind"):
                    _say(f"🔄 检测到新版本({st['behind']}个提交)，自动更新并重启…")
                    r = apply(branch)
                    if not r.get("ok"):
                        _say(f"自动更新失败: {r.get('msg')}")
            except Exception as e:  # noqa: BLE001
                log.warning("自动检测异常: %s", e)

    t = threading.Thread(target=_loop, name="auto-update", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        st = check()
        if st.get("error"):
            print("检查失败:", st["error"])
        elif st.get("behind"):
            print(f"有新版本: 落后 {st['behind']} 个提交 ({st['local']} → {st['remote']})")
            print("\n".join("  " + c for c in st["changes"]))
        else:
            print(f"已是最新 ({st.get('local')}, 分支 {st.get('branch')})")
    elif cmd == "apply":
        r = apply(restart=False)
        print(r.get("msg") or f"已更新 {r['from']} → {r['to']}，请重启服务进程生效")
        sys.exit(0 if r.get("ok") else 1)
    else:
        print("用法: python updater.py check|apply")
