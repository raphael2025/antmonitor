#!/usr/bin/env python3
"""Windows bootstrap and guardian; only the standard library is needed to start.

The guardian lives in memory, so git updates cannot change a running batch file's
read position. Dependencies are checked again before every server restart.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
MIN_PYTHON = (3, 10)
MAX_FAILURES = 5
MIN_UPTIME = 30
RESTART_DELAY = 3
IMPORT_CHECK = "import requests, fastapi, uvicorn, websockets, yaml, httpx, openpyxl"


def run_checked(args, base):
    subprocess.run([str(arg) for arg in args], cwd=str(base), check=True)


def prepare_environment(base):
    """Use a private venv; install on first use, changed requirements or damage."""
    venv = base / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("Creating the project Python environment...", flush=True)
        run_checked([sys.executable, "-m", "venv", venv], base)
    run_checked([python, "-c",
                 "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"], base)
    requirements = base / "requirements.txt"
    stamp = venv / ".antmonitor-requirements.sha256"
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    installed = stamp.read_text(encoding="ascii").strip() if stamp.exists() else ""
    probe = subprocess.run([str(python), "-c", IMPORT_CHECK], cwd=str(base),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if installed != fingerprint or probe.returncode:
        print("Installing/checking dependencies from requirements.txt...", flush=True)
        pip = subprocess.run([str(python), "-m", "pip", "--version"], cwd=str(base),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if pip.returncode:
            run_checked([python, "-m", "ensurepip", "--upgrade"], base)
        install = [python, "-m", "pip", "install", "--disable-pip-version-check",
                   "-r", requirements]
        if installed == fingerprint and probe.returncode:
            # Metadata can survive missing/damaged package files; a plain pip
            # install would report "already satisfied" and leave them broken.
            install.append("--force-reinstall")
        run_checked(install, base)
        run_checked([python, "-c", IMPORT_CHECK], base)
        stamp.write_text(fingerprint + "\n", encoding="ascii")
    return python


def prepare_config(python, base):
    """Create a first-run config and set admin's password in the venv process."""
    config = Path(os.environ.get("MINER_CONFIG") or "config.yaml")
    if not config.is_absolute():
        config = base / config
    if not config.exists():
        shutil.copyfile(base / "config.example.yaml", config)
        print("Created config.yaml. Set the admin password below.", flush=True)
    # Detect an unfinished first-run setup too (e.g. the window was closed).
    # Never replace existing users, passwords, or any other local settings.
    code = (
        "import appconfig, auth; "
        "cfg = appconfig.load_config(); "
        "u = auth._users(cfg).get('admin'); "
        "raise SystemExit(10 if u and auth._unusable(str(u.get('password', ''))) else 0)"
    )
    result = subprocess.run([str(python), "-c", code], cwd=str(base))
    if result.returncode == 10:
        print("First-run setup: choose a password for admin.", flush=True)
        run_checked([python, base / "auth.py", "passwd", "admin"], base)
    elif result.returncode:
        raise RuntimeError("Cannot read the configuration. See the error above.")


def supervise(base):
    failures = 0
    env = dict(os.environ, MINER_GUARDIAN="1")
    first_launch = True
    while True:
        python = prepare_environment(base)
        if first_launch:
            prepare_config(python, base)
            first_launch = False
        started = time.monotonic()
        child = subprocess.Popen([str(python), str(base / "server.py")],
                                 cwd=str(base), env=env)
        try:
            code = child.wait()
        except KeyboardInterrupt:
            # Ctrl+C reaches both processes in the console. Let the server shut
            # down, then terminate it if necessary; never restart an operator stop.
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=10)
            return 0
        uptime = time.monotonic() - started
        if code == 0:
            return 0
        if code == 42 or uptime >= MIN_UPTIME:
            failures = 0
        else:
            failures += 1
        print("Server exited (code={}) after {:.1f}s; quick failures {}/{}.".format(
            code, uptime, failures, MAX_FAILURES), flush=True)
        if failures >= MAX_FAILURES:
            raise RuntimeError("Auto-restart stopped. Check the traceback and logs/miner.log.")
        env["MINER_NO_BROWSER"] = "1"
        print("Restarting in {}s...".format(RESTART_DELAY), flush=True)
        time.sleep(RESTART_DELAY)


def main():
    try:
        if sys.version_info < MIN_PYTHON:
            raise RuntimeError("Install Python 3.10 or newer from https://www.python.org/downloads/windows/")
        return supervise(BASE)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("\nAntMonitor could not start: {}".format(error), file=sys.stderr, flush=True)
        print("Check Python, network access for pip, and the errors above.", file=sys.stderr)
        if not os.environ.get("MINER_NO_PAUSE") and sys.stdin.isatty():
            try:
                input("Press Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
