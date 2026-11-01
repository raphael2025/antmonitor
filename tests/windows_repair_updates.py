#!/usr/bin/env python3
"""Native PowerShell 5.1 recovery smoke test; no network or production server.

Only the two HTTP helper downloads are replaced with local copies. The actual
PowerShell entry point, Python helper, Git adoption and child run.bat all execute.
Called by tests/windows_git_bootstrap.py before its verified MinGit cache is
removed. That lets this test stay offline and start with no Git on PATH.
"""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import git_bootstrap


def git(base, *args):
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "user.name=Repair smoke",
         "-c", "user.email=repair@example.invalid", *args],
        cwd=str(base), check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout.strip()


WRAPPER = r"""
$ErrorActionPreference = 'Stop'
$initialPath = $env:PATH
$initialUtf8 = $env:PYTHONUTF8
Write-Host ('PowerShell provider location: ' + (Get-Location).Path)
Write-Host ('Native process directory: ' + [Environment]::CurrentDirectory)
$global:antmonitorRepairTestPrompted = $false
function Read-Host {
    param([string]$Prompt)
    $global:antmonitorRepairTestPrompted = $true
    if ($env:REPAIR_EXPECT_EMPTY_INPUT -eq '1') { return $null }
    throw 'Automatic directory detection unexpectedly requested input.'
}
function Invoke-WebRequest {
    param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile, [int]$TimeoutSec)
    if (-not $Uri.StartsWith($env:REPAIR_EXPECTED_RAW)) { throw "Unexpected download: $Uri" }
    $name = [IO.Path]::GetFileName(([Uri]$Uri).AbsolutePath)
    if ($name -notin @('git_bootstrap.py', 'repair_web_updates.py')) { throw 'Unexpected helper' }
    if ($OutFile.StartsWith($env:REPAIR_SITE, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'A recovery helper must not be written into the installation'
    }
    Copy-Item -LiteralPath (Join-Path $env:REPAIR_SOURCE $name) -Destination $OutFile
}
$arguments = @{ Source = $env:REPAIR_SOURCE_NAME }
if ($env:REPAIR_EXPLICIT_DIRECTORY -eq '1') { $arguments.Directory = $env:REPAIR_SITE }
if ($env:REPAIR_NO_START -eq '1') { $arguments.NoStart = $true }
try {
    & (Join-Path $env:REPAIR_SOURCE 'repair-web-updates.ps1') @arguments
} catch {
    if ($env:REPAIR_EXPECT_EMPTY_INPUT -eq '1' -and $global:antmonitorRepairTestPrompted -and
        $_.Exception.Message -like '*-Directory*' -and
        $_.Exception.Message -notlike '*null-valued*') {
        Write-Host 'Empty directory input produced the explicit retry instruction.'
        return
    }
    throw
}
if ($env:REPAIR_EXPECT_EMPTY_INPUT -eq '1') { throw 'Empty input must fail before any repair.' }
if ($env:PATH -ne $initialPath -or $env:PYTHONUTF8 -ne $initialUtf8) {
    throw 'Recovery changed the calling process environment'
}
Write-Host 'PowerShell recovery entry point succeeded.'
"""


def main(portable_git=None, portable_cache=None):
    if os.name != "nt":
        raise RuntimeError("Run this smoke test on Windows.")
    if not portable_git or not portable_cache:
        raise RuntimeError("Run tests/windows_git_bootstrap.py to supply a verified portable Git fixture.")
    temporary = Path(tempfile.mkdtemp(prefix="antmonitor-recovery-smoke-"))
    try:
        remote, work = temporary / "origin.git", temporary / "work"
        git(temporary, "init", "-q", "--bare", "-b", "main", str(remote))
        git(temporary, "clone", "-q", str(remote), str(work))
        (work / ".gitignore").write_bytes(b"config.yaml\nsettings.json\nminers.db\n")
        (work / "server.py").write_bytes(b"VERSION = 1\n")
        (work / "git_bootstrap.py").write_bytes(
            b'raise RuntimeError("The old installed helper must never run during recovery")\n'
        )
        (work / "run.bat").write_bytes(
            b'@echo off\r\ngit --version > "%MINER_REPAIR_TEST_LAUNCH_TRACE%"\r\n'
            b'where git >> "%MINER_REPAIR_TEST_LAUNCH_TRACE%"\r\n'
            b'powershell.exe -NoProfile -Command "($env:PATH -split \';\')[0]" '
            b'>> "%MINER_REPAIR_TEST_LAUNCH_TRACE%"\r\n'
            b'echo done>> "%MINER_REPAIR_TEST_LAUNCH_TRACE%"\r\n'
        )
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "Original ZIP installation")
        old = git(work, "rev-parse", "HEAD")
        (work / "server.py").write_bytes(b"VERSION = 2\n")
        git(work, "commit", "-qam", "Available newer version")
        git(work, "push", "-q", "origin", "main")
        site = temporary / "\u573a\u5730 [\u4e00] user's monitor"
        site.mkdir()
        archive = subprocess.run(["git", "-c", "core.autocrlf=false", "archive", "--format=zip", old], cwd=str(work),
                                 check=True, capture_output=True).stdout
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            zipped.extractall(site)
        for name, data in (("config.yaml", b"site: customer configuration\n"),
                           ("settings.json", b'{"max_pps":200}\n'),
                           ("miners.db", b"SQLite format 3\x00\xff\x01keep database unchanged")):
            (site / name).write_bytes(data)
        original = {p.name: p.read_bytes() for p in site.iterdir() if p.is_file()}
        launch_trace = temporary / "launcher-result.txt"
        wrapper = temporary / "invoke-recovery.ps1"
        wrapper.write_text(WRAPPER, encoding="ascii")
        windows = Path(os.environ["SystemRoot"])
        system_path = os.pathsep.join(str(p) for p in (
            windows / "System32", windows, windows / "System32/WindowsPowerShell/v1.0",
            windows / "System32/Wbem",
        ))
        env = dict(os.environ, PYTHONUTF8="1", PYTHONSAFEPATH="1", PYTHONPATH=str(site),
                   PATH=system_path, LOCALAPPDATA=str(portable_cache),
                   MINER_PYTHON_DIR=str(Path(sys.executable).parent),
                   MINER_REPO_URL=str(remote), MINER_REPAIR_TEST_LAUNCH_TRACE=str(launch_trace),
                   REPAIR_SOURCE=str(BASE), REPAIR_SITE=str(site), REPAIR_SOURCE_NAME="github",
                   REPAIR_EXPECTED_RAW="https://raw.githubusercontent.com/raphael2025/antmonitor/main/",
                   REPAIR_EXPLICIT_DIRECTORY="0", REPAIR_NO_START="0", REPAIR_EXPECT_EMPTY_INPUT="0")
        for name in ("ANTMONITOR_HOME", "ANTMONITOR_BRANCH", "ANTMONITOR_GH_REPO",
                     "ANTMONITOR_GITEE_REPO", "GH_PROXY"):
            env.pop(name, None)
        assert all(Path(part) != Path(portable_git).parent for part in system_path.split(os.pathsep))
        command = [str(windows / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                   "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(wrapper)]
        subprocess.run(command, cwd=str(site), env=env, check=True, timeout=120)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if launch_trace.is_file():
                captured = launch_trace.read_bytes().splitlines()
                if captured and captured[-1] == b"done":
                    break
            time.sleep(0.1)
        lines = launch_trace.read_text(encoding="utf-8", errors="replace").splitlines()
        assert lines[0].startswith("git version "), lines
        # Path.resolve() in the recovery helper expands Windows 8.3 names such as
        # RUNNER~1. Check actual file identity, not two spellings of the same path.
        assert Path(lines[1]).samefile(portable_git), lines
        assert lines[-1] == "done", lines
        assert Path(lines[-2]).samefile(Path(portable_git).parent), lines
        assert git(site, "rev-parse", "HEAD") == old
        assert git(site, "status", "--porcelain") == ""
        assert Path(git(site, "remote", "get-url", "origin")) == remote
        assert {name: (site / name).read_bytes() for name in original} == original
        assert not (site / "repair_web_updates.py").exists()
        print("Old ZIP recovered; original files/data preserved; launched child can execute Git.")

        # Selecting Gitee only chooses the helper source for an existing checkout.
        # Neither origin nor the installed version may change, and -NoStart must work.
        launch_trace.unlink()
        env.update(REPAIR_SOURCE_NAME="gitee", REPAIR_EXPLICIT_DIRECTORY="1", REPAIR_NO_START="1",
                   REPAIR_EXPECTED_RAW="https://gitee.com/raphael01/antmonitor/raw/main/",
                   MINER_REPO_URL="https://example.invalid/must-not-replace-origin.git")
        subprocess.run(command, cwd=str(temporary), env=env, check=True, timeout=60)
        assert not launch_trace.exists()
        assert git(site, "rev-parse", "HEAD") == old
        assert Path(git(site, "remote", "get-url", "origin")) == remote
        assert {name: (site / name).read_bytes() for name in original} == original
        print("Gitee helper source preserved existing origin; -NoStart did not launch a process.")

        # No selected installation and EOF/cancel at the prompt must fail clearly,
        # without a null-method error or launching/downloading anything.
        empty_env = dict(env, REPAIR_EXPLICIT_DIRECTORY="0", REPAIR_EXPECT_EMPTY_INPUT="1")
        subprocess.run(command, cwd=str(temporary), env=empty_env, check=True, timeout=30)
        assert not launch_trace.exists()

        (site / "server.py").write_bytes(b"local source edits\n")
        env["REPAIR_NO_START"] = "0"
        failed = subprocess.run(command, cwd=str(temporary), env=env, timeout=60)
        assert failed.returncode != 0
        assert not launch_trace.exists()
        assert (site / "server.py").read_bytes() == b"local source edits\n"
        for name in ("config.yaml", "settings.json", "miners.db"):
            assert (site / name).read_bytes() == original[name]
        print("Local edits reported honestly; no files overwritten and no extra monitor started.")
    finally:
        git_bootstrap._rmtree(temporary)


if __name__ == "__main__":
    main()
