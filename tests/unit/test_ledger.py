from datetime import UTC, datetime

from secondbrain.ledger import (
    CLASSIFYING,
    RECEIVED,
    REJECTED_SENSITIVE,
    Ledger,
)


def test_insert_accepted_capture_creates_received_record(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    capture = ledger.insert_accepted_capture(
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


def test_duplicate_discord_message_id_returns_existing_capture(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    first = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="First text.",
        received_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
    )
    second = ledger.insert_accepted_capture(
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


def test_capture_id_counter_is_per_day(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    first = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="One.",
        received_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
    )
    second = ledger.insert_accepted_capture(
        discord_message_id="1002",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Two.",
        received_at=datetime(2026, 6, 7, 12, 1, tzinfo=UTC),
    )
    next_day = ledger.insert_accepted_capture(
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


def test_insert_sensitive_rejection_stores_redacted_text_only(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    capture = ledger.insert_sensitive_rejection(
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
    capture = ledger.insert_accepted_capture(
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
    capture = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
    )

    assert ledger.mark_classifying(capture.capture_id) is True
    assert ledger.get_capture(capture.capture_id).status == CLASSIFYING
    assert ledger.mark_classifying(capture.capture_id) is False


def test_enqueueable_capture_ids_include_received_and_classifying(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    first = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="One.",
    )
    second = ledger.insert_accepted_capture(
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
    capture = ledger.insert_accepted_capture(
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
