"""Real Git and WAL SQLite acceptance tests for configuration-preserving releases."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import update_guard
import updater

KIND = 'cloud' if hasattr(updater, 'is_git_deployment') else 'local'
SOURCE = str(Path(update_guard.__file__).resolve().parent)


def git(base, *args):
    return subprocess.check_output(['git', *args], cwd=str(base), stderr=subprocess.STDOUT).decode().strip()


@pytest.fixture
def release(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    git(tmp_path, 'config', 'user.name', 'test')
    (tmp_path / '.gitignore').write_text('config.yaml\nsettings.json\ncloud_settings.json\n*.local\n')
    (tmp_path / 'server.py').write_text('# code\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'base')
    return tmp_path


@pytest.mark.parametrize('filename', ['config.yaml', 'settings.json', 'cloud_settings.json',
                                    'segments.json', 'cloud_site_id.txt', 'custom.sqlite3',
                                    'certs/site.key', 'data/ledger'])
def test_release_cannot_take_ownership_of_runtime_files(release, filename):
    path = release / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('local_secret: must_survive\n')
    original = path.read_bytes()
    git(release, 'add', '-f', filename)
    git(release, 'commit', '-qm', 'bad release')
    with pytest.raises(RuntimeError, match='运行配置/数据'):
        update_guard.validate_target(str(release), 'HEAD')
    assert path.read_bytes() == original


def test_release_cannot_overwrite_ignored_custom_config(release):
    path = release / 'operator.local'
    path.write_text('operator configuration')
    git(release, 'add', '-f', 'operator.local')
    git(release, 'commit', '-qm', 'candidate')
    target = git(release, 'rev-parse', 'HEAD')
    git(release, 'reset', '--hard', 'HEAD~1')
    path.write_text('do not overwrite')
    with pytest.raises(RuntimeError, match='忽略文件'):
        update_guard.validate_target(str(release), target)
    assert path.read_text() == 'do not overwrite'


def test_new_code_and_example_config_are_allowed(release):
    (release / 'config.example.yaml').write_text('new_option: true')
    git(release, 'add', '.')
    git(release, 'commit', '-qm', 'new option')
    update_guard.validate_target(str(release), 'HEAD')


def test_migration_uses_wal_backup_and_preserves_live_config_and_schema(tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    config.write_bytes(b'# existing comments and custom keys\ndb: {path: ledger}\ncustom: keep\n')
    key = 'CO_CONFIG' if KIND == 'cloud' else 'MINER_CONFIG'
    monkeypatch.setenv(key, str(config))
    settings = tmp_path / 'settings.json'
    settings.write_text('{"cloud": {"token": "test-only"}, "custom": 42}')
    before = (config.read_bytes(), settings.read_bytes())
    source = sqlite3.connect(str(tmp_path / 'ledger'))
    source.execute('PRAGMA journal_mode=WAL')
    source.execute('CREATE TABLE audit_sentinel(value TEXT)')
    source.execute("INSERT INTO audit_sentinel VALUES ('committed WAL row')")
    source.commit()
    backup = update_guard.database_preflight(str(tmp_path), SOURCE, sys.executable, KIND,
                                            str(tmp_path / 'recovery'))
    # Candidate runs the actual project's migrations twice. Neither production
    # schema nor JSON/YAML is touched; the recovery point includes committed WAL.
    assert source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('audit_sentinel',)]
    with sqlite3.connect(backup) as saved:
        assert saved.execute('SELECT * FROM audit_sentinel').fetchall() == [('committed WAL row',)]
    assert before == (config.read_bytes(), settings.read_bytes())
    if os.name != 'nt':
        assert os.stat(backup).st_mode & 0o777 == 0o600
    source.close()


def test_failed_migration_keeps_live_database_and_recovery_point(tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    config.write_text('db: {path: ledger}\n')
    monkeypatch.setenv('CO_CONFIG' if KIND == 'cloud' else 'MINER_CONFIG', str(config))
    with sqlite3.connect(str(tmp_path / 'ledger')) as source:
        source.execute('CREATE TABLE keep(value)')
        source.execute('INSERT INTO keep VALUES (42)')
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    (candidate / 'db.py').write_text('def connect(path):\n raise RuntimeError("migration failure")\ninit_db=connect\n')
    recovery = tmp_path / 'recovery'
    with pytest.raises(RuntimeError, match='迁移验证失败'):
        update_guard.database_preflight(str(tmp_path), str(candidate), sys.executable, KIND, str(recovery))
    with sqlite3.connect(str(tmp_path / 'ledger')) as source:
        assert source.execute('SELECT * FROM keep').fetchall() == [(42,)]
    assert list(recovery.glob('*.sqlite3'))


def test_real_update_promotes_code_but_only_migrates_a_database_copy(release, monkeypatch):
    import updater
    import threading
    branch = git(release, 'rev-parse', '--abbrev-ref', 'HEAD')
    previous = git(release, 'rev-parse', 'HEAD')
    (release / 'server.py').write_text('''VERSION=2
class Collector:
 def start(self): pass
COLLECTOR=Collector()
class Router:
 on_startup=[]
 on_shutdown=[]
class App:
 router=Router()
app=App()
''')
    (release / 'updater.py').write_text('def start_auto(*a, **k): pass\ndef start_health_timer(): pass\n')
    (release / 'db.py').write_text('''import sqlite3
def connect(path):
 c=sqlite3.connect(path)
 if 'new_column' not in [r[1] for r in c.execute('PRAGMA table_info(keep)')]:
  c.execute('ALTER TABLE keep ADD COLUMN new_column TEXT')
 c.commit()
 return c
init_db=connect
''')
    git(release, 'add', '.')
    git(release, 'commit', '-qm', 'new application and database migration')
    target = git(release, 'rev-parse', 'HEAD')
    git(release, 'reset', '--hard', previous)
    git(release, 'update-ref', 'refs/remotes/origin/' + branch, target)
    config = release / 'config.yaml'
    config.write_text('# keep comments\ndb: {path: ledger}\nunknown_option: preserve\n')
    settings_name = 'cloud_settings.json' if KIND == 'cloud' else 'settings.json'
    settings = release / settings_name
    settings.write_text('{"site_name":"test","token":"test-only","future_option":42}')
    (release / '.git/info/exclude').write_text('ledger\nledger-*\n')
    with sqlite3.connect(str(release / 'ledger')) as live:
        live.execute('CREATE TABLE keep(value)')
        live.execute('INSERT INTO keep VALUES (42)')
    before = (config.read_bytes(), settings.read_bytes())
    monkeypatch.setenv('CO_CONFIG' if KIND == 'cloud' else 'MINER_CONFIG', str(config))
    monkeypatch.setattr(updater, 'BASE', str(release))
    monkeypatch.setattr(updater, '_apply_lock', threading.Lock())
    if KIND == 'local':
        monkeypatch.setattr(updater, 'STATE_FILE', str(release / '.git/update-state.json'))
        monkeypatch.setattr(updater, 'BUSY_CHECK', None)
    status = {'git':True, 'behind':1, 'branch':branch, 'remote_full':target, 'target':target,
              'remote':target[:10], 'switch_from':'', 'changes':['candidate']}
    monkeypatch.setattr(updater, 'check', lambda *a, **kw: status)
    result = updater.apply(restart=False)
    assert result['ok'], result
    assert git(release, 'rev-parse', 'HEAD') == target
    assert before == (config.read_bytes(), settings.read_bytes())
    with sqlite3.connect(str(release / 'ledger')) as live:
        assert live.execute('SELECT * FROM keep').fetchall() == [(42,)]
        assert [r[1] for r in live.execute('PRAGMA table_info(keep)')] == ['value']
    assert Path(result['database_backup']).exists()
    resumed = updater.apply(restart=False)
    assert not resumed['ok'] and resumed['pending']
    assert git(release, 'rev-parse', 'HEAD') == target
    # Actual startup runs the accepted migration. Repeating it must be harmless.
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    for _ in range(2):
        subprocess.run([sys.executable, '-c', "import db; db.connect('ledger').close()"],
                       cwd=str(release), env=env, check=True)
    with sqlite3.connect(str(release / 'ledger')) as live:
        assert live.execute('SELECT * FROM keep').fetchall() == [(42, None)]
    assert before == (config.read_bytes(), settings.read_bytes())


def test_concurrent_ref_refresh_cannot_change_the_validated_promotion(release, monkeypatch):
    import threading
    branch = git(release, 'rev-parse', '--abbrev-ref', 'HEAD')
    previous = git(release, 'rev-parse', 'HEAD')
    (release / 'server.py').write_text('VERSION=2\n')
    git(release, 'add', '.')
    git(release, 'commit', '-qm', 'validated candidate')
    target = git(release, 'rev-parse', 'HEAD')
    (release / 'server.py').write_text('def broken(:\n')
    git(release, 'add', '.')
    git(release, 'commit', '-qm', 'unvalidated remote change')
    later = git(release, 'rev-parse', 'HEAD')
    git(release, 'reset', '--hard', previous)
    git(release, 'update-ref', 'refs/remotes/origin/' + branch, target)
    monkeypatch.setattr(updater, 'BASE', str(release))
    monkeypatch.setattr(updater, 'STATE_FILE', str(release / '.git/update-state.json'))
    monkeypatch.setattr(updater, '_apply_lock', threading.Lock())
    monkeypatch.setattr(updater, 'BUSY_CHECK', None)
    monkeypatch.delenv('MINER_CONFIG', raising=False)
    monkeypatch.setattr(updater, 'check', lambda *a: {
        'behind':1, 'branch':branch, 'remote_full':target, 'remote':target[:10]})
    validate = updater._validate_release
    def refresh_after_validation(sha):
        result = validate(sha)
        git(release, 'update-ref', 'refs/remotes/origin/' + branch, later)
        return result
    monkeypatch.setattr(updater, '_validate_release', refresh_after_validation)
    result = updater.apply(restart=False)
    assert result['ok'], result
    assert git(release, 'rev-parse', 'HEAD') == target
    assert (release / 'server.py').read_text() == 'VERSION=2\n'
