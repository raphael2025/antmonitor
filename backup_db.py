#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 在线热备份 + 完整性校验 + 滚动保留。

生产数据(快照/告警/命令审计/客户结算)只有一个 .db 文件，必须定期备份。
本脚本用 SQLite 在线备份 API（即使服务正在写入也能拿到一致快照，WAL 安全），
不要直接 copy .db（WAL 下可能拿到不一致状态）。

用法：
    python backup_db.py                       # 备份到 ./backups，保留最近14份
    python backup_db.py --dir D:\\miner_backups --keep 30
    python backup_db.py --src miner_monitor.db --dir \\\\nas\\share\\miner

建议：用 Windows 计划任务每天跑一次（见 docs/DEVELOPMENT.md 运维章节）。
"""
import argparse
import os
import sqlite3
import sys
import time

import yaml


DEFAULT_DB = "miner_monitor.db"
# 相对路径一律按本脚本所在目录(即程序目录)解析，不跟随 CWD：Windows 计划任务没填"起始于"
# 时 CWD 是 C:\Windows\System32，按 CWD 找会读不到配置/库文件，备份静默失败且没人看输出。
# 与 run.bat(先 cd 到程序目录再启动服务)下服务实际使用的库文件位置一致
BASE = os.path.dirname(os.path.abspath(__file__))


def _anchor(path):
    return path if os.path.isabs(path) else os.path.join(BASE, path)


def _db_path():
    """与 db.py / server.py 一致：配置文件路径由 MINER_CONFIG 环境变量决定。

    以前这里硬编码 "config.yaml" 且吞掉所有异常，部署环境用 MINER_CONFIG 指向
    别处时会静默备份错误/过期的库文件——出问题时完全无感知。"""
    cfg_path = _anchor(os.environ.get("MINER_CONFIG", "config.yaml"))
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return _anchor((cfg.get("db") or {}).get("path", DEFAULT_DB))
    except (OSError, yaml.YAMLError, AttributeError) as e:
        print(f"[WARN] 读取配置失败 {cfg_path}: {e}")
        print(f"[WARN] 回退到默认数据库路径 {_anchor(DEFAULT_DB)}；"
              f"若实际库不在此处请用 --src 指定或检查 MINER_CONFIG")
        return _anchor(DEFAULT_DB)


def _rm_partial(path):
    """删除失败留下的半成品备份(含 WAL/SHM 残留)，不让它冒充一份有效备份。"""
    for p in (path, path + "-wal", path + "-shm"):
        if os.path.exists(p):
            try:
                os.remove(p)
                print(f"[..] 已清理半成品备份 {p}")
            except OSError as e:
                print(f"[WARN] 清理半成品备份失败 {p}: {e}")


def backup(src, out_dir, keep, stamp):
    if keep < 1:
        print("[ERR] --keep 必须至少为 1；拒绝创建后立即删除全部备份")
        return 2
    if not os.path.exists(src):
        print(f"[ERR] 源库不存在: {src}")
        return 2
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, f"miner_monitor_{stamp}.db")

    # 1) 在线备份（一致快照，不阻塞写）
    # 任何一步失败都必须删掉半成品文件：否则它会被下面的滚动保留逻辑当成一份
    # "合法备份"占位，把真正可用的旧备份挤出保留窗口。
    err = None
    src_conn = dst_conn = None
    try:
        src_conn = sqlite3.connect(src)
        dst_conn = sqlite3.connect(dst)
        with dst_conn:
            src_conn.backup(dst_conn)   # SQLite Online Backup API
    except (sqlite3.Error, OSError) as e:
        err = e
    finally:
        for c in (src_conn, dst_conn):
            if c is not None:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
    if err is not None:
        print(f"[ERR] 在线备份失败 {src} -> {dst}: {err}")
        _rm_partial(dst)
        return 3

    # 2) 完整性校验：不通过/校验本身出错都删掉这份——留着会占一个保留名额，把好的旧备份挤出去
    try:
        chk = sqlite3.connect(dst)
        try:
            res = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()
    except sqlite3.Error as e:
        res = f"校验出错: {e}"
    if res != "ok":
        print(f"[ERR] 备份完整性校验失败: {dst} -> {res}")
        _rm_partial(dst)
        return 3
    size_mb = os.path.getsize(dst) / 1e6
    print(f"[OK] 备份成功 {dst}  ({size_mb:.1f} MB, integrity_check=ok)")

    # 3) 滚动保留：只留最近 keep 份
    backups = sorted(
        (f for f in os.listdir(out_dir) if f.startswith("miner_monitor_") and f.endswith(".db")),
        reverse=True)
    for old in backups[keep:]:
        try:
            os.remove(os.path.join(out_dir, old))
            print(f"[..] 清理旧备份 {old}")
        except OSError as e:
            print(f"[WARN] 删除旧备份失败 {old}: {e}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="SQLite 在线备份 + 完整性校验 + 滚动保留")
    ap.add_argument("--src", default=None, help="源库路径(默认读 config.yaml 的 db.path)")
    ap.add_argument("--dir", default="backups", help="备份目录(建议另一块盘/NAS)")
    ap.add_argument("--keep", type=int, default=14, help="保留份数")
    args = ap.parse_args()
    src = args.src or _db_path()
    args.dir = _anchor(args.dir)
    # 时间戳由系统提供(脚本是一次性运行，非长驻，可安全用 time)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    sys.exit(backup(src, args.dir, args.keep, stamp))


if __name__ == "__main__":
    main()
