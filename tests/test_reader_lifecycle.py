"""Real-file SQLite readers must survive maintenance and release dead threads."""
import gc
import queue
import sqlite3
import threading

import pytest

import db


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    # Other API fixtures retain an in-memory service across test modules.
    monkeypatch.setattr(db, "_DB_PATH", db._DB_PATH)
    conn = db.init_db(str(tmp_path / "readers.sqlite"))
    yield conn
    db.close_readers()
    conn.close()


def test_sweep_between_reader_acquisition_and_query(file_db):
    ready, resume = threading.Event(), threading.Event()
    outcome = queue.Queue()

    def read():
        reader = db._r(file_db)
        ready.set()
        assert resume.wait(5)
        try:
            outcome.put(reader.execute("SELECT 42 AS answer").fetchone()["answer"])
        except Exception as exc:
            outcome.put(exc)

    thread = threading.Thread(target=read)
    thread.start()
    try:
        assert ready.wait(5)
        db.close_readers()
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive()
    assert outcome.get_nowait() == 42


def test_sweep_preserves_result_until_all_rows_are_consumed(file_db):
    reader = db._r(file_db)
    cursor = reader.execute("SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3")
    assert cursor.fetchone()["n"] == 1
    db.close_readers()
    assert [row["n"] for row in cursor.fetchall()] == [2, 3]
    assert reader.execute("SELECT 4").fetchone()[0] == 4


def test_sweep_during_query_does_not_close_active_connection(file_db, monkeypatch):
    started, resume = threading.Event(), threading.Event()
    original = db.connect
    outcome = queue.Queue()

    def connection(path, query_only=False):
        conn = original(path, query_only=query_only)
        if query_only:
            def paused():
                started.set()
                assert resume.wait(5)
                return 7
            conn.create_function("paused", 0, paused)
        return conn

    monkeypatch.setattr(db, "connect", connection)

    def read():
        try:
            outcome.put(db._r(file_db).execute("SELECT paused()").fetchone()[0])
        except Exception as exc:
            outcome.put(exc)

    worker = threading.Thread(target=read)
    worker.start()
    assert started.wait(5)
    sweeper = threading.Thread(target=db.close_readers)
    sweeper.start()
    try:
        # Maintenance must defer an active connection, not wait on the query.
        sweeper.join(2)
        assert not sweeper.is_alive()
    finally:
        resume.set()
        worker.join(5)
        sweeper.join(5)
    assert outcome.get_nowait() == 7


def test_reader_is_read_only_after_reopening(file_db):
    reader = db._r(file_db)
    db.close_readers()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute("CREATE TABLE forbidden (n INTEGER)")
    assert reader.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0


def test_completed_threads_do_not_leave_reader_handles(file_db, monkeypatch):
    opened = []
    original = db.connect

    def connection(path, query_only=False):
        conn = original(path, query_only=query_only)
        if query_only:
            opened.append(conn)
        return conn

    monkeypatch.setattr(db, "connect", connection)
    for _ in range(20):
        thread = threading.Thread(target=lambda: db._r(file_db).execute("SELECT 1").fetchone())
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
    gc.collect()
    assert len(opened) == 20
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_existing_reader_remains_bound_to_its_database(file_db, tmp_path):
    file_db.execute("INSERT INTO meta(k,v) VALUES('reader_test','first')")
    file_db.commit()
    old_reader = db._r(file_db)
    second = db.init_db(str(tmp_path / "second.sqlite"))
    try:
        assert old_reader.execute("SELECT v FROM meta WHERE k='reader_test'").fetchone()[0] == "first"
        assert db._r(second).execute("SELECT v FROM meta WHERE k='reader_test'").fetchone() is None
    finally:
        db.close_readers()
        second.close()


@pytest.mark.parametrize("fail_query", [False, True])
def test_active_reader_retires_after_success_or_failure_and_reopens(
        file_db, monkeypatch, fail_query):
    started, release = threading.Event(), threading.Event()
    first_done, reuse = threading.Event(), threading.Event()
    original = db.connect
    opened = []
    outcome = queue.Queue()

    def connection(path, query_only=False):
        conn = original(path, query_only=query_only)
        if query_only:
            opened.append(conn)

            def paused():
                started.set()
                assert release.wait(5)
                if fail_query:
                    raise ValueError("query failure")
                return 7

            conn.create_function("paused", 0, paused)
        return conn

    monkeypatch.setattr(db, "connect", connection)

    def read():
        try:
            reader = db._r(file_db)
            try:
                outcome.put(reader.execute("SELECT paused()").fetchone()[0])
            except sqlite3.OperationalError as exc:
                outcome.put(str(exc))
            first_done.set()
            assert reuse.wait(5)
            outcome.put(reader.execute("SELECT 11").fetchone()[0])
            try:
                reader.execute("CREATE TABLE forbidden_after_retire (n INTEGER)")
            except sqlite3.OperationalError as exc:
                outcome.put(str(exc))
        except Exception as exc:
            outcome.put(exc)

    worker = threading.Thread(target=read)
    worker.start()
    try:
        assert started.wait(5)
        db.close_readers()
        release.set()
        assert first_done.wait(5)
        # The worker is still alive and holds its reader: finalization cannot
        # conceal a failure to retire the actual connection after this query.
        assert worker.is_alive()
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")
    finally:
        release.set()
        reuse.set()
        worker.join(5)
    assert not worker.is_alive()
    first = outcome.get_nowait()
    if fail_query:
        assert "user-defined function raised exception" in first
    else:
        assert first == 7
    assert outcome.get_nowait() == 11
    assert "readonly" in outcome.get_nowait()
    assert len(opened) == 2
