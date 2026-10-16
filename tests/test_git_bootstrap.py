# -*- coding: utf-8 -*-
"""ZIP 部署(没有 .git)转成 git 部署：网页更新才能用。用真实 git 在临时目录里模拟。"""
import io
import subprocess
import tarfile

import pytest

import git_bootstrap
import updater


def _g(*a, cwd):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """远端 main：三个版本。返回 (bare 仓库路径, [c1, c2, c3])。"""
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    _g("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _g("clone", "-q", str(bare), str(work), cwd=tmp_path)
    (work / ".gitignore").write_text("config.yaml\n")
    (work / "run.bat").write_bytes(b"@echo off\r\n")
    (work / ".gitattributes").write_text("*.bat text eol=crlf\n")
    commits = []
    for v in (1, 2, 3):
        (work / "server.py").write_text(f"VERSION = {v}\n")
        _g("add", "-A", cwd=work)
        _g("commit", "-q", "-m", f"v{v}", cwd=work)
        commits.append(_g("rev-parse", "HEAD", cwd=work))
    _g("push", "-q", "origin", "main", cwd=work)
    return bare, commits


def _unzip_like_github(bare, commit, dest):
    """GitHub 的 Download ZIP 就是 git archive：按提交导出文件，没有 .git。"""
    data = subprocess.run(["git", "archive", "--format=tar", commit], cwd=bare,
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
    assert (site / "server.py").read_text() == "VERSION = 3\n"


def test_edited_zip_folder_is_left_untouched(origin, tmp_path):
    bare, (c1, c2, c3) = origin
    site = tmp_path / "site"
    _unzip_like_github(bare, c2, site)
    (site / "server.py").write_text("VERSION = 'local hack'\n")
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
    monkeypatch.setattr(git_bootstrap, "find_git", lambda: "git")

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
