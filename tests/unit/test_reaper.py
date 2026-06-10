from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from secondbrain.capture_models import (
    DELIVERY_CLASSIFYING,
    DELIVERY_FAILED,
    DELIVERY_FORWARDED,
    FAILED,
    FORWARDING,
    PENDING_FORWARD,
    RECEIVED,
    RETRY_WAIT,
)
from secondbrain.ledger import Ledger, calculate_retry_delay_seconds
from secondbrain.reaper import StaleLeaseReaper, run_stale_lease_reaper_once


_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
_LEASE = _NOW + timedelta(seconds=60)


def make_ledger(tmp_path):
    return Ledger(tmp_path / "ledger.sqlite3")


def make_settings(**overrides):
    defaults = dict(
        delivery_retry_max_attempts=5,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
        stale_lease_reaper_interval_seconds=30,
        stale_lease_reaper_batch_size=100,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _accepted(ledger, msg_id="1001"):
    return ledger.insert_accepted_capture(
        discord_message_id=msg_id,
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note",
        received_at=_NOW,
    ).capture


def _claim_and_forward(ledger, capture, lease_until=None):
    lu = lease_until or _LEASE
    ledger.claim_due_deliveries(now=_NOW, lease_until=lu, batch_size=10)
    ledger.mark_forwarded(
        capture_id=capture.capture_id,
        delivery_attempt=capture.delivery_attempts + 1,
        lease_until=lu,
    )


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------

def test_retry_backoff_starts_at_base_delay():
    assert calculate_retry_delay_seconds(retry_attempts=1, base_delay_seconds=10, max_delay_seconds=300) == 10


def test_retry_backoff_doubles_each_attempt():
    assert calculate_retry_delay_seconds(retry_attempts=2, base_delay_seconds=10, max_delay_seconds=300) == 20
    assert calculate_retry_delay_seconds(retry_attempts=3, base_delay_seconds=10, max_delay_seconds=300) == 40


def test_retry_backoff_caps_at_maximum():
    assert calculate_retry_delay_seconds(retry_attempts=10, base_delay_seconds=10, max_delay_seconds=300) == 300


# ---------------------------------------------------------------------------
# Expired lease selection
# ---------------------------------------------------------------------------

def test_reaper_selects_expired_forwarding_lease(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    # Lease has now expired
    expired_now = _LEASE + timedelta(seconds=1)
    result = ledger.reap_expired_processing_leases(
        now=expired_now,
        batch_size=10,
        retry_max_attempts=5,
        retry_base_delay_seconds=10,
        retry_max_delay_seconds=300,
    )
    assert result.scanned == 1
    assert len(result.requeued) == 1
    assert result.requeued[0].capture_id == capture.capture_id
    ledger.close()


def test_reaper_selects_expired_forwarded_lease(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    _claim_and_forward(ledger, capture)
    expired_now = _LEASE + timedelta(seconds=1)
    result = ledger.reap_expired_processing_leases(
        now=expired_now, batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert result.scanned == 1
    assert len(result.requeued) == 1
    ledger.close()


def test_reaper_selects_expired_classifying_lease(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    _claim_and_forward(ledger, capture)
    ledger.mark_classifying_delivery(
        capture_id=capture.capture_id,
        delivery_attempt=1,
        lease_until=_LEASE,
    )
    expired_now = _LEASE + timedelta(seconds=1)
    result = ledger.reap_expired_processing_leases(
        now=expired_now, batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert result.scanned == 1
    assert len(result.requeued) == 1
    ledger.close()


def test_reaper_ignores_unexpired_lease(tmp_path):
    ledger = make_ledger(tmp_path)
    _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    # Reap before lease expires
    result = ledger.reap_expired_processing_leases(
        now=_NOW,  # same as claim time — lease hasn't expired
        batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert result.scanned == 0
    ledger.close()


def test_reaper_ignores_retry_wait(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.schedule_retry(
        capture_id=capture.capture_id,
        delivery_attempt=1,
        now=_NOW,
        error_type="TestError",
        reason_type="test",
        max_attempts=5,
        base_delay_seconds=10,
        max_delay_seconds=300,
    )
    expired_now = _LEASE + timedelta(seconds=1)
    result = ledger.reap_expired_processing_leases(
        now=expired_now, batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert result.scanned == 0
    ledger.close()


def test_reaper_ignores_complete_capture(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    _claim_and_forward(ledger, capture)
    ledger.mark_filed(
        capture_id=capture.capture_id,
        delivery_attempt=1,
        derived_note_path="projects/note.md",
    )
    expired_now = _LEASE + timedelta(seconds=1)
    result = ledger.reap_expired_processing_leases(
        now=expired_now, batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert result.scanned == 0
    ledger.close()


def test_reaper_claims_only_bounded_batch(tmp_path):
    ledger = make_ledger(tmp_path)
    for i in range(5):
        cap = _accepted(ledger, str(1001 + i))
        ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=1)
    expired_now = _LEASE + timedelta(seconds=1)
    result = ledger.reap_expired_processing_leases(
        now=expired_now, batch_size=3, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert result.scanned == 3
    ledger.close()


# ---------------------------------------------------------------------------
# Retry state mutation
# ---------------------------------------------------------------------------

def test_reaper_increments_retry_attempts_transactionally(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    expired_now = _LEASE + timedelta(seconds=1)
    ledger.reap_expired_processing_leases(
        now=expired_now, batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    updated = ledger.get_capture(capture.capture_id)
    assert updated.retry_attempts == 1
    ledger.close()


def test_reaper_moves_expired_capture_to_retry_wait(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    updated = ledger.get_capture(capture.capture_id)
    assert updated.delivery_status == RETRY_WAIT
    ledger.close()


def test_reaper_clears_processing_lease(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    assert ledger.get_capture(capture.capture_id).processing_lease_until is not None
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert ledger.get_capture(capture.capture_id).processing_lease_until is None
    ledger.close()


def test_reaper_sets_next_attempt_at(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    expired_now = _LEASE + timedelta(seconds=1)
    ledger.reap_expired_processing_leases(
        now=expired_now, batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    updated = ledger.get_capture(capture.capture_id)
    assert updated.next_attempt_at is not None
    assert updated.next_attempt_at > expired_now
    ledger.close()


def test_reaper_preserves_raw_capture(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    updated = ledger.get_capture(capture.capture_id)
    assert updated.raw_text == "note"
    assert updated.discord_message_id == "1001"
    ledger.close()


def test_reaper_appends_requeued_stale_lease_event(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    events = ledger.capture_events(capture.capture_id)
    event_types = [e["event_type"] for e in events]
    assert "REQUEUED_STALE_LEASE" in event_types
    ledger.close()


def test_reaper_skips_row_changed_by_late_terminal_callback(tmp_path):
    """If a terminal callback commits between SELECT and UPDATE, rowcount=0 and row is skipped."""
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    _claim_and_forward(ledger, capture)

    # File the capture (terminal callback commits)
    ledger.mark_filed(
        capture_id=capture.capture_id,
        delivery_attempt=1,
        derived_note_path="projects/note.md",
    )

    # Reaper attempts to claim — row is now COMPLETE, so WHERE clause skips it
    expired_now = _LEASE + timedelta(seconds=1)
    result = ledger.reap_expired_processing_leases(
        now=expired_now, batch_size=10, retry_max_attempts=5,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert result.scanned == 0
    assert len(result.requeued) == 0
    ledger.close()


# ---------------------------------------------------------------------------
# Retry exhaustion
# ---------------------------------------------------------------------------

def test_reaper_marks_failed_when_retry_cap_reached(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    # retry_max_attempts=1 means next_retry_attempts(1) >= 1, so terminal
    result = ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert len(result.failed) == 1
    updated = ledger.get_capture(capture.capture_id)
    assert updated.delivery_status == DELIVERY_FAILED
    ledger.close()


def test_reaper_clears_lease_on_terminal_failure(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert ledger.get_capture(capture.capture_id).processing_lease_until is None
    ledger.close()


def test_reaper_clears_next_attempt_on_terminal_failure(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert ledger.get_capture(capture.capture_id).next_attempt_at is None
    ledger.close()


def test_reaper_appends_retry_limit_exceeded_event(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    events = ledger.capture_events(capture.capture_id)
    event_types = [e["event_type"] for e in events]
    assert "RETRY_LIMIT_EXCEEDED" in event_types
    ledger.close()


def test_reaper_returns_failed_capture_for_visible_alert(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    result = ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert len(result.failed) == 1
    assert result.failed[0].capture_id == capture.capture_id
    ledger.close()


def test_reaper_never_requeues_failed_capture_again(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    # Run reaper again
    result2 = ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=2), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert result2.scanned == 0
    ledger.close()


# ---------------------------------------------------------------------------
# Manual retry
# ---------------------------------------------------------------------------

def test_manual_retry_moves_terminal_failed_capture_to_retry_wait(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    assert ledger.get_capture(capture.capture_id).delivery_status == DELIVERY_FAILED

    changed = ledger.manual_retry_capture(capture_id=capture.capture_id, now=_NOW)
    assert changed is True
    updated = ledger.get_capture(capture.capture_id)
    assert updated.status == RECEIVED
    assert updated.delivery_status == RETRY_WAIT
    ledger.close()


def test_manual_retry_resets_retry_attempts(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    ledger.manual_retry_capture(capture_id=capture.capture_id, now=_NOW)
    assert ledger.get_capture(capture.capture_id).retry_attempts == 0
    ledger.close()


def test_manual_retry_sets_next_attempt_at_to_now(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    retry_now = _NOW + timedelta(hours=1)
    ledger.manual_retry_capture(capture_id=capture.capture_id, now=retry_now)
    updated = ledger.get_capture(capture.capture_id)
    assert updated.next_attempt_at is not None
    assert abs((updated.next_attempt_at - retry_now).total_seconds()) < 2
    ledger.close()


def test_manual_retry_preserves_raw_text(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    ledger.manual_retry_capture(capture_id=capture.capture_id, now=_NOW)
    assert ledger.get_capture(capture.capture_id).raw_text == "note"
    ledger.close()


def test_manual_retry_preserves_delivery_attempt_history(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    original_attempts = ledger.get_capture(capture.capture_id).delivery_attempts
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    ledger.manual_retry_capture(capture_id=capture.capture_id, now=_NOW)
    # delivery_attempts should be preserved (reaper doesn't touch it)
    assert ledger.get_capture(capture.capture_id).delivery_attempts == original_attempts
    ledger.close()


def test_manual_retry_appends_event(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    ledger.manual_retry_capture(capture_id=capture.capture_id, now=_NOW)
    events = ledger.capture_events(capture.capture_id)
    assert "MANUAL_RETRY_REQUESTED" in [e["event_type"] for e in events]
    ledger.close()


def test_manual_retry_rejects_non_failed_capture(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    # Capture is RECEIVED/PENDING_FORWARD, not FAILED
    changed = ledger.manual_retry_capture(capture_id=capture.capture_id, now=_NOW)
    assert changed is False
    ledger.close()


def test_manual_retry_does_not_create_duplicate_capture(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = _accepted(ledger)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    ledger.reap_expired_processing_leases(
        now=_LEASE + timedelta(seconds=1), batch_size=10, retry_max_attempts=1,
        retry_base_delay_seconds=10, retry_max_delay_seconds=300,
    )
    ledger.manual_retry_capture(capture_id=capture.capture_id, now=_NOW)
    # Should still be exactly 1 capture
    from secondbrain.capture_models import RECEIVED
    captures = ledger.captures_by_status(RECEIVED)
    matching = [c for c in captures if c.capture_id == capture.capture_id]
    assert len(matching) == 1
    ledger.close()


# ---------------------------------------------------------------------------
# Single-flight guarantee
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reaper_run_once_skips_when_pass_is_already_running(tmp_path, capsys):
    """Acquiring the lock externally and calling run_once should log overlap_skipped."""
    from secondbrain.capture_models import LeaseReaperResult

    class FakeLedger:
        def reap_expired_processing_leases(self, **kwargs):
            return LeaseReaperResult(scanned=0, requeued=(), failed=())

    settings = make_settings()
    reaper = StaleLeaseReaper(settings=settings, ledger=FakeLedger())

    # Hold the lock to simulate a running pass
    async with reaper._lock:
        result = await reaper.run_once()

    output = capsys.readouterr().out
    assert "stale_lease_reaper_overlap_skipped" in output
    assert result.scanned == 0


@pytest.mark.asyncio
async def test_reaper_sequential_loop_does_not_overlap_passes(tmp_path, monkeypatch):
    """run_stale_lease_reaper never starts a second pass while the first is still running."""
    from secondbrain.capture_models import LeaseReaperResult
    from secondbrain.reaper import run_stale_lease_reaper

    current_passes = 0
    max_concurrent_passes = 0
    total_passes = 0

    async def tracked_reaper_once(*, settings, ledger, receipt_client=None, _now=None):
        nonlocal current_passes, max_concurrent_passes, total_passes
        current_passes += 1
        total_passes += 1
        max_concurrent_passes = max(max_concurrent_passes, current_passes)
        await asyncio.sleep(0)  # yield mid-pass to allow any concurrent start
        current_passes -= 1
        return LeaseReaperResult(scanned=0, requeued=(), failed=())

    monkeypatch.setattr("secondbrain.reaper.run_stale_lease_reaper_once", tracked_reaper_once)

    settings = make_settings(stale_lease_reaper_interval_seconds=0)
    ledger = make_ledger(tmp_path)

    task = asyncio.create_task(run_stale_lease_reaper(settings=settings, ledger=ledger))

    await asyncio.sleep(0.02)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert total_passes >= 2, f"Loop must run multiple passes; got {total_passes}"
    assert max_concurrent_passes == 1, (
        f"Passes must never overlap; max_concurrent={max_concurrent_passes}"
    )

    ledger.close()


# ---------------------------------------------------------------------------
# Receipt behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_scheduled_receipt_is_visible_and_safe(tmp_path, capsys):
    from secondbrain.receipts import format_delivery_retry_scheduled_receipt
    text = format_delivery_retry_scheduled_receipt(
        "SB-20260609-0001",
        retry_attempts=2,
        next_attempt_at=_NOW,
    )
    assert "SB-20260609-0001" in text
    assert "retry" in text.lower()
    # Must not expose raw exception or tokens
    assert "token" not in text
    assert "http" not in text.lower()


@pytest.mark.asyncio
async def test_retry_exhausted_receipt_is_visible_and_safe():
    from secondbrain.receipts import format_delivery_retry_exhausted_receipt
    text = format_delivery_retry_exhausted_receipt("SB-20260609-0001")
    assert "SB-20260609-0001" in text
    assert "manual retry" in text.lower()
    assert "token" not in text


@pytest.mark.asyncio
async def test_manual_retry_receipt_is_visible_and_safe():
    from secondbrain.receipts import format_manual_retry_accepted_receipt
    text = format_manual_retry_accepted_receipt("SB-20260609-0001")
    assert "SB-20260609-0001" in text
    assert "retry" in text.lower()


@pytest.mark.asyncio
async def test_receipt_edit_failure_does_not_abort_remaining_reaper_alerts(tmp_path, capsys):
    ledger = make_ledger(tmp_path)
    settings = make_settings()

    # Insert two captures, both with expired leases
    cap1 = _accepted(ledger, "1001")
    cap2 = _accepted(ledger, "1002")
    ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    expired_now = _LEASE + timedelta(seconds=1)

    fail_count = [0]

    class FailingReceiptClient:
        async def edit_receipt(self, *, capture_id: str, content: str) -> None:
            fail_count[0] += 1
            raise RuntimeError("Discord unavailable")

    await run_stale_lease_reaper_once(
        settings=settings,
        ledger=ledger,
        receipt_client=FailingReceiptClient(),
        _now=expired_now,
    )
    # Both receipts attempted despite first failure
    assert fail_count[0] == 2
    ledger.close()
