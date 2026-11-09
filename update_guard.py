"""Release checks that keep deployment configuration and SQLite state out of Git updates."""
import os
import io
import json
from contextlib import closing
import tarfile
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

_RUNTIME_NAMES = {'config.yaml', 'settings.json', 'cloud_settings.json', 'segments.json',
                  'cloud_site_id.txt', '.update_state.json'}
_RUNTIME_DIRS = {'data', 'backups', 'certs', 'logs', '.venv', 'venv', 'offline'}


def validate_target(base, target):
    """Reject runtime files in a release, including ignored-file overwrite by Git."""
    def git(*args, **kw):
        return subprocess.run(['git', *args], cwd=base, capture_output=True, check=True, **kw)
    paths = git('ls-tree', '-rz', '--name-only', target).stdout.decode('utf-8').split('\0')
    configured = set()
    for key in ('MINER_CONFIG', 'MINER_SETTINGS', 'MINER_SEGMENTS', 'CO_CONFIG', 'CO_SETTINGS'):
        value = os.environ.get(key)
        if value:
            configured.add(os.path.abspath(os.path.join(base, value)))
    # Custom database/log names are deployment data too, even without a .db suffix.
    import yaml
    for env_key, kind in (('MINER_CONFIG', 'local'), ('CO_CONFIG', 'cloud')):
        config = os.path.abspath(os.path.join(base, os.environ.get(env_key) or 'config.yaml'))
        if not os.path.exists(config):
            continue
        with open(config, encoding='utf-8-sig') as stream:
            cfg = yaml.safe_load(stream) or {}
        if not isinstance(cfg, dict):
            raise RuntimeError('已有配置根节点必须是对象')
        for section, field in (('db', 'path'), ('logging', 'file')):
            value = (cfg.get(section) or {}).get(field)
            if value and value != ':memory:':
                anchor = base if kind == 'local' else os.path.dirname(config)
                resolved = os.path.abspath(os.path.join(anchor, value))
                configured.update((resolved, resolved + '-wal', resolved + '-shm'))
    for name in filter(None, paths):
        parts = name.split('/')
        leaf = parts[-1]
        if (leaf in _RUNTIME_NAMES or parts[0] in _RUNTIME_DIRS or
                leaf.endswith(('.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3', '.pem')) or
                os.path.abspath(os.path.join(base, name)) in configured):
            raise RuntimeError('候选版本包含运行配置/数据，拒绝更新: ' + name)
        local = os.path.join(base, name)
        if os.path.lexists(local):
            ignored = subprocess.run(['git', 'check-ignore', '--quiet', '--', name], cwd=base)
            if ignored.returncode == 0:
                raise RuntimeError('候选版本会覆盖本机忽略文件，拒绝更新: ' + name)
            if ignored.returncode not in (0, 1):
                raise RuntimeError('无法核验本机文件保护: ' + name)


def prune_database_backups(recovery_dir, keep=10, protected=()):
    """Keep a bounded set of verified update recovery points.

    Paths referenced by the current pending update are protected even when
    their timestamps are older than newer files. Never follow symlinks.
    """
    keep = max(1, min(100, int(keep)))
    protected_paths = {os.path.abspath(os.fspath(p)) for p in (protected or ()) if p}
    try:
        entries = []
        with os.scandir(recovery_dir) as scan:
            for entry in scan:
                if (entry.name.startswith("before-update-") and entry.name.endswith(".sqlite3")
                        and entry.is_file(follow_symlinks=False)):
                    entries.append((entry.path, entry.stat(follow_symlinks=False).st_mtime_ns))
    except OSError:
        return 0
    entries.sort(key=lambda item: (item[1], item[0]), reverse=True)
    retained = set(protected_paths)
    removed = 0
    for path, _mtime in entries:
        absolute = os.path.abspath(path)
        if absolute in retained:
            continue
        if len(retained) < keep:
            retained.add(absolute)
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            # A locked/permission-protected recovery point is safer to retain.
            pass
    return removed


def database_preflight(base, candidate, interpreter, kind, recovery_dir):
    """Back up the live SQLite database, migrate only a copy twice and check integrity.

    Never restore a pre-update database automatically: that would discard samples
    collected after the update. Backups are manual recovery points.
    """
    import yaml
    env_key = 'MINER_CONFIG' if kind == 'local' else 'CO_CONFIG'
    config = os.path.abspath(os.path.join(base, os.environ.get(env_key) or 'config.yaml'))
    if not os.path.exists(config):
        return None
    with open(config, encoding='utf-8-sig') as stream:
        cfg = yaml.safe_load(stream) or {}
    if not isinstance(cfg, dict) or not isinstance(cfg.get('db', {}), dict):
        raise RuntimeError('已有数据库配置格式异常，已停止更新')
    settings_key = 'MINER_SETTINGS' if kind == 'local' else 'CO_SETTINGS'
    settings_anchor = base if kind == 'local' else os.path.dirname(config)
    settings_default = 'settings.json' if kind == 'local' else 'cloud_settings.json'
    settings = os.path.abspath(os.path.join(settings_anchor, os.environ.get(settings_key) or settings_default))
    runtime_json = [settings]
    if kind == 'local':
        runtime_json.append(os.path.abspath(os.path.join(base, os.environ.get('MINER_SEGMENTS') or 'segments.json')))
    for filename in runtime_json:
        if os.path.lexists(filename):
            try:
                with open(filename, encoding='utf-8-sig') as stream:
                    data = json.load(stream)
                if not isinstance(data, dict):
                    raise ValueError('root must be an object')
            except (OSError, ValueError) as exc:
                raise RuntimeError('已有网页配置或网段配置不可用，拒绝更新: ' + os.path.basename(filename)) from exc
    default = 'miner_monitor.db' if kind == 'local' else 'cloud_overview.db'
    path = cfg.get('db', {}).get('path', default)
    if path == ':memory:':
        return None
    anchor = base if kind == 'local' else os.path.dirname(config)
    source = Path(os.path.abspath(os.path.join(anchor, path)))
    if not source.exists():
        # A configured file disappearing is not a new-install database.
        raise RuntimeError('已有安装的数据库不存在，拒绝以空库验证更新')
    os.makedirs(recovery_dir, mode=0o700, exist_ok=True)
    fd, backup = tempfile.mkstemp(prefix='before-update-', suffix='.sqlite3', dir=recovery_dir)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as live:
            with closing(sqlite3.connect(backup)) as saved:
                live.backup(saved)
                if saved.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('已有数据库完整性检查失败')
        with open(backup, 'rb+') as stream:
            os.fsync(stream.fileno())
        with tempfile.TemporaryDirectory(prefix='migration-check-') as sandbox:
            copied = os.path.join(sandbox, 'check.db')
            with closing(sqlite3.connect(backup)) as saved, closing(sqlite3.connect(copied)) as check:
                saved.backup(check)
            script = (
                'import sys; sys.path.insert(0, sys.argv[1]); import db; '
                'path=sys.argv[2]; '
                'connect=getattr(db,"init_db",None) or db.connect; '
                'c=connect(path); assert c.execute("PRAGMA quick_check").fetchone()[0]=="ok"; c.close(); '
                'c=connect(path); assert c.execute("PRAGMA quick_check").fetchone()[0]=="ok"; c.close()'
            )
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            env.pop('PYTHONPATH', None)
            env.pop('PYTHONHOME', None)
            result = subprocess.run([interpreter, '-c', script, candidate, copied], cwd=sandbox,
                                    env=env, capture_output=True, timeout=120)
            if result.returncode:
                # Error text can include business data; retain it only in local error output.
                raise RuntimeError('已有库迁移验证失败: ' + result.stderr.decode('utf-8', 'replace')[-800:])
        return backup
    except BaseException:
        # Keep the consistent recovery point even on migration failure.
        raise


def export_candidate(base, target, destination):
    data = subprocess.run(['git', 'archive', '--format=tar', target], cwd=base,
                          capture_output=True, check=True, timeout=60).stdout
    root = os.path.realpath(destination)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            path = os.path.realpath(os.path.join(root, member.name))
            if not path.startswith(root + os.sep) or not (member.isfile() or member.isdir()):
                raise RuntimeError('候选版本包含不安全的归档路径')
        archive.extractall(destination)
