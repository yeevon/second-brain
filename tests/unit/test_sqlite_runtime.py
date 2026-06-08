"""Unit tests for SQLiteRuntime: WAL config, version check, migrations, shutdown."""
import queue as queue_module
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from secondbrain import sqlite_runtime
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
    """After startup failure conn.close() must be called explicitly.

    sqlite3.Connection does not support setting arbitrary instance attributes,
    so we wrap the connection in a proxy that intercepts close().
    """
    db = tmp_path / "test.sqlite3"
    close_calls: list[str] = []
    real_connect = sqlite3.connect

    class _TrackingConn:
        """Thin proxy that records close() calls while forwarding everything else."""

        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def close(self):
            close_calls.append("close")
            return self._conn.close()

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            setattr(self._conn, name, value)

    def tracking_connect(path, **kwargs):
        return _TrackingConn(real_connect(path, **kwargs))

    with patch("sqlite3.connect", tracking_connect):
        with patch(
            "secondbrain.sqlite_runtime.run_migrations",
            side_effect=RuntimeError("migration boom"),
        ):
            with pytest.raises(RuntimeError, match="SQLite runtime failed to start"):
                SQLiteRuntime(db, busy_timeout_ms=500)

    assert close_calls == ["close"], "conn.close() must be called after startup failure"


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

class _PausingQueue(queue_module.Queue):
    """Intercepts the first non-sentinel put() and pauses until released.

    This lets a test hold the queue.put() call mid-flight so that close()
    races exactly at the danger window: the job has been accepted but is not
    yet in the queue.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job_put_entered = threading.Event()
        self.release_job_put = threading.Event()
        self._paused_once = False

    def put(self, item, block=True, timeout=None):
        if item is not sqlite_runtime._SENTINEL and not self._paused_once:
            self._paused_once = True
            self.job_put_entered.set()
            assert self.release_job_put.wait(timeout=5.0), "release timed out"
        return super().put(item, block=block, timeout=timeout)


def test_close_cannot_overtake_job_paused_before_queue_insert(tmp_path):
    """Regression: the sentinel must not enter the queue before an accepted job.

    Reproduces the original race window:
      _submit checks _closed (False) and releases _close_lock
      -> close() sets _closed=True and enqueues SENTINEL
      -> _submit enqueues job behind SENTINEL
      -> worker exits before processing job
      -> caller blocks forever on future.result()

    The PausingQueue freezes the job's queue.put() call mid-flight so that
    close() must race exactly at that window.  With the fix, _submit holds
    _close_lock across queue.put(), so close() cannot enqueue SENTINEL until
    _submit has finished.
    """
    paused_queue = _PausingQueue(maxsize=1000)

    with patch("secondbrain.sqlite_runtime.queue.Queue", return_value=paused_queue):
        rt = SQLiteRuntime(tmp_path / "test.sqlite3")

    result: list[int] = []

    submit_thread = threading.Thread(
        target=lambda: result.append(rt.write(lambda conn: 42)),
        daemon=True,
    )
    submit_thread.start()

    # Wait until _submit is inside queue.put() holding _close_lock
    assert paused_queue.job_put_entered.wait(timeout=2.0)

    close_thread = threading.Thread(target=rt.close, daemon=True)
    close_thread.start()

    # Give close_thread time to block on _close_lock (which _submit still holds)
    time.sleep(0.05)
    assert close_thread.is_alive(), (
        "close() must be blocked waiting for _close_lock while _submit holds it; "
        "if this fails the fix is broken"
    )

    # Let _submit finish queue.put() — sentinel must follow the job
    paused_queue.release_job_put.set()

    submit_thread.join(timeout=3.0)
    close_thread.join(timeout=3.0)

    assert result == [42], "accepted job must complete and not be stranded"
    assert not submit_thread.is_alive()
    assert not close_thread.is_alive()


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

    t = threading.Thread(target=lambda: result.append(rt.write(blocking_op)))
    t.start()

    job_running.wait(timeout=2.0)

    close_thread = threading.Thread(target=rt.close)
    close_thread.start()

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

    t1 = threading.Thread(target=lambda: rt.write(job1_op))
    t1.start()

    job1_running.wait(timeout=2.0)

    t2 = threading.Thread(target=lambda: rt.write(job2_op))
    t3 = threading.Thread(target=lambda: rt.write(job3_op))
    t2.start()
    t3.start()

    # Wait long enough for t2/t3 to enqueue their jobs (they block on future.result())
    time.sleep(0.05)

    close_thread = threading.Thread(target=rt.close)
    close_thread.start()

    release_job1.set()

    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    t3.join(timeout=3.0)
    close_thread.join(timeout=3.0)

    assert set(completed) == {1, 2, 3}, f"all queued jobs must complete; got {completed}"
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert not t3.is_alive()
    assert not close_thread.is_alive()


# ---------------------------------------------------------------------------
# Unrelated OperationalError must not trigger retry loop
# ---------------------------------------------------------------------------

def test_runtime_does_not_retry_unrelated_operational_error(tmp_path):
    """An OperationalError that is not a lock error must fail immediately, not be retried."""
    rt = make_runtime(tmp_path, retry_attempts=5, retry_base_delay_ms=0)

    calls = 0

    def malformed_op(conn):
        nonlocal calls
        calls += 1
        conn.execute("SELECT * FROM table_that_does_not_exist")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        rt.read(malformed_op, operation_name="malformed_test_query")

    assert calls == 1, f"unrelated error must fail on first attempt, not retry; got {calls} calls"
    rt.close()


# ---------------------------------------------------------------------------
# Submit after close
# ---------------------------------------------------------------------------

def test_submit_after_close_raises(tmp_path):
    rt = make_runtime(tmp_path)
    rt.close()

    with pytest.raises(RuntimeError, match="closed"):
        rt.read(lambda conn: None)
