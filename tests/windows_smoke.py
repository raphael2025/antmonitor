"""Native CMD smoke test: clean ZIP-style launch, offline reuse, legacy handoff."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def main():
    if os.name != "nt":
        raise SystemExit("This smoke test requires Windows CMD.")
    root = Path(__file__).resolve().parent.parent
    without_python = '--without-python' in sys.argv
    unsupported_python = '--unsupported-python' in sys.argv
    # Exercise quoting and Unicode too; no Git checkout or preinstalled venv.
    with tempfile.TemporaryDirectory(prefix="AntMonitor Windows & 测试 ") as folder:
        base = Path(folder) / "project"
        shutil.copytree(root, base, ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", "logs"))
        batch = base / "run.bat"
        checkout_bytes = batch.read_bytes()
        # GitHub source ZIPs contain repository blobs, whose text is LF even
        # though .gitattributes makes a Git checkout use CRLF on Windows.
        batch.write_bytes(checkout_bytes.replace(b"\r\n", b"\n"))
        if unsupported_python:
            assert sys.version_info[:2] == (3, 15)
            subprocess.run([sys.executable, '-m', 'venv', str(base / '.venv')], check=True)
            (base / '.venv/notes.txt').write_text('original incomplete Python 3.15 environment')
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        (base / "config.yaml").write_text(
            "server:\n  host: 127.0.0.1\n  port: {}\n  open_browser: false\n"
            "schedule:\n  enabled: false\n"
            "public_api:\n  token: windows-smoke-token\n".format(port), encoding="utf-8")
        for offline in (False, True):
            if offline:
                batch.write_bytes(checkout_bytes)
            env = dict(os.environ, MINER_NO_PAUSE="1", PYTHONUTF8="1",
                       PY_PYTHON3="{}.{}".format(*sys.version_info[:2]))
            if without_python:
                # Keep the controller interpreter outside the child's PATH.
                # An explicit private runtime bypasses the runner's preinstalled
                # Python registry entries and exercises the real installer.
                windows = Path(os.environ['WINDIR'])
                env['PATH'] = os.pathsep.join(str(p) for p in (
                    windows / 'System32', windows,
                    windows / 'System32/WindowsPowerShell/v1.0'))
                env['MINER_PYTHON_DIR'] = str(Path(folder) / 'Python313')
            if offline:
                env["PIP_NO_INDEX"] = "1"
            log = base / ("offline.log" if offline else "first-start.log")
            with log.open("wb") as output:
                child = subprocess.Popen(["cmd.exe", "/d", "/c", "run.bat"],
                                         cwd=base, env=env, stdout=output, stderr=output)
                try:
                    deadline = time.monotonic() + 300
                    while time.monotonic() < deadline:
                        if child.poll() is not None:
                            raise RuntimeError("run.bat exited before the server became ready")
                        try:
                            request = urllib.request.Request(
                                "http://127.0.0.1:{}/api/public/health".format(port),
                                headers={"Authorization": "Bearer windows-smoke-token"})
                            with urllib.request.urlopen(request, timeout=2) as response:
                                assert response.status == 200
                            break
                        except OSError:
                            time.sleep(1)
                    else:
                        raise RuntimeError("The server did not become ready")
                    assert (base / ".venv/Scripts/python.exe").is_file()
                    actual = subprocess.check_output(
                        [str(base / ".venv/Scripts/python.exe"), "-c",
                         "import sys; print('{}.{}'.format(*sys.version_info[:2]))"],
                        text=True).strip()
                    if unsupported_python:
                        assert (3, 8) <= tuple(map(int, actual.split('.'))) < (3, 14), actual
                        backups = list(base.glob('.venv.incompatible-*'))
                        assert len(backups) == 1
                        assert (backups[0] / 'notes.txt').read_text() == 'original incomplete Python 3.15 environment'
                        print('PASS: rejected Python 3.15 and recovered its incomplete venv with Python ' + actual)
                    else:
                        expected = '3.13' if without_python else "{}.{}".format(*sys.version_info[:2])
                        assert actual == expected, actual
                    if without_python:
                        assert (Path(env['MINER_PYTHON_DIR']) / 'python.exe').is_file()
                    assert (base / ".venv/.antmonitor-requirements.sha256").is_file()
                    print("PASS: {} launch through native CMD".format(
                        "offline repeat" if offline else "first clean"))
                    if without_python and not offline:
                        print('PASS: Python downloaded and installed with no Python on child PATH')
                finally:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    child.wait(timeout=30)
                    print(log.read_text(encoding="utf-8", errors="replace"))
        # A running pre-update batch resumes at byte 1050 of the new file.
        # Reproduce that handoff by executing exactly the new continuation.
        continuation = (base / "run.bat").read_bytes()[1050:]
        (base / "legacy-handoff.bat").write_bytes(b"@echo off\r\n" + continuation)
        (base / "server.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "assert os.environ['MINER_GUARDIAN'] == '1'\n"
            "marker = Path('restart-count.txt')\n"
            "count = int(marker.read_text()) if marker.exists() else 0\n"
            "marker.write_text(str(count + 1))\n"
            "if count == 0:\n"
            "    Path('legacy-handoff.bat').write_bytes(b'@echo off\\r\\nexit /b 99\\r\\n')\n"
            "    raise SystemExit(42)\n"
            "assert os.environ['MINER_NO_BROWSER'] == '1'\n", encoding="ascii")
        result = subprocess.run(["cmd.exe", "/d", "/c", "legacy-handoff.bat"],
                                cwd=base, env=env, timeout=60)
        assert result.returncode == 0
        assert (base / "restart-count.txt").read_text() == "2"
        print("PASS: legacy handoff, update restart, replaced batch and clean shutdown")


if __name__ == "__main__":
    main()
