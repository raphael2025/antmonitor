"""The recovery helper must preserve older installations and their local data."""
import io
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

import repair_web_updates as recovery


def git(base, *args):
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "user.name=Repair test",
         "-c", "user.email=repair@example.invalid", *args],
        cwd=str(base), check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout.strip()


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery.git_bootstrap, "find_git", lambda: shutil.which("git"))
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", "-q", str(remote), str(work))
    (work / ".gitignore").write_bytes(b"config.yaml\nsettings.json\nminers.db\n")
    (work / "server.py").write_bytes(b"VERSION = 1\n")
    (work / "run.bat").write_bytes(b"@echo off\r\n")
    (work / "git_bootstrap.py").write_bytes(b"# Old source must remain intact.\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "Old published version")
    old = git(work, "rev-parse", "HEAD")
    (work / "server.py").write_bytes(b"VERSION = 2\n")
    git(work, "commit", "-qam", "New version")
    git(work, "push", "-q", "origin", "main")
    site = tmp_path / "old site [1]"
    site.mkdir()
    archive = subprocess.run(["git", "-c", "core.autocrlf=false", "archive", "--format=zip", old], cwd=str(work),
                             check=True, capture_output=True).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        zipped.extractall(site)
    for name, data in (("config.yaml", b"site: local\n"),
                       ("settings.json", b'{"max_pps":200}\n'),
                       ("miners.db", b"SQLite format 3\x00\xff\x01original database")):
        (site / name).write_bytes(data)
    before = {p.name: p.read_bytes() for p in site.iterdir() if p.is_file()}
    return site, remote, old, before


def assert_preserved(site, before):
    assert {name: (site / name).read_bytes() for name in before} == before


def test_repair_old_zip_keeps_its_version_and_all_local_data(installation):
    site, remote, old, before = installation
    result = recovery.repair(site, str(remote))
    assert result["adopted"] is True
    assert result["head"] == old
    assert result["branch"] == "main"
    assert git(site, "status", "--porcelain") == ""
    assert Path(git(site, "remote", "get-url", "origin")) == remote
    assert_preserved(site, before)
    assert not (site / "repair_web_updates.py").exists()


def test_existing_clone_keeps_origin_when_other_source_selected(installation, monkeypatch):
    site, remote, old, before = installation
    recovery.repair(site, str(remote))

    def no_adoption(*args, **kwargs):
        raise AssertionError("an existing Git checkout must not be adopted")

    monkeypatch.setattr(recovery.git_bootstrap, "adopt", no_adoption)
    result = recovery.repair(site, "https://example.invalid/not-the-existing-origin.git")
    assert result["adopted"] is False
    assert result["head"] == old
    assert Path(git(site, "remote", "get-url", "origin")) == remote
    assert_preserved(site, before)


def test_other_launcher_finishing_adoption_is_verified_normally(installation, monkeypatch):
    site, remote, old, before = installation
    adopt = recovery.git_bootstrap.adopt

    def finish_elsewhere(*args, **kwargs):
        adopt(*args, **kwargs)
        return None, []

    monkeypatch.setattr(recovery.git_bootstrap, "adopt", finish_elsewhere)
    result = recovery.repair(site, str(remote))
    assert result["adopted"] is False
    assert result["head"] == old
    assert git(site, "status", "--porcelain") == ""
    assert_preserved(site, before)


def test_repair_of_edited_zip_fails_without_replacing_source(installation):
    site, remote, _old, before = installation
    (site / "server.py").write_bytes(b"local edited server\n")
    before["server.py"] = (site / "server.py").read_bytes()
    with pytest.raises(RuntimeError, match="does not match a published version"):
        recovery.repair(site, str(remote))
    assert not (site / ".git").exists()
    assert_preserved(site, before)


def test_extra_files_are_kept_but_do_not_claim_updates_are_ready(installation):
    site, remote, _old, before = installation
    (site / "operator-notes.txt").write_bytes(b"do not delete\n")
    with pytest.raises(RuntimeError, match="local changes or extra files"):
        recovery.repair(site, str(remote))
    assert (site / "operator-notes.txt").read_bytes() == b"do not delete\n"
    assert_preserved(site, before)


def test_existing_git_checkout_still_installs_missing_git(installation, monkeypatch):
    site, remote, _old, before = installation
    recovery.repair(site, str(remote))
    calls = []
    monkeypatch.setattr(recovery.git_bootstrap, "find_git", lambda: None)

    def install():
        calls.append(True)
        return shutil.which("git")

    monkeypatch.setattr(recovery.git_bootstrap, "install_mingit", install)
    result = recovery.repair(site, str(remote))
    assert result["adopted"] is False and calls == [True]
    assert_preserved(site, before)


def test_gitfile_worktree_is_preserved(installation, tmp_path):
    site, remote, old, _before = installation
    recovery.repair(site, str(remote))
    linked = tmp_path / "linked checkout"
    git(site, "worktree", "add", "-q", "-b", "linked", str(linked), old)
    original_gitfile = (linked / ".git").read_bytes()
    result = recovery.repair(linked, "https://example.invalid/unused.git")
    assert result["adopted"] is False
    assert result["branch"] == "linked"
    assert (linked / ".git").read_bytes() == original_gitfile
    assert Path(git(linked, "remote", "get-url", "origin")) == remote


def test_invalid_directory_is_rejected_before_installing_git(tmp_path, monkeypatch):
    def no_install():
        raise AssertionError("do not install anything for an invalid directory")

    monkeypatch.setattr(recovery.git_bootstrap, "find_git", no_install)
    with pytest.raises(RuntimeError, match="existing AntMonitor folder"):
        recovery.repair(tmp_path, "unused")


def test_cli_failure_has_nonzero_exit_and_no_success_result(installation, tmp_path):
    site, remote, _old, _before = installation
    (site / "server.py").write_bytes(b"locally changed\n")
    result_file = tmp_path / "result.json"
    code = recovery.main(["--directory", str(site), "--repo-url", str(remote),
                          "--result", str(result_file)])
    assert code == 1
    assert not result_file.exists()


def test_stale_core_worktree_from_migrated_folder_is_cleared(installation, tmp_path):
    site, remote, _old, before = installation
    recovery.repair(site, str(remote))
    git(site, "config", "core.worktree", str(tmp_path / "old-pc-missing"))
    result = recovery.repair(site, str(remote))
    assert result["adopted"] is False
    probe = subprocess.run(
        ["git", "config", "--local", "--get", "core.worktree"],
        cwd=str(site), capture_output=True, text=True)
    assert probe.returncode != 0
    assert_preserved(site, before)
