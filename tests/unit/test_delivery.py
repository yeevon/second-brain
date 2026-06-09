from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from secondbrain.capture_models import (
    DELIVERY_FORWARDED,
    FORWARDING,
    PENDING_FORWARD,
    RETRY_WAIT,
)
from secondbrain.delivery import (
    DownstreamDeliveryClient,
    _run_one_dispatch_pass,
    _run_one_reaper_pass,
)
from secondbrain.ledger import Ledger


def make_ledger(tmp_path):
    return Ledger(tmp_path / "ledger.sqlite3")


def make_settings(**overrides):
    defaults = dict(
        delivery_max_attempts=5,
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
    accepted(ledger)
    client = AlwaysSucceedClient()
    await _run_one_dispatch_pass(settings=make_settings(), ledger=ledger, downstream_client=client)
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.delivery_status == DELIVERY_FORWARDED
    assert len(client.calls) == 1
    ledger.close()


@pytest.mark.asyncio
async def test_dispatcher_schedules_retry_after_webhook_failure(tmp_path):
    ledger = make_ledger(tmp_path)
    accepted(ledger)
    await _run_one_dispatch_pass(
        settings=make_settings(), ledger=ledger, downstream_client=AlwaysFailClient()
    )
    capture = ledger.get_capture("SB-20260609-0001")
    assert capture.delivery_status == RETRY_WAIT
    assert capture.next_attempt_at is not None
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
    accepted(ledger)
    client = AlwaysSucceedClient()
    await _run_one_dispatch_pass(settings=make_settings(), ledger=ledger, downstream_client=client)
    assert client.calls[0]["capture_id"] == "SB-20260609-0001"
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
# Reaper tests (via _run_one_reaper_pass)
# ---------------------------------------------------------------------------

class FakeReceiptClient:
    def __init__(self):
        self.sent: list[str] = []

    def get_channel(self, channel_id):
        return self

    async def fetch_channel(self, channel_id):
        return self

    async def send(self, content):
        self.sent.append(content)


@pytest.mark.asyncio
async def test_reaper_sends_failure_alert_for_terminal_captures(tmp_path):
    ledger = make_ledger(tmp_path)
    accepted(ledger)
    now = datetime.now(UTC)
    lease = now + timedelta(seconds=1)
    expired = now + timedelta(seconds=120)
    ledger.claim_due_deliveries(now=now, lease_until=lease, batch_size=10)
    receipt_client = FakeReceiptClient()
    await _run_one_reaper_pass(
        settings=make_settings(delivery_max_attempts=1, delivery_reaper_batch_size=100),
        ledger=ledger,
        receipt_client=receipt_client,
        _now=expired,
    )
    assert len(receipt_client.sent) == 1
    assert "SB-20260609-0001" in receipt_client.sent[0]
    ledger.close()
