"""Tests for Tech Debt Milestone TD-05 (SB-149 through SB-159)."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secondbrain.capture_models import DELIVERY_FORWARDED, FILED, FORWARDING, INBOX, RECEIVED
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger
from secondbrain.models import Classification

from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
_ROOT = Path(__file__).parent.parent.parent


def _make_settings(tmp_path, **overrides):
    data = dict(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        startup_reconcile_enabled=True,
        ledger_path=tmp_path / "runtime" / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
        downstream_delivery_enabled=False,
        delivery_retry_max_attempts=3,
        delivery_retry_base_delay_seconds=1,
        delivery_retry_max_delay_seconds=10,
        delivery_forward_lease_seconds=60,
        delivery_processing_lease_seconds=300,
        delivery_dispatch_interval_seconds=2,
        delivery_dispatch_batch_size=25,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _make_ledger(tmp_path):
    return Ledger(tmp_path / "ledger.sqlite3")


def _insert_capture(ledger, *, msg_id="1001"):
    return ledger.insert_accepted_capture(
        discord_message_id=msg_id,
        discord_channel_id="200",
        discord_guild_id="100",
        discord_author_id="300",
        raw_text="test capture",
        received_at=_NOW,
    ).capture


def _make_service(tmp_path, **overrides):
    settings = _make_settings(tmp_path, **overrides)
    ledger = Ledger(settings.ledger_path)
    fake_channel = FakeDiscordChannel()
    fake_discord = FakeDiscordClient(fake_channel)
    service = CaptureService(
        settings=settings,
        ledger=ledger,
        notify_capture=None,
        receipt_client=fake_discord,
    )
    return service, ledger


# ---------------------------------------------------------------------------
# SB-149 — capture_deferred reason split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sb149_reconcile_path_logs_historical_deferred(tmp_path, capsys):
    """_persist_accepted_capture with notify_downstream=False logs historical_reconciliation_deferred."""
    service, ledger = _make_service(tmp_path, downstream_delivery_enabled=True)

    fake_message = SimpleNamespace(
        id=1001,
        guild=SimpleNamespace(id=100),
        channel=SimpleNamespace(id=200),
        author=SimpleNamespace(id=300, bot=False),
        content="test capture",
        attachments=[],
    )

    await service._persist_accepted_capture(
        fake_message,
        raw_text="test capture",
        attachment_metadata=[],
        notify_downstream=False,
    )

    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.strip().splitlines() if line.strip()]
    deferred = [e for e in events if e.get("event") == "capture_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["reason"] == "historical_reconciliation_deferred"
    ledger.close()


@pytest.mark.asyncio
async def test_sb149_service_disabled_logs_downstream_disabled(tmp_path, capsys):
    """notify_downstream=True but _notify_capture=None and downstream_delivery_enabled=False → downstream processing disabled."""
    service, ledger = _make_service(tmp_path, downstream_delivery_enabled=False)

    fake_message = SimpleNamespace(
        id=1002,
        guild=SimpleNamespace(id=100),
        channel=SimpleNamespace(id=200),
        author=SimpleNamespace(id=300, bot=False),
        content="test capture",
        attachments=[],
    )

    await service._persist_accepted_capture(
        fake_message,
        raw_text="test capture",
        attachment_metadata=[],
        notify_downstream=True,
    )

    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.strip().splitlines() if line.strip()]
    deferred = [e for e in events if e.get("event") == "capture_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["reason"] == "downstream processing disabled"
    ledger.close()


# ---------------------------------------------------------------------------
# SB-154 — Rename delivery event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sb154_idempotent_replay_logs_correct_event(tmp_path, capsys):
    """mark_forwarded returning idempotent_replay logs delivery_idempotent_replay_accepted."""
    from secondbrain.delivery import _run_one_dispatch_pass

    ledger = _make_ledger(tmp_path)
    capture = _insert_capture(ledger, msg_id="5001")
    settings = _make_settings(tmp_path)

    now = _NOW
    lease = now + timedelta(seconds=60)

    # Claim the row so delivery_status becomes FORWARDING
    ledger.claim_due_deliveries(now=now, lease_until=lease, batch_size=10)
    # First mark_forwarded: sets status to DELIVERY_FORWARDED
    ledger.mark_forwarded(
        capture_id=capture.capture_id,
        delivery_attempt=1,
        lease_until=lease,
    )

    # Put the row back to PENDING_FORWARD with a new attempt so the dispatcher
    # claims it again, then a forward succeeds, then mark_forwarded sees DELIVERY_FORWARDED
    # and returns idempotent_replay.
    # Simplest: directly call mark_forwarded a second time while status is DELIVERY_FORWARDED.
    # The dispatch pass itself calls mark_forwarded after forward_capture succeeds.
    # We simulate this by:
    # 1. Resetting the row to FORWARDING via the ledger's internal path.
    # 2. Running the dispatch pass with a client that always succeeds.
    # But the claimed row must have delivery_status=FORWARDING.
    # Since claim_due_deliveries only touches PENDING_FORWARD rows, we need another approach.
    #
    # The cleanest way: directly exercise the idempotent_replay branch in _run_one_dispatch_pass
    # by injecting a ledger that returns idempotent_replay from mark_forwarded.

    class _IdempotentLedger:
        """Fake ledger that returns one FORWARDING capture and idempotent_replay on mark_forwarded."""
        def claim_due_deliveries(self, *, now, lease_until, batch_size):
            return [SimpleNamespace(
                capture_id=capture.capture_id,
                delivery_attempts=1,
            )]

        def mark_forwarded(self, *, capture_id, delivery_attempt, lease_until):
            from secondbrain.capture_models import DeliveryMutationResult
            return DeliveryMutationResult(
                capture_id=capture_id,
                delivery_status="DELIVERY_FORWARDED",
                delivery_attempts=delivery_attempt,
                changed=False,
                outcome="idempotent_replay",
            )

        def schedule_retry(self, **kwargs):
            pass

    capsys.readouterr()

    class _SucceedClient:
        async def forward_capture(self, *, capture_id: str, delivery_attempt: int) -> None:
            pass

    await _run_one_dispatch_pass(
        settings=settings,
        ledger=_IdempotentLedger(),
        downstream_client=_SucceedClient(),
        _now=now,
    )

    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.strip().splitlines() if line.strip()]
    event_names = [e["event"] for e in events]
    assert "delivery_idempotent_replay_accepted" in event_names
    assert "duplicate_delivery_acceptance_ignored" not in event_names
    replay_events = [e for e in events if e["event"] == "delivery_idempotent_replay_accepted"]
    assert replay_events[0]["outcome"] == "idempotent_replay"
    ledger.close()


def test_sb154_old_event_name_absent():
    """The old event name 'duplicate_delivery_acceptance_ignored' must not appear in delivery.py."""
    delivery_source = (_ROOT / "src" / "secondbrain" / "delivery.py").read_text()
    assert "duplicate_delivery_acceptance_ignored" not in delivery_source


# ---------------------------------------------------------------------------
# SB-157 — log_metadata safe-field enforcement
# ---------------------------------------------------------------------------


def test_sb157_traceback_in_field_is_redacted(capsys):
    from secondbrain.observability import log_metadata

    tb = "Traceback (most recent call last):\n  File foo.py, line 1\nValueError: oops"
    log_metadata("test_event", last_error=tb)

    output = capsys.readouterr().out
    event = json.loads(output.strip())
    assert "Traceback" not in event["last_error"]
    assert "redacted" in event["last_error"]


def test_sb157_exception_body_in_field_is_redacted(capsys):
    from secondbrain.observability import log_metadata

    exc_body = "ConnectionError: failed to connect to host after multiple retries and timeouts exceeded"
    log_metadata("test_event", last_error=exc_body)

    output = capsys.readouterr().out
    event = json.loads(output.strip())
    assert "ConnectionError: failed" not in event["last_error"]
    assert "redacted" in event["last_error"]


def test_sb157_error_type_classname_passes_through(capsys):
    from secondbrain.observability import log_metadata

    log_metadata("test_event", error_type="ValueError")

    output = capsys.readouterr().out
    event = json.loads(output.strip())
    assert event["error_type"] == "ValueError"


def test_sb157_long_string_is_truncated(capsys):
    from secondbrain.observability import log_metadata

    long_val = "x" * 600
    log_metadata("test_event", some_field=long_val)

    output = capsys.readouterr().out
    event = json.loads(output.strip())
    assert "truncated" in event["some_field"]
    assert len(event["some_field"]) < 520


def test_sb157_long_traceback_is_redacted_not_truncated(capsys):
    from secondbrain.observability import log_metadata

    tb = "Traceback (most recent call last):\n" + ("  File foo.py, line 1\n" * 40) + "ValueError: oops"
    assert len(tb) > 500
    log_metadata("test_event", last_error=tb)

    output = capsys.readouterr().out
    event = json.loads(output.strip())
    # Must be redacted (not just truncated)
    assert "redacted" in event["last_error"]
    assert "truncated" not in event["last_error"]
    assert "Traceback" not in event["last_error"]


# ---------------------------------------------------------------------------
# SB-153 — STARTUP_RECONCILE_ENABLED flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sb153_false_skips_reconcile_and_logs_event(tmp_path, capsys):
    """startup_reconcile_enabled=False skips reconcile and emits disabled event."""
    from secondbrain.app import CaptureOnlyStartup

    settings = _make_settings(tmp_path, startup_reconcile_enabled=False)
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=None)

    reconcile_called = []

    async def _fake_reconcile(client):
        reconcile_called.append(True)
        from secondbrain.reconcile import ReconcileResult
        return ReconcileResult()

    service.startup_reconcile = _fake_reconcile

    startup = CaptureOnlyStartup(capture_service=service, settings=settings)
    result = await startup.start_once(client=None)

    assert reconcile_called == [], "startup_reconcile must NOT be called when disabled"
    assert result is None

    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.strip().splitlines() if line.strip()]
    event_names = [e["event"] for e in events]
    assert "startup_reconcile_disabled_for_local_smoke" in event_names
    ledger.close()


@pytest.mark.asyncio
async def test_sb153_true_calls_reconcile(tmp_path):
    """startup_reconcile_enabled=True calls startup_reconcile."""
    from secondbrain.app import CaptureOnlyStartup
    from secondbrain.reconcile import ReconcileResult

    settings = _make_settings(tmp_path, startup_reconcile_enabled=True)
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=None)

    reconcile_called = []

    async def _fake_reconcile(client):
        reconcile_called.append(True)
        return ReconcileResult()

    service.startup_reconcile = _fake_reconcile

    startup = CaptureOnlyStartup(capture_service=service, settings=settings)
    result = await startup.start_once(client=None)

    assert reconcile_called == [True], "startup_reconcile MUST be called when enabled"
    assert result is not None
    ledger.close()


def test_sb153_invalid_value_raises(monkeypatch):
    """_parse_bool_env raises ValueError for invalid values."""
    from secondbrain.config import _parse_bool_env

    monkeypatch.setenv("STARTUP_RECONCILE_ENABLED", "banana")
    with pytest.raises(ValueError, match="must be 'true' or 'false'"):
        _parse_bool_env("STARTUP_RECONCILE_ENABLED")


def test_sb153_unset_defaults_to_true(monkeypatch):
    """_parse_bool_env returns True when env var is unset."""
    from secondbrain.config import _parse_bool_env

    monkeypatch.delenv("STARTUP_RECONCILE_ENABLED", raising=False)
    assert _parse_bool_env("STARTUP_RECONCILE_ENABLED") is True


# ---------------------------------------------------------------------------
# SB-152 — CLASSIFIER_MODE deterministic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sb152_deterministic_mode_returns_fixed_classification(monkeypatch):
    """CLASSIFIER_MODE=deterministic returns the fixed classification."""
    monkeypatch.setenv("CLASSIFIER_MODE", "deterministic")

    from secondbrain.classifier import classify_capture

    result = await classify_capture(
        "test capture text",
        api_key="fake",
        model="fake-model",
        confidence_threshold=0.75,
    )

    assert result.classification.folder == "inbox"
    assert result.classification.confidence == 1.0
    assert "smoke-test" in result.classification.tags
    assert result.route == "inbox"


@pytest.mark.asyncio
async def test_sb152_deterministic_does_not_require_gemini_key(monkeypatch):
    """CLASSIFIER_MODE=deterministic works without GEMINI_API_KEY."""
    monkeypatch.setenv("CLASSIFIER_MODE", "deterministic")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    from secondbrain.classifier import classify_capture

    # Must not raise even with api_key=""
    result = await classify_capture(
        "test",
        api_key="",
        model="fake",
        confidence_threshold=0.75,
    )
    assert result.classification.confidence == 1.0


def test_sb152_invalid_classifier_mode_raises(monkeypatch):
    """CLASSIFIER_MODE=magic raises ValueError."""
    monkeypatch.setenv("CLASSIFIER_MODE", "magic")

    from secondbrain.classifier import _get_classifier_mode

    with pytest.raises(ValueError, match="CLASSIFIER_MODE must be one of"):
        _get_classifier_mode()


@pytest.mark.asyncio
async def test_sb152_gemini_mode_selected_when_unset(monkeypatch):
    """Unset CLASSIFIER_MODE defaults to gemini and calls the Gemini client."""
    monkeypatch.delenv("CLASSIFIER_MODE", raising=False)

    from secondbrain.classifier import classify_capture

    mock_response = MagicMock()
    mock_response.parsed = None
    mock_response.text = json.dumps({
        "folder": "inbox",
        "project": None,
        "note_type": "note",
        "note_date": None,
        "title": "Test",
        "tags": [],
        "body": "body",
        "actions": [],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.9,
    })

    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    result = await classify_capture(
        "test",
        api_key="fake-key",
        model="gemini-test",
        confidence_threshold=0.75,
        client=mock_client,
    )

    mock_client.aio.models.generate_content.assert_called_once()
    assert result.route == "inbox"


# ---------------------------------------------------------------------------
# SB-158 — last_vault_write_capture_id in system_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sb158_last_vault_write_capture_id_written(tmp_path):
    """acknowledge_delivery_filed writes last_vault_write_capture_id to system_state."""
    service, ledger = _make_service(tmp_path)
    capture = _insert_capture(ledger)

    now = _NOW
    lease = now + timedelta(seconds=60)
    ledger.claim_due_deliveries(now=now, lease_until=lease, batch_size=10)
    ledger.mark_forwarded(
        capture_id=capture.capture_id,
        delivery_attempt=1,
        lease_until=lease,
    )

    await service.acknowledge_delivery_filed(
        capture_id=capture.capture_id,
        delivery_attempt=1,
        derived_note_path="projects/test.md",
    )

    stored = ledger.get_system_state("last_vault_write_capture_id")
    assert stored == capture.capture_id
    ledger.close()


def test_sb158_status_output_shows_capture_id(tmp_path):
    """format_operational_status includes last_vault_write_capture_id."""
    from secondbrain.status import StatusSettings, read_operational_status, format_operational_status

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = _insert_capture(ledger)
    ledger.set_system_states({
        "last_vault_write_at": _NOW.isoformat(),
        "last_vault_write_capture_id": capture.capture_id,
    })
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    report = format_operational_status(snapshot)

    assert capture.capture_id in report
    assert "last vault write capture:" in report


# ---------------------------------------------------------------------------
# SB-150 — n8n-init login 401 handling
# ---------------------------------------------------------------------------


def _load_n8n_init(monkeypatch):
    monkeypatch.setenv("N8N_LOCAL_PASSWORD", "s3cr3t-pass")
    monkeypatch.setenv("N8N_INTAKE_WEBHOOK_TOKEN", "tok" * 10)
    monkeypatch.setenv("CAPTURE_SERVICE_INTERNAL_TOKEN", "tok" * 10)
    monkeypatch.setenv("WRITER_SERVICE_TOKEN", "tok" * 10)
    monkeypatch.setenv("GEMINI_API_KEY", "key")

    fake_wf_content = json.dumps({"name": "fake", "nodes": [], "connections": {}})

    def _open_side_effect(path, *args, **kwargs):
        """Return a fresh StringIO for each workflow file open."""
        return io.StringIO(fake_wf_content)

    spec = importlib.util.spec_from_file_location(
        f"n8n_init_{id(monkeypatch)}",
        _ROOT / "deploy" / "local-n8n-init.py",
    )
    mod = importlib.util.module_from_spec(spec)
    with patch("builtins.open", side_effect=_open_side_effect):
        spec.loader.exec_module(mod)
    return mod


def test_sb150_login_401_exits_with_remediation(monkeypatch, capsys):
    """login() with 401 response exits(1) and prints stale-volume remediation to stderr."""
    mod = _load_n8n_init(monkeypatch)

    with patch.object(mod, "_api", return_value=(401, {})):
        with pytest.raises(SystemExit) as exc_info:
            mod.login()

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "stale" in stderr.lower() or "401" in stderr
    # Password must not appear in the error message
    assert "s3cr3t-pass" not in stderr


# ---------------------------------------------------------------------------
# SB-151 — n8n-init retry helper
# ---------------------------------------------------------------------------


def test_sb151_retry_raises_after_exhaustion(monkeypatch):
    """_retry raises after exhausting all attempts."""
    mod = _load_n8n_init(monkeypatch)

    call_count = 0

    def always_fails():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("always fails")

    with pytest.raises(RuntimeError, match="always fails"):
        mod._retry(always_fails, attempts=3, delay_s=0, label="test")

    assert call_count == 3


def test_sb151_retry_succeeds_on_eventual_success(monkeypatch):
    """_retry returns value after failing twice then succeeding."""
    mod = _load_n8n_init(monkeypatch)

    call_count = 0

    def fails_twice():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError("not yet")
        return "success"

    result = mod._retry(fails_twice, attempts=5, delay_s=0, label="test")
    assert result == "success"
    assert call_count == 3
