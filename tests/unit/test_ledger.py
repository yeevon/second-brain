from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import re
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from secondbrain.capture_models import (
    COMPLETE,
    DELIVERY_CLASSIFYING,
    DELIVERY_FAILED,
    DELIVERY_FORWARDED,
    FORWARDING,
    NOT_APPLICABLE,
    PENDING_FORWARD,
    RETRY_WAIT,
)
from secondbrain.ledger import (
    ALL_STATUSES,
    CLASSIFYING,
    FAILED,
    FILED,
    FORWARDED,
    INBOX,
    RECEIVED,
    REJECTED_SENSITIVE,
    TERMINAL_STATUSES,
    Ledger,
    calculate_retry_delay_seconds,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ledger(tmp_path, **overrides):
    defaults = dict(
        sqlite_busy_timeout_ms=500,
        sqlite_busy_retry_attempts=5,
        sqlite_busy_retry_base_delay_ms=10,
        sqlite_job_queue_maxsize=10000,
    )
    defaults.update(overrides)
    return Ledger(tmp_path / "ledger.sqlite3", SimpleNamespace(**defaults))


def table_names(ledger: Ledger) -> list[str]:
    return ledger._runtime.read(
        lambda conn: [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        ]
    )


def table_columns(ledger: Ledger, table_name: str) -> list[str]:
    return ledger._runtime.read(
        lambda conn: [
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]
    )


def index_columns(ledger: Ledger, index_name: str) -> list[str]:
    return ledger._runtime.read(
        lambda conn: [
            row["name"]
            for row in conn.execute(f"PRAGMA index_info({index_name})").fetchall()
        ]
    )


def unique_index_exists(ledger: Ledger, table_name: str, columns: list[str]) -> bool:
    def _check(conn):
        indexes = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
        for index in indexes:
            if not index["unique"]:
                continue
            cols = [
                row["name"]
                for row in conn.execute(f"PRAGMA index_info({index['name']})").fetchall()
            ]
            if cols == columns:
                return True
        return False
    return ledger._runtime.read(_check)


def insert_accepted_capture(ledger: Ledger, **kwargs):
    return ledger.insert_accepted_capture(**kwargs).capture


def insert_sensitive_rejection(ledger: Ledger, **kwargs):
    return ledger.insert_sensitive_rejection(**kwargs).capture


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

def test_capture_status_constants_match_mvp_contract():
    assert ALL_STATUSES == {
        RECEIVED,
        FORWARDED,
        CLASSIFYING,
        FILED,
        INBOX,
        REJECTED_SENSITIVE,
        FAILED,
    }
    assert TERMINAL_STATUSES == {FILED, INBOX, REJECTED_SENSITIVE, FAILED}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_minimal_schema_tables_columns_and_indexes_match_mvp(tmp_path):
    ledger = make_ledger(tmp_path)

    assert set(table_names(ledger)) >= {"captures", "capture_events", "system_state"}
    assert table_columns(ledger, "captures") == [
        "id",
        "capture_id",
        "discord_message_id",
        "discord_channel_id",
        "discord_guild_id",
        "discord_author_id",
        "raw_text",
        "redacted_text",
        "is_sensitive",
        "sensitivity_flags",
        "has_attachments",
        "attachment_metadata_json",
        "received_at",
        "status",
        "classification_json",
        "derived_note_path",
        "receipt_message_id",
        "last_error",
        "updated_at",
        # Added by migration 002
        "delivery_status",
        "delivery_attempts",
        "processing_lease_until",
        "next_attempt_at",
        # Added by migration 003
        "delivery_commit_hash",
        "delivery_reason_type",
        # Added by migration 004
        "retry_attempts",
        # Added by migration 005
        "clarification_status",
        "clarification_question",
    ]
    assert table_columns(ledger, "capture_events") == [
        "id",
        "capture_id",
        "event_type",
        "event_payload_json",
        "created_at",
    ]
    assert table_columns(ledger, "system_state") == ["key", "value", "updated_at"]

    assert index_columns(ledger, "idx_captures_status") == ["status"]
    assert index_columns(ledger, "idx_capture_events_capture_id") == ["capture_id"]
    assert index_columns(ledger, "idx_captures_delivery_due") == ["delivery_status", "next_attempt_at"]
    assert index_columns(ledger, "idx_captures_processing_lease") == ["delivery_status", "processing_lease_until"]
    assert unique_index_exists(ledger, "captures", ["capture_id"])
    assert unique_index_exists(ledger, "captures", ["discord_message_id"])

    ledger.close()


def test_minimal_schema_foreign_keys_and_sensitive_check_constraint(tmp_path):
    ledger = make_ledger(tmp_path)

    foreign_keys = ledger._runtime.read(
        lambda conn: conn.execute("PRAGMA foreign_key_list(capture_events)").fetchall()
    )
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["from"] == "capture_id"
    assert foreign_keys[0]["table"] == "captures"
    assert foreign_keys[0]["to"] == "capture_id"

    def _try_bad_insert(conn):
        try:
            conn.execute(
                """
                INSERT INTO captures (
                    capture_id,
                    discord_message_id,
                    discord_channel_id,
                    discord_guild_id,
                    discord_author_id,
                    raw_text,
                    is_sensitive,
                    received_at,
                    status,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    "SB-20260607-BAD1",
                    "1001",
                    "200",
                    "300",
                    "400",
                    "secret text",
                    "2026-06-07T12:00:00+00:00",
                    RECEIVED,
                    "2026-06-07T12:00:00+00:00",
                ),
            )
        except sqlite3.IntegrityError:
            return "rejected"
        return "accepted"

    result = ledger._runtime.write(_try_bad_insert)
    assert result == "rejected", "sensitive captures must not store raw_text without redacted_text"

    ledger.close()


# ---------------------------------------------------------------------------
# Versioned migrations
# ---------------------------------------------------------------------------

def test_schema_migrations_table_is_populated_after_startup(tmp_path):
    ledger = make_ledger(tmp_path)

    rows = ledger._runtime.read(
        lambda conn: conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    )
    assert len(rows) >= 1
    assert rows[0]["version"] == 1
    assert rows[0]["name"] == "initial_mvp_schema"
    ledger.close()


def test_reopening_database_does_not_reapply_migration(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.close()

    # Reopen — migration should not run again
    ledger2 = make_ledger(tmp_path)
    count = ledger2._runtime.read(
        lambda conn: conn.execute(
            "SELECT COUNT(*) AS c FROM schema_migrations WHERE version = 1"
        ).fetchone()["c"]
    )
    assert count == 1
    ledger2.close()


def test_existing_mvp_database_is_adopted_without_data_loss(tmp_path):
    db_path = tmp_path / "ledger.sqlite3"

    # Simulate an MVP database written before migrations existed
    raw = sqlite3.connect(str(db_path))
    raw.execute("PRAGMA foreign_keys = ON")
    raw.executescript("""
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL UNIQUE,
            discord_message_id TEXT NOT NULL UNIQUE,
            discord_channel_id TEXT NOT NULL,
            discord_guild_id TEXT NOT NULL,
            discord_author_id TEXT NOT NULL,
            raw_text TEXT,
            redacted_text TEXT,
            is_sensitive INTEGER NOT NULL DEFAULT 0,
            sensitivity_flags TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            attachment_metadata_json TEXT,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL,
            classification_json TEXT,
            derived_note_path TEXT,
            receipt_message_id TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            CHECK (
                (is_sensitive = 0 AND raw_text IS NOT NULL AND (raw_text != '' OR has_attachments = 1))
                OR (is_sensitive = 1 AND raw_text IS NULL AND redacted_text IS NOT NULL)
            )
        );
        CREATE TABLE IF NOT EXISTS capture_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_payload_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
        );
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_captures_status ON captures(status);
        CREATE INDEX IF NOT EXISTS idx_capture_events_capture_id ON capture_events(capture_id);
    """)
    raw.execute("""
        INSERT INTO captures (
            capture_id, discord_message_id, discord_channel_id,
            discord_guild_id, discord_author_id,
            raw_text, is_sensitive, has_attachments,
            received_at, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
    """, (
        "SB-20260607-0001", "1001", "200", "300", "400",
        "Existing capture",
        "2026-06-07T12:00:00+00:00", RECEIVED, "2026-06-07T12:00:00+00:00",
    ))
    raw.commit()
    raw.close()

    # Open with the migration-aware Ledger — existing row must survive
    ledger = make_ledger(tmp_path)
    capture = ledger.get_capture("SB-20260607-0001")
    assert capture.raw_text == "Existing capture"
    assert capture.status == RECEIVED

    migration_count = ledger._runtime.read(
        lambda conn: conn.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
    )
    assert migration_count == 7  # 001–005 original + 006 vault_update_proposals + 007 approval_message
    # Verify delivery columns were added and existing row has correct default
    capture = ledger.get_capture("SB-20260607-0001")
    assert capture.delivery_status == "PENDING_FORWARD"
    assert capture.delivery_attempts == 0
    assert capture.delivery_commit_hash is None
    assert capture.delivery_reason_type is None
    ledger.close()


# ---------------------------------------------------------------------------
# Basic insert / read behavior
# ---------------------------------------------------------------------------

def test_insert_accepted_capture_creates_received_record(tmp_path):
    ledger = make_ledger(tmp_path)

    capture = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
        received_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
    )

    assert capture.capture_id == "SB-20260607-0001"
    assert capture.discord_message_id == "1001"
    assert capture.status == RECEIVED
    assert capture.raw_text == "Review reconnect handling."
    assert capture.is_sensitive is False
    assert ledger.status_counts() == {RECEIVED: 1}
    ledger.close()


def test_duplicate_discord_message_id_returns_existing_capture(tmp_path):
    ledger = make_ledger(tmp_path)

    first = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="First text.",
        received_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
    )
    second = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Different retry text.",
        received_at=datetime(2026, 6, 7, 12, 1, tzinfo=UTC),
    )

    assert second.capture_id == first.capture_id
    assert second.raw_text == "First text."
    assert ledger.status_counts() == {RECEIVED: 1}
    ledger.close()


def test_duplicate_discord_message_id_for_sensitive_rejection_returns_existing_capture(tmp_path):
    ledger = make_ledger(tmp_path)

    first = insert_sensitive_rejection(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        redacted_text="[REDACTED]",
        sensitivity_flags=("password_assignment",),
        received_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
    )
    second = insert_sensitive_rejection(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        redacted_text="different retry text",
        sensitivity_flags=("api_key_assignment",),
        received_at=datetime(2026, 6, 7, 12, 1, tzinfo=UTC),
    )

    assert second.capture_id == first.capture_id
    assert second.redacted_text == "[REDACTED]"
    assert ledger.status_counts() == {REJECTED_SENSITIVE: 1}
    ledger.close()


def test_capture_id_counter_is_per_day(tmp_path):
    ledger = make_ledger(tmp_path)

    first = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="One.",
        received_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
    )
    second = insert_accepted_capture(ledger,
        discord_message_id="1002",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Two.",
        received_at=datetime(2026, 6, 7, 12, 1, tzinfo=UTC),
    )
    next_day = insert_accepted_capture(ledger,
        discord_message_id="1003",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Three.",
        received_at=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
    )

    assert first.capture_id == "SB-20260607-0001"
    assert second.capture_id == "SB-20260607-0002"
    assert next_day.capture_id == "SB-20260608-0001"
    ledger.close()


def test_capture_id_format_is_human_readable(tmp_path):
    ledger = make_ledger(tmp_path)

    capture = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
        received_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
    )

    assert re.fullmatch(r"SB-\d{8}-\d{4}", capture.capture_id)
    ledger.close()


def test_insert_sensitive_rejection_stores_redacted_text_only(tmp_path):
    ledger = make_ledger(tmp_path)

    capture = insert_sensitive_rejection(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        redacted_text="password=[REDACTED]",
        sensitivity_flags=("password_assignment",),
        received_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
    )

    assert capture.status == REJECTED_SENSITIVE
    assert capture.raw_text is None
    assert capture.redacted_text == "password=[REDACTED]"
    assert capture.is_sensitive is True
    ledger.close()


def test_set_receipt_message_id_updates_capture(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
    )

    ledger.set_receipt_message_id(capture.capture_id, "9001")

    updated = ledger.get_capture(capture.capture_id)
    assert updated.receipt_message_id == "9001"
    ledger.close()


def test_mark_classifying_transitions_only_received_rows(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
    )

    assert ledger.mark_classifying(capture.capture_id) is True
    assert ledger.get_capture(capture.capture_id).status == CLASSIFYING
    assert ledger.mark_classifying(capture.capture_id) is False
    ledger.close()


def test_update_capture_rejects_unknown_status(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
    )

    try:
        ledger.update_capture(capture.capture_id, status="SORT_OF_DONE")
    except ValueError as exc:
        assert str(exc) == "unknown capture status: SORT_OF_DONE"
    else:
        raise AssertionError("unknown capture status should be rejected")

    assert ledger.get_capture(capture.capture_id).status == RECEIVED
    ledger.close()


def test_enqueueable_capture_ids_include_received_forwarded_and_classifying(tmp_path):
    ledger = make_ledger(tmp_path)
    first = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="One.",
    )
    second = insert_accepted_capture(ledger,
        discord_message_id="1002",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Two.",
    )
    third = insert_accepted_capture(ledger,
        discord_message_id="1003",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Three.",
    )
    ledger.transition_capture(
        second.capture_id,
        from_statuses={RECEIVED},
        to_status=FORWARDED,
        event_type="CAPTURE_FORWARDED",
    )
    ledger.mark_classifying(third.capture_id)

    assert ledger.enqueueable_capture_ids() == [first.capture_id, second.capture_id, third.capture_id]
    ledger.close()


def test_reset_classifying_to_received_requeues_stale_work(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
    )
    ledger.mark_classifying(capture.capture_id)

    assert ledger.reset_classifying_to_received() == 1
    assert ledger.get_capture(capture.capture_id).status == RECEIVED
    assert ledger.reset_classifying_to_received() == 0
    ledger.close()


def test_system_state_round_trips(tmp_path):
    ledger = make_ledger(tmp_path)

    assert ledger.get_system_state("last_message") is None

    ledger.set_system_state("last_message", "123")
    ledger.set_system_state("last_message", "456")

    assert ledger.get_system_state("last_message") == "456"
    ledger.close()


# ---------------------------------------------------------------------------
# Concurrency — serialized writes (250 rapid inserts)
# ---------------------------------------------------------------------------

def test_serialized_concurrent_inserts_do_not_drop_captures(tmp_path):
    ledger = make_ledger(tmp_path)
    n = 250
    results = []

    def _insert(i):
        return ledger.insert_accepted_capture(
            discord_message_id=str(10000 + i),
            discord_channel_id="200",
            discord_guild_id="300",
            discord_author_id="400",
            raw_text=f"Rapid note {i}",
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(_insert, i) for i in range(n)]
        results = [f.result() for f in futures]

    created = [r for r in results if r.created]
    all_ids = [r.capture.capture_id for r in results]

    assert len(created) == n
    assert len(set(all_ids)) == n
    assert ledger.total_captures() == n

    counts = ledger.status_counts()
    assert counts.get(RECEIVED, 0) == n

    event_count = ledger._runtime.read(
        lambda conn: conn.execute(
            "SELECT COUNT(*) AS c FROM capture_events WHERE event_type = 'CAPTURE_RECEIVED'"
        ).fetchone()["c"]
    )
    assert event_count == n

    ledger.close()


# ---------------------------------------------------------------------------
# Concurrency — same-row transition race (mark_classifying)
# ---------------------------------------------------------------------------

def test_concurrent_mark_classifying_transitions_exactly_once(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Race me.",
    )

    transition_results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(ledger.mark_classifying, capture.capture_id) for _ in range(8)]
        transition_results = [f.result() for f in futures]

    assert transition_results.count(True) == 1
    assert transition_results.count(False) == 7
    assert ledger.get_capture(capture.capture_id).status == CLASSIFYING

    events = ledger._runtime.read(
        lambda conn: conn.execute(
            "SELECT event_type FROM capture_events WHERE capture_id = ? AND event_type = ?",
            (capture.capture_id, "CAPTURE_CLASSIFYING"),
        ).fetchall()
    )
    assert len(events) == 1
    ledger.close()


# ---------------------------------------------------------------------------
# Transient SQLITE_BUSY recovery
# ---------------------------------------------------------------------------

def test_sqlite_busy_retry_eventually_succeeds(tmp_path):
    """Hold an external write lock, release it before retry budget, verify insert succeeds."""
    db_path = tmp_path / "ledger.sqlite3"
    ledger = make_ledger(tmp_path,
        sqlite_busy_timeout_ms=0,        # no internal SQLite wait, force Python retry
        sqlite_busy_retry_attempts=6,
        sqlite_busy_retry_base_delay_ms=20,
    )

    # check_same_thread=False so we can release from a different thread
    blocker = sqlite3.connect(str(db_path), check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")

    released = threading.Event()

    def _release_after_delay():
        import time
        time.sleep(0.100)  # 100 ms — between attempt-3 (~60 ms total) and attempt-4 (~140 ms)
        blocker.execute("ROLLBACK")
        blocker.close()
        released.set()

    threading.Thread(target=_release_after_delay, daemon=True).start()

    result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Retry me.",
    )

    released.wait(timeout=2.0)
    assert result.created is True
    assert ledger.total_captures() == 1

    event_count = ledger._runtime.read(
        lambda conn: conn.execute(
            "SELECT COUNT(*) AS c FROM capture_events WHERE event_type = 'CAPTURE_RECEIVED'"
        ).fetchone()["c"]
    )
    assert event_count == 1

    ledger.close()


def test_sqlite_busy_exhausted_raises_and_leaves_no_partial_row(tmp_path):
    """Hold write lock through all retries — SQLiteBusyError must be raised, no row written."""
    from secondbrain.sqlite_runtime import SQLiteBusyError

    db_path = tmp_path / "ledger.sqlite3"
    ledger = make_ledger(tmp_path,
        sqlite_busy_timeout_ms=0,
        sqlite_busy_retry_attempts=3,
        sqlite_busy_retry_base_delay_ms=5,
    )

    blocker = sqlite3.connect(str(db_path), check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")

    try:
        with pytest.raises(SQLiteBusyError):
            ledger.insert_accepted_capture(
                discord_message_id="1001",
                discord_channel_id="200",
                discord_guild_id="300",
                discord_author_id="400",
                raw_text="Should not persist.",
            )
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert ledger.total_captures() == 0
    ledger.close()


# ---------------------------------------------------------------------------
# Structured log: retry events include operation_name from Ledger
# ---------------------------------------------------------------------------

def test_sqlite_busy_retry_log_includes_operation_name(tmp_path, capsys):
    """sqlite_busy_retry log lines must carry the operation_name supplied by Ledger."""
    from secondbrain.sqlite_runtime import SQLiteBusyError

    db_path = tmp_path / "ledger.sqlite3"
    ledger = make_ledger(tmp_path,
        sqlite_busy_timeout_ms=0,
        sqlite_busy_retry_attempts=3,
        sqlite_busy_retry_base_delay_ms=5,
    )

    blocker = sqlite3.connect(str(db_path), check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SQLiteBusyError):
            ledger.insert_accepted_capture(
                discord_message_id="1001",
                discord_channel_id="200",
                discord_guild_id="300",
                discord_author_id="400",
                raw_text="Log me.",
            )
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    captured = capsys.readouterr().out
    # Parse every JSON line and filter by the event field — a string search on
    # "sqlite_busy_retry" would also match the sqlite_runtime_started line
    # because pytest embeds the test function name in the tmp_path directory.
    retry_logs = []
    for line in captured.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("event") == "sqlite_busy_retry_count":
            retry_logs.append(data)

    assert len(retry_logs) >= 1
    log = retry_logs[0]
    assert log["event"] == "sqlite_busy_retry_count"
    assert log["operation_name"] == "insert_accepted_capture"
    assert log["attempt"] >= 1
    assert log["error_type"] == "OperationalError"
    assert isinstance(log["retrying"], bool)

    ledger.close()


# ---------------------------------------------------------------------------
# Service-level exhausted-lock test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_level_busy_exhaustion_sends_no_receipt_and_leaves_no_row(tmp_path):
    """SQLiteBusyError from the ledger must propagate through the service with no receipt sent."""
    import pytest
    from secondbrain.capture_service import CaptureService
    from secondbrain.sqlite_runtime import SQLiteBusyError
    from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient, FakeDiscordMessage

    db_path = tmp_path / "ledger.sqlite3"
    ledger = make_ledger(tmp_path,
        sqlite_busy_timeout_ms=0,
        sqlite_busy_retry_attempts=3,
        sqlite_busy_retry_base_delay_ms=5,
    )

    channel = FakeDiscordChannel()
    client = FakeDiscordClient(channel)
    settings = SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
    )
    service = CaptureService(
        settings=settings,
        ledger=ledger,
        notify_capture=None,
        receipt_client=client,
    )

    blocker = sqlite3.connect(str(db_path), check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SQLiteBusyError):
            await service.handle_gateway_message(
                FakeDiscordMessage(
                    message_id=1001,
                    channel=channel,
                    content="Should not persist.",
                )
            )
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert channel.sent_receipts == [], "no receipt must be sent when insert fails"
    assert ledger.total_captures() == 0, "no capture row must be written when insert fails"
    ledger.close()


# ===========================================================================
# SB-107 — Delivery leases
# ===========================================================================

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
_LEASE = datetime(2026, 6, 9, 12, 1, 0, tzinfo=UTC)
_LATER = datetime(2026, 6, 9, 12, 10, 0, tzinfo=UTC)


def _accepted(ledger, msg_id="1001"):
    return ledger.insert_accepted_capture(
        discord_message_id=msg_id,
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note",
        received_at=_NOW,
    ).capture


def _make_retry_settings(**overrides):
    defaults = dict(
        delivery_retry_max_attempts=5,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

def test_delivery_lease_migration_adds_required_columns(tmp_path):
    ledger = make_ledger(tmp_path)
    cols = table_columns(ledger, "captures")
    assert "delivery_status" in cols
    assert "delivery_attempts" in cols
    assert "processing_lease_until" in cols
    assert "next_attempt_at" in cols
    ledger.close()


def test_delivery_lease_migration_adds_due_and_lease_indexes(tmp_path):
    ledger = make_ledger(tmp_path)
    assert index_columns(ledger, "idx_captures_delivery_due") == ["delivery_status", "next_attempt_at"]
    assert index_columns(ledger, "idx_captures_processing_lease") == ["delivery_status", "processing_lease_until"]
    ledger.close()


def test_delivery_lease_migration_maps_received_rows_to_pending_forward(tmp_path):
    db_path = tmp_path / "ledger.sqlite3"
    _seed_mvp_db(db_path, status=RECEIVED)
    ledger = make_ledger(tmp_path)
    capture = ledger.get_capture("SB-20260607-0001")
    assert capture.delivery_status == PENDING_FORWARD
    ledger.close()


def test_delivery_lease_migration_maps_sensitive_rows_to_not_applicable(tmp_path):
    db_path = tmp_path / "ledger.sqlite3"
    _seed_mvp_db_sensitive(db_path)
    ledger = make_ledger(tmp_path)
    capture = ledger.get_capture("SB-20260607-0001")
    assert capture.delivery_status == NOT_APPLICABLE
    ledger.close()


def test_delivery_lease_migration_maps_terminal_rows_to_complete_or_failed(tmp_path):
    db_path = tmp_path / "ledger.sqlite3"
    _seed_mvp_db(db_path, status=FILED)
    ledger = make_ledger(tmp_path)
    capture = ledger.get_capture("SB-20260607-0001")
    assert capture.delivery_status == COMPLETE

    db_path2 = tmp_path / "ledger2.sqlite3"
    _seed_mvp_db(db_path2, status=FAILED)
    ledger2 = Ledger(db_path2)
    c2 = ledger2.get_capture("SB-20260607-0001")
    assert c2.delivery_status == DELIVERY_FAILED
    ledger.close()
    ledger2.close()


def test_delivery_lease_migration_resets_legacy_classifying_rows_safely(tmp_path):
    db_path = tmp_path / "ledger.sqlite3"
    _seed_mvp_db(db_path, status=CLASSIFYING)
    ledger = make_ledger(tmp_path)
    capture = ledger.get_capture("SB-20260607-0001")
    assert capture.status == RECEIVED
    assert capture.delivery_status == PENDING_FORWARD
    ledger.close()


# ---------------------------------------------------------------------------
# Insert behavior
# ---------------------------------------------------------------------------

def test_accepted_capture_starts_pending_forward(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    assert capture.delivery_status == PENDING_FORWARD
    assert capture.delivery_attempts == 0
    assert capture.processing_lease_until is None
    assert capture.next_attempt_at is None
    ledger.close()


def test_sensitive_rejection_starts_not_applicable(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = ledger.insert_sensitive_rejection(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        redacted_text="REDACTED",
        sensitivity_flags=["token"],
    ).capture
    assert capture.delivery_status == NOT_APPLICABLE
    assert capture.delivery_attempts == 0
    ledger.close()


def test_duplicate_discord_message_returns_existing_delivery_state(tmp_path):
    ledger = make_ledger(tmp_path)
    first = _accepted(ledger, "1001")
    # Simulate delivery claimed
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    second = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note",
    ).capture
    assert second.delivery_status == FORWARDING
    assert second.delivery_attempts == 1
    ledger.close()


# ---------------------------------------------------------------------------
# Due-delivery claiming
# ---------------------------------------------------------------------------

def test_claim_due_deliveries_claims_pending_rows(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    claimed = ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    assert len(claimed) == 1
    assert claimed[0].delivery_status == FORWARDING
    ledger.close()


def test_claim_due_deliveries_claims_retry_rows_only_after_next_attempt_at(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    s = _make_retry_settings()
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    # Too early — next_attempt_at is in the future
    early = ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    assert early == []
    # After next_attempt_at
    due = ledger.claim_due_deliveries(now=_LATER, lease_until=_LATER, batch_size=10)
    assert len(due) == 1
    assert due[0].delivery_attempts == 2
    ledger.close()


def test_claim_due_deliveries_increments_attempt_count_once(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    claimed = ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    assert claimed[0].delivery_attempts == 1
    ledger.close()


def test_claim_due_deliveries_assigns_forwarding_lease(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    claimed = ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    capture = claimed[0]
    assert capture.processing_lease_until is not None
    assert capture.processing_lease_until.isoformat() == _LEASE.isoformat()
    assert capture.next_attempt_at is None
    assert capture.last_error is None
    ledger.close()


def test_claim_due_deliveries_is_bounded(tmp_path):
    ledger = make_ledger(tmp_path)
    for i in range(5):
        _accepted(ledger, str(1001 + i))
    claimed = ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=3)
    assert len(claimed) == 3
    ledger.close()


def test_concurrent_claims_do_not_claim_same_capture_twice(tmp_path):
    import threading
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    results = []

    def claim():
        r = ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
        results.extend(r)

    threads = [threading.Thread(target=claim) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed_ids = [c.capture_id for c in results if c.delivery_status == FORWARDING]
    assert len(claimed_ids) == 1
    ledger.close()


# ---------------------------------------------------------------------------
# Callback transitions
# ---------------------------------------------------------------------------

def test_mark_forwarded_moves_forwarding_to_forwarded(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    result = ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    assert result.changed is True
    assert result.outcome == "changed"
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.delivery_status == DELIVERY_FORWARDED
    assert capture.processing_lease_until.isoformat() == _LATER.isoformat()
    ledger.close()


def test_mark_forwarded_duplicate_same_attempt_is_idempotent_replay(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    result2 = ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    assert result2.changed is False
    assert result2.outcome == "idempotent_replay"
    ledger.close()


def test_mark_forwarded_from_invalid_state_returns_invalid_state(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="path/note.md"
    )
    # State is now COMPLETE (FILED/COMPLETE) — attempt matches (1==1) but state is invalid
    result = ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    assert result.changed is False
    assert result.outcome == "invalid_state"
    ledger.close()


def test_mark_classifying_moves_forwarded_to_classifying(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    result = ledger.mark_classifying_delivery(
        capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER
    )
    assert result.changed is True
    assert result.outcome == "changed"
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.delivery_status == DELIVERY_CLASSIFYING
    ledger.close()


def test_mark_classifying_before_forwarded_returns_invalid_state(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    # State is FORWARDING, not DELIVERY_FORWARDED — classifying is premature
    result = ledger.mark_classifying_delivery(
        capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER
    )
    assert result.changed is False
    assert result.outcome == "invalid_state"
    ledger.close()


def test_mark_classifying_renews_existing_classifying_lease(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_classifying_delivery(
        capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER
    )
    new_lease = datetime(2026, 6, 9, 12, 20, 0, tzinfo=UTC)
    result = ledger.mark_classifying_delivery(
        capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=new_lease
    )
    assert result.changed is True
    assert result.outcome == "changed"
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.processing_lease_until.isoformat() == new_lease.isoformat()
    ledger.close()


def test_mark_filed_moves_capture_to_terminal_complete(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    result = ledger.mark_filed(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        derived_note_path="20_projects/note.md",
    )
    assert result.changed is True
    assert result.outcome == "changed"
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.status == FILED
    assert capture.delivery_status == COMPLETE
    assert capture.processing_lease_until is None
    assert capture.next_attempt_at is None
    ledger.close()


def test_mark_inbox_moves_capture_to_terminal_complete(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    result = ledger.mark_inbox(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        derived_note_path="00_inbox/note.md",
        reason_type="needs_context",
    )
    assert result.changed is True
    assert result.outcome == "changed"
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.status == INBOX
    assert capture.delivery_status == COMPLETE
    ledger.close()


def test_duplicate_identical_terminal_callback_is_idempotent(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="path/note.md"
    )
    ok2 = ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="path/note.md"
    )
    assert ok2.changed is False
    assert ok2.outcome == "idempotent_replay"
    ledger.close()


def test_conflicting_terminal_callback_is_rejected(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="path/note.md"
    )
    conflict = ledger.mark_inbox(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="path/note2.md"
    )
    assert conflict.changed is False
    assert conflict.outcome == "conflicting_replay"
    ledger.close()


def test_stale_attempt_callback_is_ignored(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    result = ledger.mark_forwarded(
        capture_id="SB-20260609-0001", delivery_attempt=99, lease_until=_LATER
    )
    assert result.changed is False
    assert result.outcome == "stale_attempt"
    ledger.close()


def test_terminal_capture_cannot_regress_to_classifying(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="path/note.md"
    )
    regress = ledger.mark_classifying_delivery(
        capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER
    )
    assert regress.changed is False
    assert regress.outcome == "invalid_state"
    ledger.close()


def test_renew_delivery_lease_stale_attempt_returns_stale_attempt(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    result = ledger.renew_delivery_lease(
        capture_id="SB-20260609-0001", delivery_attempt=99, lease_until=_LATER
    )
    assert result.changed is False
    assert result.outcome == "stale_attempt"
    ledger.close()


def test_renew_delivery_lease_after_completion_returns_invalid_state(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="path/note.md"
    )
    result = ledger.renew_delivery_lease(
        capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER
    )
    assert result.changed is False
    assert result.outcome == "invalid_state"
    ledger.close()


def test_mark_delivery_failed_terminally_stale_attempt(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    result = ledger.mark_delivery_failed_terminally(
        capture_id="SB-20260609-0001", delivery_attempt=99
    )
    assert result.changed is False
    assert result.outcome == "stale_attempt"
    ledger.close()


def test_mark_delivery_failed_terminally_already_complete_returns_ignored(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="path/note.md"
    )
    # State is COMPLETE (FILED) — late failure callback is safely ignored
    result = ledger.mark_delivery_failed_terminally(
        capture_id="SB-20260609-0001", delivery_attempt=1
    )
    assert result.changed is False
    assert result.outcome == "ignored_already_terminal"
    ledger.close()


# ---------------------------------------------------------------------------
# Retry scheduling
# ---------------------------------------------------------------------------

def test_schedule_retry_moves_active_attempt_to_retry_wait(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    s = _make_retry_settings()
    disp = ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    assert disp.retry_scheduled is True
    assert disp.failed_terminally is False
    assert disp.delivery_status == RETRY_WAIT
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.delivery_status == RETRY_WAIT
    assert capture.processing_lease_until is None
    ledger.close()


def test_schedule_retry_sets_capped_exponential_backoff(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    s = _make_retry_settings(delivery_retry_base_delay_seconds=10, delivery_retry_max_delay_seconds=300)
    disp = ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    assert disp.next_attempt_at is not None
    delay = (disp.next_attempt_at - _NOW).total_seconds()
    assert delay == 10  # attempt 1: base * 2^0 = 10
    ledger.close()


def test_schedule_retry_clears_processing_lease(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    s = _make_retry_settings()
    ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.processing_lease_until is None
    ledger.close()


def test_schedule_retry_preserves_safe_error_type(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    s = _make_retry_settings()
    ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="ConnectionError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    capture = ledger.get_capture("SB-20260609-0001")
    assert "ConnectionError" in (capture.last_error or "")
    ledger.close()


def test_schedule_retry_marks_failed_when_attempt_cap_exceeded(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    s = _make_retry_settings(delivery_retry_max_attempts=1)
    disp = ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    assert disp.failed_terminally is True
    assert disp.retry_scheduled is False
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.status == FAILED
    assert capture.delivery_status == DELIVERY_FAILED
    ledger.close()


def test_retry_cap_appends_retry_limit_exceeded_event(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    s = _make_retry_settings(delivery_retry_max_attempts=1)
    ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    events = ledger._runtime.read(
        lambda conn: [
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM capture_events WHERE capture_id = ? ORDER BY id",
                ("SB-20260609-0001",),
            ).fetchall()
        ]
    )
    assert "RETRY_LIMIT_EXCEEDED" in events
    ledger.close()


# ---------------------------------------------------------------------------
# Safe-slug validation
# ---------------------------------------------------------------------------

def test_ledger_schedule_retry_rejects_free_form_error_type(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    s = _make_retry_settings()
    import pytest as _pytest
    with _pytest.raises(ValueError, match="unsafe delivery category string"):
        ledger.schedule_retry(
            capture_id="SB-20260609-0001",
            delivery_attempt=1,
            now=_NOW,
            error_type="TimeoutError: POST https://n8n.internal?token=secret",
            reason_type="webhook_failure",
            max_attempts=s.delivery_retry_max_attempts,
            base_delay_seconds=s.delivery_retry_base_delay_seconds,
            max_delay_seconds=s.delivery_retry_max_delay_seconds,
        )
    ledger.close()


def test_ledger_schedule_retry_rejects_free_form_reason_type(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    s = _make_retry_settings()
    import pytest as _pytest
    with _pytest.raises(ValueError, match="unsafe delivery category string"):
        ledger.schedule_retry(
            capture_id="SB-20260609-0001",
            delivery_attempt=1,
            now=_NOW,
            error_type="TimeoutError",
            reason_type="free form reason with spaces",
            max_attempts=s.delivery_retry_max_attempts,
            base_delay_seconds=s.delivery_retry_base_delay_seconds,
            max_delay_seconds=s.delivery_retry_max_delay_seconds,
        )
    ledger.close()


def test_ledger_terminal_failure_rejects_free_form_reason_type(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    import pytest as _pytest
    with _pytest.raises(ValueError, match="unsafe delivery category string"):
        ledger.mark_delivery_failed_terminally(
            capture_id="SB-20260609-0001",
            delivery_attempt=1,
            reason="reason with spaces or <script>",
        )
    ledger.close()


def test_ledger_mark_inbox_rejects_free_form_reason_type(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        lease_until=_LEASE,
    )
    import pytest as _pytest
    with _pytest.raises(ValueError, match="unsafe delivery category string"):
        ledger.mark_inbox(
            capture_id="SB-20260609-0001",
            delivery_attempt=1,
            derived_note_path="00_inbox/file.md",
            reason_type="free form reason with <script>",
        )
    ledger.close()


# ---------------------------------------------------------------------------
# Local-full normalization
# ---------------------------------------------------------------------------

def test_normalize_delivery_for_local_full_handles_forwarding_row(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    # State is now FORWARDING
    assert ledger.get_capture("SB-20260609-0001").delivery_status == FORWARDING
    count = ledger.normalize_delivery_for_local_full()
    assert count == 1
    assert ledger.get_capture("SB-20260609-0001").delivery_status == NOT_APPLICABLE
    ledger.close()


def test_normalize_delivery_for_local_full_handles_forwarded_row(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    assert ledger.get_capture("SB-20260609-0001").delivery_status == DELIVERY_FORWARDED
    ledger.normalize_delivery_for_local_full()
    assert ledger.get_capture("SB-20260609-0001").delivery_status == NOT_APPLICABLE
    ledger.close()


def test_normalize_delivery_for_local_full_handles_classifying_row(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_classifying_delivery(
        capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER
    )
    assert ledger.get_capture("SB-20260609-0001").delivery_status == DELIVERY_CLASSIFYING
    ledger.normalize_delivery_for_local_full()
    assert ledger.get_capture("SB-20260609-0001").delivery_status == NOT_APPLICABLE
    ledger.close()


def test_normalize_delivery_for_local_full_clears_lease_and_retry_metadata(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    # Confirm lease is set before normalization
    assert ledger.get_capture("SB-20260609-0001").processing_lease_until is not None
    ledger.normalize_delivery_for_local_full()
    normalized = ledger.get_capture("SB-20260609-0001")
    assert normalized.processing_lease_until is None
    assert normalized.next_attempt_at is None
    assert normalized.last_error is None
    ledger.close()


def test_normalize_delivery_for_local_full_appends_audit_event(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.normalize_delivery_for_local_full()
    events = ledger._runtime.read(
        lambda conn: [
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM capture_events WHERE capture_id = ? ORDER BY id",
                ("SB-20260609-0001",),
            ).fetchall()
        ]
    )
    assert "DELIVERY_DISABLED_FOR_LOCAL_FULL" in events
    ledger.close()


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------

def test_backoff_grows_exponentially_from_base():
    assert calculate_retry_delay_seconds(retry_attempts=1, base_delay_seconds=10, max_delay_seconds=300) == 10
    assert calculate_retry_delay_seconds(retry_attempts=2, base_delay_seconds=10, max_delay_seconds=300) == 20
    assert calculate_retry_delay_seconds(retry_attempts=3, base_delay_seconds=10, max_delay_seconds=300) == 40
    assert calculate_retry_delay_seconds(retry_attempts=4, base_delay_seconds=10, max_delay_seconds=300) == 80
    assert calculate_retry_delay_seconds(retry_attempts=5, base_delay_seconds=10, max_delay_seconds=300) == 160


def test_backoff_is_capped_at_max():
    assert calculate_retry_delay_seconds(retry_attempts=10, base_delay_seconds=10, max_delay_seconds=300) == 300


# ---------------------------------------------------------------------------
# Terminal callback idempotency / conflict detection (Issue 7)
# ---------------------------------------------------------------------------

def test_duplicate_terminal_callback_with_different_path_is_rejected(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ok1 = ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="a.md"
    )
    assert ok1.changed is True
    conflict = ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="b.md"
    )
    assert conflict.changed is False
    assert conflict.outcome == "conflicting_replay"
    ledger.close()


def test_duplicate_terminal_callback_with_same_path_is_idempotent(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="a.md"
    )
    ok2 = ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="a.md"
    )
    assert ok2.changed is False
    assert ok2.outcome == "idempotent_replay"
    ledger.close()


def test_duplicate_terminal_callback_with_different_git_hash_is_rejected(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ok1 = ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="a.md",
        git_commit_hash="abc123",
    )
    assert ok1.changed is True
    conflict = ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="a.md",
        git_commit_hash="def456",
    )
    assert conflict.changed is False
    assert conflict.outcome == "conflicting_replay"
    ledger.close()


def test_duplicate_inbox_callback_with_different_reason_is_rejected(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_classifying_delivery(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ok1 = ledger.mark_inbox(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="inbox.md",
        reason_type="low_confidence",
    )
    assert ok1.changed is True
    conflict = ledger.mark_inbox(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="inbox.md",
        reason_type="needs_clarification",
    )
    assert conflict.changed is False
    assert conflict.outcome == "conflicting_replay"
    ledger.close()


def test_terminal_replay_missing_original_git_hash_is_rejected(tmp_path):
    """Replay without git_commit_hash when original had one is rejected (exact equality)."""
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="a.md",
        git_commit_hash="abc123",
    )
    # Replay omits git_commit_hash — must not be treated as idempotent
    conflict = ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="a.md",
        git_commit_hash=None,
    )
    assert conflict.changed is False
    assert conflict.outcome == "conflicting_replay"
    ledger.close()


def test_inbox_replay_missing_original_reason_type_is_rejected(tmp_path):
    """Replay without reason_type when original had one is rejected."""
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_classifying_delivery(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_inbox(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="inbox.md",
        reason_type="low_confidence",
    )
    # Replay omits reason_type — stored value "low_confidence" != None → conflict
    conflict = ledger.mark_inbox(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="inbox.md",
        reason_type="",
    )
    assert conflict.changed is False
    assert conflict.outcome == "conflicting_replay"
    ledger.close()


def test_terminal_delivery_fields_stored_on_mark_filed(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_filed(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="20_projects/a.md",
        git_commit_hash="abc123",
    )
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.delivery_commit_hash == "abc123"
    assert capture.delivery_reason_type is None
    ledger.close()


def test_terminal_delivery_fields_stored_on_mark_inbox(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.mark_forwarded(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_classifying_delivery(capture_id="SB-20260609-0001", delivery_attempt=1, lease_until=_LATER)
    ledger.mark_inbox(
        capture_id="SB-20260609-0001", delivery_attempt=1, derived_note_path="inbox/b.md",
        reason_type="low_confidence",
    )
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.delivery_reason_type == "low_confidence"
    ledger.close()


# ---------------------------------------------------------------------------
# Delivery snapshot timestamp comparison (Issue 8)
# ---------------------------------------------------------------------------

def test_delivery_snapshot_counts_same_day_expired_iso_lease(tmp_path):
    """ISO-8601 timestamps with T must compare correctly against _now() output.
    Uses the real clock so the comparison is against the same time source.
    """
    from datetime import UTC as _UTC, datetime as _dt
    now = _dt.now(_UTC)
    past_lease = now - timedelta(seconds=60)

    ledger = make_ledger(tmp_path)
    # Insert with a fixed received_at to avoid date-sensitive capture IDs
    ledger.insert_accepted_capture(
        discord_message_id="9901",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="snapshot test",
        received_at=now,
    )
    # Claim with an already-expired lease
    ledger.claim_due_deliveries(now=now, lease_until=past_lease, batch_size=10)

    snapshot = ledger.delivery_snapshot()
    assert snapshot["expired_leases"] >= 1
    ledger.close()


def test_delivery_snapshot_ignores_future_iso_lease(tmp_path):
    """A lease set far in the future must not appear as expired."""
    from datetime import UTC as _UTC, datetime as _dt
    now = _dt.now(_UTC)
    future_lease = now + timedelta(hours=24)

    ledger = make_ledger(tmp_path)
    ledger.insert_accepted_capture(
        discord_message_id="9902",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="snapshot test",
        received_at=now,
    )
    ledger.claim_due_deliveries(now=now, lease_until=future_lease, batch_size=10)

    snapshot = ledger.delivery_snapshot()
    assert snapshot["expired_leases"] == 0
    ledger.close()


# ---------------------------------------------------------------------------
# Migration seed helpers
# ---------------------------------------------------------------------------

def _seed_mvp_db(db_path, *, status):
    raw = sqlite3.connect(str(db_path))
    raw.executescript("""
        CREATE TABLE captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL UNIQUE,
            discord_message_id TEXT NOT NULL UNIQUE,
            discord_channel_id TEXT NOT NULL,
            discord_guild_id TEXT NOT NULL,
            discord_author_id TEXT NOT NULL,
            raw_text TEXT,
            redacted_text TEXT,
            is_sensitive INTEGER NOT NULL DEFAULT 0,
            sensitivity_flags TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            attachment_metadata_json TEXT,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL,
            classification_json TEXT,
            derived_note_path TEXT,
            receipt_message_id TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE capture_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_payload_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    raw.execute(
        """
        INSERT INTO captures (
            capture_id, discord_message_id, discord_channel_id,
            discord_guild_id, discord_author_id, raw_text, is_sensitive,
            has_attachments, received_at, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
        """,
        (
            "SB-20260607-0001", "1001", "200", "300", "400",
            "Existing capture",
            "2026-06-07T12:00:00+00:00", status, "2026-06-07T12:00:00+00:00",
        ),
    )
    raw.commit()
    raw.close()


def _seed_mvp_db_sensitive(db_path):
    raw = sqlite3.connect(str(db_path))
    raw.executescript("""
        CREATE TABLE captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL UNIQUE,
            discord_message_id TEXT NOT NULL UNIQUE,
            discord_channel_id TEXT NOT NULL,
            discord_guild_id TEXT NOT NULL,
            discord_author_id TEXT NOT NULL,
            raw_text TEXT,
            redacted_text TEXT,
            is_sensitive INTEGER NOT NULL DEFAULT 0,
            sensitivity_flags TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            attachment_metadata_json TEXT,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL,
            classification_json TEXT,
            derived_note_path TEXT,
            receipt_message_id TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE capture_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_payload_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    raw.execute(
        """
        INSERT INTO captures (
            capture_id, discord_message_id, discord_channel_id,
            discord_guild_id, discord_author_id, redacted_text, is_sensitive,
            has_attachments, received_at, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)
        """,
        (
            "SB-20260607-0001", "1001", "200", "300", "400",
            "REDACTED",
            "2026-06-07T12:00:00+00:00", REJECTED_SENSITIVE, "2026-06-07T12:00:00+00:00",
        ),
    )
    raw.commit()
    raw.close()


# ---------------------------------------------------------------------------
# SB-108 — stale-lease reaper migration tests
# ---------------------------------------------------------------------------

def test_stale_lease_reaper_migration_adds_retry_attempts(tmp_path):
    ledger = make_ledger(tmp_path)
    cols = table_columns(ledger, "captures")
    assert "retry_attempts" in cols
    ledger.close()


def test_existing_rows_default_retry_attempts_to_zero(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.retry_attempts == 0
    ledger.close()


def test_stale_lease_indexes_exist(tmp_path):
    ledger = make_ledger(tmp_path)
    assert index_columns(ledger, "idx_captures_stale_lease") == ["delivery_status", "processing_lease_until"]
    assert index_columns(ledger, "idx_captures_retry_due") == ["delivery_status", "next_attempt_at"]
    ledger.close()


# ---------------------------------------------------------------------------
# Unified retry_attempts accounting — webhook failure and stale-lease paths
# ---------------------------------------------------------------------------

def test_webhook_failure_increments_retry_attempts(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    s = _make_retry_settings()
    ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.retry_attempts == 1
    assert capture.delivery_attempts == 1  # claim increments delivery_attempts; retry does not
    ledger.close()


def test_webhook_failure_backoff_uses_retry_attempts(tmp_path):
    """Second webhook failure schedules delay based on retry_attempts=2, not delivery_attempts."""
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    s = _make_retry_settings(delivery_retry_base_delay_seconds=10, delivery_retry_max_delay_seconds=300)

    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )

    new_lease = _LATER + timedelta(minutes=1)
    ledger.claim_due_deliveries(now=_LATER, lease_until=new_lease, batch_size=10)

    disp = ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=2,
        now=_LATER,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    assert disp.retry_scheduled is True
    delay = (disp.next_attempt_at - _LATER).total_seconds()
    assert delay == 20  # base * 2^(retry_attempts-1) = 10 * 2^1 = 20

    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.retry_attempts == 2
    assert capture.delivery_attempts == 2
    ledger.close()


def test_mixed_webhook_failure_and_stale_lease_share_one_retry_counter(tmp_path):
    """One webhook failure + one stale-lease reap both increment the same retry_attempts."""
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    s = _make_retry_settings()

    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )

    stale_lease = _LATER + timedelta(minutes=1)
    ledger.claim_due_deliveries(now=_LATER, lease_until=stale_lease, batch_size=10)

    reap_now = _LATER + timedelta(minutes=2)
    ledger.reap_expired_processing_leases(
        now=reap_now,
        batch_size=10,
        retry_max_attempts=s.delivery_retry_max_attempts,
        retry_base_delay_seconds=s.delivery_retry_base_delay_seconds,
        retry_max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )

    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.retry_attempts == 2
    ledger.close()


def test_mixed_failure_paths_reach_one_consistent_retry_cap(tmp_path):
    """With max_attempts=2: one webhook failure + one stale lease = terminal FAILED."""
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")
    s = _make_retry_settings(delivery_retry_max_attempts=2)

    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    disp = ledger.schedule_retry(
        capture_id="SB-20260609-0001",
        delivery_attempt=1,
        now=_NOW,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=s.delivery_retry_max_attempts,
        base_delay_seconds=s.delivery_retry_base_delay_seconds,
        max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )
    assert disp.retry_scheduled is True

    stale_lease = _LATER + timedelta(minutes=1)
    ledger.claim_due_deliveries(now=_LATER, lease_until=stale_lease, batch_size=10)

    reap_now = _LATER + timedelta(minutes=2)
    result = ledger.reap_expired_processing_leases(
        now=reap_now,
        batch_size=10,
        retry_max_attempts=s.delivery_retry_max_attempts,
        retry_base_delay_seconds=s.delivery_retry_base_delay_seconds,
        retry_max_delay_seconds=s.delivery_retry_max_delay_seconds,
    )

    assert len(result.failed) == 1
    assert result.failed[0].capture_id == "SB-20260609-0001"
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.status == FAILED
    assert capture.retry_attempts == 2
    ledger.close()


def test_dispatch_claim_increments_delivery_attempts_but_not_retry_attempts(tmp_path):
    """Claiming a delivery generation increments delivery_attempts but leaves retry_attempts at zero."""
    ledger = make_ledger(tmp_path)
    _accepted(ledger, "1001")

    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)

    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.delivery_attempts == 1
    assert capture.retry_attempts == 0
    ledger.close()
