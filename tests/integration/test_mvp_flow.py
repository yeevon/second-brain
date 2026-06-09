import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from secondbrain.capture_models import (
    DELIVERY_FAILED,
    DELIVERY_FORWARDED,
    FORWARDING,
    PENDING_FORWARD,
    RETRY_WAIT,
)
from secondbrain.capture_service import CaptureService
from secondbrain.delivery import _run_one_dispatch_pass
from secondbrain.ledger import FAILED, FILED, RECEIVED, Ledger
from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID
from secondbrain.vault_writer import VaultWriter
from secondbrain.worker import CaptureQueue, process_capture_once


VALID_CLASSIFICATION = {
    "folder": "projects",
    "project": "halo",
    "note_type": "task",
    "title": "Review WebSocket reconnect handling",
    "tags": ["telemetry", "websocket"],
    "body": "Review reconnect handling in the HALO telemetry dashboard.",
    "actions": [{"text": "Review WebSocket reconnect handling", "status": "open"}],
    "needs_clarification": False,
    "clarifying_question": None,
    "confidence": 0.91,
}


def make_settings(**overrides):
    data = {
        "discord_guild_id": 100,
        "discord_capture_channel_id": 200,
        "discord_allowed_user_id": 300,
        "startup_reconcile_limit": 10,
        "periodic_reconcile_limit": 10,
        "classifier_queue_maxsize": 10,
        "gemini_api_key": "fake",
        "gemini_model": "gemini-test",
        "classification_confidence_threshold": 0.75,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_happy_path_capture_to_vault_edits_original_receipt(tmp_path):
    settings = make_settings()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    vault_writer = VaultWriter(tmp_path / "vault")
    channel = FakeDiscordChannel()
    client = FakeDiscordClient(channel)
    classifier_client = FakeGeminiClient(parsed=VALID_CLASSIFICATION)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue, receipt_client=client)
    message = make_message(1001, channel=channel, content="Review reconnect handling.")

    await service.handle_gateway_message(message)

    capture_id = await queue.get()
    capture = ledger.get_capture(capture_id)
    assert capture.status == RECEIVED
    assert capture.receipt_message_id == "9001"
    assert channel.sent_contents == [
        f"⏳ {capture_id} received.\nYour note is saved. Processing…"
    ]
    assert list((tmp_path / "vault").rglob("*.md")) == []
    assert classifier_client.aio.models.calls == []

    await process_capture_once(
        capture_id=capture_id,
        settings=settings,
        capture_service=service,
        vault_writer=vault_writer,
        classifier_client=classifier_client,
    )

    filed = ledger.get_capture(capture_id)
    notes = [path for path in (tmp_path / "vault").rglob("*.md") if "99_log" not in path.parts]
    assert filed.status == FILED
    assert filed.derived_note_path == (
        "20_projects/halo/"
        f"{filed.received_at.strftime('%Y-%m-%d')}--{capture_id}--review-websocket-reconnect-handling.md"
    )
    assert len(notes) == 1
    assert classifier_client.aio.models.calls[0]["model"] == "gemini-test"
    assert channel.sent_contents == [
        f"⏳ {capture_id} received.\nYour note is saved. Processing…"
    ]
    assert channel.messages[9001].content == (
        f"✅ {capture_id} filed.\n"
        "Location: 20_projects / halo\n"
        "Type: task\n"
        "Tags: telemetry, websocket"
    )

    audit = (tmp_path / "vault" / "99_log" / "events.ndjson").read_text(encoding="utf-8")
    assert '"event":"FILED"' in audit
    assert capture_id in audit


@pytest.mark.asyncio
async def test_saved_receipt_is_sent_only_after_sqlite_commit(tmp_path):
    settings = make_settings()
    ledger_path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(ledger_path)
    queue = CaptureQueue()
    channel = CommitCheckingDiscordChannel(ledger_path)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)

    await service.handle_gateway_message(make_message(1001, channel=channel, content="Review reconnect handling."))

    capture_id = await queue.get()
    assert channel.commit_observed is True
    assert ledger.get_capture(capture_id).status == RECEIVED
    ledger.close()


@pytest.mark.asyncio
async def test_startup_catchup_recovers_missed_message_once(tmp_path):
    settings = make_settings()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    channel = FakeDiscordChannel(
        [make_message(1001, content="Missed while app was stopped.")]
    )
    client = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue, receipt_client=client)

    result = await service.startup_reconcile(client)

    assert result.handled == 1
    assert queue.qsize() == 0
    assert ledger.status_counts() == {RECEIVED: 1}
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1001"

    queued = await service.enqueue_unfinished_captures()
    assert len(queued) == 1
    capture_id = await queue.get()
    await process_capture_once(
        capture_id=capture_id,
        settings=settings,
        capture_service=service,
        vault_writer=VaultWriter(tmp_path / "vault"),
        classifier_client=FakeGeminiClient(parsed=VALID_CLASSIFICATION),
    )

    second_result = await service.startup_reconcile(client)

    notes = [path for path in (tmp_path / "vault").rglob("*.md") if "99_log" not in path.parts]
    assert second_result.seen == 0
    assert ledger.status_counts() == {FILED: 1}
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_crash_before_sqlite_commit_is_recovered_by_next_catchup(tmp_path):
    settings = make_settings()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    channel = FakeDiscordChannel([make_message(1001, content="Recover me after crash.")])
    client = FakeDiscordClient(channel)

    async def crashing_handler(message, *, notify_downstream):
        raise RuntimeError("crashed before commit")

    service = CaptureService(settings=settings, ledger=ledger)
    original_capture = service._capture_if_allowed
    service._capture_if_allowed = crashing_handler
    with pytest.raises(RuntimeError, match="crashed before commit"):
        await service.startup_reconcile(client)
    service._capture_if_allowed = original_capture

    assert ledger.status_counts() == {}
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) is None

    queue = CaptureQueue()
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)
    result = await service.startup_reconcile(client)

    assert result.handled == 1
    assert ledger.status_counts() == {RECEIVED: 1}
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1001"


@pytest.mark.asyncio
async def test_old_bot_receipts_are_ignored_during_catchup(tmp_path):
    settings = make_settings()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    channel = FakeDiscordChannel(
        [
            make_message(
                1001,
                content="⏳ SB-20260607-0001 received.\nYour note is saved. Processing…",
                author_bot=True,
            )
        ]
    )
    client = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)

    result = await service.startup_reconcile(client)

    assert result.ignored == 1
    assert ledger.status_counts() == {}
    assert queue.qsize() == 0
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1001"


@pytest.mark.asyncio
async def test_two_rapid_messages_produce_two_rows_and_two_notes(tmp_path):
    settings = make_settings()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    vault_writer = VaultWriter(tmp_path / "vault")
    channel = FakeDiscordChannel()
    client = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue, receipt_client=client)

    await service.handle_gateway_message(make_message(1001, channel=channel, content="First rapid note."))
    await service.handle_gateway_message(make_message(1002, channel=channel, content="Second rapid note."))

    first_capture_id = await queue.get()
    second_capture_id = await queue.get()
    for capture_id in [first_capture_id, second_capture_id]:
        await process_capture_once(
            capture_id=capture_id,
            settings=settings,
            capture_service=service,
            vault_writer=vault_writer,
            classifier_client=FakeGeminiClient(parsed=VALID_CLASSIFICATION),
        )

    notes = [path for path in (tmp_path / "vault").rglob("*.md") if "99_log" not in path.parts]
    assert first_capture_id != second_capture_id
    assert ledger.status_counts() == {FILED: 2}
    assert len(notes) == 2
    ledger.close()


@pytest.mark.asyncio
async def test_rapid_capture_flow_all_messages_receive_durable_rows(tmp_path):
    """50 rapid Discord captures — every message must get a row; no duplicates; all can queue."""
    settings = make_settings()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue(maxsize=100)
    channel = FakeDiscordChannel()
    client = FakeDiscordClient(channel)
    service = CaptureService(
        settings=settings,
        ledger=ledger,
        notify_capture=queue.enqueue,
        receipt_client=client,
    )

    n = 50
    for i in range(n):
        await service.handle_gateway_message(
            make_message(2000 + i, channel=channel, content=f"Rapid note {i}")
        )

    queued_ids = [await queue.get() for _ in range(n)]

    assert len(set(queued_ids)) == n
    assert ledger.total_captures() == n
    assert ledger.status_counts().get(RECEIVED, 0) == n

    # All queued IDs should be fetchable and not duplicated
    seen_capture_ids = set()
    for capture_id in queued_ids:
        capture = ledger.get_capture(capture_id)
        assert capture.capture_id not in seen_capture_ids
        seen_capture_ids.add(capture.capture_id)

    ledger.close()


def make_message(
    message_id: int,
    *,
    channel=None,
    guild_id=100,
    channel_id=200,
    author_id=300,
    author_bot=False,
    webhook_id=None,
    content="capture this",
    attachments=None,
):
    message_channel = channel or FakeDiscordChannel(channel_id=channel_id)
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=guild_id),
        channel=message_channel,
        author=SimpleNamespace(id=author_id, bot=author_bot),
        webhook_id=webhook_id,
        content=content,
        attachments=attachments or [],
    )


class FakeDiscordClient:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


class FakeDiscordChannel:
    def __init__(self, messages=None, *, channel_id=200):
        self.id = channel_id
        self.history_messages = messages or []
        self.messages = {}
        self.sent_contents = []
        self.next_receipt_id = 9001
        for message in self.history_messages:
            message.channel = self

    async def send(self, content):
        receipt_id = self.next_receipt_id
        self.next_receipt_id += 1
        receipt = FakeDiscordReceiptMessage(receipt_id, content)
        self.messages[receipt_id] = receipt
        self.sent_contents.append(content)
        return SimpleNamespace(id=receipt_id)

    async def fetch_message(self, message_id):
        return self.messages[int(message_id)]

    def history(self, *, limit, after, oldest_first):
        after_id = 0 if after is None else after.id
        messages = [message for message in self.history_messages if message.id > after_id]
        if oldest_first:
            messages = sorted(messages, key=lambda message: message.id)
        return FakeHistory(messages[:limit])


class CommitCheckingDiscordChannel(FakeDiscordChannel):
    """Verifies the capture row is externally visible (via a separate connection) before receipt."""

    def __init__(self, ledger_path):
        super().__init__()
        self.ledger_path = ledger_path
        self.commit_observed = False

    async def send(self, content):
        # Use a completely separate SQLite connection to prove the row is committed
        verification_conn = sqlite3.connect(str(self.ledger_path))
        verification_conn.row_factory = sqlite3.Row
        try:
            rows = verification_conn.execute(
                "SELECT * FROM captures WHERE status = 'RECEIVED'"
            ).fetchall()
            assert len(rows) == 1, (
                "capture row must be externally visible from a separate connection before receipt is sent"
            )
            assert rows[0]["receipt_message_id"] is None
        finally:
            verification_conn.close()
        self.commit_observed = True
        return await super().send(content)


class FakeDiscordReceiptMessage:
    def __init__(self, message_id, content):
        self.id = message_id
        self.content = content

    async def edit(self, *, content):
        self.content = content


class FakeHistory:
    def __init__(self, messages):
        self.messages = messages

    def __aiter__(self):
        self._iterator = iter(self.messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeGeminiClient:
    def __init__(self, *, parsed):
        self.aio = SimpleNamespace(models=FakeGeminiModels(parsed=parsed))


@pytest.mark.asyncio
async def test_skipped_gateway_event_is_recovered_by_periodic_reconciliation(tmp_path):
    """A message dropped by the live gateway is recovered during the next periodic scan.

    Dangerous ordering: 1001 is the DROPPED event, 1002 is the DELIVERED event.
    In the old live-marker design, capturing 1002 would have advanced the cursor past 1001,
    permanently losing it. The current design never advances the cursor on live delivery,
    so the periodic scan recovers 1001 correctly.
    """
    from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID, _run_one_periodic_pass

    settings = make_settings()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    channel = FakeDiscordChannel(
        [
            make_message(1001, content="Dropped message."),
            make_message(1002, content="Live message."),
        ]
    )
    client = FakeDiscordClient(channel)
    service = CaptureService(
        settings=settings,
        ledger=ledger,
        notify_capture=queue.enqueue,
        receipt_client=client,
    )

    # Only message 1002 is delivered via the live gateway (1001 was dropped)
    await service.handle_gateway_message(make_message(1002, channel=channel, content="Live message."))
    await queue.get()

    # Live gateway never touches the marker
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) is None
    assert ledger.total_captures() == 1

    # Periodic scan picks up both messages; 1001 is recovered, 1002 is a duplicate
    await _run_one_periodic_pass(
        client=client,
        settings=settings,
        ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=True),
    )

    assert ledger.total_captures() == 2
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1002"
    assert ledger.get_system_state("periodic_reconcile_recovered_total") == "1"
    assert ledger.get_system_state("periodic_reconcile_duplicates_total") == "1"
    assert queue.qsize() == 1  # only the recovered capture was re-enqueued


# ===========================================================================
# SB-107 — Delivery lease integration tests
# ===========================================================================

def make_delivery_settings(**overrides):
    data = dict(
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
    data.update(overrides)
    return SimpleNamespace(**data)


class AlwaysAcceptClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def forward_capture(self, *, capture_id: str, delivery_attempt: int) -> None:
        self.calls.append({"capture_id": capture_id, "delivery_attempt": delivery_attempt})


@pytest.mark.asyncio
async def test_schedule_retry_makes_capture_reclaimable(tmp_path):
    """schedule_retry transitions DELIVERY_FORWARDED → RETRY_WAIT → reclaimable as attempt 2."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_delivery_settings()
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    short_lease = now + timedelta(seconds=30)

    insert_result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="important note",
        received_at=now,
    )
    capture_id = insert_result.capture.capture_id

    claimed = ledger.claim_due_deliveries(now=now, lease_until=short_lease, batch_size=10)
    assert len(claimed) == 1
    assert claimed[0].delivery_attempts == 1

    ledger.mark_forwarded(capture_id=capture_id, delivery_attempt=1, lease_until=short_lease)
    assert ledger.get_capture(capture_id).delivery_status == DELIVERY_FORWARDED

    # Downstream reports transient error → schedule retry
    expired = now + timedelta(seconds=120)
    disposition = ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=1,
        now=expired,
        error_type="connection_timeout",
        reason_type="webhook_failure",
        max_attempts=settings.delivery_max_attempts,
        base_delay_seconds=settings.delivery_retry_base_delay_seconds,
        max_delay_seconds=settings.delivery_retry_max_delay_seconds,
    )
    assert disposition.retry_scheduled is True

    capture = ledger.get_capture(capture_id)
    assert capture.delivery_status == RETRY_WAIT
    assert capture.next_attempt_at is not None
    assert capture.processing_lease_until is None
    assert capture.status == RECEIVED
    assert capture.raw_text == "important note"

    after_retry = capture.next_attempt_at + timedelta(seconds=1)
    reclaimed = ledger.claim_due_deliveries(now=after_retry, lease_until=after_retry, batch_size=10)
    assert len(reclaimed) == 1
    assert reclaimed[0].delivery_attempts == 2
    assert reclaimed[0].delivery_status == FORWARDING

    ledger.close()


@pytest.mark.asyncio
async def test_schedule_retry_reaches_terminal_failure_at_cap(tmp_path):
    """After max_attempts retries, schedule_retry marks capture terminally FAILED."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_delivery_settings(delivery_max_attempts=3)
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)

    insert_result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="important note",
        received_at=now,
    )
    capture_id = insert_result.capture.capture_id

    t = now
    for cycle in range(settings.delivery_max_attempts):
        lease = t + timedelta(seconds=30)
        claimed = ledger.claim_due_deliveries(now=t, lease_until=lease, batch_size=10)
        assert claimed[0].delivery_attempts == cycle + 1
        ledger.mark_forwarded(capture_id=capture_id, delivery_attempt=cycle + 1, lease_until=lease)
        expired = t + timedelta(seconds=120)
        disposition = ledger.schedule_retry(
            capture_id=capture_id,
            delivery_attempt=cycle + 1,
            now=expired,
            error_type="connection_timeout",
            reason_type="webhook_failure",
            max_attempts=settings.delivery_max_attempts,
            base_delay_seconds=settings.delivery_retry_base_delay_seconds,
            max_delay_seconds=settings.delivery_retry_max_delay_seconds,
        )
        t = expired + timedelta(seconds=300)
        if disposition.failed_terminally:
            break

    capture = ledger.get_capture(capture_id)
    assert capture.status == FAILED
    assert capture.delivery_status == DELIVERY_FAILED
    assert capture.processing_lease_until is None
    assert capture.next_attempt_at is None
    assert capture.raw_text == "important note"

    events = ledger._runtime.read(
        lambda conn: [
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM capture_events WHERE capture_id = ? ORDER BY id",
                (capture_id,),
            ).fetchall()
        ]
    )
    assert "RETRY_LIMIT_EXCEEDED" in events
    ledger.close()


@pytest.mark.asyncio
async def test_late_callback_from_old_attempt_cannot_override_new_attempt(tmp_path):
    """Stale attempt callbacks are ignored; current attempt remains authoritative."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)

    insert_result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note",
        received_at=now,
    )
    capture_id = insert_result.capture.capture_id

    # Attempt 1: claim → forward → downstream reports transient error → RETRY_WAIT
    lease1 = now + timedelta(seconds=30)
    ledger.claim_due_deliveries(now=now, lease_until=lease1, batch_size=10)
    ledger.mark_forwarded(capture_id=capture_id, delivery_attempt=1, lease_until=lease1)
    expired = now + timedelta(seconds=120)
    ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=1,
        now=expired,
        error_type="connection_timeout",
        reason_type="webhook_failure",
        max_attempts=5,
        base_delay_seconds=10,
        max_delay_seconds=300,
    )

    # Attempt 2: claim
    after_retry = expired + timedelta(seconds=300)
    ledger.claim_due_deliveries(now=after_retry, lease_until=after_retry, batch_size=10)
    assert ledger.get_capture(capture_id).delivery_attempts == 2
    assert ledger.get_capture(capture_id).delivery_status == FORWARDING

    # Late CLASSIFYING callback from attempt 1 must be ignored
    stale_ok = ledger.mark_classifying_delivery(
        capture_id=capture_id, delivery_attempt=1, lease_until=after_retry
    )
    assert stale_ok.changed is False
    assert stale_ok.outcome == "stale_attempt"

    # Attempt 2 remains authoritative
    assert ledger.get_capture(capture_id).delivery_status == FORWARDING
    assert ledger.get_capture(capture_id).delivery_attempts == 2

    ledger.close()


@pytest.mark.asyncio
async def test_saved_receipt_commit_precedes_delivery_claim(tmp_path):
    """SQLite commit (RECEIVED + PENDING_FORWARD) is visible before delivery begins."""
    db_path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(db_path)

    result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note",
    )
    capture_id = result.capture.capture_id

    # Separate connection verifies state before delivery claiming
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, delivery_status FROM captures WHERE capture_id = ?", (capture_id,)
    ).fetchone()
    conn.close()

    assert row["status"] == RECEIVED
    assert row["delivery_status"] == PENDING_FORWARD

    ledger.close()


class FakeGeminiModels:
    def __init__(self, *, parsed):
        self.parsed = parsed
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed=self.parsed)
