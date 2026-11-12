"""Native Windows check: a GitHub-style ZIP folder becomes a clean git deployment
using the downloaded portable MinGit (pinned SHA-256), with Windows file modes/CRLF."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from unittest.mock import patch


def main():
    if os.name != "nt":
        raise SystemExit("This check requires Windows.")
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    import git_bootstrap

    def sys_git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    head = sys_git("rev-parse", "HEAD", cwd=root)
    with tempfile.TemporaryDirectory(prefix="AntMonitor git bootstrap ") as folder:
        folder = Path(folder)
        # Remote with `main` at the commit under test (CI checks out a detached merge commit).
        origin = folder / "origin.git"
        sys_git("clone", "-q", "--bare", str(root), str(origin), cwd=folder)
        sys_git("--git-dir", str(origin), "branch", "-f", "main", head, cwd=folder)
        # GitHub's "Download ZIP" is `git archive --format=zip` on a server without
        # core.autocrlf; the runner's Git may have it on, which would CRLF every file.
        data = subprocess.run(["git", "-c", "core.autocrlf=false", "archive", "--format=zip",
                               head], cwd=root,
                              check=True, capture_output=True).stdout
        site = folder / "site"
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(site)
        (site / "config.yaml").write_text("schedule:\n  enabled: false\n", encoding="utf-8")

        # The pinned hashes were computed outside CI: re-download every architecture here
        # (GitHub's network, independent of where the hashes came from) and compare.
        import hashlib
        import urllib.request
        native_archive = None
        for arch, expected in git_bootstrap.MINGIT_SHA256.items():
            url = git_bootstrap._MINGIT_URL.format(v=git_bootstrap.MINGIT_VERSION, arch=arch)
            with urllib.request.urlopen(url, timeout=300) as r:
                archive = r.read()
            digest = hashlib.sha256(archive).hexdigest()
            assert digest == expected, (arch, digest)
            if arch == git_bootstrap._mingit_arch():
                native_archive = archive
            print("PASS: MinGit", arch, "sha256 matches")

        os.environ["LOCALAPPDATA"] = str(folder / "LocalAppData")
        assert native_archive is not None, "native Windows architecture not supported"
        legacy = git_bootstrap._mingit_dir()
        with zipfile.ZipFile(io.BytesIO(native_archive)) as z:
            # Use the verified archive's actual layout, which differs by architecture.
            # In 2.56 these helpers live in prefix/bin, not --exec-path's libexec/git-core.
            https_helpers = [name for name in z.namelist() if name.endswith("/git-remote-https.exe")]
            assert len(https_helpers) == 1, https_helpers
            z.extractall(legacy)
        old_git = legacy / "cmd/git.exe"
        # A partial old installation can still run `git --version`. The missing
        # HTTPS helper must make us install a complete copy, without destroying it.
        assert git_bootstrap._usable_mingit(old_git), "complete official archive must pass the runtime probe"
        (legacy / https_helpers[0]).unlink()
        assert git_bootstrap._usable_git(old_git)
        assert not git_bootstrap._usable_mingit(old_git)
        sentinel = legacy / "keep.txt"
        sentinel.write_text("existing cache must remain untouched", encoding="ascii")

        # Hold a real Windows handle without FILE_SHARE_DELETE. This models an older
        # monitor/AV lock and makes replacement of the fixed cache fail with WinError 5
        # (or the corresponding sharing violation), as reported on users' machines.
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = create_file(str(old_git), 0x80000000, 1, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            publish_probe = folder / "unpacked"
            publish_probe.mkdir()
            try:
                os.replace(str(publish_probe), str(legacy))
            except OSError as e:
                assert e.winerror in (5, 32, 33, 145), e
                print("PASS: old directory publication reproduces Windows denial:", e.winerror)
            else:
                raise AssertionError("test fixture did not prevent old directory publication")

            # Reuse only the already downloaded, independently SHA-verified bytes; the
            # installer still verifies its own archive and must not call os.replace.
            with patch.object(urllib.request, "urlopen",
                              side_effect=lambda *a, **k: io.BytesIO(native_archive)), \
                    patch.object(os, "replace", side_effect=PermissionError("publish denied")):
                git = git_bootstrap.install_mingit()
            assert Path(git).parent.parent != legacy
            assert sentinel.read_text(encoding="ascii") == "existing cache must remain untouched"
            assert old_git.is_file()
        finally:
            kernel32.CloseHandle(handle)
        print("PASS: verified portable Git installed while the old cache was locked:", git)

        # A fresh launcher must rediscover the new permanent cache without system Git
        # on PATH, including all runtime files required to execute MinGit itself.
        found = subprocess.run([sys.executable, "-c", "import git_bootstrap; print(git_bootstrap.find_git())"],
                               cwd=root, env=dict(os.environ, PATH=""), check=True,
                               capture_output=True, text=True).stdout.strip()
        assert found == git, (found, git)
        with patch.object(urllib.request, "urlopen", side_effect=AssertionError("unnecessary download")):
            assert git_bootstrap.install_mingit() == git
        print("PASS: a fresh process rediscovers the complete cache and installation reuses it")

        commit, extra = git_bootstrap.adopt(site, git, repo_url=str(origin))
        assert commit == head, (commit, head)
        assert extra == [], extra
        status = subprocess.run([git, "status", "--porcelain"], cwd=site, check=True,
                                capture_output=True, text=True).stdout
        assert status == "", status
        print("PASS: ZIP folder adopted at", commit[:10], "with a clean work tree")

        # Exercise the one-off PowerShell 5.1 repair with this verified portable Git
        # while its cache still exists, without downloading the archives a second time.
        import windows_repair_updates
        windows_repair_updates.main(portable_git=git,
                                    portable_cache=os.environ["LOCALAPPDATA"])
        import windows_install_sources
        windows_install_sources.main(native_archive)


if __name__ == "__main__":
    main()
