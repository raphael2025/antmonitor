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
IDLE = 12 * 3600  # 空闲超时：12 小时没有任何请求就失效(挂着的大屏一直在轮询，不受影响)
_PBKDF2_ITER = 600_000   # OWASP 2023 建议值；旧哈希按自身记录的迭代次数校验，照常可用

_sessions = {}  # token -> {user, role, exp}
_slock = threading.Lock()

_fails = {}            # 限流键(来源IP) -> [失败次数, 锁定到期时间戳]
_MAX_FAILS = 8         # 连续失败上限
_LOCK_SEC = 300        # 触顶后锁定秒数(5分钟)，挡在线暴破
# 按用户名再计一道：攻击者在 /16 网段里给网卡加几百个 IP 别名就能绕过"按 IP 计"。
# 只对非本机来源生效——坐在监控电脑前的人永远不会被锁在外面(也防别人故意锁死 admin)。
_ufails = {}
_MAX_USER_FAILS = 20
_USER_LOCK_SEC = 900
# 在这个账号下成功登录过的来源：不受"按用户名锁定"影响。否则局域网里任何人用几个 IP 各错几次，
# 就能把值班员从自己常用的电脑上锁在外面(默认用户名 admin/ops 是公开的)
_good_src = {}
_MAX_PW_FAILS = 5      # 同一会话改密码时旧密码连错这么多次 → 踢掉会话(防拿偷来的 cookie 在线爆破)
_pwfails = {}

# 哑口令记录：用户名不存在时拿它跑一遍等量 PBKDF2，拉平"无此用户/口令错"的耗时差(时序侧信道)。
# 随机 salt + 全零摘要，构造本身不做哈希运算(不拖慢导入)，且永远校验不通过。
_DUMMY_STORED = f"pbkdf2${_PBKDF2_ITER}${secrets.token_hex(16)}${'00' * 32}"


def hash_password(password, salt=None):
    """生成 pbkdf2$iter$salt$hex 格式的口令哈希，供 config.yaml 存储(替代明文)。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode(), bytes.fromhex(salt), _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${salt}${dk.hex()}"


# config.example.yaml 里的占位文字：公开可查，照抄后忘了改就等于公开口令
_PLACEHOLDER = "改成 python auth.py 生成的哈希"


def _unusable(stored):
    """空口令 / 示例占位符：绝不是真口令，一律不让登录(不止启动时打日志)。"""
    s = stored.strip()
    return not s or s == _PLACEHOLDER or s.startswith("改成")


def _verify_password(stored, password):
    """兼容: stored 是 pbkdf2$... 则按哈希校验，否则按明文比较(向后兼容，建议改用哈希)。
    空口令和示例占位符直接判失败。"""
    stored = str(stored or "")
    if _unusable(stored):
        return False
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


def is_local(src):
    """来源是监控电脑本机(坐在电脑前的人)。"""
    s = str(src or "")
    return s.startswith("127.") or s in ("::1", "localhost")


def needs_change(u):
    """存储的口令是出厂默认弱口令或明文(不是 pbkdf2 哈希)。仅供启动自检参考。"""
    stored = str((u or {}).get("password", "") or "")
    if not stored.startswith("pbkdf2$"):
        return True
    name = (u or {}).get("username")
    return name in _DEFAULT_PW and _verify_password(stored, _DEFAULT_PW[name])


def password_problem(username, new):
    """新口令是否可用，返回 "" 或原因。"""
    new = str(new or "")
    if len(new) < 8:
        return "新密码至少 8 位"
    if new in _DEFAULT_PW.values() or new.lower() == str(username).lower():
        return "新密码不能是默认密码或与用户名相同"
    if len(set(new)) < 4:
        return "新密码太简单(至少包含 4 种不同字符)"
    return ""


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


def login_ex(cfg, username, password, src=None, local=None):
    """返回 (token, "") 或 (None, 给用户看的原因)。src: 客户端 IP。
    local: 是否坐在监控电脑前(服务端结合代理头/Host 判断后传入)；不传则只按 src 判。"""
    now = time.time()
    local = is_local(src) if local is None else bool(local)
    uname = username if isinstance(username, str) else None
    known = uname in _users(cfg) if uname else False
    with _slock:
        rec = _fails.get(src) if src else None
        if rec and rec[1] > now:
            return None, "失败次数过多，请稍后再试"
        trusted_src = local or (src in _good_src.get(uname, ()))
        urec = _ufails.get(uname) if uname else None
        if urec and urec[1] > now and not trusted_src:
            return None, "该账号失败次数过多，已临时锁定，请稍后再试(监控电脑本机不受限)"
        # 先占位计一次失败再去校验：PBKDF2 期间会释放 GIL，若校验完才计数，
        # 几十个并发错误请求会全部跑完校验，按 IP 的上限形同虚设
        if src:
            rec = _fails.setdefault(src, [0, 0])
            rec[0] += 1
            if rec[0] >= _MAX_FAILS:
                rec[1], rec[0] = now + _LOCK_SEC, 0
        if known and not trusted_src:   # 只给真实存在的账号计数：随机用户名不能把这张表撑爆
            urec = _ufails.setdefault(uname, [0, 0, now])
            if now - urec[2] > _USER_LOCK_SEC:           # 计数窗口过期，重新计
                urec[0], urec[2] = 0, now
            urec[0] += 1
            if urec[0] >= _MAX_USER_FAILS:
                urec[1], urec[0], urec[2] = now + _USER_LOCK_SEC, 0, now
    u = _users(cfg).get(uname) if uname else None
    try:
        if not u:   # 用户名不存在也跑一遍等量哈希，避免"无此用户"秒回暴露有效用户名(时序侧信道)
            _verify_password(_DUMMY_STORED, password)
        ok = bool(u) and _verify_password(u.get("password", ""), password)
    except Exception:   # 口令校验出任何异常都按登录失败处理，不让其冒泡成 500
        ok = False
    if not ok:
        if u and _unusable(str(u.get("password", "") or "")):
            return None, (f"账号 {uname} 还没有设置密码：请在监控电脑上运行 "
                          f"python auth.py passwd {uname} 设置")
        return None, "用户名或密码错误"
    with _slock:            # 口令正确：撤销刚才的占位计数，记住这个来源
        _fails.pop(src, None)
        if uname and not local:
            _ufails.pop(uname, None)
        if src:
            good = _good_src.setdefault(uname, set())
            if len(good) < 50:
                good.add(src)
    # 看"实际输入的口令"弱不弱，而不是存储格式：admin888/123456 这类局域网里谁都猜得到。
    # 弱口令只打标给前端提示改密，不再锁写权限/禁止远程改密——服务器部署时首次登录
    # 几乎都是局域网 IP，以前的"远程只读"会让人没法运维。
    must_change = bool(password_problem(uname, password))
    weak_remote = must_change and not local   # 仅作提示文案区分，不再剥夺权限
    token = secrets.token_hex(24)
    with _slock:
        # 顺手清理已过锁定期的陈旧限流项 + 过期会话，防内存无限增长
        for k in [k for k, v in _fails.items() if v[1] and v[1] < now]:
            _fails.pop(k, None)
        for k in [k for k, v in _ufails.items() if v[1] and v[1] < now]:
            _ufails.pop(k, None)
        for t in [t for t, s in _sessions.items() if s["exp"] < now or now - s["last"] > IDLE]:
            _sessions.pop(t, None)
        _sessions[token] = {"user": uname, "role": u.get("role", "viewer"), "exp": now + TTL,
                            "last": now, "must_change": must_change, "weak_remote": weak_remote}
    return token, ""


def login(cfg, username, password, src=None):
    """兼容旧调用：只返回 token 或 None。"""
    return login_ex(cfg, username, password, src)[0]


def change_password(cfg, username, old, new, keep_token=None, path=None):
    """改自己的密码：校验旧密码 → 写哈希进 config.yaml(原地、保留注释) → 内存立即生效 →
    踢掉该用户其它会话。返回 "" 或原因。"""
    import appconfig   # 延迟导入：auth 被很多地方 import，别把配置写回逻辑拖进来
    u = _users(cfg).get(username)
    if not u or not _verify_password(u.get("password", ""), old):
        if keep_token:
            with _slock:
                n = _pwfails[keep_token] = _pwfails.get(keep_token, 0) + 1
                if n >= _MAX_PW_FAILS:
                    _pwfails.pop(keep_token, None)
                    _sessions.pop(keep_token, None)
                    return "旧密码连续错误次数过多，已退出登录"
        return "旧密码不对"
    _pwfails.pop(keep_token, None)
    if old == new:
        return "新密码不能与旧密码相同"
    err = password_problem(username, new)
    if err:
        return err
    h = hash_password(new)
    err = appconfig.set_user_password(username, h, path)
    if err:
        return err
    u["password"] = h
    with _slock:
        for t in [t for t, s in _sessions.items() if s["user"] == username and t != keep_token]:
            _sessions.pop(t, None)
        if keep_token in _sessions:
            _sessions[keep_token]["must_change"] = False
    return ""


def logout(token):
    with _slock:
        _sessions.pop(token, None)


def session(token):
    with _slock:
        s = _sessions.get(token)
        if not s:
            return None
        now = time.time()
        if s["exp"] < now or now - s.get("last", now) > IDLE:
            _sessions.pop(token, None)
            return None
        s["last"] = now
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


def _cli_passwd(username):
    """python auth.py passwd <用户>：交互输入两次新密码，直接写进 config.yaml(保留注释)。"""
    import getpass
    import appconfig
    cfg = appconfig.load_config()
    if username not in _users(cfg):
        print(f"config.yaml 里没有用户 {username}")
        return 1
    while True:
        new = getpass.getpass(f"为 {username} 设置新密码(输入时不显示): ")
        err = password_problem(username, new)
        if err:
            print(err)
            continue
        if getpass.getpass("再输一次: ") != new:
            print("两次不一致，重来")
            continue
        break
    err = appconfig.set_user_password(username, hash_password(new))
    if err:
        print("写入失败:", err)
        return 1
    print(f"已更新 {username} 的密码(旧配置备份为 config.yaml.bak)。重启服务后生效。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "passwd":
        sys.exit(_cli_passwd(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] != "passwd":   # 旧用法：打印哈希，手动粘到 config.yaml
        print(hash_password(sys.argv[1]))
    else:
        print("用法: python auth.py passwd <用户名>   交互设置密码并写入 config.yaml(推荐)\n"
              "      python auth.py <password>         只打印 pbkdf2 哈希")
