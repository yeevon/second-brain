"""Unit tests for ledger replay-safety: _schedule_retry and _mark_delivery_failed_terminally."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from secondbrain.capture_models import (
    COMPLETE,
    DELIVERY_FAILED,
    DELIVERY_FORWARDED,
    FORWARDING,
    RETRY_WAIT,
)
from secondbrain.ledger import (
    FILED,
    INBOX,
    Ledger,
)

_NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
_LEASE = _NOW + timedelta(seconds=300)


# ── Setup helpers ─────────────────────────────────────────────────────────────


def _make_ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite3")


def _insert(ledger: Ledger, msg_id: str = "1001") -> str:
    result = ledger.insert_accepted_capture(
        discord_message_id=msg_id,
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="test note",
        received_at=_NOW,
    )
    return result.capture.capture_id


def _claim(ledger: Ledger, capture_id: str) -> int:
    """Claim the capture for delivery; returns the new delivery_attempts value."""
    claimed = ledger.claim_due_deliveries(
        now=_NOW, lease_until=_LEASE, batch_size=10
    )
    match = next((c for c in claimed if c.capture_id == capture_id), None)
    assert match is not None, "capture was not claimed"
    return match.delivery_attempts


def _forwarded(ledger: Ledger, capture_id: str, delivery_attempt: int) -> None:
    """Advance from FORWARDING to DELIVERY_FORWARDED (prerequisite for mark_filed)."""
    result = ledger.mark_forwarded(
        capture_id=capture_id,
        delivery_attempt=delivery_attempt,
        lease_until=_LEASE,
    )
    assert result.outcome == "changed"


def _retry_settings() -> dict:
    return {
        "error_type": "timeout_error",
        "reason_type": "webhook_error",
        "max_attempts": 5,
        "base_delay_seconds": 10,
        "max_delay_seconds": 300,
    }


# ── _schedule_retry replay-safety ────────────────────────────────────────────


def test_schedule_retry_returns_retry_scheduled_on_first_failure(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)

    disposition = ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=attempt,
        now=_NOW,
        **_retry_settings(),
    )

    assert disposition.outcome == "retry_scheduled"
    assert disposition.retry_scheduled is True
    assert disposition.failed_terminally is False
    assert disposition.next_attempt_at is not None


def test_schedule_retry_returns_terminal_failure_at_max_attempts(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)

    settings = _retry_settings()
    settings["max_attempts"] = 1  # fail immediately

    disposition = ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=attempt,
        now=_NOW,
        **settings,
    )

    assert disposition.outcome == "terminal_failure"
    assert disposition.failed_terminally is True
    assert disposition.retry_scheduled is False


def test_schedule_retry_returns_ignored_stale_attempt(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)

    stale_attempt = attempt - 1  # deliberately wrong

    disposition = ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=stale_attempt,
        now=_NOW,
        **_retry_settings(),
    )

    assert disposition.outcome == "ignored_stale_attempt"
    assert disposition.retry_scheduled is False
    assert disposition.failed_terminally is False


def test_schedule_retry_returns_ignored_already_terminal_for_complete(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)
    _forwarded(ledger, capture_id, attempt)
    ledger.mark_filed(
        capture_id=capture_id,
        delivery_attempt=attempt,
        derived_note_path="vault/note.md",
    )

    # capture is now COMPLETE — retry should be ignored
    disposition = ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=attempt,
        now=_NOW,
        **_retry_settings(),
    )

    assert disposition.outcome == "ignored_already_terminal"
    assert disposition.retry_scheduled is False


def test_schedule_retry_returns_ignored_already_terminal_for_delivery_failed(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)

    # First call causes terminal failure immediately
    settings = _retry_settings()
    settings["max_attempts"] = 1
    ledger.schedule_retry(
        capture_id=capture_id, delivery_attempt=attempt, now=_NOW, **settings
    )

    # delivery_status is now DELIVERY_FAILED — second call should be ignored
    settings["max_attempts"] = 5
    disposition = ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=attempt,
        now=_NOW,
        **settings,
    )

    assert disposition.outcome == "ignored_already_terminal"


def test_schedule_retry_returns_ignored_retry_already_scheduled(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)

    # First call schedules a retry
    ledger.schedule_retry(
        capture_id=capture_id, delivery_attempt=attempt, now=_NOW, **_retry_settings()
    )

    # delivery_status is now RETRY_WAIT — second call with same attempt is idempotent
    disposition = ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=attempt,
        now=_NOW,
        **_retry_settings(),
    )

    assert disposition.outcome == "ignored_retry_already_scheduled"
    assert disposition.retry_scheduled is True


# ── _mark_delivery_failed_terminally replay-safety ──────────────────────────


def test_mark_failed_terminally_returns_changed_on_first_call(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)

    result = ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=attempt,
        reason="webhook_error",
    )

    assert result.outcome == "changed"
    assert result.changed is True
    assert result.delivery_status == DELIVERY_FAILED


def test_mark_failed_terminally_returns_idempotent_replay_on_same_reason(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)

    ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=attempt,
        reason="webhook_error",
    )

    # Exact same call again — idempotent
    result = ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=attempt,
        reason="webhook_error",
    )

    assert result.outcome == "idempotent_replay"
    assert result.changed is False


def test_mark_failed_terminally_returns_conflicting_replay_on_different_reason(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)

    ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=attempt,
        reason="webhook_error",
    )

    # Same attempt, different reason — conflicting replay
    result = ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=attempt,
        reason="classification_error",
    )

    assert result.outcome == "conflicting_replay"
    assert result.changed is False


def test_mark_failed_terminally_returns_ignored_already_terminal_for_complete(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)
    _forwarded(ledger, capture_id, attempt)
    ledger.mark_filed(
        capture_id=capture_id,
        delivery_attempt=attempt,
        derived_note_path="vault/note.md",
    )

    result = ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=attempt,
        reason="late_error",
    )

    assert result.outcome == "ignored_already_terminal"
    assert result.changed is False


def test_mark_failed_terminally_returns_stale_attempt_for_old_delivery_number(tmp_path):
    ledger = _make_ledger(tmp_path)
    capture_id = _insert(ledger)
    attempt = _claim(ledger, capture_id)

    result = ledger.mark_delivery_failed_terminally(
        capture_id=capture_id,
        delivery_attempt=attempt - 1,  # stale
        reason="webhook_error",
    )

    assert result.outcome == "stale_attempt"
    assert result.changed is False
