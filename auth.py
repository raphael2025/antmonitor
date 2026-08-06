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

# 哑口令记录：用户名不存在时拿它跑一遍等量 PBKDF2，拉平"无此用户/口令错"的耗时差(时序侧信道)。
# 随机 salt + 全零摘要，构造本身不做哈希运算(不拖慢导入)，且永远校验不通过。
_DUMMY_STORED = f"pbkdf2${_PBKDF2_ITER}${secrets.token_hex(16)}${'00' * 32}"


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
    return hmac.compare_digest(stored.encode("utf-8"), str(password).encode("utf-8"))   # 明文回退


def _users(cfg):
    return {u["username"]: u for u in cfg.get("auth", {}).get("users", [])}


_DEFAULT_PW = {"admin": "admin888", "ops": "ops888", "viewer": "viewer888"}


def weak_default_users(cfg):
    """返回口令不安全的用户名列表(供启动自检告警)。能重启/换池全场的系统不该留弱口令。

    两类都算不安全，第二类才是最容易踩的：
      1. 口令哈希对应的正是出厂默认弱口令(admin888 等)；
      2. password 字段压根不是 pbkdf2$ 哈希 —— 空值、config.example.yaml 里那句
         "改成 python auth.py 生成的哈希" 占位符、或任意手填明文。_verify_password
         对非哈希值会走明文比较回退，于是这些字面量(占位符是公开可查的)直接成了
         可用口令，等于认证绕过。忘记按注释改密码正是最常见的部署失误，必须报警。

    返回值仍是 list[str](用户名)，与 server.py 启动自检的打印方式保持兼容。
    """
    out = []
    for name, u in _users(cfg).items():
        stored = str(u.get("password", "") or "")
        if not stored.startswith("pbkdf2$"):
            out.append(name)          # 非哈希格式：占位符/空口令/明文，一律告警
        elif name in _DEFAULT_PW and _verify_password(stored, _DEFAULT_PW[name]):
            out.append(name)          # 哈希过，但哈希的就是出厂弱口令
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
    try:
        if not u:   # 用户名不存在也跑一遍等量哈希，避免"无此用户"秒回暴露有效用户名(时序侧信道)
            _verify_password(_DUMMY_STORED, password)
        ok = bool(u) and _verify_password(u.get("password", ""), password)
    except Exception:   # 口令校验出任何异常都按登录失败处理，不让其冒泡成 500
        ok = False
    if not ok:
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
