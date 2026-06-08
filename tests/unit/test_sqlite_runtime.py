"""Unit tests for SQLiteRuntime: WAL config, version check, migrations, shutdown."""
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from secondbrain.migrations import Migration, _apply, run_migrations
from secondbrain.sqlite_runtime import (
    MIN_SQLITE_VERSION,
    SQLiteBusyError,
    SQLiteRuntime,
    _is_transient_lock_error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_runtime(tmp_path, **kwargs) -> SQLiteRuntime:
    defaults = dict(
        busy_timeout_ms=500,
        retry_attempts=5,
        retry_base_delay_ms=10,
        job_queue_maxsize=1000,
    )
    defaults.update(kwargs)
    return SQLiteRuntime(tmp_path / "test.sqlite3", **defaults)


# ---------------------------------------------------------------------------
# WAL + PRAGMA verification
# ---------------------------------------------------------------------------

def test_runtime_enables_wal_mode(tmp_path):
    rt = make_runtime(tmp_path, busy_timeout_ms=250)

    journal_mode = rt.read(lambda conn: conn.execute("PRAGMA journal_mode").fetchone()[0])
    assert journal_mode == "wal"
    rt.close()


def test_runtime_enables_foreign_keys(tmp_path):
    rt = make_runtime(tmp_path)

    foreign_keys = rt.read(lambda conn: conn.execute("PRAGMA foreign_keys").fetchone()[0])
    assert foreign_keys == 1
    rt.close()


def test_runtime_sets_busy_timeout(tmp_path):
    rt = make_runtime(tmp_path, busy_timeout_ms=750)

    busy_timeout = rt.read(lambda conn: conn.execute("PRAGMA busy_timeout").fetchone()[0])
    assert busy_timeout == 750
    rt.close()


# ---------------------------------------------------------------------------
# SQLite version check
# ---------------------------------------------------------------------------

def test_sqlite_runtime_rejects_version_below_minimum(tmp_path):
    below_min = (MIN_SQLITE_VERSION[0], MIN_SQLITE_VERSION[1], MIN_SQLITE_VERSION[2] - 1)

    with patch("secondbrain.sqlite_runtime.sqlite3.sqlite_version_info", below_min):
        with pytest.raises(RuntimeError, match="minimum required"):
            make_runtime(tmp_path)


def test_sqlite_runtime_accepts_supported_version(tmp_path):
    assert sqlite3.sqlite_version_info >= MIN_SQLITE_VERSION
    rt = make_runtime(tmp_path)
    rt.close()


# ---------------------------------------------------------------------------
# Migration integration (run inside runtime startup)
# ---------------------------------------------------------------------------

def test_fresh_database_applies_initial_migration(tmp_path):
    rt = make_runtime(tmp_path)

    rows = rt.read(
        lambda conn: conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    )
    assert len(rows) >= 1
    assert rows[0]["version"] == 1
    rt.close()


def test_reopening_runtime_does_not_reapply_migration(tmp_path):
    db = tmp_path / "test.sqlite3"
    rt1 = SQLiteRuntime(db, busy_timeout_ms=500)
    rt1.close()

    rt2 = SQLiteRuntime(db, busy_timeout_ms=500)
    count = rt2.read(
        lambda conn: conn.execute(
            "SELECT COUNT(*) AS c FROM schema_migrations WHERE version = 1"
        ).fetchone()["c"]
    )
    assert count == 1
    rt2.close()


def test_failed_migration_prevents_runtime_startup(tmp_path):
    with patch(
        "secondbrain.sqlite_runtime.run_migrations",
        side_effect=RuntimeError("migration boom"),
    ):
        with pytest.raises(RuntimeError, match="SQLite runtime failed to start"):
            make_runtime(tmp_path)


def test_failed_startup_releases_sqlite_connection(tmp_path):
    """After startup failure the worker-owned connection must be explicitly closed."""
    db = tmp_path / "test.sqlite3"

    with patch(
        "secondbrain.sqlite_runtime.run_migrations",
        side_effect=RuntimeError("migration boom"),
    ):
        with pytest.raises(RuntimeError, match="SQLite runtime failed to start"):
            SQLiteRuntime(db, busy_timeout_ms=500)

    # If the connection was leaked, BEGIN IMMEDIATE would time-out or block.
    check = sqlite3.connect(str(db), isolation_level=None)
    try:
        check.execute("BEGIN IMMEDIATE")
        check.commit()
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Migration atomicity
# ---------------------------------------------------------------------------

def test_partial_migration_failure_rolls_back_schema_and_version_record(tmp_path):
    """A mid-migration failure must leave no partial schema and no version record."""
    db = tmp_path / "test.sqlite3"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)

    bad = Migration(
        version=999,
        name="bad_migration",
        statements=(
            "CREATE TABLE partial_table (id INTEGER PRIMARY KEY)",
            "SELECT * FROM table_that_does_not_exist",
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        _apply(conn, bad)

    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    ]
    assert "partial_table" not in tables, "partial DDL must be rolled back"

    count = conn.execute(
        "SELECT COUNT(*) AS c FROM schema_migrations WHERE version = 999"
    ).fetchone()["c"]
    assert count == 0, "failed migration version must not be recorded"

    conn.close()


def test_concurrent_runtime_startup_applies_migration_once(tmp_path):
    """Two runtimes started at the same time must apply migration 001 exactly once."""
    db = tmp_path / "shared.sqlite3"
    started: list[SQLiteRuntime] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2, timeout=5.0)

    def start_runtime():
        try:
            barrier.wait()
            rt = SQLiteRuntime(db, busy_timeout_ms=2000)
            started.append(rt)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start_runtime) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    for rt in started:
        rt.close()

    assert errors == [], f"unexpected startup errors: {errors}"
    assert len(started) == 2

    check = sqlite3.connect(str(db))
    count = check.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
    ).fetchone()[0]
    check.close()

    assert count == 1, f"expected exactly 1 migration record, got {count}"


# ---------------------------------------------------------------------------
# Transient lock error classification
# ---------------------------------------------------------------------------

def test_is_transient_lock_error_with_sqlite_busy_error_code():
    exc = sqlite3.OperationalError("some lock message")
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    assert _is_transient_lock_error(exc)


def test_is_transient_lock_error_with_sqlite_locked_error_code():
    exc = sqlite3.OperationalError("some lock message")
    exc.sqlite_errorcode = sqlite3.SQLITE_LOCKED
    assert _is_transient_lock_error(exc)


def test_is_transient_lock_error_with_unrelated_error_code():
    exc = sqlite3.OperationalError("no such table: foo")
    exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT
    assert not _is_transient_lock_error(exc)


def test_is_transient_lock_error_message_fallback_database_locked():
    exc = sqlite3.OperationalError("database is locked")
    assert _is_transient_lock_error(exc)


def test_is_transient_lock_error_message_fallback_schema_locked():
    exc = sqlite3.OperationalError("database schema is locked")
    assert _is_transient_lock_error(exc)


def test_is_transient_lock_error_message_fallback_unrelated():
    exc = sqlite3.OperationalError("no such table: foo")
    assert not _is_transient_lock_error(exc)


# ---------------------------------------------------------------------------
# Shutdown — sentinel cannot overtake an accepted job
# ---------------------------------------------------------------------------

def test_submit_racing_close_never_strands_accepted_job(tmp_path):
    """A job accepted by _submit must complete even if close() is called while it runs."""
    rt = make_runtime(tmp_path, job_queue_maxsize=1000)

    job_running = threading.Event()
    release_job = threading.Event()
    result: list[int] = []

    def blocking_op(conn):
        job_running.set()
        release_job.wait(timeout=5.0)
        return 42

    def submit_thread():
        result.append(rt.write(blocking_op))

    t = threading.Thread(target=submit_thread)
    t.start()

    job_running.wait(timeout=2.0)

    # close() is now racing with the in-flight job
    close_thread = threading.Thread(target=rt.close)
    close_thread.start()

    # Release the in-flight job after close() has been requested
    time.sleep(0.02)
    release_job.set()

    t.join(timeout=3.0)
    close_thread.join(timeout=3.0)

    assert result == [42], "accepted job must complete, not be stranded"
    assert not t.is_alive()
    assert not close_thread.is_alive()


def test_close_drains_already_queued_jobs_before_sentinel(tmp_path):
    """Jobs queued while the worker is busy must all complete before the runtime exits."""
    rt = make_runtime(tmp_path, job_queue_maxsize=1000)

    job1_running = threading.Event()
    release_job1 = threading.Event()
    completed: list[int] = []

    def job1_op(conn):
        job1_running.set()
        release_job1.wait(timeout=5.0)
        completed.append(1)
        return 1

    def job2_op(conn):
        completed.append(2)
        return 2

    def job3_op(conn):
        completed.append(3)
        return 3

    # Start job1 — it will block the worker thread
    t1 = threading.Thread(target=lambda: rt.write(job1_op))
    t1.start()

    job1_running.wait(timeout=2.0)

    # Job2 and job3 are submitted while the worker is stuck in job1
    t2 = threading.Thread(target=lambda: rt.write(job2_op))
    t3 = threading.Thread(target=lambda: rt.write(job3_op))
    t2.start()
    t3.start()

    # Wait long enough for t2/t3 to enqueue their jobs (they block on future.result())
    time.sleep(0.05)

    # Request shutdown — with the fix, SENTINEL goes in after job2 and job3
    close_thread = threading.Thread(target=rt.close)
    close_thread.start()

    # Release job1 so the worker can continue
    release_job1.set()

    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    t3.join(timeout=3.0)
    close_thread.join(timeout=3.0)

    assert set(completed) == {1, 2, 3}, f"all queued jobs must complete; got {completed}"
    assert not close_thread.is_alive()


# ---------------------------------------------------------------------------
# Submit after close
# ---------------------------------------------------------------------------

def test_submit_after_close_raises(tmp_path):
    rt = make_runtime(tmp_path)
    rt.close()

    with pytest.raises(RuntimeError, match="closed"):
        rt.read(lambda conn: None)
