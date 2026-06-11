from types import SimpleNamespace

import pytest

from secondbrain.capture_models import CLASSIFYING, FAILED, FILED, INBOX, RECEIVED, REJECTED_SENSITIVE
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger
from secondbrain.models import Classification
from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID

from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient, FakeDiscordMessage


def make_settings(tmp_path, **overrides):
    data = {
        "discord_guild_id": 100,
        "discord_capture_channel_id": 200,
        "discord_allowed_user_id": 300,
        "startup_reconcile_limit": 10,
        "ledger_path": tmp_path / "runtime" / "ledger.sqlite3",
        "vault_path": tmp_path / "vault",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_classification(**overrides):
    data = {
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
    data.update(overrides)
    return Classification.model_validate(data)


@pytest.mark.asyncio
async def test_service_accepts_normal_message_and_notifies_after_commit(tmp_path):
    notified = []

    async def notify(capture_id):
        notified.append(capture_id)

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    channel = CommitCheckingChannel()
    service = CaptureService(
        settings=settings,
        ledger=ledger,
        notify_capture=notify,
        receipt_client=FakeDiscordClient(channel),
    )
    channel.service = service

    await service.handle_gateway_message(FakeDiscordMessage(channel=channel, content="Review reconnect."))

    capture = service.captures_by_status(RECEIVED)[0]
    assert capture.raw_text == "Review reconnect."
    assert capture.receipt_message_id == "9001"
    assert notified == [capture.capture_id]
    assert channel.commit_observed is True


@pytest.mark.asyncio
async def test_service_rejects_secret_without_notifying_downstream(tmp_path):
    notified = []

    async def notify(capture_id):
        notified.append(capture_id)

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    channel = FakeDiscordChannel()
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=notify)

    await service.handle_gateway_message(
        FakeDiscordMessage(channel=channel, content="password=hunter2")
    )

    capture = service.captures_by_status(REJECTED_SENSITIVE)[0]
    assert capture.raw_text is None
    assert capture.redacted_text == "password=[REDACTED]"
    assert notified == []


@pytest.mark.asyncio
async def test_service_duplicate_message_does_not_notify_twice(tmp_path):
    notified = []

    async def notify(capture_id):
        notified.append(capture_id)

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=notify)
    message = FakeDiscordMessage(message_id=1001, content="Review reconnect.")

    await service.handle_gateway_message(message)
    await service.handle_gateway_message(message)

    assert service.total_captures() == 1
    assert len(notified) == 1


def test_service_claim_for_processing_transitions_received_to_classifying(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="100",
        discord_author_id="300",
        raw_text="Review reconnect.",
    ).capture
    service = CaptureService(settings=settings, ledger=ledger)

    claimed = service.claim_for_processing(capture.capture_id)

    assert claimed is not None
    assert service.get_capture(capture.capture_id).status == CLASSIFYING


def test_retry_clears_stale_failure_metadata(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = _insert_capture(ledger, "1001")
    ledger.update_capture(
        capture.capture_id,
        status=FAILED,
        classification_json=make_classification(title="stale").model_dump(mode="json"),
        derived_note_path="20_projects/halo/stale.md",
        last_error="temporary failure",
    )
    service = CaptureService(settings=settings, ledger=ledger)

    service.retry(capture.capture_id)

    updated = service.get_capture(capture.capture_id)
    assert updated.status == RECEIVED
    assert updated.last_error is None
    assert updated.derived_note_path is None
    assert ledger.capture_classification_json(capture.capture_id) is None


def test_successful_filing_after_retry_does_not_retain_old_error(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = _insert_capture(ledger, "1001")
    ledger.update_capture(
        capture.capture_id,
        status=FAILED,
        classification_json=make_classification(title="stale").model_dump(mode="json"),
        derived_note_path="20_projects/halo/stale.md",
        last_error="temporary failure",
    )
    service = CaptureService(settings=settings, ledger=ledger)

    service.retry(capture.capture_id)
    claimed = service.claim_for_processing(capture.capture_id)
    service.mark_filed(
        capture_id=capture.capture_id,
        classification=make_classification(),
        note_path="20_projects/halo/file.md",
    )

    updated = service.get_capture(capture.capture_id)
    assert claimed is not None
    assert updated.status == FILED
    assert updated.last_error is None


@pytest.mark.asyncio
async def test_service_complete_filed_updates_state_before_editing_receipt(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = _insert_classifying_capture(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    receipt_client = StateCheckingReceiptClient()
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=receipt_client)
    receipt_client.service = service
    receipt_client.expected_capture_id = capture.capture_id
    receipt_client.expected_status = FILED
    receipt_client.expected_path = "20_projects/halo/file.md"

    await service.complete_filed(
        capture_id=capture.capture_id,
        classification=make_classification(),
        note_path="20_projects/halo/file.md",
    )

    assert receipt_client.checked is True


@pytest.mark.asyncio
async def test_service_complete_inbox_updates_state_before_editing_receipt(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = _insert_classifying_capture(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    receipt_client = StateCheckingReceiptClient()
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=receipt_client)
    receipt_client.service = service
    receipt_client.expected_capture_id = capture.capture_id
    receipt_client.expected_status = INBOX
    receipt_client.expected_path = "00_inbox/file.md"

    await service.complete_inbox(
        capture_id=capture.capture_id,
        classification=make_classification(folder="inbox", project=None),
        note_path="00_inbox/file.md",
        reason="classification was uncertain",
    )

    assert receipt_client.checked is True


@pytest.mark.asyncio
async def test_service_complete_failed_preserves_raw_text(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = _insert_classifying_capture(ledger)
    service = CaptureService(settings=settings, ledger=ledger)

    await service.complete_failed(capture_id=capture.capture_id, reason="worker error: RuntimeError: boom")

    updated = service.get_capture(capture.capture_id)
    assert updated.status == FAILED
    assert updated.raw_text == "Review reconnect."


@pytest.mark.asyncio
async def test_service_receipt_edit_failure_sends_one_replacement(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = _insert_classifying_capture(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    channel = FakeDiscordChannel()
    channel.fail_receipt_edit = True
    channel.next_receipt_id = 9002
    channel.sent_receipts.append((9001, "old receipt"))
    channel.messages[9001] = channel.messages.get(9001) or _EditableReceipt(9001, channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=FakeDiscordClient(channel))

    await service.complete_filed(
        capture_id=capture.capture_id,
        classification=make_classification(),
        note_path="20_projects/halo/file.md",
    )

    updated = service.get_capture(capture.capture_id)
    assert len(channel.replacement_receipts) == 1
    assert updated.receipt_message_id == str(channel.replacement_receipts[0][0])


def test_service_status_snapshot_reports_expected_counts(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    filed = _insert_capture(ledger, "1001")
    inbox = _insert_capture(ledger, "1002")
    failed = _insert_capture(ledger, "1003")
    ledger.insert_sensitive_rejection(
        discord_message_id="1004",
        discord_channel_id="200",
        discord_guild_id="100",
        discord_author_id="300",
        redacted_text="password=[REDACTED]",
        sensitivity_flags=("password_assignment",),
    )
    ledger.update_capture(filed.capture_id, status=FILED, derived_note_path="20_projects/halo/file.md")
    ledger.update_capture(inbox.capture_id, status=INBOX, derived_note_path="00_inbox/file.md")
    ledger.update_capture(failed.capture_id, status=FAILED)
    ledger.set_system_state(LAST_RECONCILED_MESSAGE_ID, "1004")
    service = CaptureService(settings=settings, ledger=ledger)

    snapshot = service.status_snapshot()

    assert snapshot.total_captures == 4
    assert snapshot.filed == 1
    assert snapshot.inbox == 1
    assert snapshot.rejected_sensitive == 1
    assert snapshot.failed == 1
    assert snapshot.last_reconciled_discord_message_id == "1004"
    assert snapshot.last_successful_vault_write == "00_inbox/file.md"


@pytest.mark.asyncio
async def test_service_startup_reconcile_recovers_missed_message_once(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    channel = FakeDiscordChannel(
        [FakeDiscordMessage(message_id=1001, content="Missed while offline.")]
    )
    service = CaptureService(settings=settings, ledger=ledger)

    first = await service.startup_reconcile(FakeDiscordClient(channel))
    second = await service.startup_reconcile(FakeDiscordClient(channel))

    assert first.handled == 1
    assert second.seen == 0
    assert service.total_captures() == 1
    assert service.last_reconciled_message_id() == "1001"


@pytest.mark.asyncio
async def test_capture_row_is_committed_before_receipt_is_sent(tmp_path):
    """The capture row must be durable (committed) before the receipt is delivered."""
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    channel = CommitCheckingChannel()
    channel.expected_raw_text = "Live capture."
    service = CaptureService(settings=settings, ledger=ledger)
    channel.service = service

    await service.handle_gateway_message(
        FakeDiscordMessage(message_id=1001, channel=channel, content="Live capture.")
    )

    assert channel.commit_observed is True
    assert service.last_reconciled_message_id() is None


@pytest.mark.asyncio
async def test_gateway_captured_messages_appear_as_duplicates_in_startup_scan(tmp_path):
    """Gateway captures don't advance the marker; startup sees them as duplicates from history."""
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)

    await service.handle_gateway_message(FakeDiscordMessage(message_id=1001, content="Live one."))
    await service.handle_gateway_message(FakeDiscordMessage(message_id=1002, content="Live two."))

    assert service.last_reconciled_message_id() is None

    channel = FakeDiscordChannel(
        [
            FakeDiscordMessage(message_id=1001, content="Live one."),
            FakeDiscordMessage(message_id=1002, content="Live two."),
            FakeDiscordMessage(message_id=1003, content="Missed while offline."),
        ]
    )
    result = await service.startup_reconcile(FakeDiscordClient(channel))

    assert result.seen == 3
    assert result.duplicates == 2
    assert result.recovered == 1
    assert service.total_captures() == 3
    assert service.last_reconciled_message_id() == "1003"


@pytest.mark.asyncio
async def test_startup_reconcile_counts_existing_duplicate_as_ignored(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)
    ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="100",
        discord_author_id="300",
        raw_text="Already durable.",
    )
    channel = FakeDiscordChannel(
        [FakeDiscordMessage(message_id=1001, content="Duplicate history event.")]
    )

    result = await service.startup_reconcile(FakeDiscordClient(channel))

    assert result.handled == 1
    assert result.duplicates == 1
    assert result.recovered == 0
    assert result.ignored == 0
    assert service.total_captures() == 1
    assert channel.sent_receipts == []


@pytest.mark.asyncio
async def test_capture_row_is_durable_even_when_downstream_notification_fails(tmp_path):
    """Row is committed before downstream notify; gateway never advances the reconcile marker."""
    async def fail_notify(capture_id):
        raise RuntimeError("downstream unavailable")

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=fail_notify)

    with pytest.raises(RuntimeError, match="downstream unavailable"):
        await service.handle_gateway_message(
            FakeDiscordMessage(message_id=1001, content="Durable before notify.")
        )

    assert service.total_captures() == 1
    assert service.last_reconciled_message_id() is None


def test_reconcile_marker_never_moves_backward(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)

    from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID
    ledger.advance_system_state_snowflake(LAST_RECONCILED_MESSAGE_ID, "1002")
    ledger.advance_system_state_snowflake(LAST_RECONCILED_MESSAGE_ID, "1001")

    assert service.last_reconciled_message_id() == "1002"


class CommitCheckingChannel(FakeDiscordChannel):
    def __init__(self):
        super().__init__()
        self.service = None
        self.expected_raw_text = "Review reconnect."
        self.commit_observed = False

    async def send(self, content):
        captures = self.service.captures_by_status(RECEIVED)
        assert len(captures) == 1
        assert captures[0].raw_text == self.expected_raw_text
        self.commit_observed = True
        return await super().send(content)


class StateCheckingReceiptClient:
    def __init__(self):
        self.service = None
        self.expected_capture_id = None
        self.expected_status = None
        self.expected_path = None
        self.checked = False

    def get_channel(self, channel_id):
        return self

    async def fetch_message(self, message_id):
        return self

    async def edit(self, *, content):
        capture = self.service.get_capture(self.expected_capture_id)
        assert capture.status == self.expected_status
        assert capture.derived_note_path == self.expected_path
        self.checked = True


class _EditableReceipt:
    def __init__(self, message_id, channel):
        self.id = message_id
        self.channel = channel

    async def edit(self, *, content):
        self.channel.edit_attempts += 1
        if self.channel.fail_receipt_edit:
            raise RuntimeError("edit failed")


def _insert_capture(ledger, discord_message_id):
    return ledger.insert_accepted_capture(
        discord_message_id=discord_message_id,
        discord_channel_id="200",
        discord_guild_id="100",
        discord_author_id="300",
        raw_text="Review reconnect.",
    ).capture


def _insert_classifying_capture(ledger):
    capture = _insert_capture(ledger, "1001")
    ledger.mark_classifying(capture.capture_id)
    return ledger.get_capture(capture.capture_id)


# ===========================================================================
# SB-107 audit — Attempt-aware callback service methods
# ===========================================================================

from datetime import UTC, datetime, timedelta

from secondbrain.capture_models import (
    COMPLETE,
    DELIVERY_FAILED,
    DELIVERY_FORWARDED,
    FORWARDING,
    NOT_APPLICABLE,
    PENDING_FORWARD,
    RETRY_WAIT,
)


_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def make_delivery_settings(tmp_path, **overrides):
    data = {
        "capture_processing_mode": "capture-only",
        "discord_guild_id": 100,
        "discord_capture_channel_id": 200,
        "discord_allowed_user_id": 300,
        "startup_reconcile_limit": 10,
        "ledger_path": tmp_path / "runtime" / "ledger.sqlite3",
        "vault_path": None,
        "delivery_retry_max_attempts": 5,
        "delivery_retry_base_delay_seconds": 10,
        "delivery_retry_max_delay_seconds": 300,
        "delivery_forward_lease_seconds": 60,
        "delivery_processing_lease_seconds": 300,
        "delivery_dispatch_interval_seconds": 2,
        "delivery_dispatch_batch_size": 25,
        "stale_lease_reaper_interval_seconds": 30,
        "stale_lease_reaper_batch_size": 100,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _make_service_with_capture(tmp_path, **settings_overrides):
    settings = make_delivery_settings(tmp_path, **settings_overrides)
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)
    capture = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="test note",
        received_at=_NOW,
    ).capture
    return service, ledger, capture


def test_service_ignores_stale_classifying_callback(tmp_path):
    """acknowledge_delivery_classifying with wrong attempt returns False."""
    service, ledger, capture = _make_service_with_capture(tmp_path)
    now = _NOW
    lease = now + timedelta(seconds=60)
    # Claim attempt 1
    ledger.claim_due_deliveries(now=now, lease_until=lease, batch_size=10)
    ledger.mark_forwarded(capture_id=capture.capture_id, delivery_attempt=1, lease_until=lease)

    # Stale attempt — must be ignored
    result = service.acknowledge_delivery_classifying(
        capture_id=capture.capture_id, delivery_attempt=99
    )
    assert result.changed is False
    assert result.outcome == "stale_attempt"
    # Current state unchanged
    assert ledger.get_capture(capture.capture_id).delivery_status == DELIVERY_FORWARDED
    ledger.close()


@pytest.mark.asyncio
async def test_service_terminal_callback_is_idempotent(tmp_path):
    """Same filed callback repeated with same path reports idempotent_replay."""
    service, ledger, capture = _make_service_with_capture(tmp_path)
    now = _NOW
    lease = now + timedelta(seconds=60)
    ledger.claim_due_deliveries(now=now, lease_until=lease, batch_size=10)
    ledger.mark_forwarded(capture_id=capture.capture_id, delivery_attempt=1, lease_until=lease)

    ok1 = await service.acknowledge_delivery_filed(
        capture_id=capture.capture_id, delivery_attempt=1, derived_note_path="a.md"
    )
    ok2 = await service.acknowledge_delivery_filed(
        capture_id=capture.capture_id, delivery_attempt=1, derived_note_path="a.md"
    )
    assert ok1.changed is True
    assert ok1.outcome == "changed"
    assert ok2.changed is False
    assert ok2.outcome == "idempotent_replay"
    ledger.close()


@pytest.mark.asyncio
async def test_service_rejects_conflicting_terminal_callback(tmp_path):
    """Filed callback with different path after completion reports conflicting_replay."""
    service, ledger, capture = _make_service_with_capture(tmp_path)
    now = _NOW
    lease = now + timedelta(seconds=60)
    ledger.claim_due_deliveries(now=now, lease_until=lease, batch_size=10)
    ledger.mark_forwarded(capture_id=capture.capture_id, delivery_attempt=1, lease_until=lease)

    await service.acknowledge_delivery_filed(
        capture_id=capture.capture_id, delivery_attempt=1, derived_note_path="a.md"
    )
    conflict = await service.acknowledge_delivery_filed(
        capture_id=capture.capture_id, delivery_attempt=1, derived_note_path="b.md"
    )
    assert conflict.changed is False
    assert conflict.outcome == "conflicting_replay"
    ledger.close()


def test_service_renews_current_attempt_lease(tmp_path):
    """renew_delivery_lease for the current attempt updates processing_lease_until."""
    service, ledger, capture = _make_service_with_capture(tmp_path)
    now = _NOW
    short_lease = now + timedelta(seconds=10)
    ledger.claim_due_deliveries(now=now, lease_until=short_lease, batch_size=10)
    ledger.mark_forwarded(capture_id=capture.capture_id, delivery_attempt=1, lease_until=short_lease)

    before_renewal = datetime.now(UTC)
    result = service.renew_delivery_lease(
        capture_id=capture.capture_id, delivery_attempt=1
    )
    assert result.changed is True
    assert result.outcome == "changed"
    updated = ledger.get_capture(capture.capture_id)
    assert updated.processing_lease_until is not None
    # New lease must be in the future relative to the real clock
    assert updated.processing_lease_until > before_renewal
    ledger.close()


# ===========================================================================
# SB-107 audit — Local vs downstream processing isolation (Issue 4)
# ===========================================================================

def test_local_processing_does_not_leave_filed_note_pending_forward(tmp_path):
    """capture-processing-mode=local-full inserts with NOT_APPLICABLE delivery status."""
    settings = make_delivery_settings(tmp_path, capture_processing_mode="local-full")
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)

    result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="local note",
        initial_delivery_status=service._initial_delivery_status,
        received_at=_NOW,
    )
    assert result.capture.delivery_status == NOT_APPLICABLE
    ledger.close()


def test_capture_only_mode_inserts_as_pending_forward(tmp_path):
    """capture-processing-mode=capture-only inserts with PENDING_FORWARD delivery status."""
    settings = make_delivery_settings(tmp_path, capture_processing_mode="capture-only")
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)

    result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="downstream note",
        initial_delivery_status=service._initial_delivery_status,
        received_at=_NOW,
    )
    assert result.capture.delivery_status == PENDING_FORWARD
    ledger.close()


@pytest.mark.asyncio
async def test_schedule_delivery_retry_returns_retry_disposition(tmp_path):
    """schedule_delivery_retry wires through settings correctly."""
    service, ledger, capture = _make_service_with_capture(tmp_path)
    now = _NOW
    lease = now + timedelta(seconds=60)
    ledger.claim_due_deliveries(now=now, lease_until=lease, batch_size=10)

    disposition = await service.schedule_delivery_retry(
        capture_id=capture.capture_id,
        delivery_attempt=1,
        error_type="TimeoutError",
    )
    assert disposition.retry_scheduled is True
    assert disposition.failed_terminally is False
    assert ledger.get_capture(capture.capture_id).delivery_status == RETRY_WAIT
    ledger.close()


@pytest.mark.asyncio
async def test_acknowledge_delivery_failed_marks_capture_terminally_failed(tmp_path):
    """acknowledge_delivery_failed sets delivery_status to FAILED."""
    service, ledger, capture = _make_service_with_capture(tmp_path)
    now = _NOW
    lease = now + timedelta(seconds=60)
    ledger.claim_due_deliveries(now=now, lease_until=lease, batch_size=10)
    ledger.mark_forwarded(capture_id=capture.capture_id, delivery_attempt=1, lease_until=lease)

    result = await service.acknowledge_delivery_failed(
        capture_id=capture.capture_id,
        delivery_attempt=1,
        reason_type="writer_failure",
    )
    assert result.changed is True
    assert result.outcome == "changed"
    updated = ledger.get_capture(capture.capture_id)
    assert updated.delivery_status == DELIVERY_FAILED
    ledger.close()


# ===========================================================================
# SB-107 audit 2 — Local vs downstream isolation
# ===========================================================================

@pytest.mark.asyncio
async def test_local_worker_filed_capture_is_not_pending_forward(tmp_path):
    """Local-full filing must normalize delivery_status to NOT_APPLICABLE."""
    from types import SimpleNamespace as NS
    from secondbrain.models import Classification
    settings = make_delivery_settings(tmp_path, capture_processing_mode="local-full")
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)

    # Insert with default initial_delivery_status (will be NOT_APPLICABLE)
    result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="local note",
        initial_delivery_status=service._initial_delivery_status,
        received_at=_NOW,
    )
    capture = result.capture
    # Manually put it through local worker flow
    ledger.mark_classifying(capture.capture_id)
    classification = Classification(
        folder="projects",
        project="test",
        note_type="reference",
        title="Test Note",
        tags=["test"],
        body="Body content.",
        actions=[],
        needs_clarification=False,
        clarifying_question=None,
        confidence=0.9,
    )
    service.mark_filed(
        capture_id=capture.capture_id,
        note_path="20_projects/local.md",
        classification=classification,
    )
    updated = ledger.get_capture(capture.capture_id)
    assert updated.delivery_status == NOT_APPLICABLE
    ledger.close()


def test_dispatcher_never_claims_terminal_note_with_pending_forward_state(tmp_path):
    """Dispatcher claim query restricts to status=RECEIVED; terminal notes are not claimed."""
    import sqlite3
    db_path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(db_path)
    # Insert a capture in normal RECEIVED / PENDING_FORWARD state
    result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="local note",
        received_at=_NOW,
    )
    cid = result.capture.capture_id
    # Force the capture to FILED while keeping delivery_status = PENDING_FORWARD
    # (simulates a migration edge case where local filing happened before SB-107)
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.execute(
        "UPDATE captures SET status = 'FILED' WHERE capture_id = ?", (cid,)
    )
    raw_conn.commit()
    raw_conn.close()
    # Dispatcher must not claim this
    claimed = ledger.claim_due_deliveries(now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10)
    assert len(claimed) == 0
    ledger.close()


@pytest.mark.asyncio
async def test_local_full_startup_normalizes_migrated_received_rows(tmp_path):
    """enqueue_unfinished_captures normalizes PENDING_FORWARD rows in local-full mode."""
    settings = make_delivery_settings(tmp_path, capture_processing_mode="local-full")
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)

    # Insert a row as if migrated from MVP (will use NOT_APPLICABLE from service._initial_delivery_status)
    # But simulate the old PENDING_FORWARD state by inserting directly
    result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="migrated note",
        initial_delivery_status=PENDING_FORWARD,  # simulating migration 002 output
        received_at=_NOW,
    )
    assert result.capture.delivery_status == PENDING_FORWARD

    # Startup normalization must convert it
    await service.enqueue_unfinished_captures()
    updated = ledger.get_capture(result.capture.capture_id)
    assert updated.delivery_status == NOT_APPLICABLE
    ledger.close()


@pytest.mark.asyncio
async def test_service_retry_rejects_free_form_error_type_without_http_adapter(tmp_path):
    """schedule_delivery_retry must reject free-form error strings at the domain layer."""
    service, ledger, capture = _make_service_with_capture(tmp_path)
    ledger.claim_due_deliveries(now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10)

    with pytest.raises(ValueError, match="unsafe delivery category string"):
        await service.schedule_delivery_retry(
            capture_id=capture.capture_id,
            delivery_attempt=1,
            error_type="TimeoutError: POST https://n8n.example.com?token=secret",
        )
    ledger.close()


@pytest.mark.asyncio
async def test_local_full_startup_normalizes_forwarding_row(tmp_path):
    """normalize_delivery_for_local_full handles in-flight FORWARDING rows."""
    settings = make_delivery_settings(tmp_path, capture_processing_mode="local-full")
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)

    ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note",
        initial_delivery_status=PENDING_FORWARD,
        received_at=_NOW,
    )
    cid = "SB-20260609-0001"
    ledger.claim_due_deliveries(now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10)
    assert ledger.get_capture(cid).delivery_status == FORWARDING

    await service.enqueue_unfinished_captures()
    assert ledger.get_capture(cid).delivery_status == NOT_APPLICABLE
    ledger.close()


@pytest.mark.asyncio
async def test_local_full_startup_normalizes_forwarded_row(tmp_path):
    """normalize_delivery_for_local_full handles DELIVERY_FORWARDED rows."""
    settings = make_delivery_settings(tmp_path, capture_processing_mode="local-full")
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)

    ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note",
        initial_delivery_status=PENDING_FORWARD,
        received_at=_NOW,
    )
    cid = "SB-20260609-0001"
    ledger.claim_due_deliveries(now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10)
    ledger.mark_forwarded(capture_id=cid, delivery_attempt=1,
                          lease_until=_NOW + timedelta(seconds=300))
    assert ledger.get_capture(cid).delivery_status == DELIVERY_FORWARDED

    await service.enqueue_unfinished_captures()
    assert ledger.get_capture(cid).delivery_status == NOT_APPLICABLE
    ledger.close()


@pytest.mark.asyncio
async def test_local_full_normalization_clears_retry_metadata(tmp_path):
    """normalize_delivery_for_local_full clears processing_lease_until and next_attempt_at."""
    settings = make_delivery_settings(tmp_path, capture_processing_mode="local-full")
    ledger = Ledger(settings.ledger_path)
    service = CaptureService(settings=settings, ledger=ledger)

    ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note",
        initial_delivery_status=PENDING_FORWARD,
        received_at=_NOW,
    )
    cid = "SB-20260609-0001"
    lease = _NOW + timedelta(seconds=60)
    ledger.claim_due_deliveries(now=_NOW, lease_until=lease, batch_size=10)
    ledger.mark_forwarded(capture_id=cid, delivery_attempt=1, lease_until=lease)
    # Confirm lease is set
    assert ledger.get_capture(cid).processing_lease_until is not None

    await service.enqueue_unfinished_captures()

    normalized = ledger.get_capture(cid)
    assert normalized.delivery_status == NOT_APPLICABLE
    assert normalized.processing_lease_until is None
    assert normalized.next_attempt_at is None
    ledger.close()


@pytest.mark.asyncio
async def test_service_acknowledge_delivery_inbox_rejects_free_form_reason_type_without_http_adapter(tmp_path):
    """acknowledge_delivery_inbox must reject unsafe reason_type strings at the domain layer."""
    service, ledger, capture = _make_service_with_capture(tmp_path)
    lease = _NOW + timedelta(seconds=60)
    ledger.claim_due_deliveries(now=_NOW, lease_until=lease, batch_size=10)
    ledger.mark_forwarded(capture_id=capture.capture_id, delivery_attempt=1, lease_until=lease)

    with pytest.raises(ValueError, match="unsafe delivery category string"):
        await service.acknowledge_delivery_inbox(
            capture_id=capture.capture_id,
            delivery_attempt=1,
            derived_note_path="00_inbox/file.md",
            reason_type="free form reason with spaces",
        )
    ledger.close()
