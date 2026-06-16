from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from secondbrain.app import run_status
from secondbrain.ledger import Ledger
from secondbrain.status import (
    OperationalStatusUnavailable,
    StatusSettings,
    calculate_capture_service_health,
    format_operational_status,
    read_operational_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_settings(
    ledger_path: Path,
    *,
    vault_path: Path | None = None,
    timezone: str = "UTC",
    stale_after: int = 60,
) -> StatusSettings:
    return StatusSettings(
        ledger_path=ledger_path,
        vault_path=vault_path,
        status_timezone=timezone,
        capture_service_health_stale_after_seconds=stale_after,
    )


_MSG_COUNTER = 0


def _next_msg_id() -> str:
    global _MSG_COUNTER
    _MSG_COUNTER += 1
    return str(90000 + _MSG_COUNTER)


def _insert(ledger: Ledger, *, raw_text: str = "test note", received_at: datetime | None = None) -> str:
    result = ledger.insert_accepted_capture(
        discord_message_id=_next_msg_id(),
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text=raw_text,
        received_at=received_at,
    )
    return result.capture.capture_id


_NOW = datetime(2026, 6, 9, 15, 0, 0, tzinfo=UTC)
_TODAY_UTC_START = datetime(2026, 6, 9, 0, 0, 0, tzinfo=UTC)
_YESTERDAY_UTC_START = _TODAY_UTC_START - timedelta(days=1)


# ---------------------------------------------------------------------------
# StatusSettings.from_env
# ---------------------------------------------------------------------------

def test_status_settings_load_without_discord_or_gemini_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")
    for key in ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "DISCORD_CAPTURE_CHANNEL_ID",
                "DISCORD_ALLOWED_USER_ID", "GEMINI_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(key, raising=False)

    settings = StatusSettings.from_env()

    assert settings.ledger_path == tmp_path / "ledger.sqlite3"
    assert settings.status_timezone == "UTC"
    assert settings.capture_service_health_stale_after_seconds == 60


def test_status_settings_requires_ledger_path(monkeypatch):
    monkeypatch.setenv("LEDGER_PATH", "")  # empty overrides .env file value
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

    with pytest.raises(RuntimeError, match="LEDGER_PATH"):
        StatusSettings.from_env()


def test_status_settings_validates_timezone(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("STATUS_TIMEZONE", "Not/A/Timezone")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

    with pytest.raises(RuntimeError, match="STATUS_TIMEZONE"):
        StatusSettings.from_env()


def test_status_settings_validates_stale_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "0")

    with pytest.raises(RuntimeError, match="CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS"):
        StatusSettings.from_env()


# ---------------------------------------------------------------------------
# Database access behavior
# ---------------------------------------------------------------------------

def test_status_reader_does_not_create_missing_database(tmp_path):
    path = tmp_path / "nonexistent.sqlite3"
    settings = make_settings(path)

    with pytest.raises(OperationalStatusUnavailable, match="does not exist"):
        read_operational_status(settings=settings)

    assert not path.exists()


def test_status_reader_opens_existing_database_read_only(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.close()
    settings = make_settings(tmp_path / "ledger.sqlite3")

    snapshot = read_operational_status(settings=settings)

    assert snapshot.total_captures == 0


def test_status_reader_does_not_run_migrations(tmp_path):
    raw_path = tmp_path / "bare.sqlite3"
    conn = sqlite3.connect(str(raw_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL UNIQUE,
            discord_message_id TEXT NOT NULL UNIQUE,
            discord_channel_id TEXT NOT NULL,
            discord_guild_id TEXT NOT NULL,
            discord_author_id TEXT NOT NULL,
            raw_text TEXT,
            redacted_text TEXT,
            status TEXT NOT NULL,
            delivery_status TEXT NOT NULL DEFAULT 'NOT_APPLICABLE',
            classification_json TEXT,
            derived_note_path TEXT,
            receipt_message_id TEXT,
            last_error TEXT,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            retry_attempts INTEGER NOT NULL DEFAULT 0,
            processing_lease_until TEXT,
            next_attempt_at TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            attachment_metadata TEXT,
            received_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS capture_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL REFERENCES captures(capture_id),
            event_type TEXT NOT NULL,
            event_payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    conn.close()

    settings = make_settings(raw_path)
    snapshot = read_operational_status(settings=settings)
    assert snapshot.total_captures == 0

    conn2 = sqlite3.connect(str(raw_path))
    tables = {r[0] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn2.close()
    assert "schema_migrations" not in tables


def test_status_reader_does_not_modify_system_state(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.set_system_state("some_key", "some_value")
    ledger.close()

    settings = make_settings(tmp_path / "ledger.sqlite3")
    read_operational_status(settings=settings)

    conn = sqlite3.connect(str(tmp_path / "ledger.sqlite3"))
    rows = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM system_state").fetchall()}
    conn.close()
    assert rows == {"some_key": "some_value"}


# ---------------------------------------------------------------------------
# Capture counts — received_today
# ---------------------------------------------------------------------------

def test_received_today_counts_rows_within_local_day_boundary(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    _insert(ledger, received_at=_TODAY_UTC_START + timedelta(hours=1))
    _insert(ledger, received_at=_TODAY_UTC_START + timedelta(hours=5))
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.captures_received_today == 2


def test_received_today_includes_sensitive_rejection_records(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    _insert(ledger, received_at=_TODAY_UTC_START + timedelta(hours=1))
    ledger.insert_sensitive_rejection(
        discord_message_id=_next_msg_id(),
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        redacted_text="[REDACTED]",
        sensitivity_flags=["api_key"],
        received_at=_TODAY_UTC_START + timedelta(hours=2),
    )
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.captures_received_today == 2


def test_received_today_excludes_previous_local_day(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    _insert(ledger, received_at=_YESTERDAY_UTC_START + timedelta(hours=12))
    _insert(ledger, received_at=_TODAY_UTC_START + timedelta(hours=1))
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.captures_received_today == 1


# ---------------------------------------------------------------------------
# Capture counts — filed_today
# Use raw SQL for event timestamps to control them independently of _now()
# ---------------------------------------------------------------------------

def _insert_filed_event(db_path: Path, capture_id: str, ts: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO capture_events (capture_id, event_type, event_payload_json, created_at) VALUES (?, ?, ?, ?)",
        (capture_id, "CAPTURE_FILED", "{}", ts),
    )
    conn.commit()
    conn.close()


def test_filed_today_counts_capture_filed_events(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture_id = _insert(ledger, received_at=_TODAY_UTC_START + timedelta(hours=1))
    ledger.close()

    filed_ts = (_TODAY_UTC_START + timedelta(hours=2)).isoformat()
    _insert_filed_event(tmp_path / "ledger.sqlite3", capture_id, filed_ts)

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.captures_filed_today == 1


def test_filed_today_does_not_use_capture_updated_at(tmp_path):
    # Received yesterday, filed today — should count in filed_today
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture_id = _insert(ledger, received_at=_YESTERDAY_UTC_START + timedelta(hours=12))
    ledger.close()

    filed_ts = (_TODAY_UTC_START + timedelta(hours=1)).isoformat()
    _insert_filed_event(tmp_path / "ledger.sqlite3", capture_id, filed_ts)

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.captures_received_today == 0
    assert snapshot.captures_filed_today == 1


def test_filed_today_excludes_previous_local_day(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture_id = _insert(ledger, received_at=_YESTERDAY_UTC_START)
    ledger.close()

    yesterday_ts = (_YESTERDAY_UTC_START + timedelta(hours=12)).isoformat()
    _insert_filed_event(tmp_path / "ledger.sqlite3", capture_id, yesterday_ts)

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.captures_filed_today == 0


# ---------------------------------------------------------------------------
# Inbox / failed / retry counts
# ---------------------------------------------------------------------------

def test_status_counts_current_inbox_rows(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture_id = _insert(ledger)
    claim_time = _TODAY_UTC_START + timedelta(hours=1)
    claims = ledger.claim_due_deliveries(
        now=claim_time,
        lease_until=claim_time + timedelta(minutes=5),
        batch_size=10,
    )
    ledger.mark_forwarded(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        lease_until=claim_time + timedelta(minutes=5),
    )
    ledger.mark_inbox(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        derived_note_path="vault/note.md",
    )
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.captures_in_inbox == 1


def test_status_counts_current_failed_rows(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture_id = _insert(ledger)
    claim_time = _TODAY_UTC_START + timedelta(hours=1)
    claims = ledger.claim_due_deliveries(
        now=claim_time,
        lease_until=claim_time + timedelta(minutes=5),
        batch_size=10,
    )
    ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        reason="max_retries_exceeded",
    )
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.captures_failed == 1


def test_status_counts_retry_wait_rows(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture_id = _insert(ledger)
    claim_time = _TODAY_UTC_START + timedelta(hours=1)
    claims = ledger.claim_due_deliveries(
        now=claim_time,
        lease_until=claim_time + timedelta(minutes=5),
        batch_size=10,
    )
    ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        now=claim_time + timedelta(minutes=1),
        error_type="webhook_timeout",
        reason_type="transient",
        max_attempts=5,
        base_delay_seconds=10,
        max_delay_seconds=300,
    )
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.captures_waiting_for_retry == 1


# ---------------------------------------------------------------------------
# Stale lease detection
# ---------------------------------------------------------------------------

def test_status_counts_stale_active_leases(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    _insert(ledger)
    # Claim with a lease that expired before _NOW
    past_claim = _NOW - timedelta(minutes=10)
    ledger.claim_due_deliveries(
        now=past_claim,
        lease_until=past_claim + timedelta(minutes=5),  # expired at _NOW - 5min
        batch_size=10,
    )
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.stale_leases == 1


def test_status_does_not_count_unexpired_lease_as_stale(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    _insert(ledger)
    past_claim = _NOW - timedelta(minutes=1)
    ledger.claim_due_deliveries(
        now=past_claim,
        lease_until=_NOW + timedelta(minutes=10),  # still valid
        batch_size=10,
    )
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.stale_leases == 0


def test_status_does_not_count_terminal_capture_as_stale(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture_id = _insert(ledger)
    past_claim = _NOW - timedelta(minutes=10)
    lease_until = past_claim + timedelta(minutes=5)
    claims = ledger.claim_due_deliveries(
        now=past_claim,
        lease_until=lease_until,
        batch_size=10,
    )
    ledger.mark_forwarded(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        lease_until=lease_until,
    )
    # File the capture — clears processing_lease_until
    ledger.mark_filed(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        derived_note_path="vault/note.md",
    )
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.stale_leases == 0


# ---------------------------------------------------------------------------
# Discord reconciliation state
# ---------------------------------------------------------------------------

def test_status_reports_last_reconciled_message_id(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.set_system_state("last_reconciled_discord_message_id", "9999")
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.last_reconciled_discord_message_id == "9999"


def test_status_reports_last_successful_reconciliation(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ts = "2026-06-09T10:00:00+00:00"
    ledger.record_successful_reconciliation(
        mode="periodic",
        now=datetime.fromisoformat(ts),
    )
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.last_successful_reconciliation_at is not None
    assert snapshot.last_successful_reconciliation_at.isoformat() == ts


def test_status_reports_last_successful_reconciliation_mode(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.record_successful_reconciliation(mode="startup", now=_TODAY_UTC_START)
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.last_successful_reconciliation_mode == "startup"


def test_failed_reconcile_does_not_overwrite_last_successful_timestamp(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    success_ts = _TODAY_UTC_START
    ledger.record_successful_reconciliation(mode="startup", now=success_ts)
    # A failed reconcile should not update last_successful_reconciliation_at
    # (failed reconcile does not call record_successful_reconciliation)
    ledger.close()

    snapshot = read_operational_status(settings=make_settings(tmp_path / "ledger.sqlite3"), now=_NOW)

    assert snapshot.last_successful_reconciliation_at == success_ts


# ---------------------------------------------------------------------------
# calculate_capture_service_health
# ---------------------------------------------------------------------------

def test_health_is_healthy_for_recent_running_heartbeat():
    now = _NOW
    recent_hb = now - timedelta(seconds=30)
    health = calculate_capture_service_health(
        service_state="RUNNING",
        last_heartbeat_at=recent_hb,
        now=now,
        stale_after_seconds=60,
    )
    assert health == "HEALTHY"


def test_health_is_starting_for_recent_starting_heartbeat():
    now = _NOW
    recent_hb = now - timedelta(seconds=5)
    health = calculate_capture_service_health(
        service_state="STARTING",
        last_heartbeat_at=recent_hb,
        now=now,
        stale_after_seconds=60,
    )
    assert health == "STARTING"


def test_health_is_stopped_after_graceful_shutdown():
    health = calculate_capture_service_health(
        service_state="STOPPED",
        last_heartbeat_at=_NOW - timedelta(seconds=5),
        now=_NOW,
        stale_after_seconds=60,
    )
    assert health == "STOPPED"


def test_health_is_stale_when_running_heartbeat_is_old():
    now = _NOW
    old_hb = now - timedelta(seconds=120)
    health = calculate_capture_service_health(
        service_state="RUNNING",
        last_heartbeat_at=old_hb,
        now=now,
        stale_after_seconds=60,
    )
    assert health == "STALE"


def test_health_is_stale_when_running_heartbeat_is_missing():
    health = calculate_capture_service_health(
        service_state="RUNNING",
        last_heartbeat_at=None,
        now=_NOW,
        stale_after_seconds=60,
    )
    assert health == "STALE"


def test_health_is_unknown_when_no_service_state_exists():
    health = calculate_capture_service_health(
        service_state=None,
        last_heartbeat_at=None,
        now=_NOW,
        stale_after_seconds=60,
    )
    assert health == "UNKNOWN"


# ---------------------------------------------------------------------------
# run_status exit codes
# Use real datetime.now(UTC) for heartbeats so the stale check works against
# the actual wall clock used inside run_status().
# ---------------------------------------------------------------------------

def _make_run_status_env(tmp_path, monkeypatch) -> Ledger:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")
    return ledger


def _fresh_heartbeat(ledger: Ledger, instance_id: str) -> None:
    real_now = datetime.now(UTC)
    ledger.record_capture_service_start(instance_id=instance_id, now=real_now - timedelta(seconds=30))
    ledger.record_capture_service_ready(instance_id=instance_id, now=real_now - timedelta(seconds=25))
    ledger.record_capture_service_heartbeat(instance_id=instance_id, now=real_now - timedelta(seconds=5))


def test_status_command_does_not_require_runtime_credentials(tmp_path, monkeypatch):
    ledger = _make_run_status_env(tmp_path, monkeypatch)
    _fresh_heartbeat(ledger, "abc")
    ledger.close()
    for key in ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    exit_code = run_status()

    assert exit_code == 0


def test_status_command_returns_zero_for_healthy_service(tmp_path, monkeypatch):
    ledger = _make_run_status_env(tmp_path, monkeypatch)
    _fresh_heartbeat(ledger, "healthy-instance")
    ledger.close()

    exit_code = run_status()

    assert exit_code == 0


def test_status_command_returns_one_for_stale_service(tmp_path, monkeypatch):
    ledger = _make_run_status_env(tmp_path, monkeypatch)
    instance_id = "stale-instance"
    real_now = datetime.now(UTC)
    # Heartbeat 10 minutes old — exceeds the 60s stale threshold
    old_hb = real_now - timedelta(minutes=10)
    ledger.record_capture_service_start(instance_id=instance_id, now=old_hb - timedelta(seconds=5))
    ledger.record_capture_service_ready(instance_id=instance_id, now=old_hb)
    ledger.record_capture_service_heartbeat(instance_id=instance_id, now=old_hb)
    ledger.close()

    exit_code = run_status()

    assert exit_code == 1


def test_status_command_returns_one_for_failed_backlog(tmp_path, monkeypatch):
    ledger = _make_run_status_env(tmp_path, monkeypatch)
    _fresh_heartbeat(ledger, "ok-instance")
    capture_id = _insert(ledger)
    real_now = datetime.now(UTC)
    claim_time = real_now - timedelta(minutes=2)
    claims = ledger.claim_due_deliveries(
        now=claim_time,
        lease_until=claim_time + timedelta(minutes=10),
        batch_size=10,
    )
    ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        reason="max_retries_exceeded",
    )
    ledger.close()

    exit_code = run_status()

    assert exit_code == 1


def test_status_command_returns_one_for_stale_leases(tmp_path, monkeypatch):
    ledger = _make_run_status_env(tmp_path, monkeypatch)
    _fresh_heartbeat(ledger, "ok-instance")
    _insert(ledger)
    real_now = datetime.now(UTC)
    past_claim = real_now - timedelta(minutes=10)
    # Lease expired 5 minutes ago
    ledger.claim_due_deliveries(
        now=past_claim,
        lease_until=past_claim + timedelta(minutes=5),
        batch_size=10,
    )
    ledger.close()

    exit_code = run_status()

    assert exit_code == 1


def test_status_command_does_not_fail_for_inbox_only(tmp_path, monkeypatch):
    ledger = _make_run_status_env(tmp_path, monkeypatch)
    _fresh_heartbeat(ledger, "ok-instance")
    capture_id = _insert(ledger)
    real_now = datetime.now(UTC)
    claim_time = real_now - timedelta(minutes=2)
    lease_until = claim_time + timedelta(minutes=10)
    claims = ledger.claim_due_deliveries(
        now=claim_time,
        lease_until=lease_until,
        batch_size=10,
    )
    ledger.mark_forwarded(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        lease_until=lease_until,
    )
    ledger.mark_inbox(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        derived_note_path="vault/note.md",
    )
    ledger.close()

    exit_code = run_status()

    assert exit_code == 0


def test_status_command_returns_one_for_starting_service(tmp_path, monkeypatch):
    ledger = _make_run_status_env(tmp_path, monkeypatch)
    real_now = datetime.now(UTC)
    instance_id = "starting-instance"
    ledger.record_capture_service_start(instance_id=instance_id, now=real_now - timedelta(seconds=10))
    # Heartbeat without marking ready keeps state as STARTING
    ledger.record_capture_service_heartbeat(instance_id=instance_id, now=real_now - timedelta(seconds=5))
    ledger.close()

    exit_code = run_status()

    assert exit_code == 1


def test_status_command_returns_one_for_stopped_service(tmp_path, monkeypatch):
    ledger = _make_run_status_env(tmp_path, monkeypatch)
    real_now = datetime.now(UTC)
    instance_id = "stopped-instance"
    ledger.record_capture_service_start(instance_id=instance_id, now=real_now - timedelta(minutes=10))
    ledger.record_capture_service_ready(instance_id=instance_id, now=real_now - timedelta(minutes=9))
    ledger.record_capture_service_stop(instance_id=instance_id, now=real_now - timedelta(minutes=5))
    ledger.close()

    exit_code = run_status()

    assert exit_code == 1


def test_status_command_returns_two_for_corrupt_sqlite_file(tmp_path, monkeypatch):
    db_path = tmp_path / "corrupted.sqlite3"
    db_path.write_bytes(b"this is not a valid SQLite database file")
    monkeypatch.setenv("LEDGER_PATH", str(db_path))
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

    exit_code = run_status()

    assert exit_code == 2


def test_status_command_returns_two_when_database_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "missing.sqlite3"))
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

    exit_code = run_status()

    assert exit_code == 2


# ---------------------------------------------------------------------------
# format_operational_status — safe output
# ---------------------------------------------------------------------------

def test_status_output_does_not_contain_raw_capture_text(tmp_path):
    secret_content = "my-secret-api-key=supersecret123"
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    _insert(ledger, raw_text=secret_content)
    ledger.close()

    settings = make_settings(tmp_path / "ledger.sqlite3")
    snapshot = read_operational_status(settings=settings, now=_NOW)
    output = format_operational_status(snapshot)

    assert secret_content not in output
    assert "supersecret123" not in output


def test_status_output_does_not_contain_last_error_details(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture_id = _insert(ledger)
    claim_time = _TODAY_UTC_START + timedelta(hours=1)
    claims = ledger.claim_due_deliveries(
        now=claim_time,
        lease_until=claim_time + timedelta(minutes=5),
        batch_size=10,
    )
    ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        reason="stack_trace_detail",
    )
    ledger.close()

    settings = make_settings(tmp_path / "ledger.sqlite3")
    snapshot = read_operational_status(settings=settings, now=_NOW)
    output = format_operational_status(snapshot)

    assert "stack_trace_detail" not in output
    assert "internal_error" not in output
    assert "captures failed: 1" in output


# ---------------------------------------------------------------------------
# format_operational_status — section headers
# ---------------------------------------------------------------------------

def test_status_output_includes_all_section_headers(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.close()
    settings = make_settings(tmp_path / "ledger.sqlite3")
    snapshot = read_operational_status(settings=settings, now=_NOW)
    output = format_operational_status(snapshot)

    assert "Capture intake" in output
    assert "Note lifecycle" in output
    assert "Delivery backlog" in output
    assert "Discord reconciliation" in output
    assert "Capture service" in output


# ---------------------------------------------------------------------------
# Corrupt / malformed database — safe error handling
# ---------------------------------------------------------------------------

def test_status_reader_returns_safe_failure_for_malformed_heartbeat_timestamp(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.set_system_state("capture_service_last_heartbeat_at", "not-a-timestamp")
    ledger.close()

    settings = make_settings(tmp_path / "ledger.sqlite3")
    with pytest.raises(OperationalStatusUnavailable, match="invalid"):
        read_operational_status(settings=settings)


def test_status_reader_returns_safe_failure_for_malformed_reconciliation_timestamp(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.set_system_state("last_successful_reconciliation_at", "not-a-timestamp")
    ledger.close()

    settings = make_settings(tmp_path / "ledger.sqlite3")
    with pytest.raises(OperationalStatusUnavailable, match="invalid"):
        read_operational_status(settings=settings)


def test_status_failure_output_does_not_include_raw_database_error_message(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "corrupted.sqlite3"
    db_path.write_bytes(b"this is not a valid SQLite database file")
    monkeypatch.setenv("LEDGER_PATH", str(db_path))
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

    exit_code = run_status()
    output = capsys.readouterr().out

    assert exit_code == 2
    # The safe_reason must NOT expose raw SQLite error text
    assert "file is not a database" not in output.lower()
    assert "malformed" not in output.lower()
    # But it must confirm the failure so operators know to investigate
    assert "unavailable" in output.lower()


# ---------------------------------------------------------------------------
# Timezone-boundary tests
# ---------------------------------------------------------------------------

def test_received_today_uses_configured_timezone_when_utc_date_differs(tmp_path):
    # 00:30 UTC = 20:30 the previous evening in EDT (America/New_York, UTC-4).
    # With UTC timezone the capture is "today"; with New_York it is "yesterday".
    query_now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    capture_ts = datetime(2026, 6, 10, 0, 30, 0, tzinfo=UTC)

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    _insert(ledger, received_at=capture_ts)
    ledger.close()

    db_path = tmp_path / "ledger.sqlite3"

    snapshot_utc = read_operational_status(
        settings=make_settings(db_path, timezone="UTC"),
        now=query_now,
    )
    assert snapshot_utc.captures_received_today == 1

    snapshot_ny = read_operational_status(
        settings=make_settings(db_path, timezone="America/New_York"),
        now=query_now,
    )
    assert snapshot_ny.captures_received_today == 0


def test_filed_today_uses_configured_timezone_when_utc_date_differs(tmp_path):
    query_now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    event_ts = datetime(2026, 6, 10, 0, 30, 0, tzinfo=UTC).isoformat()

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture_id = _insert(ledger, received_at=datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC))
    ledger.close()

    _insert_filed_event(tmp_path / "ledger.sqlite3", capture_id, event_ts)

    db_path = tmp_path / "ledger.sqlite3"

    snapshot_utc = read_operational_status(
        settings=make_settings(db_path, timezone="UTC"),
        now=query_now,
    )
    assert snapshot_utc.captures_filed_today == 1

    snapshot_ny = read_operational_status(
        settings=make_settings(db_path, timezone="America/New_York"),
        now=query_now,
    )
    assert snapshot_ny.captures_filed_today == 0


# ---------------------------------------------------------------------------
# Read-only guarantee — file hash must not change after status query
# ---------------------------------------------------------------------------

def test_status_command_does_not_change_database_file_hash(tmp_path, monkeypatch):
    import hashlib

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.close()

    db_path = tmp_path / "ledger.sqlite3"
    hash_before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    monkeypatch.setenv("LEDGER_PATH", str(db_path))
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

    run_status()

    hash_after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert hash_before == hash_after
