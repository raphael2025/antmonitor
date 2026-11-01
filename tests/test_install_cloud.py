"""Cloud onboarding: real settings files and installer actions, no real services/network."""
import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.sh"
sys.path.insert(0, str(ROOT))

import logs  # noqa: E402

logs.setup({"logging": {"file": str(Path(tempfile.gettempdir()) / "miner_test.log"),
                        "level": "WARNING"}})
import appconfig  # noqa: E402
import cloud_report  # noqa: E402


def connect_code(**overrides):
    payload = {"u": "https://cloud.example.invalid:8443", "t": "test-token-only", "f": "ab" * 32}
    payload.update(overrides)
    return "AMC1-" + base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode().rstrip("=")


def embedded_setup():
    text = SCRIPT.read_text(encoding="utf-8")
    code = text.split("<<'PY_CLOUD_SETUP' 2>&1\n", 1)[1].split("\nPY_CLOUD_SETUP\n", 1)[0]
    scope = {"__name__": "installer_cloud_test", "appconfig": appconfig, "cloud_report": cloud_report}
    exec(compile(code, "install.sh/PY_CLOUD_SETUP", "exec"), scope)
    return scope


class Terminal:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.prompts = []
        self.messages = []

    def ask(self, text, secret=False):
        self.prompts.append((text, secret))
        return next(self.answers)

    def write(self, text):
        self.messages.append(text)


class CloudSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="antmonitor-cloud-setup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = self.root / "settings.json"
        self.config = self.root / "config.yaml"
        self.id_file = self.root / "cloud_site_id.txt"
        self.config.write_text("cloud:\n  site_name: YAML 名称\n  site_type: hydro\n"
                               "  site_id: yaml-id\n  interval: 93\n  customer_hours: 42\n", encoding="utf-8")
        self.original = {"cloud": {"enabled": True, "url": "https://old.example.invalid", "token": "old-token",
                                    "site_name": "现有场地", "site_type": "mixed", "site_id": "saved-id",
                                    "interval": 77, "custom_setting": "preserve"},
                         "scan_interval": 330, "unrelated_setting": {"keep": True}}
        self.settings.write_text(json.dumps(self.original, ensure_ascii=False), encoding="utf-8")
        self.settings.chmod(0o600)
        for obj, key, value in ((appconfig, "CONFIG_FILE", str(self.config)),
                                (appconfig, "SETTINGS_FILE", str(self.settings)),
                                (cloud_report, "_ID_FILE", str(self.id_file))):
            patch = mock.patch.object(obj, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.setup = embedded_setup()

    def run_setup(self, code, terminal=None, result=(True, "ping passed"), **kwargs):
        output = io.StringIO()
        with mock.patch.object(cloud_report, "test", return_value=result) as ping, \
                mock.patch.object(cloud_report, "site_id", side_effect=AssertionError("must not write identity before ping")), \
                contextlib.redirect_stdout(output):
            status = self.setup["configure"](code, terminal=terminal, **kwargs)
        return status, output.getvalue(), ping

    def saved(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_new_code_name_type_identity_need_no_extra_prompts(self):
        terminal = Terminal()
        status, output, ping = self.run_setup(connect_code(n="云端给的名称", y="hydro", s="new-site"), terminal)
        self.assertEqual(status, 0)
        self.assertFalse(terminal.prompts)
        cloud = self.saved()["cloud"]
        self.assertEqual((cloud["site_name"], cloud["site_type"], cloud["site_id"]), ("云端给的名称", "hydro", "new-site"))
        self.assertEqual(ping.call_args.args[1], "new-site")
        self.assertIn("连接验证通过", output)
        self.assertNotIn("test-token-only", output)

    def test_legacy_code_prompts_default_to_effective_saved_values_and_preserves_other_settings(self):
        terminal = Terminal("", "")
        status, _output, _ping = self.run_setup(connect_code(), terminal)
        self.assertEqual(status, 0)
        cloud = self.saved()["cloud"]
        self.assertEqual((cloud["site_name"], cloud["site_type"], cloud["site_id"]), ("现有场地", "mixed", "saved-id"))
        self.assertIn("现有场地", terminal.prompts[0][0])
        self.assertIn("[3]", terminal.prompts[1][0])
        self.assertEqual(cloud["interval"], 77)
        self.assertEqual(cloud["customer_hours"], 42)
        self.assertEqual(cloud["custom_setting"], "preserve")
        self.assertEqual(self.saved()["unrelated_setting"], {"keep": True})
        self.assertEqual(self.saved()["scan_interval"], 330)
        self.assertFalse(self.id_file.exists())
        if os.name == "posix":
            self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_explicit_options_override_code_metadata(self):
        status, _output, _ping = self.run_setup(connect_code(n="码名称", y="hydro"), Terminal(),
                                              name="显式名称", site_type="air")
        self.assertEqual(status, 0)
        self.assertEqual(self.saved()["cloud"]["site_name"], "显式名称")
        self.assertEqual(self.saved()["cloud"]["site_type"], "air")

    def test_legacy_code_without_tty_does_not_prompt_or_reset_name(self):
        status, _output, _ping = self.run_setup(connect_code())
        self.assertEqual(status, 0)
        self.assertEqual(self.saved()["cloud"]["site_name"], "现有场地")
        self.assertEqual(self.saved()["cloud"]["site_type"], "mixed")

    def test_identity_from_existing_file_is_preserved_without_creating_a_new_file(self):
        self.config.write_text("cloud:\n  site_name: YAML 名称\n", encoding="utf-8")
        self.original["cloud"].pop("site_id")
        self.settings.write_text(json.dumps(self.original), encoding="utf-8")
        self.id_file.write_text("st-existing\n", encoding="ascii")
        status, _output, ping = self.run_setup(connect_code())
        self.assertEqual(status, 0)
        self.assertEqual(self.saved()["cloud"]["site_id"], "st-existing")
        self.assertEqual(ping.call_args.args[1], "st-existing")
        self.assertEqual(self.id_file.read_text(), "st-existing\n")

    def test_padded_existing_identity_is_not_silently_changed(self):
        self.settings.unlink()
        self.config.write_text('cloud:\n  site_name: old\n  site_id: " yaml-id "\n')
        status, _output, ping = self.run_setup(connect_code())
        self.assertEqual(status, 1)
        ping.assert_not_called()
        self.assertFalse(self.settings.exists())

    def test_legacy_code_can_bind_unicode_and_space_site_ids(self):
        for sid in ("云海 二期", "legacy site", "legacy/site"):
            with self.subTest(sid=sid):
                status, _output, ping = self.run_setup(connect_code(s=sid))
                self.assertEqual(status, 0)
                self.assertEqual(ping.call_args.args[1], sid)
                self.assertEqual(self.saved()["cloud"]["site_id"], sid)

    def test_legacy_code_without_s_preserves_existing_unicode_or_space_settings_identity(self):
        for sid in ("云海 二期", "legacy site"):
            with self.subTest(sid=sid):
                self.original["cloud"]["site_id"] = sid
                self.settings.write_text(json.dumps(self.original, ensure_ascii=False))
                status, _output, ping = self.run_setup(connect_code())
                self.assertEqual(status, 0)
                self.assertEqual(ping.call_args.args[1], sid)
                self.assertEqual(self.saved()["cloud"]["site_id"], sid)

    def test_fresh_identity_is_saved_only_after_ping_passes(self):
        self.config.write_text("{}\n")
        self.settings.unlink()
        code = connect_code(n="新场地", y="air")
        status, output, ping = self.run_setup(code, result=(False, "测试超时"))
        self.assertEqual(status, 1)
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.id_file.exists())
        self.assertIn("未修改", output)
        status, _output, ping = self.run_setup(code)
        self.assertEqual(status, 0)
        self.assertEqual(self.saved()["cloud"]["site_id"], ping.call_args.args[1])
        self.assertRegex(self.saved()["cloud"]["site_id"], r"^st-[0-9a-f]{12}$")

    def test_ping_failure_preserves_exact_settings_and_never_reports_success(self):
        before = self.settings.read_bytes()
        owner = (self.settings.stat().st_uid, self.settings.stat().st_gid)
        status, output, _ping = self.run_setup(connect_code(s="new-id", n="新名称", y="air"),
                                              result=(False, "证书指纹不匹配"))
        self.assertEqual(status, 1)
        self.assertIn("连接验证未通过", output)
        self.assertNotIn("已保存", output)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual((self.settings.stat().st_uid, self.settings.stat().st_gid), owner)
        self.assertFalse(self.id_file.exists())

    def test_failed_code_can_be_retried_with_one_more_paste(self):
        replacement = connect_code(n="正确场地", y="air", s="right-site")
        terminal = Terminal(replacement)
        results = [(False, "旧码凭据错误"), (True, "pass")]
        with mock.patch.object(cloud_report, "test", side_effect=results), contextlib.redirect_stdout(io.StringIO()):
            status = self.setup["configure"](connect_code(n="旧码", y="hydro"), terminal=terminal)
        self.assertEqual(status, 0)
        self.assertEqual(self.saved()["cloud"]["site_id"], "right-site")
        self.assertTrue(terminal.prompts[0][1], "code input must be hidden")
        self.assertIn("未修改原云端配置", terminal.messages[1])

    def test_failed_code_can_be_skipped_and_keeps_original_bytes(self):
        before = self.settings.read_bytes()
        status, output, _ping = self.run_setup(connect_code(n="新场地", y="air"), Terminal(""),
                                              result=(False, "连接超时"))
        self.assertEqual(status, 10)
        self.assertIn("已跳过", output)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_blank_first_prompt_skips_without_ping_or_save(self):
        before = self.settings.read_bytes()
        terminal = Terminal("")
        status, _output, ping = self.run_setup("", terminal)
        self.assertEqual(status, 10)
        ping.assert_not_called()
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertTrue(terminal.prompts[0][1])

    def test_dash_without_terminal_fails_clearly(self):
        with self.assertRaisesRegex(self.setup["SetupError"], "需要终端"):
            self.setup["configure"]("-")

    def test_invalid_code_never_pings_or_overwrites(self):
        invalid = ["wrong", "AMC1-%%%", "AMC1-W10", connect_code(u="file:///tmp/x"),
                   connect_code(u="http://public.example.invalid"), connect_code(f="bad-fingerprint"),
                   connect_code(s="x" * 65), connect_code(s="\n\x7f"), connect_code(s=" site "),
                   connect_code(s="云海\n 二期"),
                   connect_code(n=["wrong"]), connect_code(y="water")]
        before = self.settings.read_bytes()
        for code in invalid:
            with self.subTest(code=code):
                status, _output, ping = self.run_setup(code)
                self.assertEqual(status, 1)
                ping.assert_not_called()
                self.assertEqual(self.settings.read_bytes(), before)

    def test_corrupt_settings_are_not_replaced(self):
        self.settings.write_text('{"cloud":')
        before = self.settings.read_bytes()
        status, _output, ping = self.run_setup(connect_code(n="name", y="air"))
        self.assertEqual(status, 1)
        ping.assert_not_called()
        self.assertEqual(self.settings.read_bytes(), before)

    def test_concurrent_cloud_change_is_preserved_instead_of_overwritten(self):
        newer = copy.deepcopy(self.original)
        newer["cloud"]["site_name"] = "其他管理员刚改的名称"

        def ping(_cloud, _sid):
            self.settings.write_text(json.dumps(newer))
            return True, "passed"

        with mock.patch.object(cloud_report, "test", side_effect=ping), contextlib.redirect_stdout(io.StringIO()):
            status = self.setup["configure"](connect_code(n="过期的修改", y="air"))
        self.assertEqual(status, 1)
        self.assertEqual(self.saved(), newer)

    @unittest.skipUnless(os.name == "posix", "requires POSIX service UID/GID support")
    def test_service_identity_drops_groups_then_gid_then_uid_before_file_operations(self):
        import grp
        import pwd
        account = SimpleNamespace(pw_name="testservice", pw_uid=1200, pw_gid=1300)
        steps = []
        with mock.patch.object(pwd, "getpwnam", return_value=account), \
                mock.patch.object(grp, "getgrnam", return_value=SimpleNamespace(gr_gid=1400)), \
                mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(os, "getegid", return_value=0), \
                mock.patch.object(os, "initgroups", side_effect=lambda *args: steps.append(("groups", *args))), \
                mock.patch.object(os, "setgid", side_effect=lambda gid: steps.append(("gid", gid))), \
                mock.patch.object(os, "setuid", side_effect=lambda uid: steps.append(("uid", uid))):
            self.setup["use_service_account"]("testservice", "custom-group")
        self.assertEqual(steps, [("groups", "testservice", 1400), ("gid", 1400), ("uid", 1200)])


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires Bash on Unix")
class InstallerCloudActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="antmonitor-cloud-actions-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o755)
        self.install = self.root / "installed"
        self.install.mkdir(mode=0o755)
        self.log = self.root / "calls.log"
        self.settings = self.install / "settings.json"
        (self.install / "server.py").write_text("# fixture\n")
        (self.install / "config.yaml").write_text("cloud:\n  site_name: fixture\n  site_type: hydro\n")
        for name in ("appconfig.py", "logs.py"):
            shutil.copy(ROOT / name, self.install / name)
        (self.install / "cloud_report.py").write_text('''
import os, re
_ID_FILE = os.path.join(os.path.dirname(__file__), "cloud_site_id.txt")
def normalize_fingerprint(value):
    value = str(value or "").lower()
    return value if not value or re.fullmatch(r"[0-9a-f]{64}", value) else None
def test(cloud, sid):
    return (False, "模拟连接失败") if os.environ.get("TEST_PING_FAIL") else (True, "test passed")
''', encoding="utf-8")
        venv = self.install / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(sys.executable)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("ANTMONITOR_", "AM_", "MINER_")) and key != "GH_PROXY"}
        self.env.update(ANTMONITOR_HOME=str(self.install), TEST_LOG=str(self.log))
        self.runner = self.root / "runner.sh"
        self.stubs = r'''
need_root() { :; }
systemctl() {
  printf 'systemctl %s\n' "$*" >>"$TEST_LOG"
  case "$1" in
    show) case "$3" in
      LoadState) echo loaded ;;
      User) echo "${TEST_SERVICE_USER:-$(id -un)}" ;;
      Group) echo "${TEST_SERVICE_GROUP:-$(id -gn)}" ;;
      *) return 94 ;;
    esac ;;
    restart|is-active) [[ "${TEST_RESTART_FAIL:-0}" != 1 ]] ;;
    *) return 95 ;;
  esac
}
sleep() { :; }
ensure_packages() { echo packages >>"$TEST_LOG"; }
sync_code() { echo sync >>"$TEST_LOG"; touch "$INSTALL_DIR/server.py"; }
setup_venv() { echo venv >>"$TEST_LOG"; }
ensure_config() { echo config >>"$TEST_LOG"; }
admin_unusable() { return 0; }
set_admin_password() { echo password >>"$TEST_LOG"; echo 'PASSWORD_STEP'; }
write_systemd() { echo write_systemd >>"$TEST_LOG"; }
start_svc() { echo start_svc >>"$TEST_LOG"; }
'''

    def make_runner(self, tail='main "$@"', extra=""):
        text = SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0]
        self.runner.write_text(text + "\n" + self.stubs + "\n" + extra + "\n" + tail + "\n", encoding="utf-8")

    def run_installer(self, *args, **extra_env):
        self.make_runner(extra="has_tty() { return 1; }")
        return subprocess.run([shutil.which("bash"), str(self.runner), *args], cwd=self.root,
                              env={**self.env, **extra_env}, text=True, capture_output=True, timeout=15)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_connect_only_changes_settings_and_restarts_existing_service(self):
        result = self.run_installer("connect", "--connect-code", connect_code(n="场地", y="air", s="site-1"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertTrue(any("restart antmonitor.service" in row for row in calls))
        self.assertTrue(all(row.startswith("systemctl ") for row in calls), calls)
        self.assertEqual(json.loads(self.settings.read_text())["cloud"]["site_id"], "site-1")

    def test_alias_and_explicit_code_without_action_also_avoid_updates(self):
        for action in ([], ["configure-cloud"]):
            with self.subTest(action=action):
                result = self.run_installer(*action, "--connect-code", connect_code(n="场地", y="air"))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(all(row.startswith("systemctl ") for row in self.calls()))

    def test_failed_ping_does_not_save_restart_or_report_success(self):
        before = b'{"cloud":{"site_name":"old","token":"old-token"}}'
        self.settings.write_bytes(before)
        result = self.run_installer("connect", "--connect-code", connect_code(n="new", y="air"), TEST_PING_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse(any("restart" in row for row in self.calls()))
        self.assertNotIn("已接入云端", result.stdout)
        self.assertNotIn("配置已保存", result.stdout)

    def test_dash_without_tty_fails_before_installing_anything(self):
        (self.install / "server.py").unlink()
        result = self.run_installer("--connect-code", "-")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("需要终端", result.stdout + result.stderr)
        self.assertFalse(self.calls())

    def test_connect_without_code_or_tty_is_a_clear_error(self):
        result = self.run_installer("connect")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SSH 终端", result.stdout)
        self.assertFalse(self.calls())

    def test_unattended_new_install_does_not_block_on_cloud(self):
        (self.install / "server.py").unlink()
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.settings.exists())
        self.assertIn("start_svc", self.calls())
        self.assertNotIn("完成扫描后自动上报", result.stdout)

    def test_restart_failure_is_distinguished_from_save_success(self):
        result = self.run_installer("connect", "--connect-code", connect_code(n="site", y="air"), TEST_RESTART_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.settings.exists())
        self.assertIn("配置已保存，但服务重启失败", result.stdout)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "UID preservation requires root in isolated fixture")
    def test_service_account_owns_atomic_600_file_without_recursive_chown(self):
        import grp
        import pwd
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no nobody account")
        # Only a temporary fixture is chowned; no host service or project file changes.
        try:
            os.chown(self.install, account.pw_uid, account.pw_gid)
        except OSError as error:
            if error.errno in (1, 22):
                self.skipTest("sandbox cannot map another UID: " + str(error))
            raise
        self.settings.write_text('{"unrelated_setting": 123}')
        self.settings.chmod(0o600)
        os.chown(self.settings, account.pw_uid, account.pw_gid)
        config = self.install / "config.yaml"
        config.chmod(0o600)
        os.chown(config, account.pw_uid, account.pw_gid)
        result = self.run_installer("connect", "--connect-code", connect_code(n="site", y="hydro"),
                                    TEST_SERVICE_USER=account.pw_name,
                                    TEST_SERVICE_GROUP=grp.getgrgid(account.pw_gid).gr_name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.settings.stat().st_uid, account.pw_uid)
        self.assertEqual(self.settings.stat().st_gid, account.pw_gid)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.settings.read_text())["unrelated_setting"], 123)
        self.assertTrue(all(row.startswith("systemctl ") for row in self.calls()))

    def test_first_interactive_install_prompts_after_password_and_hides_code(self):
        import pty
        import select
        import signal
        import time
        (self.install / "server.py").unlink()
        self.make_runner()
        code = connect_code(n="一贴完成", y="hydro", s="pty-site")
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execve(shutil.which("bash"), ["bash", str(self.runner), "install"], self.env)
        received = bytearray()
        sent = False
        status = None
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    received.extend(chunk)
                    if not sent and "粘贴云端接入码" in received.decode("utf-8", errors="replace"):
                        os.write(fd, (code + "\n").encode())
                        sent = True
                child, child_status = os.waitpid(pid, os.WNOHANG)
                if child:
                    status = child_status
                    break
            if status is None:
                child, status = os.waitpid(pid, os.WNOHANG)
                if not child:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    self.fail("interactive installer did not finish: " + received.decode(errors="replace"))
        finally:
            os.close(fd)
        output = received.decode("utf-8", errors="replace")
        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, output)
        self.assertTrue(sent, output)
        self.assertLess(output.index("PASSWORD_STEP"), output.index("粘贴云端接入码"))
        self.assertNotIn(code, output, "pasted secret must not echo")
        self.assertNotIn("场地名称 [", output)
        self.assertEqual(json.loads(self.settings.read_text())["cloud"]["site_id"], "pty-site")


if __name__ == "__main__":
    unittest.main()
