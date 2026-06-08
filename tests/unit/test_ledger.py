from datetime import UTC, datetime
import re
import sqlite3

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


def test_insert_accepted_capture_creates_received_record(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

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


def test_minimal_schema_tables_columns_and_indexes_match_mvp(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

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


def test_minimal_schema_foreign_keys_and_sensitive_check_constraint(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    foreign_keys = ledger._connection.execute("PRAGMA foreign_key_list(capture_events)").fetchall()
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["from"] == "capture_id"
    assert foreign_keys[0]["table"] == "captures"
    assert foreign_keys[0]["to"] == "capture_id"

    try:
        ledger._connection.execute(
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
        pass
    else:
        raise AssertionError("sensitive captures must not store raw_text without redacted_text")


def test_duplicate_discord_message_id_returns_existing_capture(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

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


def test_duplicate_discord_message_id_for_sensitive_rejection_returns_existing_capture(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

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


def test_capture_id_counter_is_per_day(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

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


def test_capture_id_format_is_human_readable(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    capture = insert_accepted_capture(ledger,
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
        received_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
    )

    assert re.fullmatch(r"SB-\d{8}-\d{4}", capture.capture_id)


def test_insert_sensitive_rejection_stores_redacted_text_only(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

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


def test_set_receipt_message_id_updates_capture(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
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


def test_mark_classifying_transitions_only_received_rows(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
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


def test_update_capture_rejects_unknown_status(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
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


def test_enqueueable_capture_ids_include_received_and_classifying(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
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
    ledger.mark_classifying(second.capture_id)

    assert ledger.enqueueable_capture_ids() == [first.capture_id, second.capture_id]


def test_reset_classifying_to_received_requeues_stale_work(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
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


def test_system_state_round_trips(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    assert ledger.get_system_state("last_message") is None

    ledger.set_system_state("last_message", "123")
    ledger.set_system_state("last_message", "456")

    assert ledger.get_system_state("last_message") == "456"


def test_event_writes_require_ledger_mutation_lock(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    try:
        ledger._append_event("SB-20260607-0001", "BROKEN_DIRECT_WRITE")
    except RuntimeError as exc:
        assert str(exc) == "ledger mutation lock must be held for SQLite writes"
    else:
        raise AssertionError("direct event write should require the mutation lock")


def table_names(ledger: Ledger) -> list[str]:
    rows = ledger._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def table_columns(ledger: Ledger, table_name: str) -> list[str]:
    rows = ledger._connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [row["name"] for row in rows]


def index_columns(ledger: Ledger, index_name: str) -> list[str]:
    rows = ledger._connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    return [row["name"] for row in rows]


def unique_index_exists(ledger: Ledger, table_name: str, columns: list[str]) -> bool:
    indexes = ledger._connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    for index in indexes:
        if not index["unique"]:
            continue
        if index_columns(ledger, index["name"]) == columns:
            return True
    return False


def insert_accepted_capture(ledger: Ledger, **kwargs):
    return ledger.insert_accepted_capture(**kwargs).capture


def insert_sensitive_rejection(ledger: Ledger, **kwargs):
    return ledger.insert_sensitive_rejection(**kwargs).capture
