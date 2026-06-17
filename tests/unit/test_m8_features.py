"""Tests for Milestone 8 tech-debt resolution features (SB-130 through SB-135)."""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from secondbrain.ledger import Ledger
from secondbrain.migrations import (
    ColumnSpec,
    Migration,
    SchemaAssertion,
    _apply,
)
from secondbrain.sqlite_runtime import SQLiteRuntime


# ---------------------------------------------------------------------------
# SB-130 TD-007: Constructor validation
# ---------------------------------------------------------------------------


def test_sqlite_runtime_rejects_none_database_path(tmp_path):
    with pytest.raises(ValueError, match="database_path must not be empty or None"):
        SQLiteRuntime(None)


def test_sqlite_runtime_rejects_empty_database_path(tmp_path):
    with pytest.raises(ValueError, match="database_path must not be empty or None"):
        SQLiteRuntime("   ")


def test_sqlite_runtime_rejects_zero_queue_maxsize(tmp_path):
    with pytest.raises(ValueError, match="job_queue_maxsize must be positive"):
        SQLiteRuntime(tmp_path / "test.sqlite3", job_queue_maxsize=0)


def test_sqlite_runtime_rejects_negative_queue_maxsize(tmp_path):
    with pytest.raises(ValueError, match="job_queue_maxsize must be positive"):
        SQLiteRuntime(tmp_path / "test.sqlite3", job_queue_maxsize=-1)


def test_sqlite_runtime_rejects_negative_retry_attempts(tmp_path):
    with pytest.raises(ValueError, match="retry_attempts must not be negative"):
        SQLiteRuntime(tmp_path / "test.sqlite3", retry_attempts=-1)


def test_sqlite_runtime_accepts_zero_retry_attempts(tmp_path, capsys):
    rt = SQLiteRuntime(tmp_path / "test.sqlite3", retry_attempts=0)
    rt.close()


# ---------------------------------------------------------------------------
# SB-130 TD-004: Startup watchdog
# ---------------------------------------------------------------------------


def test_sqlite_runtime_startup_timeout_raises(tmp_path, capsys, monkeypatch):
    """Startup watchdog raises RuntimeError if worker does not signal ready in time."""
    import threading
    db_path = tmp_path / "test.sqlite3"

    original_start = SQLiteRuntime.__init__

    def patched_worker(self) -> None:
        import time
        time.sleep(10)

    monkeypatch.setattr(SQLiteRuntime, "_worker", patched_worker)
    with pytest.raises(RuntimeError, match="did not start within"):
        SQLiteRuntime(db_path, startup_timeout_s=0.05)


# ---------------------------------------------------------------------------
# SB-130 TD-003: Queue metrics
# ---------------------------------------------------------------------------


def test_sqlite_runtime_emits_queue_depth_log(tmp_path, capsys):
    rt = SQLiteRuntime(tmp_path / "test.sqlite3")
    rt.read(lambda conn: None, operation_name="test_op")
    rt.close()
    output = capsys.readouterr().out
    assert "sqlite_queue_depth" in output
    assert '"operation_name":"test_op"' in output


def test_sqlite_runtime_emits_queue_wait_ms_log(tmp_path, capsys):
    rt = SQLiteRuntime(tmp_path / "test.sqlite3")
    rt.read(lambda conn: None, operation_name="wait_test")
    rt.close()
    output = capsys.readouterr().out
    assert "sqlite_queue_wait_ms" in output


def test_sqlite_runtime_emits_job_duration_ms_log(tmp_path, capsys):
    rt = SQLiteRuntime(tmp_path / "test.sqlite3")
    rt.write(lambda conn: None, operation_name="duration_test")
    rt.close()
    output = capsys.readouterr().out
    assert "sqlite_job_duration_ms" in output


# ---------------------------------------------------------------------------
# SB-130 TD-004: Shutdown watchdog
# ---------------------------------------------------------------------------


def test_sqlite_runtime_shutdown_timeout_logs_warning(tmp_path, capsys, monkeypatch):
    """Shutdown watchdog logs a warning when the worker thread takes too long to stop."""
    import threading
    import time

    rt = SQLiteRuntime(tmp_path / "test.sqlite3", shutdown_timeout_s=0)

    block_event = threading.Event()

    def blocking_op(_conn):
        block_event.wait(timeout=2)

    future_holder = []

    def submit():
        from concurrent.futures import Future
        future = rt._submit(blocking_op, write=False, operation_name="blocking")
        future_holder.append(future)

    t = threading.Thread(target=submit)
    t.start()
    time.sleep(0.02)  # let the job queue up

    rt.close()
    block_event.set()
    t.join(timeout=2)

    output = capsys.readouterr().out
    assert "sqlite_runtime_shutdown_timeout" in output


# ---------------------------------------------------------------------------
# SB-131 TD-005: Schema assertions
# ---------------------------------------------------------------------------


def test_schema_assertion_passes_for_correct_schema(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    assertion = SchemaAssertion(
        table="t",
        expected_columns=(
            ColumnSpec("name", "TEXT", not_null=True),
        ),
    )
    assertion.verify(conn)  # Should not raise
    conn.close()


def test_schema_assertion_fails_for_missing_column(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    assertion = SchemaAssertion(
        table="t",
        expected_columns=(ColumnSpec("name", "TEXT"),),
    )
    with pytest.raises(RuntimeError, match="missing from 't'"):
        assertion.verify(conn)
    conn.close()


def test_schema_assertion_fails_for_missing_table(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    assertion = SchemaAssertion(
        table="nonexistent",
        expected_columns=(ColumnSpec("id", "INTEGER"),),
    )
    with pytest.raises(RuntimeError, match="does not exist"):
        assertion.verify(conn)
    conn.close()


def test_schema_assertion_fails_for_wrong_type(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (val TEXT)")
    assertion = SchemaAssertion(
        table="t",
        expected_columns=(ColumnSpec("val", "INTEGER"),),
    )
    with pytest.raises(RuntimeError, match="has type"):
        assertion.verify(conn)
    conn.close()


def test_schema_assertion_fails_for_missing_not_null(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (name TEXT)")  # no NOT NULL
    assertion = SchemaAssertion(
        table="t",
        expected_columns=(ColumnSpec("name", "TEXT", not_null=True),),
    )
    with pytest.raises(RuntimeError, match="must be NOT NULL"):
        assertion.verify(conn)
    conn.close()


def test_migration_schema_assertion_blocks_bad_migration(tmp_path):
    """A migration with a wrong assertion rolls back and raises."""
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
        )
    """)

    bad_migration = Migration(
        version=99,
        name="bad",
        statements=("CREATE TABLE foo (bar TEXT)",),
        assertions=(
            SchemaAssertion(
                table="foo",
                expected_columns=(ColumnSpec("bar", "TEXT", not_null=True),),  # wrong: no NOT NULL
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="must be NOT NULL"):
        _apply(conn, bad_migration)

    applied = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert applied == 0  # rolled back
    conn.close()


def test_schema_assertion_passes_for_existing_index(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE INDEX idx_t_name ON t(name)")
    assertion = SchemaAssertion(
        table="t",
        expected_columns=(ColumnSpec("name", "TEXT"),),
        expected_indexes=("idx_t_name",),
    )
    assertion.verify(conn)  # should not raise
    conn.close()


def test_schema_assertion_fails_for_missing_index(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    # No index created — assertion should fail
    assertion = SchemaAssertion(
        table="t",
        expected_columns=(ColumnSpec("name", "TEXT"),),
        expected_indexes=("idx_t_name",),
    )
    with pytest.raises(RuntimeError, match="index 'idx_t_name' missing from 't'"):
        assertion.verify(conn)
    conn.close()


def test_migration_index_assertion_blocks_recording(tmp_path):
    """A missing index in expected_indexes must roll back and block migration recording."""
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
        )
    """)
    bad = Migration(
        version=88,
        name="missing_index",
        statements=("CREATE TABLE bar (val TEXT)",),
        assertions=(
            SchemaAssertion(
                table="bar",
                expected_columns=(ColumnSpec("val", "TEXT"),),
                expected_indexes=("idx_bar_val",),  # index was never created
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="index 'idx_bar_val' missing from 'bar'"):
        _apply(conn, bad)

    applied = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert applied == 0  # rolled back, not recorded
    conn.close()


# ---------------------------------------------------------------------------
# SB-133 TD-001: Exception sanitization in last_error
# ---------------------------------------------------------------------------


def test_vault_write_failure_produces_sanitized_last_error(tmp_path):
    """last_error must not contain raw exception message text."""
    import asyncio
    from tests.unit.test_worker import (
        FailingVaultWriter, FakeClient, make_capture_service, make_settings,
        insert_capture, VALID_CLASSIFICATION,
    )
    from secondbrain.worker import process_capture_once

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)

    asyncio.run(process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger),
        vault_writer=FailingVaultWriter(),
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    ))

    updated = ledger.get_capture(capture.capture_id)
    assert updated.last_error is not None
    assert "vault unavailable" not in updated.last_error  # raw exception message excluded
    assert "OSError" in updated.last_error  # error type included
    ledger.close()


# ---------------------------------------------------------------------------
# SB-133 TD-008: logging_config.py
# ---------------------------------------------------------------------------


def test_configure_logging_respects_log_level(tmp_path, monkeypatch):
    import logging
    from secondbrain.logging_config import configure_logging

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    logger = logging.getLogger("secondbrain")
    # Remove handlers so configure_logging can add one
    logger.handlers.clear()

    configure_logging()
    assert logger.level == logging.DEBUG
    logger.handlers.clear()  # clean up


def test_configure_logging_is_idempotent():
    import logging
    from secondbrain.logging_config import configure_logging

    logger = logging.getLogger("secondbrain")
    logger.handlers.clear()
    configure_logging()
    handler_count = len(logger.handlers)
    configure_logging()
    assert len(logger.handlers) == handler_count  # not doubled
    logger.handlers.clear()


# ---------------------------------------------------------------------------
# SB-134 TD-002: Shutdown step resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_step_failure_does_not_prevent_later_steps():
    """A failed shutdown step must not prevent subsequent steps from running."""
    steps_executed = []

    class FailingApiServer:
        async def stop(self):
            raise RuntimeError("api server stop failed")

    class RecordingClient:
        async def close(self):
            steps_executed.append("discord_close")

    from secondbrain.app import run_service_runtime
    from unittest.mock import AsyncMock, MagicMock

    api_task = asyncio.create_task(asyncio.sleep(0))
    discord_task = asyncio.create_task(asyncio.sleep(0))
    await asyncio.sleep(0)

    capture_service = MagicMock()
    capture_service.record_capture_service_stop = MagicMock()
    capture_service.close = MagicMock()

    startup = MagicMock()
    startup.periodic_task = None
    startup.worker_task = None
    startup.reaper_task = None
    startup.heartbeat_task = None
    startup.delivery_task = None

    # Trigger immediate stop via tasks completing
    await run_service_runtime(
        api_task=api_task,
        discord_task=discord_task,
        api_server=FailingApiServer(),
        client=RecordingClient(),
        startup=startup,
        capture_service=capture_service,
        instance_id="test-instance",
    )

    assert "discord_close" in steps_executed
    capture_service.close.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_logs_step_failure(capsys):
    """Failed shutdown steps emit a shutdown_step_failed log event."""
    import asyncio
    from secondbrain.app import run_service_runtime
    from unittest.mock import MagicMock

    class FailingApiServer:
        async def stop(self):
            raise RuntimeError("intentional failure")

    class NoopClient:
        async def close(self):
            pass

    api_task = asyncio.create_task(asyncio.sleep(0))
    discord_task = asyncio.create_task(asyncio.sleep(0))
    await asyncio.sleep(0)

    capture_service = MagicMock()
    capture_service.record_capture_service_stop = MagicMock()
    capture_service.close = MagicMock()
    startup = MagicMock()
    for attr in ("periodic_task", "worker_task", "reaper_task", "heartbeat_task", "delivery_task"):
        setattr(startup, attr, None)

    await run_service_runtime(
        api_task=api_task,
        discord_task=discord_task,
        api_server=FailingApiServer(),
        client=NoopClient(),
        startup=startup,
        capture_service=capture_service,
    )

    output = capsys.readouterr().out
    assert "shutdown_step_failed" in output
    assert "stop_api_server" in output


# ---------------------------------------------------------------------------
# SB-134 TD-009: RECEIPT_REPLACED event has reason field
# ---------------------------------------------------------------------------


def test_receipt_replaced_event_includes_reason(tmp_path):
    """RECEIPT_REPLACED event payload must include a 'reason' field."""
    import asyncio
    import json
    from secondbrain.receipts import deliver_final_receipt

    ledger = Ledger(tmp_path / "ledger.sqlite3")

    result = ledger.insert_accepted_capture(
        discord_message_id="msg-9901",
        discord_channel_id="100",
        discord_guild_id="200",
        discord_author_id="300",
        raw_text="receipt replacement test",
    )
    capture_id = result.capture.capture_id
    ledger.set_receipt_message_id(capture_id, "999999")
    record = ledger.get_capture(capture_id)

    class FailingChannel:
        async def fetch_message(self, msg_id):
            raise RuntimeError("discord unavailable")
        async def send(self, content):
            class FakeMsg:
                id = "new-receipt-111"
            return FakeMsg()

    class FailingClient:
        def get_channel(self, channel_id):
            return FailingChannel()
        async def fetch_channel(self, channel_id):
            return FailingChannel()

    delivery = asyncio.run(deliver_final_receipt(FailingClient(), record, "Final content"))

    assert delivery.replaced is True
    assert delivery.replacement_reason == "RuntimeError"

    ledger.update_capture(
        capture_id,
        receipt_message_id=delivery.receipt_message_id,
        event_type="RECEIPT_REPLACED",
        event_payload={
            "old_receipt_message_id": record.receipt_message_id,
            "new_receipt_message_id": delivery.receipt_message_id,
            "reason": delivery.replacement_reason,
        },
    )

    events = ledger.capture_events(capture_id)
    replaced_events = [e for e in events if e["event_type"] == "RECEIPT_REPLACED"]
    assert len(replaced_events) == 1
    payload = json.loads(replaced_events[0]["event_payload_json"])
    assert payload.get("reason") == "RuntimeError"
    ledger.close()


# ---------------------------------------------------------------------------
# SB-134 TD-009: receipt_repairs_today in status
# ---------------------------------------------------------------------------


def test_status_includes_receipt_repairs_today(tmp_path):
    from secondbrain.status import StatusSettings, read_operational_status

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    result = ledger.insert_accepted_capture(
        discord_message_id="msg-9902",
        discord_channel_id="100",
        discord_guild_id="200",
        discord_author_id="300",
        raw_text="status test",
    )
    capture_id = result.capture.capture_id
    ledger.update_capture(
        capture_id,
        event_type="RECEIPT_REPLACED",
        event_payload={"old_receipt_message_id": "x", "new_receipt_message_id": "y", "reason": "FakeError"},
    )
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    assert snapshot.receipt_repairs_today == 1


# ---------------------------------------------------------------------------
# SB-135 TD-011: preflight command
# ---------------------------------------------------------------------------


def _make_env(tmp_path, monkeypatch, **overrides):
    """Set env vars for preflight tests, patching load_dotenv to be a no-op."""
    monkeypatch.setattr("secondbrain.preflight.load_dotenv", lambda: None)
    # Clear all relevant env vars first
    for key in [
        "CAPTURE_PROCESSING_MODE", "DISCORD_BOT_TOKEN", "LEDGER_PATH",
        "CAPTURE_SERVICE_INTERNAL_TOKEN", "DOWNSTREAM_DELIVERY_ENABLED",
        "N8N_INTAKE_WEBHOOK_URL", "N8N_INTAKE_WEBHOOK_TOKEN",
        "GEMINI_API_KEY", "GEMINI_MODEL", "VAULT_PATH",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)


def test_preflight_fails_when_mode_missing(tmp_path, monkeypatch):
    _make_env(tmp_path, monkeypatch)
    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight()
    _, all_passed = format_preflight_results(checks)
    assert not all_passed


def test_preflight_fails_when_discord_token_missing(tmp_path, monkeypatch):
    _make_env(tmp_path, monkeypatch,
        CAPTURE_PROCESSING_MODE="capture-only",
        LEDGER_PATH=str(tmp_path / "ledger.sqlite3"),
        CAPTURE_SERVICE_INTERNAL_TOKEN="tok",
        DOWNSTREAM_DELIVERY_ENABLED="false",
    )
    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight()
    _, all_passed = format_preflight_results(checks)
    assert not all_passed
    assert any("DISCORD_BOT_TOKEN" in c.name and not c.passed for c in checks)


def test_preflight_passes_for_capture_only_mode(tmp_path, monkeypatch):
    _make_env(tmp_path, monkeypatch,
        CAPTURE_PROCESSING_MODE="capture-only",
        DISCORD_BOT_TOKEN="Bot.fake.token",
        LEDGER_PATH=str(tmp_path / "ledger.sqlite3"),
        CAPTURE_SERVICE_INTERNAL_TOKEN="secret-tok",
        DOWNSTREAM_DELIVERY_ENABLED="false",
    )
    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight()
    _, all_passed = format_preflight_results(checks)
    assert all_passed


def test_preflight_fails_when_vault_path_missing_for_local_full(tmp_path, monkeypatch):
    _make_env(tmp_path, monkeypatch,
        CAPTURE_PROCESSING_MODE="local-full",
        DISCORD_BOT_TOKEN="Bot.fake.token",
        LEDGER_PATH=str(tmp_path / "ledger.sqlite3"),
        GEMINI_API_KEY="key",
        GEMINI_MODEL="gemini-3.5-flash",
    )
    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight()
    _, all_passed = format_preflight_results(checks)
    assert not all_passed
    assert any("VAULT_PATH" in c.name and not c.passed for c in checks)


def test_preflight_fails_when_vault_path_nonexistent(tmp_path, monkeypatch):
    _make_env(tmp_path, monkeypatch,
        CAPTURE_PROCESSING_MODE="local-full",
        DISCORD_BOT_TOKEN="Bot.fake.token",
        LEDGER_PATH=str(tmp_path / "ledger.sqlite3"),
        GEMINI_API_KEY="key",
        GEMINI_MODEL="gemini-3.5-flash",
        VAULT_PATH=str(tmp_path / "nonexistent-vault"),
    )
    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight()
    _, all_passed = format_preflight_results(checks)
    assert not all_passed
    assert any("VAULT_PATH" in c.name and not c.passed for c in checks)


def test_preflight_rejects_floating_gemini_model(tmp_path, monkeypatch):
    _make_env(tmp_path, monkeypatch,
        CAPTURE_PROCESSING_MODE="local-full",
        DISCORD_BOT_TOKEN="Bot.fake.token",
        LEDGER_PATH=str(tmp_path / "ledger.sqlite3"),
        GEMINI_API_KEY="key",
        GEMINI_MODEL="gemini-flash-latest",
        VAULT_PATH=str(tmp_path),
    )
    from secondbrain.preflight import run_preflight
    checks = run_preflight()
    model_checks = [c for c in checks if "GEMINI_MODEL" in c.name]
    assert any(not c.passed for c in model_checks)


def test_preflight_fails_webhook_url_when_downstream_enabled(tmp_path, monkeypatch):
    _make_env(tmp_path, monkeypatch,
        CAPTURE_PROCESSING_MODE="capture-only",
        DISCORD_BOT_TOKEN="Bot.fake.token",
        LEDGER_PATH=str(tmp_path / "ledger.sqlite3"),
        CAPTURE_SERVICE_INTERNAL_TOKEN="tok",
        DOWNSTREAM_DELIVERY_ENABLED="true",
    )
    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight()
    _, all_passed = format_preflight_results(checks)
    assert not all_passed
    assert any("N8N_INTAKE_WEBHOOK_URL" in c.name and not c.passed for c in checks)


def test_preflight_sqlite_openable_passes_when_file_does_not_exist(tmp_path, monkeypatch):
    """Preflight SQLite check must pass (not create) when the DB file doesn't exist yet."""
    from secondbrain.preflight import _check_sqlite_openable
    ledger_path = tmp_path / "nonexistent.sqlite3"
    assert not ledger_path.exists()
    check = _check_sqlite_openable(ledger_path)
    assert check.passed
    assert not ledger_path.exists()  # must not have been created


def test_preflight_sqlite_openable_does_not_create_file(tmp_path, monkeypatch):
    """_check_sqlite_openable with mode=rw must not create the DB file."""
    from secondbrain.preflight import _check_sqlite_openable
    path = tmp_path / "should_not_exist.sqlite3"
    _check_sqlite_openable(path)
    assert not path.exists()


def test_preflight_sqlite_openable_succeeds_for_existing_db(tmp_path):
    """_check_sqlite_openable must pass for a real pre-existing SQLite file."""
    import sqlite3 as _sqlite3
    from secondbrain.preflight import _check_sqlite_openable
    db_path = tmp_path / "existing.sqlite3"
    conn = _sqlite3.connect(str(db_path))
    conn.close()
    check = _check_sqlite_openable(db_path)
    assert check.passed


def test_preflight_compose_fails_when_dotenv_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("N8N_ENV_FILE", raising=False)
    monkeypatch.delenv("N8N_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.delenv("LOCAL_VAULT_PATH", raising=False)
    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight(compose=True, compose_dir=tmp_path)
    _, all_passed = format_preflight_results(checks)
    assert not all_passed
    assert any(".env file" in c.name and not c.passed for c in checks)


def test_preflight_compose_passes_with_all_files(tmp_path, monkeypatch):
    monkeypatch.delenv("N8N_ENV_FILE", raising=False)
    monkeypatch.delenv("N8N_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.delenv("LOCAL_VAULT_PATH", raising=False)

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "CAPTURE_SERVICE_INTERNAL_TOKEN=tok1\n"
        "WRITER_SERVICE_TOKEN=tok2\n"
        "N8N_INTAKE_WEBHOOK_TOKEN=tok3\n"
        "GEMINI_API_KEY=key1\n"
    )
    (tmp_path / "n8n.local.env").write_text("N8N_HOST=localhost\n")
    (tmp_path / "n8n-encryption-key.local").write_text("deadbeef\n")

    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight(compose=True, compose_dir=tmp_path)
    _, all_passed = format_preflight_results(checks)
    assert all_passed, [c for c in checks if not c.passed]


def test_preflight_compose_fails_when_required_key_missing_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("N8N_ENV_FILE", raising=False)
    monkeypatch.delenv("N8N_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.delenv("LOCAL_VAULT_PATH", raising=False)

    dotenv = tmp_path / ".env"
    # Missing GEMINI_API_KEY
    dotenv.write_text(
        "CAPTURE_SERVICE_INTERNAL_TOKEN=tok1\n"
        "WRITER_SERVICE_TOKEN=tok2\n"
        "N8N_INTAKE_WEBHOOK_TOKEN=tok3\n"
    )
    (tmp_path / "n8n.local.env").write_text("N8N_HOST=localhost\n")
    (tmp_path / "n8n-encryption-key.local").write_text("deadbeef\n")

    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight(compose=True, compose_dir=tmp_path)
    _, all_passed = format_preflight_results(checks)
    assert not all_passed
    assert any("GEMINI_API_KEY" in c.name and not c.passed for c in checks)


def test_preflight_compose_fails_when_required_key_empty_in_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("N8N_ENV_FILE", raising=False)
    monkeypatch.delenv("N8N_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.delenv("LOCAL_VAULT_PATH", raising=False)

    dotenv = tmp_path / ".env"
    # GEMINI_API_KEY is present but has an empty value
    dotenv.write_text(
        "CAPTURE_SERVICE_INTERNAL_TOKEN=tok1\n"
        "WRITER_SERVICE_TOKEN=tok2\n"
        "N8N_INTAKE_WEBHOOK_TOKEN=tok3\n"
        "GEMINI_API_KEY=\n"
    )
    (tmp_path / "n8n.local.env").write_text("N8N_HOST=localhost\n")
    (tmp_path / "n8n-encryption-key.local").write_text("deadbeef\n")

    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight(compose=True, compose_dir=tmp_path)
    _, all_passed = format_preflight_results(checks)
    assert not all_passed
    gemini_check = next(c for c in checks if "GEMINI_API_KEY" in c.name)
    assert not gemini_check.passed
    assert "empty" in gemini_check.detail


def test_preflight_compose_checks_local_vault_path_when_set(tmp_path, monkeypatch):
    monkeypatch.delenv("N8N_ENV_FILE", raising=False)
    monkeypatch.delenv("N8N_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.setenv("LOCAL_VAULT_PATH", str(tmp_path / "nonexistent-vault"))

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "CAPTURE_SERVICE_INTERNAL_TOKEN=tok1\n"
        "WRITER_SERVICE_TOKEN=tok2\n"
        "N8N_INTAKE_WEBHOOK_TOKEN=tok3\n"
        "GEMINI_API_KEY=key1\n"
    )
    (tmp_path / "n8n.local.env").write_text("")
    (tmp_path / "n8n-encryption-key.local").write_text("")

    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight(compose=True, compose_dir=tmp_path)
    _, all_passed = format_preflight_results(checks)
    assert not all_passed
    assert any("LOCAL_VAULT_PATH" in c.name and not c.passed for c in checks)


def test_preflight_command_exit_code_0_on_pass(tmp_path, monkeypatch):
    _make_env(tmp_path, monkeypatch,
        CAPTURE_PROCESSING_MODE="capture-only",
        DISCORD_BOT_TOKEN="Bot.fake.token",
        LEDGER_PATH=str(tmp_path / "ledger.sqlite3"),
        CAPTURE_SERVICE_INTERNAL_TOKEN="tok",
        DOWNSTREAM_DELIVERY_ENABLED="false",
    )
    from secondbrain.app import main
    result = main(["preflight"])
    assert result == 0


def test_preflight_command_exit_code_1_on_failure(tmp_path, monkeypatch):
    _make_env(tmp_path, monkeypatch)
    from secondbrain.app import main
    result = main(["preflight"])
    assert result == 1


# ---------------------------------------------------------------------------
# SB-135 TD-012: Background task heartbeats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_writes_heartbeat_to_system_state(tmp_path):
    from secondbrain.reaper import run_stale_lease_reaper

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = SimpleNamespace(
        stale_lease_reaper_interval_seconds=0,
        stale_lease_reaper_batch_size=10,
        delivery_retry_max_attempts=5,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
    )

    task = asyncio.create_task(run_stale_lease_reaper(settings=settings, ledger=ledger))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    heartbeat = ledger.get_system_state("reaper_last_heartbeat_at")
    assert heartbeat is not None
    ledger.close()


def test_status_includes_background_task_fields(tmp_path):
    from secondbrain.status import StatusSettings, read_operational_status

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    now = datetime.now(UTC)
    ledger.set_system_state("reaper_last_heartbeat_at", now.isoformat())
    ledger.set_system_state("reconcile_last_heartbeat_at", now.isoformat())
    ledger.set_system_state("background_task_stale", "false")
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    assert snapshot.reaper_last_heartbeat_at is not None
    assert snapshot.reconcile_last_heartbeat_at is not None
    assert snapshot.background_task_stale is False


def test_heartbeat_logs_background_task_stale_when_reaper_is_old(tmp_path, capsys):
    from secondbrain.heartbeat import _check_background_task_liveness

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    old_ts = (datetime.now(UTC) - timedelta(seconds=1000)).isoformat()
    ledger.set_system_state("reaper_last_heartbeat_at", old_ts)

    _check_background_task_liveness(
        ledger=ledger,
        reaper_liveness_threshold_s=60,
        reconcile_liveness_threshold_s=60,
    )

    output = capsys.readouterr().out
    assert "background_task_stale" in output
    assert '"task":"reaper"' in output
    ledger.close()


# ---------------------------------------------------------------------------
# SB-135 TD-013: last_vault_write_at persisted after successful filing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acknowledge_delivery_filed_writes_last_vault_write_at(tmp_path):
    from secondbrain.capture_service import CaptureService

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    insert_result = ledger.insert_accepted_capture(
        discord_message_id="msg-9903",
        discord_channel_id="100",
        discord_guild_id="200",
        discord_author_id="300",
        raw_text="vault write test",
        initial_delivery_status="FORWARDED",
    )
    capture_id = insert_result.capture.capture_id

    settings = SimpleNamespace(
        capture_processing_mode="capture-only",
        downstream_delivery_enabled=True,
        writer_service_url=None,
        writer_service_token=None,
    )

    service = CaptureService(settings=settings, ledger=ledger)

    before = ledger.get_system_state("last_vault_write_at")
    assert before is None

    await service.acknowledge_delivery_filed(
        capture_id=capture_id,
        delivery_attempt=0,
        derived_note_path="00-inbox/test.md",
        git_commit_hash="abc123",
    )

    after = ledger.get_system_state("last_vault_write_at")
    assert after is not None
    assert datetime.fromisoformat(after)  # valid ISO datetime

    service.close()
    ledger.close()


@pytest.mark.asyncio
async def test_apply_correction_writes_last_vault_write_at(tmp_path):
    """apply_correction() must update last_vault_write_at even when a correction is applied."""
    from unittest.mock import AsyncMock
    from secondbrain.capture_service import CaptureService
    from secondbrain.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    insert_result = ledger.insert_accepted_capture(
        discord_message_id="msg-corr-1",
        discord_channel_id="100",
        discord_guild_id="200",
        discord_author_id="300",
        raw_text="correction vault write test",
        initial_delivery_status="NOT_APPLICABLE",
    )
    capture_id = insert_result.capture.capture_id
    # Pretend it's already filed so it has a note path
    ledger.transition_capture(
        capture_id,
        from_statuses={"RECEIVED"},
        to_status="CLASSIFYING",
        event_type="CAPTURE_CLASSIFYING",
        event_payload={},
    )
    from secondbrain.capture_models import FILED
    ledger.transition_capture(
        capture_id,
        from_statuses={"CLASSIFYING"},
        to_status=FILED,
        derived_note_path="00-inbox/original.md",
        classification_json={"folder": "inbox", "confidence": 0.9, "project": None, "needs_clarification": False},
        event_type="CAPTURE_FILED",
        event_payload={"path": "00-inbox/original.md"},
    )

    settings = SimpleNamespace(
        capture_processing_mode="capture-only",
        downstream_delivery_enabled=True,
        writer_service_url="http://fake",
        writer_service_token="tok",
    )

    class _FakeWriterClient:
        async def move_note(self, *, capture_id, new_folder, new_project, correction_reason):
            return {
                "old_note_path": "00-inbox/original.md",
                "new_note_path": "02-projects/moved.md",
                "git_commit_hash": "deadbeef",
            }

    service = CaptureService(settings=settings, ledger=ledger)
    service._writer_client = _FakeWriterClient()

    before = ledger.get_system_state("last_vault_write_at")
    assert before is None

    await service.apply_correction(
        capture_id=capture_id,
        new_folder="02-projects",
        correction_reason="move to projects",
    )

    after = ledger.get_system_state("last_vault_write_at")
    assert after is not None
    assert datetime.fromisoformat(after)

    service.close()
    ledger.close()


def test_status_displays_last_vault_write_at(tmp_path):
    from secondbrain.status import StatusSettings, read_operational_status, format_operational_status

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    now_iso = datetime.now(UTC).isoformat()
    ledger.set_system_state("last_vault_write_at", now_iso)
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    assert snapshot.last_vault_write_at is not None

    output = format_operational_status(snapshot)
    assert "last vault write at" in output
