# -*- coding: utf-8 -*-
"""ZIP 部署(没有 .git)转成 git 部署：网页更新才能用。用真实 git 在临时目录里模拟。"""
import io
import errno
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
import threading
import zipfile

import pytest

import git_bootstrap
import updater


def _g(*a, cwd):
    # autocrlf 关掉：Windows 机器上全局开着它时，提交/导出会改换行，和 GitHub 打的包对不上
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                           "-c", "core.autocrlf=false", *a], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """远端 main：三个版本。返回 (bare 仓库路径, [c1, c2, c3])。"""
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    _g("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _g("clone", "-q", str(bare), str(work), cwd=tmp_path)
    (work / ".gitignore").write_bytes(b"config.yaml\n")
    (work / "run.bat").write_bytes(b"@echo off\r\n")
    (work / ".gitattributes").write_bytes(b"*.bat text eol=crlf\n")
    commits = []
    for v in (1, 2, 3):
        (work / "server.py").write_bytes(f"VERSION = {v}\n".encode())
        _g("add", "-A", cwd=work)
        _g("commit", "-q", "-m", f"v{v}", cwd=work)
        commits.append(_g("rev-parse", "HEAD", cwd=work))
    _g("push", "-q", "origin", "main", cwd=work)
    return bare, commits


def _unzip_like_github(bare, commit, dest):
    """GitHub 的 Download ZIP 就是 git archive：按提交导出文件，没有 .git。"""
    data = subprocess.run(["git", "-c", "core.autocrlf=false", "archive", "--format=tar", commit],
                          cwd=bare,
                          check=True, capture_output=True).stdout
    dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        # "tar" 过滤器保留可执行位(新版 Python 的默认值会提示弃用)
        t.extractall(dest, **({"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}))
    (dest / "config.yaml").write_text("site: x\n")   # 现场自己的配置(已被忽略)


def test_zip_folder_is_adopted_at_its_own_version_and_can_update(origin, tmp_path, monkeypatch):
    bare, (c1, c2, c3) = origin
    site = tmp_path / "site"
    _unzip_like_github(bare, c2, site)
    commit, extra = git_bootstrap.adopt(site, "git", repo_url=str(bare))
    assert commit == c2 and extra == []
    assert not (site / ".git.adopting").exists()
    assert _g("status", "--porcelain", cwd=site) == ""          # 工作区干净，更新器才肯更新
    assert (site / "config.yaml").read_text() == "site: x\n"     # 现场配置原样保留
    monkeypatch.setattr(updater, "BASE", str(site))
    monkeypatch.setattr(updater, "STATE_FILE", str(tmp_path / "st.json"))
    st = updater.check()
    assert st["branch"] == "main" and st["behind"] == 1
    monkeypatch.setattr(updater, "_selfcheck", lambda: "")
    assert updater.apply(restart=False)["ok"]
    assert (site / "server.py").read_bytes() == b"VERSION = 3\n"


def test_edited_zip_folder_is_left_untouched(origin, tmp_path):
    bare, (c1, c2, c3) = origin
    site = tmp_path / "site"
    _unzip_like_github(bare, c2, site)
    (site / "server.py").write_bytes(b"VERSION = 'local hack'\n")
    assert git_bootstrap.adopt(site, "git", repo_url=str(bare)) == (None, [])
    assert not (site / ".git").exists() and not (site / ".git.adopting").exists()


def test_extra_files_are_reported(origin, tmp_path):
    bare, (c1, c2, c3) = origin
    site = tmp_path / "site"
    _unzip_like_github(bare, c3, site)
    (site / "notes.txt").write_text("x")
    commit, extra = git_bootstrap.adopt(site, "git", repo_url=str(bare))
    assert commit == c3 and extra == ["notes.txt"]


def test_unreachable_repo_leaves_folder_untouched(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "server.py").write_text("x\n")
    with pytest.raises(RuntimeError):
        git_bootstrap.adopt(site, "git", repo_url=str(tmp_path / "missing.git"))
    assert not (site / ".git").exists() and not (site / ".git.adopting").exists()


def test_bootstrap_never_blocks_startup(monkeypatch, tmp_path):
    """下载 Git 或拉取仓库失败(断网)：只打提示，照常启动。"""
    monkeypatch.delenv("MINER_NO_GIT_BOOTSTRAP", raising=False)
    monkeypatch.setattr(git_bootstrap, "find_git", lambda: shutil.which("git"))

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(git_bootstrap, "adopt", boom)
    assert git_bootstrap.bootstrap(tmp_path) == ""


def test_portable_git_download_must_match_pinned_hash(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setattr(git_bootstrap.urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(b"tampered"))
    with pytest.raises(RuntimeError, match="sha256"):
        git_bootstrap.install_mingit()
    assert not git_bootstrap._mingit_dir().exists()


def test_bootstrap_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("MINER_NO_GIT_BOOTSTRAP", "1")
    assert git_bootstrap.bootstrap(tmp_path) == ""
    assert not (tmp_path / ".git").exists()


@pytest.fixture
def portable_archive(monkeypatch, tmp_path):
    """A tiny pinned archive; native Windows CI separately runs the actual MinGit."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setattr(git_bootstrap.shutil, "which", lambda name: None)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("cmd/git.exe", b"test Git binary")
        z.writestr("ucrt64/bin/runtime.dll", b"test Git runtime")
        z.writestr("ucrt64/bin/git-remote-https.exe", b"test HTTPS runtime")
    data = archive.getvalue()
    monkeypatch.setitem(git_bootstrap.MINGIT_SHA256, "64-bit", hashlib.sha256(data).hexdigest())
    downloads = []

    def download(url, timeout):
        downloads.append(url)
        return io.BytesIO(data)

    def usable(git):
        git = Path(git)
        return (git.is_file() and git.read_bytes() == b"test Git binary"
                and (git.parent.parent / "ucrt64/bin/runtime.dll").is_file()
                and (git.parent.parent / "ucrt64/bin/git-remote-https.exe").is_file())

    monkeypatch.setattr(git_bootstrap.urllib.request, "urlopen", download)
    monkeypatch.setattr(git_bootstrap, "_usable_mingit", usable)
    monkeypatch.setattr(git_bootstrap.time, "sleep", lambda seconds: None)
    return data, downloads


def _fake_install(dest, data, complete=False):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dest)
    if complete:
        (dest / git_bootstrap._MINGIT_COMPLETE).write_text(
            git_bootstrap._mingit_marker("64-bit"), encoding="ascii")
    return str(dest / "cmd/git.exe")


def test_portable_git_reuses_working_legacy_install(portable_archive):
    data, downloads = portable_archive
    git = _fake_install(git_bootstrap._mingit_dir(), data)
    assert git_bootstrap.install_mingit() == git
    assert git_bootstrap.find_git() == git
    assert downloads == []


def test_portable_git_uses_verified_mirror_when_github_is_unreachable(monkeypatch, portable_archive):
    data, downloads = portable_archive

    def download(url, timeout):
        downloads.append(url)
        if "github.com" in url:
            raise OSError("GitHub blocked")
        return io.BytesIO(data)

    monkeypatch.setattr(git_bootstrap.urllib.request, "urlopen", download)
    assert git_bootstrap.find_git() is None
    git = git_bootstrap.install_mingit()
    assert git_bootstrap.find_git() == git
    assert [url.split('/')[2] for url in downloads] == ["github.com", "sourceforge.net"]


def test_gitee_portable_git_needs_no_github_request(monkeypatch, portable_archive):
    _, downloads = portable_archive
    monkeypatch.setenv("MINER_DOWNLOAD_SOURCE", "gitee")
    git = git_bootstrap.install_mingit()
    assert git_bootstrap.find_git() == git
    assert len(downloads) == 1 and downloads[0].startswith("https://sourceforge.net/")


def test_offline_git_zip_is_verified_without_network(monkeypatch, portable_archive, tmp_path):
    data, downloads = portable_archive
    local = tmp_path / "local-mingit.zip"
    local.write_bytes(data)
    monkeypatch.setenv("MINER_MINGIT_ZIP", str(local))
    git = git_bootstrap.install_mingit()
    assert git_bootstrap.find_git() == git
    assert downloads == []


def test_bad_offline_git_zip_never_installs_or_silently_downloads(monkeypatch, portable_archive, tmp_path):
    _, downloads = portable_archive
    local = tmp_path / "local-mingit.zip"
    local.write_bytes(b"untrusted local bytes")
    monkeypatch.setenv("MINER_MINGIT_ZIP", str(local))
    with pytest.raises(RuntimeError, match="local.*sha256"):
        git_bootstrap.install_mingit()
    assert git_bootstrap.find_git() is None
    assert downloads == []


def test_portable_git_never_renames_or_replaces_existing_cache(monkeypatch, portable_archive):
    data, downloads = portable_archive
    legacy = git_bootstrap._mingit_dir()
    (legacy / "cmd").mkdir(parents=True)
    (legacy / "cmd/git.exe").write_bytes(b"partial old extraction")
    sentinel = legacy / "keep.txt"
    sentinel.write_text("another process may still be using this folder")

    def denied(*a, **kw):
        raise PermissionError(errno.EACCES, "directory publish denied")

    monkeypatch.setattr(git_bootstrap.os, "replace", denied)
    monkeypatch.setattr(git_bootstrap.os, "rename", denied)
    git = git_bootstrap.install_mingit()
    assert Path(git).parent.parent != legacy
    assert sentinel.read_text() == "another process may still be using this folder"
    assert (legacy / "cmd/git.exe").read_bytes() == b"partial old extraction"
    assert git_bootstrap.find_git() == git
    assert git_bootstrap.install_mingit() == git
    assert len(downloads) == 1


def test_discovery_ignores_incomplete_corrupt_and_wrong_arch_caches(portable_archive):
    data, _ = portable_archive
    legacy = git_bootstrap._mingit_dir()
    prefix = legacy.name + "-64-bit-"
    unfinished = legacy.parent / (prefix + "a-unfinished")
    _fake_install(unfinished, data)
    corrupt_marker = legacy.parent / (prefix + "b-bad-marker")
    _fake_install(corrupt_marker, data)
    (corrupt_marker / git_bootstrap._MINGIT_COMPLETE).write_bytes(b"\xff\xfe")
    wrong_hash = legacy.parent / (prefix + "c-wrong-hash")
    _fake_install(wrong_hash, data)
    (wrong_hash / git_bootstrap._MINGIT_COMPLETE).write_text("unverified")
    broken_runtime = legacy.parent / (prefix + "d-no-runtime")
    _fake_install(broken_runtime, data, complete=True)
    (broken_runtime / "ucrt64/bin/runtime.dll").unlink()
    wrong_arch = legacy.parent / (legacy.name + "-arm64-e-other-architecture")
    _fake_install(wrong_arch, data, complete=True)
    assert git_bootstrap.find_git() is None
    good = legacy.parent / (prefix + "z-good")
    git = _fake_install(good, data, complete=True)
    assert git_bootstrap.find_git() == git
    assert unfinished.exists() and corrupt_marker.exists() and broken_runtime.exists()


def test_portable_git_retries_transient_extraction_access_denied(monkeypatch, portable_archive):
    extractall = zipfile.ZipFile.extractall
    attempts = []

    def briefly_locked(self, path, *a, **kw):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError(errno.EACCES, "antivirus still scanning")
        return extractall(self, path, *a, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "extractall", briefly_locked)
    git = git_bootstrap.install_mingit()
    assert git_bootstrap.find_git() == git
    assert len(attempts) == 2


def test_permanent_access_denied_is_bounded_and_leaves_old_cache(monkeypatch, portable_archive):
    data, _ = portable_archive
    legacy = git_bootstrap._mingit_dir()
    old = legacy.parent / (legacy.name + "-64-bit-previous-unfinished")
    _fake_install(old, data)
    attempts = []

    def locked(self, path, *a, **kw):
        attempts.append(Path(path))
        (Path(path) / "partial").write_bytes(b"unfinished")
        raise PermissionError(errno.EACCES, "locked")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", locked)
    with pytest.raises(PermissionError, match="locked"):
        git_bootstrap.install_mingit()
    assert len(attempts) == 3
    assert not attempts[0].exists()
    assert (old / "cmd/git.exe").read_bytes() == b"test Git binary"
    assert git_bootstrap.find_git() is None


def test_extraction_left_by_interrupted_cleanup_is_not_discoverable(monkeypatch, portable_archive):
    extractall = zipfile.ZipFile.extractall
    rmtree = git_bootstrap._rmtree
    created = []

    def interrupted(self, path, *a, **kw):
        created.append(Path(path))
        extractall(self, path, *a, **kw)
        raise RuntimeError("installer interrupted before runtime verification")

    def cleanup(path):
        if Path(path) not in created:
            rmtree(path)

    monkeypatch.setattr(zipfile.ZipFile, "extractall", interrupted)
    monkeypatch.setattr(git_bootstrap, "_rmtree", cleanup)
    with pytest.raises(RuntimeError, match="interrupted"):
        git_bootstrap.install_mingit()
    assert (created[0] / "cmd/git.exe").is_file()
    assert not (created[0] / git_bootstrap._MINGIT_COMPLETE).exists()
    assert git_bootstrap.find_git() is None


def test_concurrent_portable_git_installers_keep_their_own_complete_cache(monkeypatch, portable_archive):
    data, downloads = portable_archive
    ready = threading.Barrier(2)

    def download(url, timeout):
        downloads.append(url)
        ready.wait(timeout=10)
        return io.BytesIO(data)

    monkeypatch.setattr(git_bootstrap.urllib.request, "urlopen", download)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: git_bootstrap.install_mingit(), range(2)))
    assert len(set(results)) == 2
    assert git_bootstrap.find_git() in results
    for git in results:
        assert Path(git).is_file()
        assert (Path(git).parent.parent / git_bootstrap._MINGIT_COMPLETE).is_file()


@pytest.mark.parametrize("result, usable", [
    (subprocess.CompletedProcess([], 0, "git version 2.56.0.windows.1\n", ""), True),
    (subprocess.CompletedProcess([], 1, "git version 2.56.0.windows.1\n", "DLL missing"), False),
    (subprocess.CompletedProcess([], 0, "not a Git executable", ""), False),
    (FileNotFoundError("runtime missing"), False),
    (subprocess.TimeoutExpired("git", 10), False),
])
def test_git_is_validated_by_running_it(monkeypatch, result, usable):
    def run(args, **kw):
        assert args == ["portable/git.exe", "--version"]
        assert kw["timeout"] == 10
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(git_bootstrap.subprocess, "run", run)
    assert git_bootstrap._usable_git("portable/git.exe") is usable


def test_git_version_retries_a_short_windows_sharing_lock(monkeypatch):
    attempts = []
    delays = []

    def run(*a, **kw):
        attempts.append(1)
        if len(attempts) < 3:
            error = OSError("sharing violation")
            error.winerror = 32
            raise error
        return subprocess.CompletedProcess([], 0, "git version 2.56.0\n", "")

    monkeypatch.setattr(git_bootstrap.subprocess, "run", run)
    monkeypatch.setattr(git_bootstrap.time, "sleep", delays.append)
    assert git_bootstrap._usable_git("git.exe")
    assert len(attempts) == 3 and delays == [0.2, 0.4]


@pytest.mark.parametrize("result, usable", [
    (subprocess.CompletedProcess([], 1, "", "error: remote-curl: usage: git remote-curl <remote> [<url>]\n"), True),
    (subprocess.CompletedProcess([], 1, "", "git: 'remote-https' is not a git command\n"), False),
    (subprocess.CompletedProcess([], 0, "", "remote-curl: usage:"), False),
    (subprocess.CompletedProcess([], 3221225781, "", ""), False),  # Windows: a DLL is missing
    (FileNotFoundError("helper missing"), False),
    (subprocess.TimeoutExpired("git remote-https", 10), False),
])
def test_portable_git_starts_its_own_https_runtime_without_network(monkeypatch, result, usable):
    monkeypatch.setattr(git_bootstrap, "_usable_git", lambda git: True)
    monkeypatch.setenv("GIT_EXEC_PATH", "another-installation")
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")

    def run(args, **kw):
        assert args == ["git.exe", "-c", "help.autocorrect=0", "-c", "protocol.allow=never", "remote-https"]
        assert kw["timeout"] == 10
        assert kw["stdin"] == subprocess.DEVNULL
        assert kw["env"]["PATH"] == ""
        assert kw["env"]["LC_ALL"] == "C"
        assert "GIT_EXEC_PATH" not in kw["env"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(git_bootstrap.subprocess, "run", run)
    assert git_bootstrap._usable_mingit("git.exe") is usable


def test_bootstrap_installs_git_for_an_existing_clone(monkeypatch, tmp_path):
    monkeypatch.delenv("MINER_NO_GIT_BOOTSTRAP", raising=False)
    (tmp_path / ".git").mkdir()
    git = str(tmp_path / "portable/cmd/git.exe")
    monkeypatch.setattr(git_bootstrap, "find_git", lambda: None)
    installs = []
    monkeypatch.setattr(git_bootstrap, "install_mingit", lambda: installs.append(git) or git)
    monkeypatch.setattr(git_bootstrap, "adopt", lambda *a: pytest.fail("already a clone"))
    assert git_bootstrap.bootstrap(tmp_path) == str(Path(git).parent)
    assert installs == [git]


def test_bootstrap_keeps_portable_git_when_adoption_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("MINER_NO_GIT_BOOTSTRAP", raising=False)
    git = str(tmp_path / "portable/cmd/git.exe")
    monkeypatch.setattr(git_bootstrap, "find_git", lambda: git)

    def offline(*a, **kw):
        raise RuntimeError("offline")

    monkeypatch.setattr(git_bootstrap, "adopt", offline)
    assert git_bootstrap.bootstrap(tmp_path) == str(Path(git).parent)
    assert "Web updates unavailable (offline)" in capsys.readouterr().out
    assert not (tmp_path / ".git").exists()


def test_adoption_failure_cleans_only_its_own_metadata(origin, tmp_path, monkeypatch):
    bare, (_, c2, _) = origin
    site = tmp_path / "site"
    _unzip_like_github(bare, c2, site)
    other = site / ".git.adopting-other-process"
    other.mkdir()
    sentinel = other / "active"
    sentinel.write_text("do not remove")
    attempts = []

    def denied(source, dest):
        attempts.append(source)
        raise PermissionError(errno.EACCES, "metadata publish denied")

    monkeypatch.setattr(git_bootstrap.os, "rename", denied)
    monkeypatch.setattr(git_bootstrap.time, "sleep", lambda seconds: None)
    with pytest.raises(PermissionError, match="metadata publish denied"):
        git_bootstrap.adopt(site, "git", repo_url=str(bare))
    assert len(attempts) == 3
    assert not (site / ".git").exists()
    assert list(site.glob(".git.adopting-*")) == [other]
    assert sentinel.read_text() == "do not remove"
    assert (site / "server.py").read_bytes() == b"VERSION = 2\n"
    assert (site / "config.yaml").read_text() == "site: x\n"
