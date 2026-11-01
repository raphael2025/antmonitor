#!/usr/bin/env python3
"""Native PS5.1 install routing with the real SHA-verified portable Git binary.

HTTP is stubbed at the download boundary; git.exe, cache discovery, clone/copy,
SHA validation, source integrity and child batch launching are real. No Git or
Python is present on the subprocess PATH. Public Git URLs are redirected to
local bare fixtures by Git's normal insteadOf configuration.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import time

BASE = Path(__file__).resolve().parents[1]
GITHUB = "https://github.com/raphael2025/antmonitor.git"
GITEE = "https://gitee.com/raphael01/antmonitor.git"

WRAPPER = r'''
$ErrorActionPreference = 'Stop'
$initialPath = $env:PATH
$initialSource = $env:MINER_DOWNLOAD_SOURCE
function Invoke-WebRequest {
    param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile, [int]$TimeoutSec)
    Add-Content -LiteralPath $env:INSTALL_HTTP_LOG -Value $Uri
    if ($env:INSTALL_OFFLINE -eq '1') { throw 'All HTTP blocked for local-source test.' }
    if (-not $Uri.StartsWith('https://sourceforge.net/projects/git-for-windows.mirror/files/')) {
        throw "GitHub blocked; unexpected download: $Uri"
    }
    Copy-Item -LiteralPath $env:INSTALL_GIT_ZIP -Destination $OutFile
}
$parameters = @{ NoStart = ($env:INSTALL_START -ne '1') }
if ($env:INSTALL_SOURCE_NAME) { $parameters.Source = $env:INSTALL_SOURCE_NAME }
if ($env:INSTALL_SOURCE_PATH) { $parameters.SourcePath = $env:INSTALL_SOURCE_PATH }
& (Join-Path $env:INSTALL_SCRIPT_ROOT 'install.ps1') @parameters
if ($env:PATH -ne $initialPath -or $env:MINER_DOWNLOAD_SOURCE -ne $initialSource) {
    throw 'Installer changed the calling process environment.'
}
'''


def main(native_archive):
    if os.name != "nt":
        raise RuntimeError("Native Windows is required.")
    import git_bootstrap

    def git(directory, *args):
        return subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=Install smoke",
             "-c", "user.email=install@example.invalid", *args], cwd=str(directory),
            check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()

    temporary = Path(tempfile.mkdtemp(prefix="antmonitor-install-native-"))
    try:
        work = temporary / "source fixture"
        work.mkdir()
        for name in ("server.py", "ensure_python.ps1", "windows_launcher.py", "requirements.txt"):
            (work / name).write_bytes(b"# installer fixture\n")
        (work / ".gitignore").write_bytes(b"config.yaml\nsettings.json\nminer_monitor.db\noffline/\n")
        (work / "run.bat").write_bytes(
            b'@echo off\r\ngit --version > "%INSTALL_LAUNCH_LOG%"\r\n'
            b'where git >> "%INSTALL_LAUNCH_LOG%"\r\necho done>> "%INSTALL_LAUNCH_LOG%"\r\n'
        )
        git(work, "init", "-q", "-b", "main")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "installer fixture")
        remote = temporary / "origin.git"
        git(temporary, "clone", "-q", "--bare", str(work), str(remote))
        head = git(work, "rev-parse", "HEAD")
        git_config = temporary / "gitconfig"
        # Gitee is available. GitHub is unavailable, without using external DNS/network.
        git(temporary, "config", "--file", str(git_config), "url." + remote.as_posix() + ".insteadOf", GITEE)
        git(temporary, "config", "--file", str(git_config), "url." + (temporary / "blocked.git").as_posix() + ".insteadOf", GITHUB)
        archive = temporary / "verified-mingit.zip"
        archive.write_bytes(native_archive)
        wrapper = temporary / "invoke-install.ps1"
        wrapper.write_text(WRAPPER, encoding="ascii")
        windows = Path(os.environ["SystemRoot"])
        ps = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
        system_path = os.pathsep.join(str(p) for p in (
            windows / "System32", windows, windows / "System32/WindowsPowerShell/v1.0", windows / "System32/Wbem",
        ))
        entry = temporary / "\u573a\u5730 [\u4e00] user's entry"
        entry.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ANTMONITOR_", "MINER_", "GIT_CONFIG_")) and k not in ("GH_PROXY", "GIT_EXEC_PATH")}
        cache = temporary / "new LocalAppData"
        http_log, launch_log = temporary / "http.txt", temporary / "launch.txt"
        env.update(PATH=system_path, LOCALAPPDATA=str(cache), GIT_CONFIG_GLOBAL=str(git_config),
                   INSTALL_SCRIPT_ROOT=str(BASE), INSTALL_GIT_ZIP=str(archive), INSTALL_HTTP_LOG=str(http_log),
                   INSTALL_LAUNCH_LOG=str(launch_log), INSTALL_SOURCE_NAME="gitee", INSTALL_SOURCE_PATH="",
                   INSTALL_START="0", INSTALL_OFFLINE="0", ANTMONITOR_HOME="installed [site]")
        command = [str(ps), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]

        def run(expect_success=True):
            result = subprocess.run(command, cwd=str(entry), env=env, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=240)
            print(result.stdout)
            if expect_success:
                assert result.returncode == 0, result.stderr
            else:
                assert result.returncode != 0, result.stdout
            return result

        # Cold Gitee installation: no PATH Git/Python, real MinGit installed from the
        # fixed-hash SourceForge response, []/Chinese path, complete clean Git checkout.
        run()
        installed = entry / "installed [site]"
        assert git(installed, "rev-parse", "HEAD") == head
        assert git(installed, "config", "--get", "remote.origin.url") == GITEE
        assert git(installed, "status", "--porcelain") == ""
        urls = http_log.read_text(encoding="utf-8-sig").splitlines()
        assert len(urls) == 1 and urls[0].startswith("https://sourceforge.net/"), urls
        assert not launch_log.exists()
        found = subprocess.run([os.sys.executable, "-c", "import git_bootstrap; print(git_bootstrap.find_git())"],
                               cwd=str(BASE), env=dict(env, PATH=""), check=True,
                               capture_output=True, text=True).stdout.strip()
        assert Path(found).is_file(), found
        print("PASS: Gitee cold install needs neither PATH Git/Python nor GitHub; Python rediscovers its verified cache.")

        # Automatic routing tries GitHub then Gitee, launches with inherited portable
        # Git PATH using literal paths. No second download (complete cache is reused).
        env.update(INSTALL_SOURCE_NAME="auto", ANTMONITOR_HOME="auto [site]", INSTALL_START="1")
        run()
        auto_site = entry / "auto [site]"
        assert git(auto_site, "config", "--get", "remote.origin.url") == GITEE
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if launch_log.exists() and launch_log.read_bytes().splitlines()[-1:] == [b"done"]:
                break
            time.sleep(0.1)
        lines = launch_log.read_text(encoding="utf-8", errors="replace").splitlines()
        assert lines[0].startswith("git version ") and lines[-1] == "done", lines
        assert Path(lines[1]).samefile(found), lines
        assert len(http_log.read_text(encoding="utf-8-sig").splitlines()) == 1
        print("PASS: auto falls back to Gitee and starts the literal-path batch with working Git.")

        # Existing custom origin and customer data must not be touched by rerunning.
        custom = "https://example.invalid/custom/monitor.git"
        git(installed, "remote", "set-url", "origin", custom)
        for name in ("config.yaml", "settings.json", "miner_monitor.db"):
            (installed / name).write_bytes(b"customer data\x00\xff")
        before = {p.name: p.read_bytes() for p in installed.iterdir() if p.is_file()}
        env.update(ANTMONITOR_HOME=str(installed), INSTALL_SOURCE_NAME="github", INSTALL_START="0", INSTALL_OFFLINE="1")
        run()
        assert git(installed, "config", "--get", "remote.origin.url") == custom
        assert {name: (installed / name).read_bytes() for name in before} == before
        print("PASS: rerunning preserves existing source, custom origin, config and database.")

        # Explicit GitHub never uses Gitee; failed ZIP also leaves destination empty.
        env.update(ANTMONITOR_HOME=str(entry / "failed github"))
        run(expect_success=False)
        assert not (entry / "failed github" / "server.py").exists()

        # No sources reachable: downloaded source remains a usable installer entry.
        env.update(ANTMONITOR_HOME=str(entry / "offline site"), INSTALL_SOURCE_NAME="auto", INSTALL_SOURCE_PATH=str(work))
        request_count = len(http_log.read_text(encoding="utf-8-sig").splitlines())
        run()
        assert (entry / "offline site" / "run.bat").is_file()
        assert len(http_log.read_text(encoding="utf-8-sig").splitlines()) == request_count
        print("PASS: local source installs without network requests.")

        # Partial and non-empty unknown installations are never overwritten/promoted.
        partial = entry / "partial"
        partial.mkdir()
        (partial / "server.py").write_bytes(b"unfinished customer file")
        env.update(ANTMONITOR_HOME=str(partial))
        run(expect_success=False)
        assert (partial / "server.py").read_bytes() == b"unfinished customer file"
        assert not (partial / "run.bat").exists()
        (entry / "offline site" / ".antmonitor-installing").write_text("interrupted")
        env.update(ANTMONITOR_HOME=str(entry / "offline site"))
        run(expect_success=False)
        print("PASS: incomplete source/copy is not mistaken for a complete installation.")

        # A tampered local MinGit archive must fail closed before any source writes.
        env.update(LOCALAPPDATA=str(temporary / "bad cache"), INSTALL_SOURCE_PATH="", INSTALL_SOURCE_NAME="gitee",
                   ANTMONITOR_HOME=str(entry / "bad package"), MINER_MINGIT_ZIP=str(temporary / "bad.zip"))
        (temporary / "bad.zip").write_bytes(b"unverified archive")
        failed = run(expect_success=False)
        assert "SHA256" in failed.stdout + failed.stderr
        assert not (entry / "bad package").exists()
        assert not (temporary / "bad cache" / "AntMonitor").exists()
        print("PASS: local MinGit checksum rejection cannot create a partial install.")
    finally:
        git_bootstrap._rmtree(temporary)


if __name__ == "__main__":
    raise SystemExit("Run tests/windows_git_bootstrap.py to supply the verified native MinGit ZIP.")
