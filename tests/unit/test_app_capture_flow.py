import asyncio
from types import SimpleNamespace

import pytest

from secondbrain.app import LocalWorkerStartup, format_status_report, start_local_worker_and_enqueue_recovered
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import FAILED, FILED, INBOX, RECEIVED, REJECTED_SENSITIVE, Ledger
from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID
from secondbrain.worker import CaptureQueue
from secondbrain.vault_writer import VaultWriter


def make_settings(tmp_path):
    return SimpleNamespace(
        discord_guild_id=300,
        discord_capture_channel_id=200,
        discord_allowed_user_id=400,
        classifier_queue_maxsize=10,
        startup_reconcile_limit=10,
        gemini_api_key="fake",
        gemini_model="gemini-test",
        classification_confidence_threshold=0.75,
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
    )


def make_message(*, content="capture this", message_id=1001, attachments=None):
    channel = FakeChannel()
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=300),
        channel=channel,
        author=SimpleNamespace(id=400, bot=False),
        webhook_id=None,
        content=content,
        attachments=attachments or [],
    )


def make_service(settings, ledger, queue, receipt_client=None):
    return CaptureService(
        settings=settings,
        ledger=ledger,
        notify_capture=queue.enqueue,
        receipt_client=receipt_client,
    )


@pytest.mark.asyncio
async def test_capture_handler_persists_receipts_and_enqueues_capture(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    message = make_message(content="Review reconnect handling.")
    service = make_service(make_settings(tmp_path), ledger, queue, receipt_client=FakeClient(message.channel))

    await service.handle_gateway_message(message)

    capture_id = await queue.get()
    capture = ledger.get_capture(capture_id)

    assert capture.status == RECEIVED
    assert capture.raw_text == "Review reconnect handling."
    assert capture.receipt_message_id == "9001"
    assert message.channel.last_content == (
        f"⏳ {capture.capture_id} received.\n"
        "Your note is saved. Processing…"
    )


@pytest.mark.asyncio
async def test_capture_handler_logs_metadata_without_raw_text(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    service = make_service(make_settings(tmp_path), ledger, queue)

    await service.handle_gateway_message(make_message(content="Review reconnect handling."))

    output = capsys.readouterr().out
    assert "capture_received" in output
    assert "discord_message_id" in output
    assert "Review reconnect handling." not in output


@pytest.mark.asyncio
async def test_startup_reconcile_advances_marker_after_commit(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    settings = make_settings(tmp_path)
    service = make_service(settings, ledger, queue)

    await service.startup_reconcile(FakeHistoryClient([make_message(content="Review reconnect handling.", message_id=1002)]))

    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1002"


@pytest.mark.asyncio
async def test_duplicate_capture_does_not_send_new_receipt_or_enqueue(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    service = make_service(make_settings(tmp_path), ledger, queue)
    original = make_message(content="Review reconnect handling.", message_id=1001)
    duplicate = make_message(content="Review reconnect handling again.", message_id=1001)

    await service.handle_gateway_message(original)
    await queue.get()
    await service.handle_gateway_message(duplicate)

    assert queue.qsize() == 0
    assert duplicate.channel.sent_contents == []
    assert ledger.status_counts() == {RECEIVED: 1}


@pytest.mark.asyncio
async def test_saved_receipt_failure_does_not_block_enqueue(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    message = make_message(content="Review reconnect handling.")
    message.channel.fail_send = True
    service = make_service(make_settings(tmp_path), ledger, queue, receipt_client=FakeClient(message.channel))

    await service.handle_gateway_message(message)

    capture_id = await queue.get()
    capture = ledger.get_capture(capture_id)
    assert capture.status == RECEIVED
    assert capture.receipt_message_id is None


@pytest.mark.asyncio
async def test_saved_receipt_warns_when_attachment_is_not_archived(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    message = make_message(
        content="Review this attached sketch.",
        attachments=[SimpleNamespace(filename="sketch.png", content_type="image/png", size=100, url="url")],
    )
    service = make_service(make_settings(tmp_path), ledger, queue, receipt_client=FakeClient(message.channel))

    await service.handle_gateway_message(message)

    assert "⚠️ Attachment detected but not archived in the MVP." in message.channel.last_content


@pytest.mark.asyncio
async def test_capture_handler_rejects_sensitive_message_without_enqueueing(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    service = make_service(make_settings(tmp_path), ledger, queue)

    message = make_message(content="password=hunter2")

    await service.handle_gateway_message(message)

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
    assert message.channel.last_content == (
        "⚠️ Message rejected.\n"
        "It appears to contain a credential or sensitive identifier.\n"
        "The original text was not saved or sent to Gemini."
    )
    output = capsys.readouterr().out
    assert "capture_rejected_sensitive" in output
    assert "hunter2" not in output
    assert "password=[REDACTED]" not in output


def test_format_status_report_includes_operational_counts(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    filed = insert_capture(ledger, discord_message_id="1001")
    inbox = insert_capture(ledger, discord_message_id="1002")
    rejected = ledger.insert_sensitive_rejection(
        discord_message_id="1003",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        redacted_text="password=[REDACTED]",
        sensitivity_flags=("password_assignment",),
    ).capture
    failed = insert_capture(ledger, discord_message_id="1004")
    ledger.update_capture(filed.capture_id, status=FILED, derived_note_path="20_projects/halo/filed.md")
    ledger.update_capture(inbox.capture_id, status=INBOX, derived_note_path="00_inbox/inbox.md")
    ledger.update_capture(failed.capture_id, status=FAILED)
    ledger.set_system_state(LAST_RECONCILED_MESSAGE_ID, "1513233540316266517")
    settings = SimpleNamespace(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
    )

    service = CaptureService(settings=settings, ledger=ledger)
    report = format_status_report(settings, service.status_snapshot())

    assert f"ledger path: {tmp_path / 'ledger.sqlite3'}" in report
    assert f"vault path: {tmp_path / 'vault'}" in report
    assert "total captures: 4" in report
    assert "captures filed: 1" in report
    assert "captures in inbox: 1" in report
    assert "captures rejected as sensitive: 1" in report
    assert "captures failed: 1" in report
    assert "last reconciled Discord message ID: 1513233540316266517" in report
    assert "last successful vault write: 00_inbox/inbox.md" in report
    assert rejected.status == REJECTED_SENSITIVE


@pytest.mark.asyncio
async def test_startup_recovery_does_not_deadlock_when_backlog_exceeds_queue_size(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    first = insert_attachment_only_capture(ledger, discord_message_id="1001")
    second = insert_attachment_only_capture(ledger, discord_message_id="1002")
    queue = CaptureQueue(maxsize=1)
    service = CaptureService(
        settings=settings,
        ledger=ledger,
        notify_capture=queue.enqueue,
    )

    worker_task, capture_ids = await asyncio.wait_for(
        start_local_worker_and_enqueue_recovered(
            settings=settings,
            capture_service=service,
            queue=queue,
            vault_writer=VaultWriter(settings.vault_path),
        ),
        timeout=1,
    )

    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker_task

    assert capture_ids == [first.capture_id, second.capture_id]
    assert ledger.status_counts() == {INBOX: 2}


@pytest.mark.asyncio
async def test_reconciliation_failure_does_not_permanently_block_worker_startup(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = insert_attachment_only_capture(ledger, discord_message_id="1001")
    queue = CaptureQueue(maxsize=1)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)
    startup = LocalWorkerStartup(
        settings=settings,
        capture_service=service,
        queue=queue,
        vault_writer=VaultWriter(settings.vault_path),
    )

    with pytest.raises(RuntimeError, match="simulated Discord history failure"):
        await startup.start_once(FailingHistoryClient())

    assert startup.worker_task is None

    result = await startup.start_once(FakeHistoryClient([]))
    assert result is not None
    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        startup.worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup.worker_task

    assert result.capture_ids == [capture.capture_id]
    assert ledger.status_counts() == {INBOX: 1}


@pytest.mark.asyncio
async def test_repeated_ready_callback_does_not_start_second_worker(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    insert_attachment_only_capture(ledger, discord_message_id="1001")
    queue = CaptureQueue(maxsize=1)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)
    startup = LocalWorkerStartup(
        settings=settings,
        capture_service=service,
        queue=queue,
        vault_writer=VaultWriter(settings.vault_path),
    )

    first = await startup.start_once(FakeHistoryClient([]))
    assert first is not None
    second = await startup.start_once(FakeHistoryClient([]))

    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        first.worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first.worker_task

    assert second is None
    assert startup.worker_task is first.worker_task


@pytest.mark.asyncio
async def test_enqueue_failure_cancels_started_worker_and_allows_clean_retry(tmp_path, monkeypatch):
    started = []
    cancelled = []

    async def recording_worker(**kwargs):
        marker = object()
        started.append(marker)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(marker)
            raise

    monkeypatch.setattr("secondbrain.app.run_capture_worker", recording_worker)

    settings = make_settings(tmp_path)
    queue = CaptureQueue(maxsize=1)
    service = FlakyEnqueueService()
    startup = LocalWorkerStartup(
        settings=settings,
        capture_service=service,
        queue=queue,
        vault_writer=VaultWriter(settings.vault_path),
    )

    with pytest.raises(RuntimeError, match="simulated enqueue failure"):
        await startup.start_once(FakeHistoryClient([]))

    assert startup.worker_task is None
    assert len(started) == 1
    assert cancelled == started

    result = await startup.start_once(FakeHistoryClient([]))
    await asyncio.sleep(0)

    assert result is not None
    assert result.capture_ids == ["recovered-capture"]
    assert startup.worker_task is result.worker_task
    assert len(started) == 2
    assert cancelled == started[:1]
    assert not result.worker_task.done()

    result.worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await result.worker_task

    assert cancelled == started


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


class FakeClient:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


class FakeHistoryClient:
    def __init__(self, messages):
        self.channel = FakeHistoryChannel(messages)

    def get_channel(self, channel_id):
        return self.channel


class FailingHistoryClient:
    def get_channel(self, channel_id):
        return FailingHistoryChannel()


class FailingHistoryChannel:
    def history(self, *, limit, after, oldest_first):
        raise RuntimeError("simulated Discord history failure")


class FlakyEnqueueService:
    def __init__(self):
        self.enqueue_calls = 0

    async def startup_reconcile(self, client):
        from secondbrain.reconcile import ReconcileResult

        return ReconcileResult(seen=0, handled=0, ignored=0, warning=None)

    async def enqueue_unfinished_captures(self):
        self.enqueue_calls += 1
        if self.enqueue_calls == 1:
            await asyncio.sleep(0)
            raise RuntimeError("simulated enqueue failure")
        return ["recovered-capture"]


class FakeHistoryChannel:
    def __init__(self, messages):
        self.messages = messages

    def history(self, *, limit, after, oldest_first):
        after_id = 0 if after is None else after.id
        messages = [message for message in self.messages if message.id > after_id]
        if oldest_first:
            messages = sorted(messages, key=lambda message: message.id)
        return FakeHistory(messages[:limit])


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


def insert_capture(ledger: Ledger, *, discord_message_id: str = "1001"):
    return ledger.insert_accepted_capture(
        discord_message_id=discord_message_id,
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
    ).capture


def insert_attachment_only_capture(ledger: Ledger, *, discord_message_id: str):
    return ledger.insert_accepted_capture(
        discord_message_id=discord_message_id,
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="",
        has_attachments=True,
        attachment_metadata=[
            {
                "filename": "image.png",
                "content_type": "image/png",
                "size": 100,
                "url": "https://cdn.discordapp.com/attachments/image.png",
            }
        ],
    ).capture
