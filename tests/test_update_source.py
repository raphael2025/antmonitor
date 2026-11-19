# -*- coding: utf-8 -*-
"""更新源探测：GitHub/Gitee 谁通用谁；都通时保持当前来源；全不通保留原顺序。"""
import update_source as us
import updater

GH = updater.GITHUB_HTTPS
GE = updater.GITEE_HTTPS


def _prober(latencies):
    return lambda url, timeout: latencies.get(url)


def setup_function(_):
    us.reset_cache()


def test_unreachable_source_goes_last_and_reachable_first():
    got = us.ordered([GH, GE], preferred=GH, prober=_prober({GH: None, GE: 0.5}), cache_ttl=0)
    assert got == [GE, GH]


def test_both_reachable_keeps_preferred_even_if_slower():
    got = us.ordered([GH, GE], preferred=GH, prober=_prober({GH: 2.0, GE: 0.1}), cache_ttl=0)
    assert got == [GH, GE]            # 不因 Gitee 稍快就切(镜像可能稍旧)
    got = us.ordered([GH, GE], preferred=GE, prober=_prober({GH: 0.1, GE: 2.0}), cache_ttl=0)
    assert got == [GE, GH]


def test_preferred_down_falls_back_by_latency():
    a, b, c = "https://a.example/r.git", "https://b.example/r.git", "https://c.example/r.git"
    got = us.ordered([a, b, c], preferred=a, prober=_prober({a: None, b: 3.0, c: 1.0}), cache_ttl=0)
    assert got == [c, b, a]


def test_all_unreachable_keeps_original_order():
    got = us.ordered([GH, GE], preferred=GH, prober=_prober({}), cache_ttl=0)
    assert got == [GH, GE]


def test_results_are_cached_briefly():
    calls = []

    def prober(url, timeout):
        calls.append(url)
        return 0.1
    us.rank([GH, GE], prober=prober, cache_ttl=60)
    us.rank([GH, GE], prober=prober, cache_ttl=60)
    assert len(calls) == 2            # 第二次命中缓存


def test_probe_git_accepts_only_real_git_answer():
    ok = lambda u, t: (200, "application/x-git-upload-pack-advertisement", b"")  # noqa: E731
    assert us.probe_git(GH, fetcher=ok) is not None
    login_page = lambda u, t: (200, "text/html", b"<html>login")  # noqa: E731
    assert us.probe_git(GH, fetcher=login_page) is None             # 被劫持成登录页/拦截页
    denied = lambda u, t: (403, "application/x-git-upload-pack-advertisement", b"")  # noqa: E731
    assert us.probe_git(GH, fetcher=denied) is None

    def boom(u, t):
        raise TimeoutError()
    assert us.probe_git(GH, fetcher=boom) is None


def test_fetch_sources_use_working_mirror_first(monkeypatch):
    monkeypatch.setattr(updater, "ensure_public_update_remote", lambda: GH)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: GH)
    monkeypatch.setattr(updater.update_source, "ordered", lambda urls, preferred=None, **k: [GE, GH])
    assert updater._update_fetch_sources() == [GE, "origin"]     # GitHub(origin) 不通 → Gitee 在前
    monkeypatch.setattr(updater.update_source, "ordered", lambda urls, preferred=None, **k: [GH, GE])
    assert updater._update_fetch_sources() == ["origin", GE]


def test_custom_origin_is_never_rerouted(monkeypatch):
    monkeypatch.setattr(updater, "ensure_public_update_remote", lambda: "")
    monkeypatch.setattr(updater, "_git", lambda *a, **k: "https://git.example.com/me/fork.git")
    assert updater._update_fetch_sources() == ["origin"]


def test_probe_error_falls_back_to_default_order(monkeypatch):
    monkeypatch.setattr(updater, "ensure_public_update_remote", lambda: GH)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: GH)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(updater.update_source, "ordered", boom)
    assert updater._update_fetch_sources() == ["origin", GE]


def test_non_http_sources_keep_their_order():
    assert us.ordered(["/srv/mirror.git", GH], preferred=GH, prober=_prober({})) == ["/srv/mirror.git", GH]
