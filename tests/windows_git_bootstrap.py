"""Native Windows check: a GitHub-style ZIP folder becomes a clean git deployment
using the downloaded portable MinGit (pinned SHA-256), with Windows file modes/CRLF."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


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
        for arch, expected in git_bootstrap.MINGIT_SHA256.items():
            url = git_bootstrap._MINGIT_URL.format(v=git_bootstrap.MINGIT_VERSION, arch=arch)
            with urllib.request.urlopen(url, timeout=300) as r:
                digest = hashlib.sha256(r.read()).hexdigest()
            assert digest == expected, (arch, digest)
            print("PASS: MinGit", arch, "sha256 matches")

        os.environ["LOCALAPPDATA"] = str(folder / "LocalAppData")
        git = git_bootstrap.install_mingit()
        print("PASS: portable Git downloaded and verified:", git)
        commit, extra = git_bootstrap.adopt(site, git, repo_url=str(origin))
        assert commit == head, (commit, head)
        assert extra == [], extra
        status = subprocess.run([git, "status", "--porcelain"], cwd=site, check=True,
                                capture_output=True, text=True).stdout
        assert status == "", status
        print("PASS: ZIP folder adopted at", commit[:10], "with a clean work tree")


if __name__ == "__main__":
    main()
