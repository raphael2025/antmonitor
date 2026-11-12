"""Operational display stays on completed rounds; cleanup preserves installation state."""
import hashlib
import importlib
import json
import sqlite3
from pathlib import Path

import pytest

import db
from conftest import rec

BASE = 1_800_000_000 - 1_800_000_000 % 3600


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv('MINER_CONFIG', str(Path(__file__).with_name('server_config.yaml')))
    server = importlib.import_module('server')
    server._cache.clear()
    monkeypatch.setattr(server, '_check_public_token', lambda request: None)
    monkeypatch.setattr(server, '_current_containers', lambda: [])
    monkeypatch.setattr(server.db, 'count_active', lambda conn: 0)
    return server


def test_538_machines_do_not_disappear_during_long_scan(api, monkeypatch):
    clock = [BASE + 2100]
    monkeypatch.setattr(db.time, 'time', lambda: clock[0])
    raw = [rec('10.0.%d.%d' % (i // 250, i % 250 + 1), worker='A',
               hr=300, power=5500, measured_ts=BASE if i < 537 else BASE + 1799)
           for i in range(538)]
    meta = {'scan_id': 42, 'ts': BASE + 1800, 'started_ts': BASE, 'kind': 'full'}
    monkeypatch.setattr(api.SVC, 'latest', lambda: (meta, raw))
    monkeypatch.setattr(api.SVC, 'progress', {'running': True, 'done': 2938, 'total': 8200,
                                            'progress_ts': clock[0], 'started': clock[0]})
    for advance in (0, 1800):
        clock[0] += advance
        summary = api.api_summary()
        public = api._build_public_summary()
        assert (summary['online'], summary['unknown'], summary['total']) == (538, 0, 538)
        assert summary['total_hashrate_th'] == public['total_hashrate_ths'] == 161400
        assert summary['total_power_kw'] == public['total_power_kw'] == 2959
        assert public['customer_hashrates'][0]['hashrate_ths'] == 161400
        assert public['scan_ts'] == meta['ts']
        assert public['scan_started_ts'] == BASE
        assert public['measurement_ts'] == BASE
        assert public['scanning'] is True
        assert public['scan_progress_ts'] == BASE + 2100  # never refreshed by reading the API
        assert raw[0]['measured_ts'] == BASE
        assert not db.record_freshness(raw[0], meta['ts'], clock[0])  # action guard stays strict


def _save(conn, monkeypatch, start, end, value=300):
    monkeypatch.setattr(db.time, 'time', lambda: BASE + end)
    return db.save_scan(conn, 'full', [rec('10.0.0.1', worker='A', hr=value,
                                         measured_ts=BASE + start)], started_ts=BASE + start)


def test_running_scan_intervals_do_not_create_artificial_history_gaps(conn, monkeypatch):
    _save(conn, monkeypatch, 0, 0)
    _save(conn, monkeypatch, 300, 1800)
    _save(conn, monkeypatch, 2100, 3600)
    r = db.customer_report(conn, 1)[0]
    assert r['power_kwh'] == 3
    assert r['delivered_th_h'] == 300
    assert r['avg_hashrate_ths'] == 300
    assert r['uptime_pct'] == 100
    assert r['hashrate_ths'] == 300
    assert r['hashrate_ts'] == BASE + 2100
    assert r['legacy_power_kwh'] is None


def test_long_actual_collection_outage_is_not_filled(conn, monkeypatch):
    _save(conn, monkeypatch, 0, 0)
    _save(conn, monkeypatch, 3300, 3600)
    r = db.customer_report(conn, 1)[0]
    assert r['power_kwh'] == .75
    assert r['delivered_th_h'] == 75
    assert r['power_observed_hours'] == .25


def test_open_long_scan_hour_is_recomputed_after_it_completes(conn, monkeypatch):
    _save(conn, monkeypatch, 0, 0)
    monkeypatch.setattr(db.time, 'time', lambda: BASE + 3600)
    assert db.customer_report(conn, 1)[0]['power_kwh'] == .75
    # The next round began on schedule, but takes two hours. Reading the report
    # while it runs must not seal an incomplete archive forever.
    _save(conn, monkeypatch, 300, 7200)
    r = db.customer_report(conn, 2)[0]
    assert r['power_kwh'] == 6
    assert r['delivered_th_h'] == 600
    assert r['avg_hashrate_ths'] == 300


def test_first_completed_scan_does_not_backfill_before_completion(conn, monkeypatch):
    _save(conn, monkeypatch, 0, 1800)
    _save(conn, monkeypatch, 2100, 3600)
    r = db.customer_report(conn, 1)[0]
    assert r['power_kwh'] == 1.5
    assert r['delivered_th_h'] == 150


def test_cleanup_preserves_configs_roster_controls_and_valid_statistics(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', db._DB_PATH)
    path = tmp_path / 'monitor.db'
    config_files = {
        'config.yaml': 'scan:\n  username: private-user\n  password: private-password\n',
        'segments.json': '["172.16.13"]',
        'settings.json': '{"cloud":{"url":"https://example.invalid","token":"preserve"}}',
        'cloud_site_id.txt': 'st-preserve',
    }
    for name, content in config_files.items():
        (tmp_path / name).write_text(content)
    before = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in config_files}
    c = db.init_db(str(path))
    try:
        db.upsert_known_miners(c, [rec('10.0.0.1', worker='customer-A')], BASE)
        db.set_machine_state(c, ['10.0.0.1'], 'repair')
        c.execute("INSERT INTO command_log(ts,user,action,ip,ok,msg) VALUES(?,?,?,?,?,?)",
                  (BASE, 'admin', 'reboot', '10.0.0.1', 1, 'keep audit'))
        c.execute("INSERT INTO zero_reboot_state VALUES(?,?,?,?)", ('10.0.0.1', BASE, 2, BASE))
        c.execute("INSERT INTO alerts(ts,ip,type,severity,detail) VALUES(?,?,?,?,?)",
                  (BASE, '10.0.0.1', 'overheat', 'crit', 'keep interlock'))
        c.execute("INSERT INTO worker_hourly VALUES(?,?,?,?,?,?,?)", (BASE, 'legacy-only', 9, 9, 9, 999, 999))
        c.execute("INSERT INTO customer_metrics_hourly VALUES(?,?)", (BASE, 3600))
        c.execute("INSERT INTO worker_metrics_hourly VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (BASE, 'customer-A', 300, 0, 3, 0, 0, 3600, 3600, 1))
        c.execute("INSERT INTO scans(ts,kind,total,online,offline,total_hr) VALUES(?,?,?,?,?,?)",
                  (BASE - 3600, 'full', 1, 1, 0, 999))
        old_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("INSERT INTO snapshots(scan_id,ip,status,worker,hr_rt) VALUES(?,?,?,?,?)",
                  (old_id, '10.0.0.1', 'online', 'legacy-only', 999))
        c.execute("DELETE FROM meta WHERE k='statistics_cleanup_v1'")
        c.commit()
        _save(c, monkeypatch, 0, 0)
        roster = dict(c.execute('SELECT * FROM known_miners').fetchone())
    finally:
        c.close()
        db.close_readers()
    c = db.init_db(str(path))
    try:
        assert c.execute('SELECT COUNT(*) FROM worker_hourly').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM scans WHERE unknown IS NULL').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM worker_metrics_hourly').fetchone()[0] == 1
        assert dict(c.execute('SELECT * FROM known_miners').fetchone()) == roster
        assert c.execute('SELECT COUNT(*) FROM command_log').fetchone()[0] == 1
        assert c.execute('SELECT attempts FROM zero_reboot_state').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM alerts').fetchone()[0] == 1
        assert db.meta_get(c, 'statistics_cleanup_v1')
        backups = list((tmp_path / 'backups').glob('*.db'))
        assert len(backups) == 1
        backup_digest = hashlib.sha256(backups[0].read_bytes()).hexdigest()
        with sqlite3.connect(str(backups[0])) as backup:
            assert backup.execute('SELECT COUNT(*) FROM worker_hourly').fetchone()[0] == 1
        c.close()
        db.close_readers()
        c = db.init_db(str(path))
        assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == backup_digest
        assert len(list((tmp_path / 'backups').glob('*.db'))) == 1
    finally:
        c.close()
        db.close_readers()
    assert before == {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in config_files}


def test_backup_failure_does_not_delete_any_statistics(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', db._DB_PATH)
    path = tmp_path / 'retained.db'
    c = db.init_db(str(path))
    try:
        c.execute("DELETE FROM meta WHERE k='statistics_cleanup_v1'")
        c.execute("INSERT INTO worker_hourly VALUES(?,?,?,?,?,?,?)", (BASE, 'A', 1, 1, 1, 300, 3))
        c.commit()
        def fail(*args):
            raise OSError('disk write failure')
        monkeypatch.setattr(db.os, 'replace', fail)
        with pytest.raises(OSError):
            db._cleanup_legacy_statistics(c, str(path))
        assert c.execute('SELECT COUNT(*) FROM worker_hourly').fetchone()[0] == 1
        assert db.meta_get(c, 'statistics_cleanup_v1') is None
        assert list((tmp_path / 'backups').glob('*.db')) == []
    finally:
        c.close()
        db.close_readers()


def test_cleanup_deletes_and_marker_are_one_transaction(conn):
    conn.execute("DELETE FROM meta WHERE k='statistics_cleanup_v1'")
    conn.execute("INSERT INTO scans(ts,kind,total,online,offline,total_hr) VALUES(?,?,?,?,?,?)",
                 (BASE, 'quick', 1, 1, 0, 300))
    conn.execute("INSERT INTO worker_hourly VALUES(?,?,?,?,?,?,?)", (BASE, 'A', 1, 1, 1, 300, 3))
    conn.execute("CREATE TRIGGER stop_cleanup BEFORE DELETE ON worker_hourly BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.Error):
        db._cleanup_legacy_statistics(conn, ':memory:')
    assert conn.execute('SELECT COUNT(*) FROM scans WHERE unknown IS NULL').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM worker_hourly').fetchone()[0] == 1
    assert db.meta_get(conn, 'statistics_cleanup_v1') is None
