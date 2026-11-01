"""Installer source routing, using local Git fixtures and network/package stubs."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
REAL_GIT = shutil.which("git")
BASH = shutil.which("bash")
GITHUB = "https://github.com/raphael2025/antmonitor.git"
GITEE = "https://gitee.com/raphael01/antmonitor.git"
FORK = "https://example.invalid/team/antmonitor.git"


@unittest.skipUnless(os.name != "nt" and REAL_GIT and BASH, "requires Linux/macOS Git and Bash")
class InstallSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="antmonitor-install-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.install = self.root / "installed"
        self.template = self.root / "template"
        self.template.mkdir()
        (self.template / "server.py").write_text("# fixture\n", encoding="utf-8")
        (self.template / "requirements.txt").write_text("", encoding="utf-8")
        self.git("init", "-q", str(self.template))
        self.git("-C", str(self.template), "symbolic-ref", "HEAD", "refs/heads/main")
        self.git("-C", str(self.template), "add", ".")
        self.git("-C", str(self.template), "-c", "user.name=Test", "-c",
                 "user.email=test@example.invalid", "commit", "-qm", "fixture")
        self.log = self.root / "calls.jsonl"
        self.archive = self.root / "fixture.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, data in (("server.py", b"# archive fixture\n"), ("requirements.txt", b"")):
                info = tarfile.TarInfo("antmonitor-main/" + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.tool("git", r'''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["TEST_LOG"], "a") as out:
    out.write(json.dumps(["git", *args]) + "\n")
i = 0
while i < len(args) and args[i] in ("-c", "-C"):
    i += 2
action = args[i]
real = os.environ["TEST_REAL_GIT"]
if action == "clone":
    if os.environ.get("TEST_CLONE_FAIL") or (os.environ.get("TEST_GITHUB_FAIL") and "github.com" in args[-2]):
        sys.exit(1)
    repo, dest = args[-2:]
    subprocess.run([real, "clone", "-q", "--branch", "main", os.environ["TEST_TEMPLATE"], dest], check=True)
    subprocess.run([real, "-C", dest, "remote", "set-url", "origin", repo], check=True)
    sys.exit(0)
if action in ("fetch", "pull"):
    if os.environ.get("TEST_UPDATE_FAIL"):
        sys.exit(1)
    if os.environ.get("TEST_ORIGIN_FAIL") and "origin" in args:
        sys.exit(1)
    if action == "fetch" and any("gitee.com" in arg or "github.com" in arg for arg in args):
        sys.exit(subprocess.run([real, *args[:i], "fetch", os.environ["TEST_TEMPLATE"], "main"]).returncode)
    sys.exit(0)
sys.exit(subprocess.run([real, *args]).returncode)
''')
        self.tool("curl", r'''
import json, os, sys
with open(os.environ["TEST_LOG"], "a") as out:
    out.write(json.dumps(["curl", *sys.argv[1:]]) + "\n")
if os.environ.get("TEST_CURL_FAIL"):
    sys.exit(22)
with open(os.environ["TEST_ARCHIVE"], "rb") as archive:
    sys.stdout.buffer.write(archive.read())
''')
        self.tool("apt-get", r'''
import json, os, sys
with open(os.environ["TEST_LOG"], "a") as out:
    out.write(json.dumps(["apt-get", *sys.argv[1:]]) + "\n")
sys.exit(1 if os.environ.get("TEST_APT_GIT_FAIL") and sys.argv[-1:] == ["git"] else 0)
''')
        # A restricted PATH can emulate missing Git without hiding the shell tools.
        for name in ("mkdir", "mktemp", "cp", "rm", "tar", "gzip", "dirname"):
            (self.bin / name).symlink_to(shutil.which(name))
        (self.bin / "python3").symlink_to(sys.executable)

    def tool(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def git(self, *args):
        return subprocess.run([REAL_GIT, *args], check=True, text=True,
                              capture_output=True).stdout.strip()

    def existing(self, origin):
        self.git("clone", "-q", str(self.template), str(self.install))
        self.git("-C", str(self.install), "remote", "set-url", "origin", origin)

    def origin(self):
        return self.git("-C", str(self.install), "remote", "get-url", "origin")

    def calls(self, tool=None):
        rows = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [row for row in rows if tool is None or row[0] == tool]

    def run_installer(self, *args, missing_git=False, local_source=False, local_archive=False, operation="sync_code", **extra):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(text.count('\nmain "$@"'), 1)
        # Run the real argument parsing and selected installer function, never main's
        # package installation / service lifecycle. apt-get is stubbed when requested.
        runner = self.root / "runner.sh"
        if local_source:
            source_dir = self.root / "source"
            shutil.copytree(self.template, source_dir)
            self.git("-C", str(source_dir), "remote", "add", "origin", GITHUB)
            if local_archive:
                shutil.rmtree(source_dir / ".git")
            runner = source_dir / "install.sh"
        runner.write_text(text.rsplit('\nmain "$@"', 1)[0] + "\n" + operation + "\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("ANTMONITOR_") and key != "GH_PROXY"}
        if missing_git:
            (self.bin / "git").unlink()
        env.update(PATH=str(self.bin) + ("" if missing_git else os.pathsep + os.environ.get("PATH", "")),
                   ANTMONITOR_HOME=str(self.install), TEST_LOG=str(self.log),
                   TEST_REAL_GIT=REAL_GIT, TEST_TEMPLATE=str(self.template),
                   TEST_ARCHIVE=str(self.archive))
        env.update(extra)
        return subprocess.run([BASH, str(runner), *args], cwd=self.root, env=env,
                              text=True, capture_output=True, timeout=20)

    def test_gitee_one_click_installs_git_and_clones_gitee(self):
        result = self.run_installer("--source", "gitee", operation="ensure_packages\nsync_code")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.origin(), GITEE)
        self.assertTrue(any(row[-1] == "git" for row in self.calls("apt-get")))
        self.assertFalse(self.calls("curl"))

    def test_source_environment_variable(self):
        result = self.run_installer(ANTMONITOR_SOURCE="gitee")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.origin(), GITEE)

    def test_explicit_source_fetches_selected_platform_even_from_local_source(self):
        result = self.run_installer("--source", "gitee", local_source=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.origin(), GITEE)
        self.assertTrue(any("clone" in row and GITEE in row for row in self.calls("git")))

    def test_unspecified_source_preserves_local_install_semantics(self):
        result = self.run_installer(local_source=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.install / "server.py").read_text(), "# fixture\n")
        self.assertFalse(any("clone" in row for row in self.calls("git")))
        self.assertFalse(self.calls("curl"))

    @unittest.skipUnless(shutil.which("rsync"), "requires real rsync to check destination preservation")
    def test_local_archive_updates_source_without_deleting_custom_database_or_other_files(self):
        self.install.mkdir()
        (self.install / "server.py").write_text("# old archive installation\n")
        original = {
            "config.yaml": b"db:\n  path: other.db\n",
            "settings.json": b'{"max_pps": 200}\n',
            "other.db": b"SQLite format 3\x00\xffcustomer database",
            "other.db-wal": b"customer write-ahead log\x00\xff",
            "business/notes.txt": b"customer file not shipped with AntMonitor\n",
        }
        for name, contents in original.items():
            target = self.install / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        # A downloaded package can also contain example/local configuration. It
        # must not overwrite this installation's existing configuration.
        (self.template / "config.yaml").write_text("db:\n  path: replacement.db\n")
        result = self.run_installer(local_source=True, local_archive=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.install / "server.py").read_text(), "# fixture\n")
        for name, contents in original.items():
            self.assertEqual((self.install / name).read_bytes(), contents, name)
        self.assertFalse((self.install / ".git").exists())
        self.assertFalse(self.calls("curl"))

    def test_gitee_uses_existing_git_when_apt_git_install_fails(self):
        result = self.run_installer("--source", "gitee", operation="ensure_packages\nsync_code",
                                    TEST_APT_GIT_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.origin(), GITEE)

    def test_default_new_install_still_uses_github(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.origin(), GITHUB)

    def test_auto_uses_gitee_after_github_clone_failure(self):
        result = self.run_installer(TEST_GITHUB_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.origin(), GITEE)
        self.assertFalse(self.calls("curl"))
        clones = [row for row in self.calls("git") if "clone" in row]
        self.assertEqual([row[-2] for row in clones], [GITHUB, GITEE])

    def test_explicit_github_never_clones_gitee(self):
        result = self.run_installer("--source", "github", TEST_GITHUB_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(GITEE in row for row in self.calls("git")))
        self.assertTrue(self.calls("curl"))

    def test_auto_existing_fallback_keeps_origin_config_and_database(self):
        self.existing(GITHUB)
        for name in ("config.yaml", "settings.json", "miner_monitor.db"):
            (self.install / name).write_bytes(b"keep data\x00\xff")
        result = self.run_installer(TEST_ORIGIN_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.origin(), GITHUB)
        self.assertTrue(any("fetch" in row and GITEE in row for row in self.calls("git")))
        for name in ("config.yaml", "settings.json", "miner_monitor.db"):
            self.assertEqual((self.install / name).read_bytes(), b"keep data\x00\xff")

    def test_auto_never_falls_back_for_custom_origin_or_dirty_tree(self):
        self.existing(FORK)
        result = self.run_installer(TEST_ORIGIN_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.origin(), FORK)
        self.assertFalse(any(GITEE in row for row in self.calls("git")))
        self.git("-C", str(self.install), "remote", "set-url", "origin", GITHUB)
        (self.install / "server.py").write_text("# local edits\n")
        result = self.run_installer(TEST_ORIGIN_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.install / "server.py").read_text(), "# local edits\n")
        self.assertFalse(any(GITEE in row for row in self.calls("git")))

    def test_both_sources_and_archive_unavailable_preserve_existing_files(self):
        self.install.mkdir()
        (self.install / "settings.json").write_bytes(b"keep settings")
        result = self.run_installer(TEST_CLONE_FAIL="1", TEST_CURL_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.install / "settings.json").read_bytes(), b"keep settings")
        self.assertFalse((self.install / "server.py").exists())

    def test_relative_directory_is_bound_to_entry_directory(self):
        result = self.run_installer("--dir", ".", operation='cd /\nprintf "%s" "$INSTALL_DIR"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout).resolve(), self.root.resolve())

    def test_gitee_clone_failure_never_downloads_an_archive(self):
        result = self.run_installer("--source", "gitee", TEST_CLONE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls("curl"))
        self.assertFalse((self.install / "server.py").exists())

    def test_gitee_missing_git_fails_without_archive_fallback(self):
        result = self.run_installer("--source", "gitee", missing_git=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls("curl"))

    def test_gitee_failed_git_install_stops_before_downloading(self):
        result = self.run_installer("--source", "gitee", missing_git=True,
                                    operation="ensure_packages\nsync_code", TEST_APT_GIT_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("自动安装失败", result.stdout + result.stderr)
        self.assertFalse(self.calls("curl"))

    def test_github_clone_failure_keeps_github_archive_fallback(self):
        result = self.run_installer(TEST_CLONE_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("curl")[0][-1],
                         "https://github.com/raphael2025/antmonitor/archive/refs/heads/main.tar.gz")
        self.assertTrue((self.install / "server.py").exists())

    def test_github_without_git_can_still_use_archive(self):
        result = self.run_installer(missing_git=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.calls("curl"))

    def test_existing_gitee_origin_is_preserved_without_source_argument(self):
        self.existing(GITEE)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.origin(), GITEE)
        self.assertFalse(any("set-url" in row for row in self.calls("git")))

    def test_existing_archive_install_migrates_to_gitee_and_preserves_settings(self):
        self.install.mkdir()
        (self.install / "server.py").write_text("# old archive install\n")
        config = b"server:\n  port: 8801\n"
        settings = b'{"cloud":{"enabled":true,"site_name":"test"}}\n'
        (self.install / "config.yaml").write_bytes(config)
        (self.install / "settings.json").write_bytes(settings)
        result = self.run_installer("--source", "gitee")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.origin(), GITEE)
        self.assertEqual((self.install / "server.py").read_text(), "# fixture\n")
        self.assertEqual((self.install / "config.yaml").read_bytes(), config)
        self.assertEqual((self.install / "settings.json").read_bytes(), settings)
        self.assertFalse(self.calls("curl"))

    def test_explicit_source_switches_official_origin_both_directions(self):
        self.existing(GITHUB)
        for source, expected in (("gitee", GITEE), ("github", GITHUB)):
            with self.subTest(source=source):
                result = self.run_installer("--source", source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.origin(), expected)
        fetches = [row for row in self.calls("git") if "fetch" in row]
        self.assertTrue(fetches)
        self.assertTrue(all(row[-3:] == ["fetch", "--prune", "origin"] for row in fetches))

    def test_explicit_source_does_not_rewrite_fork_origin(self):
        self.existing(FORK)
        result = self.run_installer("--source", "gitee")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.origin(), FORK)
        self.assertFalse(any("fetch" in row or "pull" in row for row in self.calls("git")))

    def test_existing_fork_is_preserved_when_source_is_unspecified(self):
        self.existing(FORK)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.origin(), FORK)

    def test_existing_git_repo_without_git_never_gets_overwritten_by_archive(self):
        self.existing(GITEE)
        before = (self.install / "server.py").read_bytes()
        result = self.run_installer(missing_git=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls("curl"))
        self.assertEqual((self.install / "server.py").read_bytes(), before)

    def test_custom_repository_override_remains_supported(self):
        result = self.run_installer(ANTMONITOR_REPO=FORK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.origin(), FORK)

    def test_failed_custom_repository_does_not_fall_back_to_official_code(self):
        result = self.run_installer(ANTMONITOR_REPO=FORK, TEST_CLONE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls("curl"))

    def test_failed_existing_gitee_update_is_reported_as_failure(self):
        self.existing(GITEE)
        result = self.run_installer(TEST_UPDATE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.origin(), GITEE)
        self.assertFalse(self.calls("curl"))

    def test_invalid_source_is_rejected_before_download(self):
        result = self.run_installer("--source", "unsupported")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.calls())

    def test_missing_source_value_has_a_clear_error(self):
        result = self.run_installer("--source")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--source 需要一个值", result.stderr)
        self.assertFalse(self.calls())

    def test_conflicting_custom_repository_and_source_are_rejected(self):
        result = self.run_installer("--source", "gitee", ANTMONITOR_REPO=FORK)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.calls())


if __name__ == "__main__":
    unittest.main()
