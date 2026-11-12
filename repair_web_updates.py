#!/usr/bin/env python3
"""Repair Git availability without replacing an existing AntMonitor installation.

The PowerShell entry point downloads this file and git_bootstrap.py to a temporary
directory outside the installation. Keeping the old source files intact lets
git_bootstrap match an older ZIP to its own published commit.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

# Load the downloaded sibling explicitly. The installed folder may contain the
# old broken module, and Python's safe-path mode deliberately omits script dirs.
_bootstrap_spec = importlib.util.spec_from_file_location(
    "_antmonitor_repair_git_bootstrap", Path(__file__).resolve().with_name("git_bootstrap.py")
)
git_bootstrap = importlib.util.module_from_spec(_bootstrap_spec)
_bootstrap_spec.loader.exec_module(git_bootstrap)


def _git(git, base, *args):
    result = subprocess.run(
        [str(git), "-c", "i18n.logOutputEncoding=utf-8", *args],
        cwd=str(base), stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30,
        env=dict(os.environ, **git_bootstrap._GIT_ENV),
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:500])
    return result.stdout.strip()


def repair(base, repo_url, branch="main"):
    """Prepare Git and repository metadata; never update or overwrite source/data."""
    base = Path(base).resolve()
    if not all((base / name).is_file() for name in ("server.py", "run.bat")):
        raise RuntimeError("Select the existing AntMonitor folder containing server.py and run.bat.")

    git = git_bootstrap.find_git() or git_bootstrap.install_mingit()
    version = _git(git, base, "--version")
    if not version.startswith("git version "):
        raise RuntimeError("The selected Git executable did not return a valid version.")

    adopted = False
    # A .git file is valid for linked worktrees. Preserve it and its origin too.
    if not (base / ".git").exists():
        commit, _extra = git_bootstrap.adopt(base, git, repo_url=repo_url, branch=branch)
        if not commit and not (base / ".git").exists():
            raise RuntimeError(
                "Git is ready, but this folder does not match a published version. "
                "No source or data files were replaced. Keep this folder and its data; "
                "check local source edits or the selected repository before retrying."
            )
        adopted = bool(commit)

    # Whole-folder copy keeps .git but often leaves core.worktree pointing at the old PC.
    git_dir = base / ".git"
    if git_dir.is_dir():
        probe = subprocess.run(
            [str(git), "--git-dir", str(git_dir), "--work-tree", str(base),
             "config", "--local", "--get", "core.worktree"],
            cwd=str(base), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
            env=dict(os.environ, **git_bootstrap._GIT_ENV),
        )
        worktree = (probe.stdout or "").strip() if probe.returncode == 0 else ""
        if worktree:
            candidate = Path(worktree) if os.path.isabs(worktree) else (base / worktree)
            if not candidate.is_dir():
                subprocess.run(
                    [str(git), "--git-dir", str(git_dir), "--work-tree", str(base),
                     "config", "--local", "--unset-all", "core.worktree"],
                    cwd=str(base), stdin=subprocess.DEVNULL, capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=30,
                    env=dict(os.environ, **git_bootstrap._GIT_ENV),
                )

    top = Path(_git(git, base, "rev-parse", "--show-toplevel")).resolve()
    if os.path.normcase(str(top)) != os.path.normcase(str(base)):
        raise RuntimeError("Git points at a different working tree; the selected folder was not repaired.")
    current_branch = _git(git, base, "symbolic-ref", "--short", "HEAD")
    _git(git, base, "remote", "get-url", "origin")
    dirty = _git(git, base, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise RuntimeError(
            "Git is ready, but local changes or extra files still block web updates. "
            "No files were removed. Review these files before updating:\n" + dirty[:1000]
        )
    return {
        "git": str(Path(git).resolve()),
        "directory": str(base),
        "branch": current_branch,
        "head": _git(git, base, "rev-parse", "HEAD"),
        "adopted": adopted,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    try:
        result = repair(args.directory, args.repo_url, args.branch)
        Path(args.result).write_text(json.dumps(result, ensure_ascii=True), encoding="utf-8")
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("Web update repair failed: {}".format(error), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
