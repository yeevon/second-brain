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


class CommitCheckingChannel(FakeDiscordChannel):
    def __init__(self):
        super().__init__()
        self.service = None
        self.commit_observed = False

    async def send(self, content):
        captures = self.service.captures_by_status(RECEIVED)
        assert len(captures) == 1
        assert captures[0].raw_text == "Review reconnect."
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
