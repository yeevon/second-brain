from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from secondbrain.capture_models import (
    DELIVERY_FAILED,
    DELIVERY_FORWARDED,
    FORWARDING,
    PENDING_FORWARD,
    RETRY_WAIT,
)
from secondbrain.delivery import (
    DownstreamDeliveryClient,
    _run_one_dispatch_pass,
    run_delivery_dispatcher,
)
from secondbrain.ledger import Ledger


# Fixed timestamp so generated capture IDs are date-stable
_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def make_ledger(tmp_path):
    return Ledger(tmp_path / "ledger.sqlite3")


def make_settings(**overrides):
    defaults = dict(
        delivery_retry_max_attempts=5,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
        delivery_forward_lease_seconds=60,
        delivery_processing_lease_seconds=300,
        delivery_dispatch_interval_seconds=2,
        delivery_dispatch_batch_size=25,
        delivery_reaper_interval_seconds=30,
        delivery_reaper_batch_size=100,
        discord_capture_channel_id=200,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def accepted(ledger, msg_id="1001"):
    return ledger.insert_accepted_capture(
        discord_message_id=msg_id,
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note",
        received_at=_NOW,
    ).capture


class AlwaysSucceedClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def forward_capture(self, *, capture_id: str, delivery_attempt: int) -> None:
        self.calls.append({"capture_id": capture_id, "delivery_attempt": delivery_attempt})


class AlwaysFailClient:
    async def forward_capture(self, *, capture_id: str, delivery_attempt: int) -> None:
        raise ConnectionError("downstream unavailable")


class TransactionVisibilityClient:
    """Reads the DB from a separate connection during forward_capture to verify claim committed."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.saw_forwarding: bool = False

    async def forward_capture(self, *, capture_id: str, delivery_attempt: int) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT delivery_status FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        conn.close()
        if row and row["delivery_status"] == FORWARDING:
            self.saw_forwarding = True


class FakeReceiptEditClient:
    def __init__(self):
        self.edits: list[dict] = []
        self.should_raise = False

    async def edit_receipt(self, *, capture_id: str, content: str) -> None:
        if self.should_raise:
            raise RuntimeError("receipt edit failed")
        self.edits.append({"capture_id": capture_id, "content": content})


# ---------------------------------------------------------------------------
# Dispatcher tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatcher_calls_downstream_only_after_claim_commit(tmp_path):
    ledger = make_ledger(tmp_path)
    accepted(ledger)
    client = TransactionVisibilityClient(tmp_path / "ledger.sqlite3")
    await _run_one_dispatch_pass(settings=make_settings(), ledger=ledger, downstream_client=client)
    assert client.saw_forwarding, "claim must be committed and visible before HTTP call"
    ledger.close()


@pytest.mark.asyncio
async def test_dispatcher_marks_webhook_acceptance_forwarded(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = accepted(ledger)
    client = AlwaysSucceedClient()
    await _run_one_dispatch_pass(settings=make_settings(), ledger=ledger, downstream_client=client)
    updated = ledger.get_capture(capture.capture_id)
    assert updated.delivery_status == DELIVERY_FORWARDED
    assert len(client.calls) == 1
    ledger.close()


@pytest.mark.asyncio
async def test_dispatcher_schedules_retry_after_webhook_failure(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = accepted(ledger)
    await _run_one_dispatch_pass(
        settings=make_settings(), ledger=ledger, downstream_client=AlwaysFailClient()
    )
    updated = ledger.get_capture(capture.capture_id)
    assert updated.delivery_status == RETRY_WAIT
    assert updated.next_attempt_at is not None
    ledger.close()


@pytest.mark.asyncio
async def test_dispatcher_does_not_hold_database_transaction_during_http_call(tmp_path):
    """The separate-connection visibility test confirms the claim is committed first."""
    ledger = make_ledger(tmp_path)
    accepted(ledger)
    client = TransactionVisibilityClient(tmp_path / "ledger.sqlite3")
    await _run_one_dispatch_pass(settings=make_settings(), ledger=ledger, downstream_client=client)
    assert client.saw_forwarding
    ledger.close()


@pytest.mark.asyncio
async def test_dispatcher_posts_capture_id_and_delivery_attempt(tmp_path):
    ledger = make_ledger(tmp_path)
    capture = accepted(ledger)
    client = AlwaysSucceedClient()
    await _run_one_dispatch_pass(settings=make_settings(), ledger=ledger, downstream_client=client)
    assert client.calls[0]["capture_id"] == capture.capture_id
    assert client.calls[0]["delivery_attempt"] == 1
    ledger.close()


@pytest.mark.asyncio
async def test_dispatcher_processes_bounded_batch(tmp_path):
    ledger = make_ledger(tmp_path)
    for i in range(5):
        accepted(ledger, str(1001 + i))
    client = AlwaysSucceedClient()
    await _run_one_dispatch_pass(
        settings=make_settings(delivery_dispatch_batch_size=3),
        ledger=ledger,
        downstream_client=client,
    )
    assert len(client.calls) == 3
    ledger.close()


# ---------------------------------------------------------------------------
# Dispatcher containment tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatcher_loop_survives_claim_failure(tmp_path):
    """If claim_due_deliveries raises, the loop continues rather than dying."""
    call_count = 0

    class BrokenLedger:
        def claim_due_deliveries(self, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("sqlite unavailable")
            return []

    settings = make_settings(delivery_dispatch_interval_seconds=0)

    async def run_for_a_bit():
        task = asyncio.create_task(
            run_delivery_dispatcher(
                settings=settings,
                ledger=BrokenLedger(),
                downstream_client=AlwaysSucceedClient(),
            )
        )
        # Give it time to run at least 3 iterations
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await run_for_a_bit()
    assert call_count >= 3, "loop must continue after claim failures"


@pytest.mark.asyncio
async def test_stale_while_forwarding_logs_stale_not_forwarded(tmp_path, capsys):
    """Dispatcher must log stale_delivery_acceptance_ignored (not capture_forwarded) when
    mark_forwarded returns stale_attempt.  This test executes the actual race by using a
    downstream client that advances the delivery state *before* mark_forwarded is called."""
    ledger = make_ledger(tmp_path)
    accepted(ledger)
    settings = make_settings()

    class StaleWhileForwardingClient:
        """Simulates a race: while we were forwarding, another thread scheduled a retry
        and the dispatcher reclaimed the row under a new attempt number."""

        def __init__(self, ledger, settings):
            self._ledger = ledger
            self._settings = settings

        async def forward_capture(self, *, capture_id: str, delivery_attempt: int) -> None:
            now = datetime.now(UTC)
            # Advance attempt counter so the pending mark_forwarded call will be stale
            self._ledger.schedule_retry(
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                now=now,
                error_type="SyntheticRace",
                reason_type="test_race",
                max_attempts=self._settings.delivery_retry_max_attempts,
                base_delay_seconds=1,
                max_delay_seconds=10,
            )
            self._ledger.claim_due_deliveries(
                now=now + timedelta(seconds=20),
                lease_until=now + timedelta(seconds=80),
                batch_size=10,
            )

    client = StaleWhileForwardingClient(ledger, settings)
    await _run_one_dispatch_pass(
        settings=settings,
        ledger=ledger,
        downstream_client=client,
        _now=_NOW,
    )

    output = capsys.readouterr().out
    assert "stale_delivery_acceptance_ignored" in output, (
        "stale race must be logged as stale_delivery_acceptance_ignored"
    )
    assert '"event":"capture_forwarded"' not in output, (
        "stale race must NOT log capture_forwarded"
    )
    ledger.close()


@pytest.mark.asyncio
async def test_dispatcher_edits_receipt_to_retry_warning_on_webhook_failure(tmp_path):
    """When webhook fails and retry is scheduled, receipt is edited with retry message."""
    ledger = make_ledger(tmp_path)
    capture = accepted(ledger)
    receipt_client = FakeReceiptEditClient()
    await _run_one_dispatch_pass(
        settings=make_settings(delivery_retry_max_attempts=5),
        ledger=ledger,
        downstream_client=AlwaysFailClient(),
        receipt_edit_client=receipt_client,
    )
    assert len(receipt_client.edits) == 1
    assert receipt_client.edits[0]["capture_id"] == capture.capture_id
    assert "retry" in receipt_client.edits[0]["content"].lower()
    ledger.close()


@pytest.mark.asyncio
async def test_dispatcher_edits_receipt_to_failed_when_retry_cap_exceeded(tmp_path):
    """When webhook fails and retry cap is hit, receipt is edited with failure message."""
    ledger = make_ledger(tmp_path)
    capture = accepted(ledger)
    receipt_client = FakeReceiptEditClient()
    await _run_one_dispatch_pass(
        settings=make_settings(delivery_retry_max_attempts=1),  # cap = 1 attempt
        ledger=ledger,
        downstream_client=AlwaysFailClient(),
        receipt_edit_client=receipt_client,
    )
    updated = ledger.get_capture(capture.capture_id)
    assert updated.delivery_status == DELIVERY_FAILED
    assert len(receipt_client.edits) == 1
    assert "manual review" in receipt_client.edits[0]["content"].lower()
    ledger.close()


@pytest.mark.asyncio
async def test_dispatcher_receipt_edit_failure_does_not_kill_pass(tmp_path):
    """Receipt edit errors are swallowed so the dispatch pass continues."""
    ledger = make_ledger(tmp_path)
    accepted(ledger)
    receipt_client = FakeReceiptEditClient()
    receipt_client.should_raise = True
    # Should not raise even though receipt edit throws
    await _run_one_dispatch_pass(
        settings=make_settings(delivery_retry_max_attempts=5),
        ledger=ledger,
        downstream_client=AlwaysFailClient(),
        receipt_edit_client=receipt_client,
    )
    ledger.close()
