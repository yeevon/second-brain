"""Unit tests for SQLiteRuntime: WAL config, version check, migrations, shutdown."""
from unittest.mock import patch
import sqlite3

import pytest

from secondbrain.sqlite_runtime import MIN_SQLITE_VERSION, SQLiteBusyError, SQLiteRuntime


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
    current = sqlite3.sqlite_version_info
    # As long as current version is >= MIN, runtime should start without error
    assert current >= MIN_SQLITE_VERSION
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
    """Patch run_migrations to raise so the runtime startup fails cleanly."""
    with patch(
        "secondbrain.sqlite_runtime.run_migrations",
        side_effect=RuntimeError("migration boom"),
    ):
        with pytest.raises(RuntimeError, match="SQLite runtime failed to start"):
            make_runtime(tmp_path)


# ---------------------------------------------------------------------------
# Safe shutdown — queued jobs drain before close
# ---------------------------------------------------------------------------

def test_runtime_processes_queued_jobs_before_shutdown(tmp_path):
    rt = make_runtime(tmp_path)

    n = 20
    futures = []
    # Submit writes directly via internal _submit to bypass the public Ledger layer
    for i in range(n):
        f = rt.write(
            lambda conn, i=i: conn.execute(
                """
                INSERT INTO captures (
                    capture_id, discord_message_id, discord_channel_id,
                    discord_guild_id, discord_author_id,
                    raw_text, is_sensitive, has_attachments,
                    received_at, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    f"SB-20260607-{i:04d}",
                    str(10000 + i),
                    "200", "300", "400",
                    f"note {i}",
                    "2026-06-07T12:00:00+00:00",
                    "RECEIVED",
                    "2026-06-07T12:00:00+00:00",
                ),
            )
        )

    count = rt.read(lambda conn: conn.execute("SELECT COUNT(*) AS c FROM captures").fetchone()["c"])
    assert count == n

    rt.close()


def test_submit_after_close_raises(tmp_path):
    rt = make_runtime(tmp_path)
    rt.close()

    with pytest.raises(RuntimeError, match="closed"):
        rt.read(lambda conn: None)
