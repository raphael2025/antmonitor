#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git 检测/更新器（云端与场地端同一份文件，自我更新自己所在目录的仓库）。

- check():  git fetch 后对比本地/远端，返回落后多少个提交 + 更新内容
- apply():  仅快进合并(ff-only) → 自检(全部 *.py 编译 + 子进程 import server)，
            失败自动回滚 → 退出进程，交给守护(systemd/NSSM/run.bat 循环)用新代码拉起
            全程持一把互斥锁，并发调用直接被拒绝(不排队)
- start_auto(cfg): 后台线程按 check_interval 轮询，检测到新版本自动 apply
- CLI: python updater.py check | apply   (CLI 的 apply 不重启进程，需手动重启服务)

约定：
- 部署机以 git clone 方式部署，config.yaml/数据库等本机文件已被 .gitignore 排除，
  更新永远不会碰配置和数据。
- 生产建议跟踪专用 release 分支(update.branch)，开发分支随便推不影响线上。
- Docker 部署不用本更新器（重建镜像），见 docs/DEPLOY.md。
"""
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

# 更新互斥：apply() 全程串行。两次 POST /api/update/apply 撞车(人为双击、或自动检测
# 线程与手动调用同时触发)会让两串 git fetch/merge/reset 打在同一个 .git 索引上，
# 轻则 index.lock 报错，重则一个线程正在 reset --hard 回滚、另一个线程正在编译/导入
# 同一份工作区(TOCTOU)，自检结果不可信。取不到锁就直接拒绝，绝不排队等待。
_apply_lock = threading.Lock()

# 自检子进程超时(秒)。冷启动 import fastapi 一套在忙碌的 Windows 机器上可能要十几秒，
# 留足余量：超时会被判定为自检失败并回滚，宁可宽松也别误伤好版本。
SELFCHECK_TIMEOUT = 60


def _git(*args, timeout=60):
    r = subprocess.run(["git", *args], cwd=BASE, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return (r.stdout or "").strip()


def check(branch=None):
    """返回 {git, branch, local, remote, behind, changes[], checked_ts} 或 {git:False/error}。"""
    try:
        _git("rev-parse", "--git-dir", timeout=15)
    except Exception:
        return {"git": False, "error": "本目录不是 git 仓库(未用 git 部署)"}
    try:
        branch = branch or _git("rev-parse", "--abbrev-ref", "HEAD")
        _git("fetch", "--quiet", "origin", timeout=120)
        local = _git("rev-parse", "HEAD")
        remote = _git("rev-parse", f"origin/{branch}")
        behind = int(_git("rev-list", "--count", f"HEAD..origin/{branch}") or 0)
        changes = (_git("log", "--oneline", f"HEAD..origin/{branch}", "-10").splitlines()
                   if behind else [])
        return {"git": True, "branch": branch, "local": local[:10], "remote": remote[:10],
                "behind": behind, "changes": changes, "checked_ts": int(time.time())}
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
    prev = _git("rev-parse", "HEAD")
    try:
        _git("merge", "--ff-only", f"origin/{st['branch']}")
    except Exception as e:
        return {"ok": False, "msg": f"合并失败(本地有改动或分叉?): {e}"}
    err = _selfcheck()   # 自检不过就立刻回滚，绝不带病重启
    if err:
        _git("reset", "--hard", prev)
        return {"ok": False, "msg": f"新代码自检失败，已回滚到 {prev[:10]}: {err}"[:300]}
    new = _git("rev-parse", "HEAD")
    if restart:
        threading.Timer(1.5, lambda: os._exit(42)).start()   # 等响应发出去再退
    return {"ok": True, "from": prev[:10], "to": new[:10],
            "changes": st.get("changes"), "restarting": restart}


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
                if st.get("behind"):
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
