#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更新源探测：GitHub / Gitee 谁连得上就用谁。

只用标准库(Windows 压缩包首次启动时项目 venv 还不存在，git_bootstrap 也要用)。

为什么需要：旧逻辑只在"第一个源 fetch 失败"之后才换另一个，而 GitHub 被墙/被劫持时
fetch 会先卡满 120 秒才失败，网页更新看起来像死机。这里先用几秒钟的轻量探测判断
哪个源通，把通的排前面，不通的排最后(仍保留作最后手段)。

排序规则(rank)：
  1. 能连通的排在不能连通的前面；
  2. 都能连通时，保持用户当前部署的来源(preferred)优先——避免 GitHub/Gitee 镜像
     同步有先后时，无谓地切到可能稍旧的那一个；preferred 不通才按延迟从小到大；
  3. 都不通：原顺序，调用方照旧逐个尝试并汇总报错。
探测结果只缓存很短时间(默认 60 秒)，更新检查/应用连着做不会重复探测。
"""
import concurrent.futures
import threading
import time
import urllib.error
import urllib.request

PROBE_TIMEOUT = 6
CACHE_TTL = 60
_cache = {}               # url -> (ts, latency|None)
_cache_lock = threading.Lock()
_UA = {"User-Agent": "antmonitor-update-probe"}


def _get(url, timeout, headers=None):
    """返回 (状态码, content-type, 前 64 字节)；网络层失败抛异常。"""
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", "") or "", r.read(64)


def probe_git(url, timeout=PROBE_TIMEOUT, fetcher=None):
    """该 git 仓库的匿名 HTTPS 能不能真的用：请求 smart-HTTP 的 info/refs。
    成功返回延迟秒数，失败返回 None。只认 200 且确实是 git 的回应(挡劫持/登录页)。"""
    fetcher = fetcher or _get
    target = url.rstrip("/") + "/info/refs?service=git-upload-pack"
    t0 = time.time()
    try:
        status, ctype, head = fetcher(target, timeout)
    except Exception:  # noqa: BLE001 - 超时/断连/DNS/证书/HTTP 错误都算不通
        return None
    if status != 200:
        return None
    if "git-upload-pack" not in ctype and b"git-upload-pack" not in head and b"001e#" not in head:
        return None
    return max(time.time() - t0, 0.001)


def probe_download(url, timeout=PROBE_TIMEOUT, fetcher=None):
    """普通下载地址能不能连(MinGit 之类)：只取 1 字节，跟随重定向。"""
    fetcher = fetcher or (lambda u, t: _get(u, t, {"Range": "bytes=0-0"}))
    t0 = time.time()
    try:
        status, _ctype, _head = fetcher(url, timeout)
    except Exception:  # noqa: BLE001
        return None
    return max(time.time() - t0, 0.001) if status in (200, 206) else None


def _probe_cached(url, prober, timeout, cache_ttl):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < cache_ttl:
            return hit[1]
    latency = prober(url, timeout)
    with _cache_lock:
        _cache[url] = (time.time(), latency)
    return latency


def rank(urls, preferred=None, timeout=PROBE_TIMEOUT, prober=probe_git, cache_ttl=CACHE_TTL):
    """按上述规则排序，返回 [(url, latency|None), ...]。并行探测，总耗时约一个 timeout。"""
    urls = list(dict.fromkeys(urls))
    # 只对 http(s) 地址探测；本地路径/ssh 之类(自建镜像、测试)原样保持顺序，不能误判成"不通"
    if len(urls) <= 1 or not all(u.lower().startswith(("http://", "https://")) for u in urls):
        return [(u, None) for u in urls]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futs = {u: pool.submit(_probe_cached, u, lambda x, t: prober(x, t), timeout, cache_ttl)
                for u in urls}
        lat = {u: f.result() for u, f in futs.items()}
    up = [u for u in urls if lat[u] is not None]
    down = [u for u in urls if lat[u] is None]
    if preferred in up:
        up.sort(key=lambda u: (u != preferred, lat[u]))
    else:
        up.sort(key=lambda u: lat[u])
    return [(u, lat[u]) for u in up + down]


def ordered(urls, preferred=None, **kw):
    return [u for u, _ in rank(urls, preferred=preferred, **kw)]


def reset_cache():
    with _cache_lock:
        _cache.clear()


if __name__ == "__main__":   # 手动排查：python update_source.py <url> [<url> ...]
    import sys
    for u, lat in rank(sys.argv[1:] or [
            "https://github.com/raphael2025/antmonitor.git",
            "https://gitee.com/raphael01/antmonitor.git"], cache_ttl=0):
        print(("%.2fs" % lat) if lat else "不通", u)
