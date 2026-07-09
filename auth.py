#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量登录 + 角色权限（RBAC）。

角色等级：viewer(1) < ops(2) < admin(3)
- viewer：只读监控
- ops   ：监控 + 远程命令 + 触发扫描
- admin ：全部（含查看审计/未来用户管理）
会话令牌存内存，重启后失效（需重新登录）。
"""
import sys
import time
import hmac
import hashlib
import secrets
import threading

ROLE_RANK = {"viewer": 1, "ops": 2, "admin": 3}
COOKIE = "mm_token"
TTL = 7 * 86400  # 令牌有效期
_PBKDF2_ITER = 200_000

_sessions = {}  # token -> {user, role, exp}
_slock = threading.Lock()

_fails = {}            # 限流键(来源IP) -> [失败次数, 锁定到期时间戳]
_MAX_FAILS = 8         # 连续失败上限
_LOCK_SEC = 300        # 触顶后锁定秒数(5分钟)，挡在线暴破


def hash_password(password, salt=None):
    """生成 pbkdf2$iter$salt$hex 格式的口令哈希，供 config.yaml 存储(替代明文)。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode(), bytes.fromhex(salt), _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${salt}${dk.hex()}"


def _verify_password(stored, password):
    """兼容: stored 是 pbkdf2$... 则按哈希校验，否则按明文比较(向后兼容，建议改用哈希)。"""
    stored = str(stored or "")
    if stored.startswith("pbkdf2$"):
        try:
            _, iters, salt, hexd = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", str(password).encode(),
                                     bytes.fromhex(salt), int(iters))
            return hmac.compare_digest(dk.hex(), hexd)
        except Exception:
            return False
    return hmac.compare_digest(stored, str(password))   # 明文回退


def _users(cfg):
    return {u["username"]: u for u in cfg.get("auth", {}).get("users", [])}


_DEFAULT_PW = {"admin": "admin888", "ops": "ops888", "viewer": "viewer888"}


def weak_default_users(cfg):
    """返回仍在使用出厂默认弱口令的用户名(供启动自检告警)。能重启/换池全场的系统不该留默认口令。"""
    out = []
    for name, u in _users(cfg).items():
        if name in _DEFAULT_PW and _verify_password(u.get("password", ""), _DEFAULT_PW[name]):
            out.append(name)
    return out


def enabled(cfg):
    return cfg.get("auth", {}).get("enabled", False)


def locked(src):
    """该来源是否在锁定期内(供登录前快速拒绝)。"""
    if not src:
        return False
    with _slock:
        rec = _fails.get(src)
        return bool(rec and rec[1] > time.time())


def login(cfg, username, password, src=None):
    """src: 客户端 IP，用于失败限流(按来源 IP 计，避免锁死合法用户)。返回 token 或 None。"""
    now = time.time()
    if src:   # 锁定期内直接拒绝，不再校验口令
        with _slock:
            rec = _fails.get(src)
            if rec and rec[1] > now:
                return None
    u = _users(cfg).get(username)
    if not u or not _verify_password(u.get("password", ""), password):
        if src:   # 记一次失败，连续达上限则锁定该来源
            with _slock:
                rec = _fails.get(src, [0, 0])
                rec[0] += 1
                if rec[0] >= _MAX_FAILS:
                    rec[1] = now + _LOCK_SEC
                    rec[0] = 0
                _fails[src] = rec
        return None
    token = secrets.token_hex(24)
    with _slock:
        _fails.pop(src, None)   # 登录成功清除该来源失败计数
        # 顺手清理已过锁定期的陈旧限流项 + 过期会话，防内存无限增长
        for k in [k for k, v in _fails.items() if v[1] and v[1] < now]:
            _fails.pop(k, None)
        for t in [t for t, s in _sessions.items() if s["exp"] < now]:
            _sessions.pop(t, None)
        _sessions[token] = {"user": username, "role": u.get("role", "viewer"), "exp": now + TTL}
    return token


def logout(token):
    with _slock:
        _sessions.pop(token, None)


def session(token):
    with _slock:
        s = _sessions.get(token)
        if not s:
            return None
        if s["exp"] < time.time():
            _sessions.pop(token, None)
            return None
        return s


def current(cfg, token):
    """返回 {user, role}；auth 关闭时视为只读 viewer(而非 admin)，
    破坏性操作(命令/下架/改设置)需 ops+，关 auth 也不会被匿名滥用。"""
    if not enabled(cfg):
        return {"user": "anonymous", "role": "viewer"}
    return session(token)


def has_role(sess, min_role):
    if not sess:
        return False
    return ROLE_RANK.get(sess["role"], 0) >= ROLE_RANK.get(min_role, 99)


if __name__ == "__main__":   # python auth.py <明文密码> → 打印哈希，粘到 config.yaml 的 password
    if len(sys.argv) > 1:
        print(hash_password(sys.argv[1]))
    else:
        print("用法: python auth.py <password>  生成 pbkdf2 哈希填入 config.yaml")
