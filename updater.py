#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git 检测/更新器（云端与场地端同一份文件，自我更新自己所在目录的仓库）。

- check():  git fetch 后对比本地/远端，返回落后多少个提交 + 更新内容
- apply():  仅快进合并(ff-only) → 全部 *.py 编译自检，失败自动回滚 → 退出进程
            交给守护(systemd/NSSM/run.bat 循环)用新代码重新拉起
- start_auto(cfg): 后台线程按 check_interval 轮询，检测到新版本自动 apply
- CLI: python updater.py check | apply   (CLI 的 apply 不重启进程，需手动重启服务)

约定：
- 部署机以 git clone 方式部署，config.yaml/数据库等本机文件已被 .gitignore 排除，
  更新永远不会碰配置和数据。
- 生产建议跟踪专用 release 分支(update.branch)，开发分支随便推不影响线上。
- Docker 部署不用本更新器（重建镜像），见 docs/DEPLOY.md。
"""
import glob
import os
import py_compile
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))


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


def apply(branch=None, restart=True):
    """更新到远端最新：ff-only 合并 → 编译自检(失败回滚) → 退出进程交给守护重启。"""
    st = check(branch)
    if st.get("error") or not st.get("behind"):
        return {"ok": False, "msg": st.get("error") or "已是最新版本", "check": st}
    prev = _git("rev-parse", "HEAD")
    try:
        _git("merge", "--ff-only", f"origin/{st['branch']}")
    except Exception as e:
        return {"ok": False, "msg": f"合并失败(本地有改动或分叉?): {e}"}
    try:   # 编译自检：新代码语法都过不了就立刻回滚，绝不带病重启
        for f in glob.glob(os.path.join(BASE, "*.py")):
            py_compile.compile(f, doraise=True)
    except Exception as e:
        _git("reset", "--hard", prev)
        return {"ok": False, "msg": f"新代码编译失败，已回滚到 {prev[:10]}: {e}"[:300]}
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
                print(f"[update] {msg}")
        except Exception:
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
            except Exception as e:
                print(f"[update] 自动检测异常: {e}")

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
