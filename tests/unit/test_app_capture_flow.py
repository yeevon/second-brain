from types import SimpleNamespace

import pytest

from secondbrain.app import create_capture_handler
from secondbrain.ledger import RECEIVED, REJECTED_SENSITIVE, Ledger
from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID
from secondbrain.worker import CaptureQueue


def make_settings():
    return SimpleNamespace(classifier_queue_maxsize=10)


def make_message(*, content="capture this", message_id=1001):
    channel = FakeChannel()
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=300),
        channel=channel,
        author=SimpleNamespace(id=400),
        content=content,
        attachments=[],
    )


@pytest.mark.asyncio
async def test_capture_handler_persists_receipts_and_enqueues_capture(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    handler = create_capture_handler(make_settings(), ledger, queue)

    await handler(make_message(content="Review reconnect handling."))

    capture_id = await queue.get()
    capture = ledger.get_capture(capture_id)

    assert capture.status == RECEIVED
    assert capture.raw_text == "Review reconnect handling."
    assert capture.receipt_message_id == "9001"


@pytest.mark.asyncio
async def test_live_capture_advances_reconcile_marker_after_commit(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    handler = create_capture_handler(
        make_settings(),
        ledger,
        queue,
        advance_reconcile_marker=True,
    )

    await handler(make_message(content="Review reconnect handling.", message_id=1002))

    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1002"


@pytest.mark.asyncio
async def test_duplicate_capture_does_not_send_new_receipt_or_enqueue(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    handler = create_capture_handler(make_settings(), ledger, queue)
    original = make_message(content="Review reconnect handling.", message_id=1001)
    duplicate = make_message(content="Review reconnect handling again.", message_id=1001)

    await handler(original)
    await queue.get()
    await handler(duplicate)

    assert queue.qsize() == 0
    assert duplicate.channel.sent_contents == []
    assert ledger.status_counts() == {RECEIVED: 1}


@pytest.mark.asyncio
async def test_saved_receipt_failure_does_not_block_enqueue(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    handler = create_capture_handler(make_settings(), ledger, queue)
    message = make_message(content="Review reconnect handling.")
    message.channel.fail_send = True

    await handler(message)

    capture_id = await queue.get()
    capture = ledger.get_capture(capture_id)
    assert capture.status == RECEIVED
    assert capture.receipt_message_id is None


@pytest.mark.asyncio
async def test_capture_handler_rejects_sensitive_message_without_enqueueing(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    handler = create_capture_handler(make_settings(), ledger, queue)

    message = make_message(content="password=hunter2")

    await handler(message)

    assert queue.qsize() == 0
    assert ledger.status_counts() == {REJECTED_SENSITIVE: 1}
    assert ledger.enqueueable_capture_ids() == []

    rejected = ledger.captures_by_status(REJECTED_SENSITIVE)
    assert len(rejected) == 1
    assert rejected[0].raw_text is None
    assert rejected[0].redacted_text == "password=[REDACTED]"
    assert "hunter2" not in rejected[0].redacted_text
    assert rejected[0].receipt_message_id == "9001"
    assert "hunter2" not in message.channel.last_content
    assert "password_assignment" in message.channel.last_content


class FakeChannel:
    def __init__(self):
        self.id = 200
        self.fail_send = False
        self.sent_contents = []

    async def send(self, content):
        if self.fail_send:
            raise RuntimeError("discord unavailable")
        self.sent_contents.append(content)
        self.last_content = content
        return SimpleNamespace(id=9001)
