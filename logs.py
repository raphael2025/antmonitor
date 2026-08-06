#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一日志：控制台 + 轮转文件。

为什么需要：之前全项目用 print()，输出被 NSSM/run.bat 重定向到一个永不轮转的文件，
跑几个月就是几百 MB，且没有级别/时间戳，出事后无法回溯"几点发生了什么"。

用法：
    import logs
    log = logs.get(__name__)
    log.info("...")

进程入口(server.py)启动时调用一次 logs.setup(CFG)；未调用时按默认参数惰性初始化，
保证任何模块被单独 import(如 CLI/测试)也能正常打日志。

setup() 可重复调用，且**后一次覆盖前一次**：各业务模块在顶层写 log = logs.get(__name__)，
import 顺序决定了惰性 setup(None) 往往先于入口拿到 config.yaml，如果"只生效第一次"，
用户配的 level/file/max_mb 就会被默认值静默吃掉。故这里每次都拆掉旧 handler 重装。
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

BASE = os.path.dirname(os.path.abspath(__file__))
_configured = False       # 仅用于 get() 判断"要不要惰性初始化"，不再是"只配一次"的门闩
_own_handlers = []        # 本模块装上去的 handler，重配时负责关闭并卸掉

FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup(cfg=None):
    """按 config.logging 配置根 logger。可重复调用，每次都以最新 cfg 重新配置。"""
    global _configured
    _configured = True
    lc = (cfg or {}).get("logging") or {}
    level = getattr(logging, str(lc.get("level", "INFO")).upper(), logging.INFO)
    path = lc.get("file") or os.path.join(BASE, "logs", "miner.log")
    # 下限保护：maxBytes<=0 时 stdlib 直接不轮转，文件无限涨——正是本模块要解决的老问题
    max_bytes = max(int(float(lc.get("max_mb", 20)) * 1024 * 1024), 1024 * 1024)
    backups = max(int(lc.get("backups", 5)), 1)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):      # 清掉自己上次装的 + uvicorn/别处装的，避免重复输出
        root.removeHandler(h)
        if h in _own_handlers:         # 只关自己开的文件句柄，别人的 handler 不敢动
            try:
                h.close()
            except Exception:
                pass
    _own_handlers.clear()

    fmt = logging.Formatter(FMT, DATEFMT)

    # 控制台：Windows 控制台默认 GBK，中文日志会抛 UnicodeEncodeError 反过来打断业务线程
    for stream in (sys.stdout,):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    _own_handlers.append(sh)

    try:
        # 裸文件名(如 "miner.log")时 dirname 为空串，makedirs("") 会抛 FileNotFoundError，
        # 被下面 except 兜住后静默退化成"只有控制台"，用户很难察觉——补个 "." 兜底
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups,
                                 encoding="utf-8", delay=True)
        fh.setFormatter(fmt)
        root.addHandler(fh)
        _own_handlers.append(fh)
    except OSError as e:      # 日志文件写不了不能拖垮监控本身
        root.warning("日志文件不可写(%s)，仅输出到控制台: %s", path, e)

    # 第三方库降噪：urllib3 每次重试/连接都 DEBUG 刷屏，扫描几千台会淹没自己的日志
    for noisy in ("urllib3", "requests", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # uvicorn 的 access 日志由它自己管，这里只统一格式
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def get(name):
    if not _configured:
        setup(None)
    return logging.getLogger(name)
