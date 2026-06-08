from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import re
import sqlite3
import threading
from types import SimpleNamespace

import pytest

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
    assert migration_count == 1
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
        if data.get("event") == "sqlite_busy_retry":
            retry_logs.append(data)

    assert len(retry_logs) >= 1
    log = retry_logs[0]
    assert log["event"] == "sqlite_busy_retry"
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
