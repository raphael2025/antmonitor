#!/usr/bin/env python3
"""Turn a ZIP download into a git deployment so the web panel can update it.

Only the standard library is used: this runs from windows_launcher.py before the
project venv exists. Every step is best effort: any failure leaves the folder
exactly as it was and the monitor still starts (it just cannot self-update).

Steps, all inside a temporary git dir that only becomes `.git` on success:
1. find git on PATH, else download a pinned portable MinGit (SHA-256 checked);
2. fetch the public repository;
3. find the commit whose files match the extracted ZIP, so the work tree is
   clean (the updater refuses to update a dirty tree) and the normal
   "fast-forward -> self-check -> restart" path brings it to the latest version.
If no commit matches (files edited, or not a GitHub ZIP), nothing is changed.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

REPO_URL = os.environ.get("MINER_REPO_URL") or "https://github.com/raphael2025/antmonitor.git"
BRANCH = "main"
MINGIT_VERSION = "2.56.0"
_MINGIT_URL = ("https://github.com/git-for-windows/git/releases/download/"
               "v{v}.windows.1/MinGit-{v}-{arch}.zip")
# SHA-256 of the official MinGit archives; a download with any other content is rejected.
MINGIT_SHA256 = {
    "64-bit": "064b440ff870ed5198527e8f3a92cdf5bd2fd0fedf5e718af95e3fdaddeff718",
    "32-bit": "9f8266486c8818b91cbb6b719e35972a406f7560e86b82fbda2f3dcb7c069f12",
    "arm64": "cb3b0f2d486ea52673227151a5baf5bc13861ff80e74e94e46d614d1bfcd5c06",
}
MAX_COMMITS = 300   # how far back a ZIP may be matched
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
            "GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "30"}


def _say(msg):
    print(msg, flush=True)


def _mingit_dir():
    root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(root) / "AntMonitor" / ("MinGit-" + MINGIT_VERSION)


def _mingit_arch():
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    return {"AMD64": "64-bit", "X86": "32-bit", "ARM64": "arm64"}.get(arch)


def find_git():
    """git on PATH (ignoring the Store alias), else a portable MinGit installed earlier."""
    found = shutil.which("git")
    if found and "\\windowsapps\\" not in found.lower():
        return found
    portable = _mingit_dir() / "cmd" / "git.exe"
    return str(portable) if portable.is_file() else None


def install_mingit():
    """Download, verify and unpack MinGit into the per-user folder; returns git.exe."""
    arch = _mingit_arch()
    if not arch:
        raise RuntimeError("unsupported Windows architecture for portable Git")
    dest = _mingit_dir()
    tmp = Path(tempfile.mkdtemp(prefix="antmonitor-mingit-"))
    try:
        archive = tmp / "mingit.zip"
        url = _MINGIT_URL.format(v=MINGIT_VERSION, arch=arch)
        _say("Git not found. Downloading portable Git {} for web updates...".format(MINGIT_VERSION))
        with urllib.request.urlopen(url, timeout=120) as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != MINGIT_SHA256[arch]:
            raise RuntimeError("portable Git download failed verification (sha256 mismatch)")
        unpack = tmp / "unpacked"
        with zipfile.ZipFile(archive) as z:
            z.extractall(unpack)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(str(unpack), str(dest))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    git = dest / "cmd" / "git.exe"
    if not git.is_file():
        raise RuntimeError("portable Git is missing cmd\\git.exe")
    return str(git)


def _runner(git, git_dir, base):
    def g(*args, timeout=120):
        r = subprocess.run([git, "--git-dir", str(git_dir), "--work-tree", str(base),
                            "-c", "safe.directory=*", *args], cwd=str(base),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=dict(os.environ, **_GIT_ENV))
        if r.returncode:
            raise RuntimeError("git {} failed: {}".format(args[0], (r.stderr or r.stdout).strip()[:300]))
        return r.stdout
    return g


def _blobs(listing):
    """{path: blob} from `ls-files -s` (mode blob stage) or `ls-tree -r` (mode type blob).
    Modes are ignored: Windows has no executable bit."""
    out = {}
    for line in listing.splitlines():
        meta, _tab, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3:
            out[path] = parts[2] if parts[1] in ("blob", "tree", "commit") else parts[1]
    return out


def adopt(base, git, repo_url=REPO_URL, branch=BRANCH):
    """Make `base` a clone of repo_url at the newest commit whose files all match the folder.
    Returns (commit, extra_files) or (None, []) and then leaves `base` untouched.
    extra_files are files the commit does not have (they will show as untracked)."""
    base = Path(base)
    if (base / ".git").exists():
        return None, []
    git_dir = base / ".git.adopting"
    shutil.rmtree(git_dir, ignore_errors=True)
    g = _runner(git, git_dir, base)
    done = False
    try:
        g("init", "-q")
        # The temporary git dir lives inside the work tree; older ZIPs' .gitignore does not
        # list it, so exclude it here or `add -A` would hash git's own object files.
        (git_dir / "info").mkdir(exist_ok=True)
        with open(git_dir / "info" / "exclude", "a", encoding="utf-8") as f:
            f.write("\n/.git.adopting/\n")
        g("config", "core.autocrlf", "false")   # keep files byte-identical to the ZIP
        g("remote", "add", "origin", repo_url)
        g("fetch", "-q", "--no-tags", "origin",
          "+refs/heads/{0}:refs/remotes/origin/{0}".format(branch), timeout=300)
        g("add", "-A")                              # hashes the folder, honouring .gitignore
        here = _blobs(g("ls-files", "-s"))
        match, extra = None, []
        for commit in g("rev-list", "--max-count", str(MAX_COMMITS),
                        "origin/" + branch).split():
            files = _blobs(g("ls-tree", "-r", commit))
            if all(here.get(p) == b for p, b in files.items()):
                match, extra = commit, sorted(set(here) - set(files))
                break
        if not match:
            return None, []
        g("update-ref", "refs/heads/" + branch, match)
        g("symbolic-ref", "HEAD", "refs/heads/" + branch)
        g("config", "branch.{}.remote".format(branch), "origin")
        g("config", "branch.{}.merge".format(branch), "refs/heads/" + branch)
        g("read-tree", match)                       # index (incl. file modes) from the commit
        if g("status", "--porcelain", "--untracked-files=no").strip():
            return None, []
        done = True
    finally:
        if not done:
            shutil.rmtree(git_dir, ignore_errors=True)
    os.replace(str(git_dir), str(base / ".git"))
    return match, extra


def bootstrap(base):
    """Called by the Windows launcher on every start. Returns the directory that must be
    on PATH for the server to find git ("" when nothing is needed), never raises."""
    if os.environ.get("MINER_NO_GIT_BOOTSTRAP"):
        return ""
    base = Path(base)
    try:
        git = find_git()
        if (base / ".git").exists():
            return str(Path(git).parent) if git and not shutil.which("git") else ""
        if git is None:
            git = install_mingit()
        commit, extra = adopt(base, git)
        if commit:
            _say("Web updates enabled: this folder now tracks {} at {}.".format(BRANCH, commit[:10]))
            if extra:
                _say("Note: extra files block web updates until removed or moved: "
                     + ", ".join(extra[:10]))
        else:
            _say("Web updates unavailable: the files do not match any published version "
                 "(edited, or not downloaded from GitHub). Use `git clone` to enable updates.")
        return str(Path(git).parent) if not shutil.which("git") else ""
    except Exception as e:  # noqa: BLE001 - never block the monitor from starting
        _say("Web updates unavailable ({}). The monitor still starts normally.".format(e))
        return ""
