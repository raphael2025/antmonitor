"""Bootstrap failures must stop before a restart loop; updates must recheck deps."""
import hashlib
from pathlib import Path
import subprocess

import pytest

import windows_launcher as launcher


def environment(tmp_path):
    python = tmp_path / ".venv" / (
        "Scripts/python.exe" if launcher.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    (tmp_path / "requirements.txt").write_text("fastapi>=0.100\n")
    return python


def test_first_install_then_offline_restart_and_changed_requirements(tmp_path, monkeypatch):
    python = environment(tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.prepare_environment(tmp_path) == python
    installs = lambda: [c for c in calls if "install" in c]
    assert len(installs()) == 1
    assert installs()[0][:4] == [str(python), "-m", "pip", "install"]
    assert "--only-binary=:all:" in installs()[0]
    launcher.prepare_environment(tmp_path)
    assert len(installs()) == 1
    (tmp_path / "requirements.txt").write_text("fastapi>=0.110\n")
    launcher.prepare_environment(tmp_path)
    assert len(installs()) == 2


def test_missing_module_repaired_even_when_requirements_unchanged(tmp_path, monkeypatch):
    environment(tmp_path)
    digest = hashlib.sha256((tmp_path / "requirements.txt").read_bytes()).hexdigest()
    (tmp_path / ".venv/.antmonitor-requirements.sha256").write_text(digest)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        # Fail only the initial, quiet import probe; installing repairs it.
        code = int(args[-1] == launcher.IMPORT_CHECK and not kwargs.get("check"))
        return subprocess.CompletedProcess(args, code)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    launcher.prepare_environment(tmp_path)
    assert any("install" in c and "--force-reinstall" in c for c in calls)


def test_failed_install_not_marked_successful(tmp_path, monkeypatch):
    environment(tmp_path)

    def run(args, **kwargs):
        if "install" in args:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        launcher.prepare_environment(tmp_path)
    assert not (tmp_path / ".venv/.antmonitor-requirements.sha256").exists()


@pytest.mark.parametrize("version,accepted", [
    ((3, 7), False), ((3, 8), True), ((3, 9), True), ((3, 10), True),
    ((3, 11), True), ((3, 12), True), ((3, 13), True), ((3, 14), True),
    ((3, 15), False),
])
def test_runtime_requires_validated_minor(version, accepted):
    assert launcher.runtime_supported(version, gil_disabled=False) is accepted


def test_free_threaded_runtime_is_not_selected():
    assert launcher.runtime_supported((3, 13), gil_disabled=True) is False


def test_incompatible_venv_rebuilt_without_deleting_old_files_or_config(tmp_path, monkeypatch):
    python = environment(tmp_path)
    notes = tmp_path / ".venv/notes.txt"
    notes.write_text("old environment files")
    config = tmp_path / "config.yaml"
    config.write_text("existing user configuration")
    database = tmp_path / "miner_monitor.db"
    database.write_bytes(b"existing database")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[-1] == "--check-runtime":
            return subprocess.CompletedProcess(args, 1)  # old Python 3.15
        if "venv" in args:
            python.parent.mkdir(parents=True)
            python.touch()
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.prepare_environment(tmp_path) == python
    backups = list(tmp_path.glob(".venv.incompatible-*"))
    assert len(backups) == 1 and (backups[0] / "notes.txt").read_text() == "old environment files"
    assert config.read_text() == "existing user configuration"
    assert database.read_bytes() == b"existing database"
    assert any("venv" in c for c in calls)
    assert any("--only-binary=:all:" in c for c in calls)


def test_existing_configuration_preserved(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    original = "auth:\n  enabled: false\n"
    config.write_text(original)
    monkeypatch.delenv("MINER_CONFIG", raising=False)
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda args, **kw: subprocess.CompletedProcess(args, 0))
    launcher.prepare_config(Path("python"), tmp_path)
    assert config.read_text() == original


def test_new_and_interrupted_setup_prompt_for_admin(tmp_path, monkeypatch):
    monkeypatch.delenv("MINER_CONFIG", raising=False)
    (tmp_path / "config.example.yaml").write_text("auth: {}\n")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0 if "passwd" in args else 10)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    launcher.prepare_config(Path("python"), tmp_path)
    launcher.prepare_config(Path("python"), tmp_path)
    assert len([c for c in calls if "passwd" in c]) == 2
    assert (tmp_path / "config.yaml").read_text() == "auth: {}\n"


def test_update_exit_rechecks_environment_and_does_not_count_as_failure(tmp_path, monkeypatch):
    # Four failed starts, update exit 42, four failed starts, then clean shutdown.
    codes = iter([1] * 4 + [42] + [1] * 4 + [0])
    launches = []
    prepared = []
    monkeypatch.setattr(launcher, "prepare_environment",
                        lambda base: prepared.append(base) or Path("python"))
    monkeypatch.setattr(launcher, "prepare_config", lambda *args: None)
    monkeypatch.setattr(launcher.time, "sleep", lambda secs: None)
    monkeypatch.delenv("MINER_NO_BROWSER", raising=False)

    def popen(args, **kwargs):
        launches.append(dict(kwargs["env"]))
        return type("Child", (), {"wait": lambda self: next(codes)})()

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    assert launcher.supervise(tmp_path) == 0
    assert len(prepared) == 10
    assert all(e["MINER_GUARDIAN"] == "1" for e in launches)
    assert "MINER_NO_BROWSER" not in launches[0]
    assert all(e["MINER_NO_BROWSER"] == "1" for e in launches[1:])


def test_repeated_crashes_stop_at_five(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setattr(launcher, "prepare_environment", lambda base: Path("python"))
    monkeypatch.setattr(launcher, "prepare_config", lambda *args: None)
    monkeypatch.setattr(launcher.time, "sleep", lambda secs: None)

    def popen(*args, **kwargs):
        launches.append(1)
        return type("Child", (), {"wait": lambda self: 1})()

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="Auto-restart stopped"):
        launcher.supervise(tmp_path)
    assert len(launches) == 5


def test_ctrl_c_does_not_restart(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(launcher, "prepare_environment", lambda base: Path("python"))
    monkeypatch.setattr(launcher, "prepare_config", lambda *args: None)

    class Child:
        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is None:
                raise KeyboardInterrupt
            return 0

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: Child())
    assert launcher.supervise(tmp_path) == 0
    assert waits == [None, 10]


def test_batch_legacy_handoff_and_update_safe_launch_lines():
    batch = (Path(launcher.__file__).parent / "run.bat").read_bytes()
    assert batch.isascii() and b"\n" not in batch.replace(b"\r\n", b"")
    assert batch[1050:].startswith(b"rem Legacy update handoff:")
    assert b'cd /d "%~dp0"' in batch
    launch_lines = [line for line in batch.splitlines() if
                    b'"windows_launcher.py"' in line and b"--check-runtime" not in line]
    assert len(launch_lines) == 1
    assert all(line.endswith(b" & exit /b") for line in launch_lines)
    powershell = [line for line in batch.splitlines() if b'".\\ensure_python.ps1"' in line]
    assert len(powershell) == 1 and powershell[0].endswith(b" & exit /b")
    assert b'"windows_launcher.py" --check-runtime' in batch
