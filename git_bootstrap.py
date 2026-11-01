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
import errno
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
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
_MINGIT_COMPLETE = ".antmonitor-complete"


def _say(msg):
    print(msg, flush=True)


def _rmtree(path):
    """shutil.rmtree that also removes read-only files: git object files are read-only on
    Windows, and a plain rmtree would leave a half-made .git.adopting behind."""
    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    if os.path.lexists(str(path)):
        if sys.version_info >= (3, 12):
            shutil.rmtree(str(path), onexc=retry)
        else:   # Python 3.8-3.11
            shutil.rmtree(str(path), onerror=retry)


def _mingit_dir():
    root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(root) / "AntMonitor" / ("MinGit-" + MINGIT_VERSION)


def _mingit_arch():
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    return {"AMD64": "64-bit", "X86": "32-bit", "ARM64": "arm64"}.get(arch)


def _retry_access_denied(operation):
    """Allow a short antivirus/sharing lock to clear, without delaying startup forever."""
    for attempt in range(3):
        try:
            return operation()
        except OSError as e:
            if (e.errno not in (errno.EACCES, errno.EPERM)
                    and getattr(e, "winerror", None) not in (5, 32, 33)) or attempt == 2:
                raise
            time.sleep(0.2 * (attempt + 1))


def _usable_git(git):
    """An incomplete extraction can contain git.exe but still be missing its DLLs."""
    def version():
        return subprocess.run([str(git), "--version"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                              timeout=10, env=dict(os.environ, **_GIT_ENV))
    try:
        result = _retry_access_denied(version)
        return result.returncode == 0 and result.stdout.strip().startswith("git version ")
    except (OSError, subprocess.SubprocessError):
        return False


def _usable_mingit(git):
    if not _usable_git(git):
        return False
    try:
        # `--version` can work in an old partial installation even when HTTPS fetch
        # cannot. Ask Git for its own runtime path (also supports 32-bit and ARM64).
        result = _retry_access_denied(lambda: subprocess.run(
            [str(git), "--exec-path"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
            timeout=10, env=dict(os.environ, **_GIT_ENV)))
        return (result.returncode == 0 and bool(result.stdout.strip())
                and (Path(result.stdout.strip()) / "git-remote-https.exe").is_file())
    except (OSError, subprocess.SubprocessError):
        return False


def _mingit_marker(arch):
    return "MinGit {} {}\nsha256 {}\n".format(MINGIT_VERSION, arch, MINGIT_SHA256[arch])


def _find_mingit():
    # Keep using healthy installations created by the old launcher. Never repair one
    # in place: an older monitor (or antivirus) can still have its files open.
    legacy = _mingit_dir()
    git = legacy / "cmd" / "git.exe"
    if git.is_file() and _usable_mingit(git):
        return str(git)
    arch = _mingit_arch()
    if not arch:
        return None
    try:
        candidates = sorted(legacy.parent.glob(legacy.name + "-" + arch + "-*"))
    except OSError:
        return None
    for candidate in candidates:
        try:
            # Only fully extracted and verified installs are discoverable after a
            # restart. A killed/concurrent installer may leave an unfinished folder.
            if (candidate / _MINGIT_COMPLETE).read_text(encoding="ascii") != _mingit_marker(arch):
                continue
            git = candidate / "cmd" / "git.exe"
            if git.is_file() and _usable_mingit(git):
                return str(git)
        except (OSError, UnicodeError):
            continue
    return None


def _path_dir_for(git):
    """Directory the server must have first on PATH to run this git: "" when PATH already
    resolves `git` to it. The Windows Store alias under WindowsApps does not count as git."""
    if not git:
        return ""
    on_path = shutil.which("git")
    if on_path and "\\windowsapps\\" not in on_path.lower() \
            and os.path.normcase(os.path.abspath(on_path)) == os.path.normcase(os.path.abspath(git)):
        return ""
    return str(Path(git).parent)


def find_git():
    """git on PATH (ignoring the Store alias), else a portable MinGit installed earlier."""
    found = shutil.which("git")
    if found and "\\windowsapps\\" not in found.lower() and _usable_git(found):
        return found
    return _find_mingit()


def install_mingit():
    """Reuse or install verified MinGit in an owned per-user cache; returns git.exe.

    Extract directly into a unique permanent folder. Publishing by renaming a directory
    from TEMP fails with WinError 5 on some Windows machines, and replacing an existing
    install can fail while Git or antivirus holds a file open. A completion marker makes
    discovery safe without either operation, including concurrent launcher starts.
    """
    arch = _mingit_arch()
    if not arch:
        raise RuntimeError("unsupported Windows architecture for portable Git")
    existing = _find_mingit()
    if existing:
        return existing
    legacy = _mingit_dir()
    tmp = Path(tempfile.mkdtemp(prefix="antmonitor-mingit-"))
    dest = None
    complete = False
    try:
        archive = tmp / "mingit.zip"
        url = _MINGIT_URL.format(v=MINGIT_VERSION, arch=arch)
        _say("Git not found. Downloading portable Git {} for web updates...".format(MINGIT_VERSION))
        with urllib.request.urlopen(url, timeout=120) as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != MINGIT_SHA256[arch]:
            raise RuntimeError("portable Git download failed verification (sha256 mismatch)")
        _retry_access_denied(lambda: legacy.parent.mkdir(parents=True, exist_ok=True))
        dest = Path(_retry_access_denied(lambda: tempfile.mkdtemp(
            prefix=legacy.name + "-" + arch + "-", dir=str(legacy.parent))))
        with zipfile.ZipFile(archive) as z:
            _retry_access_denied(lambda: z.extractall(dest))
        git = dest / "cmd" / "git.exe"
        if not git.is_file() or not _usable_mingit(git):
            raise RuntimeError("portable Git could not run or is missing its HTTPS runtime")
        _retry_access_denied(lambda: (dest / _MINGIT_COMPLETE).write_text(
            _mingit_marker(arch), encoding="ascii"))
        complete = True
        return str(git)
    finally:
        _rmtree(tmp)
        # Only remove this invocation's incomplete folder. Pre-existing installs and
        # other launchers' folders are never removed, even when they appear broken.
        if dest is not None and not complete:
            _rmtree(dest)


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
    # Concurrent launcher starts must never remove each other's unfinished metadata.
    git_dir = Path(tempfile.mkdtemp(prefix=".git.adopting-", dir=str(base)))
    g = _runner(git, git_dir, base)
    done = False
    try:
        g("init", "-q")
        # The temporary git dir lives inside the work tree; older ZIPs' .gitignore does not
        # list it, so exclude it here or `add -A` would hash git's own object files.
        (git_dir / "info").mkdir(exist_ok=True)
        with open(git_dir / "info" / "exclude", "a", encoding="utf-8") as f:
            f.write("\n/.git.adopting*/\n")
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
        if (base / ".git").exists():
            return None, []  # another launcher finished first; keep its metadata
        _retry_access_denied(lambda: os.rename(str(git_dir), str(base / ".git")))
        done = True
    finally:
        if not done:
            _rmtree(git_dir)
    return match, extra


def bootstrap(base):
    """Called by the Windows launcher on every start. Returns the directory that must be
    on PATH for the server to find git ("" when nothing is needed), never raises."""
    if os.environ.get("MINER_NO_GIT_BOOTSTRAP"):
        return ""
    base = Path(base)
    git = None
    try:
        git = find_git()
        if git is None:
            git = install_mingit()
        if (base / ".git").exists():
            return _path_dir_for(git)
        commit, extra = adopt(base, git)
        if commit:
            _say("Web updates enabled: this folder now tracks {} at {}.".format(BRANCH, commit[:10]))
            if extra:
                _say("Note: extra files block web updates until removed or moved: "
                     + ", ".join(extra[:10]))
        elif not (base / ".git").exists():
            _say("Web updates unavailable: the files do not match any published version "
                 "(edited, or not downloaded from GitHub). Use `git clone` to enable updates.")
        return _path_dir_for(git)
    except Exception as e:  # noqa: BLE001 - never block the monitor from starting
        _say("Web updates unavailable ({}). The monitor still starts normally.".format(e))
        # Git is still useful when an offline/edited ZIP cannot be adopted yet. The
        # server must inherit its PATH so a later repair/update can use it.
        return _path_dir_for(git)
