import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from secondbrain.capture_service import CaptureService
from secondbrain.ledger import FAILED, FILED, INBOX, RECEIVED, Ledger
from secondbrain.vault_writer import VaultWriter
import secondbrain.worker as worker_module
from secondbrain.worker import (
    CaptureQueue,
    process_capture_once,
    run_capture_worker,
)


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


def make_settings():
    return SimpleNamespace(
        gemini_api_key="fake",
        gemini_model="gemini-test",
        classification_confidence_threshold=0.75,
    )


def make_capture_service(ledger: Ledger, *, receipt_client=None):
    return CaptureService(
        settings=make_settings(),
        ledger=ledger,
        receipt_client=receipt_client,
    )


def insert_capture(ledger: Ledger):
    return insert_accepted_capture(ledger,
        discord_message_id="1513233540316266517",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
        received_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
    )


def insert_attachment_only_capture(ledger: Ledger):
    return insert_accepted_capture(ledger,
        discord_message_id="1513233540316266519",
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
        received_at=datetime(2026, 6, 7, 12, 31, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_process_capture_routes_classifier_failure_to_inbox(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    writer = VaultWriter(tmp_path / "vault")

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger),
        vault_writer=writer,
        classifier_client=FakeClient(error=RuntimeError("timeout")),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert result is not None
    assert result.status == INBOX
    assert result.inbox_reason == "classifier failed: RuntimeError: timeout"
    assert updated.status == INBOX
    assert updated.derived_note_path == result.note_path

    note_path = tmp_path / "vault" / result.note_path
    assert note_path.exists()
    markdown = note_path.read_text(encoding="utf-8")
    assert 'area: "inbox"' in markdown
    assert "# Unclassified capture" in markdown
    assert "Review reconnect handling." in markdown


@pytest.mark.asyncio
async def test_classifier_validation_failure_does_not_log_model_output(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)

    await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger),
        vault_writer=VaultWriter(tmp_path / "vault"),
        classifier_client=FakeClient(
            parsed={
                "folder": "projects",
                "body": "DO NOT PRINT THIS CAPTURE TEXT",
            }
        ),
    )

    output = capsys.readouterr().out

    assert "DO NOT PRINT THIS CAPTURE TEXT" not in output
    assert "classifier_failure" in output
    assert "error_type" in output
    assert "timestamp" in output
    assert capture.capture_id in output


@pytest.mark.asyncio
async def test_process_capture_routes_low_confidence_to_inbox(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    writer = VaultWriter(tmp_path / "vault")
    payload = {**VALID_CLASSIFICATION, "confidence": 0.2}

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger),
        vault_writer=writer,
        classifier_client=FakeClient(parsed=payload),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert result is not None
    assert result.status == INBOX
    assert result.inbox_reason == "classification confidence below threshold"
    assert updated.status == INBOX
    assert updated.derived_note_path.startswith("00_inbox/")


@pytest.mark.asyncio
async def test_process_capture_routes_attachment_only_capture_to_inbox_without_gemini(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_attachment_only_capture(ledger)
    writer = VaultWriter(tmp_path / "vault")

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger),
        vault_writer=writer,
        classifier_client=FakeClient(error=AssertionError("Gemini should not be called")),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert result is not None
    assert result.status == INBOX
    assert result.inbox_reason == (
        "attachment-only capture; attachment content was not archived or classified"
    )
    assert updated.status == INBOX
    assert updated.derived_note_path.startswith("00_inbox/")

    markdown = (tmp_path / "vault" / updated.derived_note_path).read_text(encoding="utf-8")
    assert "# Attachment-only capture" in markdown
    assert "attachment content was not archived or classified" in markdown


@pytest.mark.asyncio
async def test_process_capture_preserves_attachment_warning_in_final_receipt(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_accepted_capture(
        ledger,
        discord_message_id="1513233540316270000",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
        has_attachments=True,
        attachment_metadata=[
            {
                "filename": "image.png",
                "content_type": "image/png",
                "size": 100,
                "url": "https://cdn.discordapp.com/attachments/image.png",
            }
        ],
        received_at=datetime(2026, 6, 7, 12, 32, 0, tzinfo=UTC),
    )
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    receipt_client = RecordingReceiptClient()

    await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger, receipt_client=receipt_client),
        vault_writer=VaultWriter(tmp_path / "vault"),
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    assert "⚠️ Attachment detected but not archived in the MVP." in receipt_client.edited_content


@pytest.mark.asyncio
async def test_process_capture_edits_successful_filing_receipt(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    receipt_client = RecordingReceiptClient()

    await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger, receipt_client=receipt_client),
        vault_writer=VaultWriter(tmp_path / "vault"),
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    assert receipt_client.edited_content == (
        f"✅ {capture.capture_id} filed.\n"
        "Location: 20_projects / halo\n"
        "Type: task\n"
        "Tags: telemetry, websocket"
    )


@pytest.mark.asyncio
async def test_process_capture_edits_classifier_failure_inbox_receipt(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    receipt_client = RecordingReceiptClient()

    await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger, receipt_client=receipt_client),
        vault_writer=VaultWriter(tmp_path / "vault"),
        classifier_client=FakeClient(error=RuntimeError("timeout")),
    )

    assert receipt_client.edited_content == (
        f"⚠️ {capture.capture_id} saved to 00_inbox.\n"
        "Reason: automatic classification failed. Your note is safe."
    )


@pytest.mark.asyncio
async def test_process_capture_marks_failed_when_vault_write_fails(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger),
        vault_writer=FailingVaultWriter(),
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert result is not None
    assert result.status == FAILED
    assert result.note_path is None
    assert updated.status == FAILED
    assert updated.raw_text == "Review reconnect handling."
    assert updated.derived_note_path is None
    assert updated.last_error == "vault write failed: OSError: vault unavailable"

    output = capsys.readouterr().out
    assert "capture_failed" in output
    assert '"error_type":"OSError"' in output
    assert "vault unavailable" not in output


@pytest.mark.asyncio
async def test_process_capture_edits_vault_failure_receipt(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    receipt_client = RecordingReceiptClient()

    await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger, receipt_client=receipt_client),
        vault_writer=FailingVaultWriter(),
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    assert receipt_client.edited_content == (
        f"❌ {capture.capture_id} captured but vault filing failed.\n"
        "Your original note is safe in the local ledger."
    )


@pytest.mark.asyncio
async def test_vault_failure_receipt_preserves_attachment_warning(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_accepted_capture(
        ledger,
        discord_message_id="attachment-failure",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review attached sketch.",
        has_attachments=True,
        attachment_metadata=[],
    )
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    receipt_client = RecordingReceiptClient()

    await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger, receipt_client=receipt_client),
        vault_writer=FailingVaultWriter(),
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    assert "⚠️ Attachment detected but not archived in the MVP." in receipt_client.edited_content


@pytest.mark.asyncio
async def test_run_capture_worker_marks_unexpected_errors_failed(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    queue = CaptureQueue()

    async def broken_process_capture_once(**kwargs):
        raise RuntimeError("unexpected boom")

    monkeypatch.setattr(worker_module, "process_capture_once", broken_process_capture_once)
    worker = asyncio.create_task(
        run_capture_worker(
            settings=make_settings(),
            capture_service=make_capture_service(ledger),
            queue=queue,
            vault_writer=VaultWriter(tmp_path / "vault"),
            classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
        )
    )

    try:
        await queue.enqueue(capture.capture_id)
        await wait_for_status(ledger, capture.capture_id, FAILED)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    updated = ledger.get_capture(capture.capture_id)
    assert updated.status == FAILED
    assert updated.last_error == "worker error: RuntimeError: unexpected boom"


@pytest.mark.asyncio
async def test_service_enqueue_unfinished_captures_resets_classifying_and_returns_work(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    first = insert_capture(ledger)
    second = insert_accepted_capture(ledger,
        discord_message_id="1513233540316266518",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Second capture.",
        received_at=datetime(2026, 6, 7, 12, 30, 0, tzinfo=UTC),
    )
    ledger.mark_classifying(second.capture_id)
    service = CaptureService(
        settings=make_settings(),
        ledger=ledger,
        notify_capture=queue.enqueue,
    )

    queued = await service.enqueue_unfinished_captures()
    first_queued = await queue.get()
    second_queued = await queue.get()

    assert queued == [first.capture_id, second.capture_id]
    assert [first_queued, second_queued] == queued
    assert ledger.get_capture(second.capture_id).status == RECEIVED


@pytest.mark.asyncio
async def test_capture_queue_waits_for_consumer_on_bounded_queue():
    queue = CaptureQueue(maxsize=1)
    consumed = []

    async def consume_two():
        for _ in range(2):
            consumed.append(await queue.get())
            queue.task_done()

    consumer = asyncio.create_task(consume_two())
    await queue.enqueue("one")
    producer = asyncio.create_task(queue.enqueue("two"))
    await consumer
    await producer

    assert consumed == ["one", "two"]


@pytest.mark.asyncio
async def test_run_capture_worker_consumes_queue_and_files_capture(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    queue = CaptureQueue()
    writer = VaultWriter(tmp_path / "vault")
    worker = asyncio.create_task(
        run_capture_worker(
            settings=make_settings(),
            capture_service=make_capture_service(ledger),
            queue=queue,
            vault_writer=writer,
            classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
        )
    )

    try:
        await queue.enqueue(capture.capture_id)
        await wait_for_status(ledger, capture.capture_id, FILED)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    updated = ledger.get_capture(capture.capture_id)
    assert updated.status == FILED
    assert updated.derived_note_path is not None
    assert (tmp_path / "vault" / updated.derived_note_path).exists()


@pytest.mark.asyncio
async def test_process_capture_keeps_filed_status_when_receipt_edit_fails(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    writer = VaultWriter(tmp_path / "vault")

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger, receipt_client=FailingReceiptClient()),
        vault_writer=writer,
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert result is not None
    assert result.status == FILED
    assert updated.status == FILED
    assert updated.last_error is None


@pytest.mark.asyncio
async def test_process_capture_replaces_failed_receipt_edit_once(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")
    receipt_client = ReplacementReceiptClient()

    await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger, receipt_client=receipt_client),
        vault_writer=VaultWriter(tmp_path / "vault"),
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert updated.receipt_message_id == "9002"
    assert receipt_client.channel.sent_contents == [
        (
            f"✅ {capture.capture_id} filed.\n"
            "Location: 20_projects / halo\n"
            "Type: task\n"
            "Tags: telemetry, websocket"
        )
    ]
    assert event_types(ledger, capture.capture_id)[-1] == "RECEIPT_REPLACED"


@pytest.mark.asyncio
async def test_process_capture_sends_replacement_when_initial_receipt_is_missing(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    receipt_client = ReplacementReceiptClient()

    await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        capture_service=make_capture_service(ledger, receipt_client=receipt_client),
        vault_writer=VaultWriter(tmp_path / "vault"),
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert updated.receipt_message_id == "9002"
    assert receipt_client.channel.sent_contents == [
        (
            f"✅ {capture.capture_id} filed.\n"
            "Location: 20_projects / halo\n"
            "Type: task\n"
            "Tags: telemetry, websocket"
        )
    ]
    assert event_types(ledger, capture.capture_id)[-1] == "RECEIPT_REPLACED"


class FakeClient:
    def __init__(self, *, parsed=None, error=None):
        self.aio = SimpleNamespace(models=FakeModels(parsed=parsed, error=error))


class FakeModels:
    def __init__(self, *, parsed, error):
        self.parsed = parsed
        self.error = error

    async def generate_content(self, **kwargs):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(parsed=self.parsed)


class FailingVaultWriter:
    def write_note(self, **kwargs):
        raise OSError("vault unavailable")


class FailingReceiptClient:
    def get_channel(self, channel_id):
        return FailingReceiptChannel()


class RecordingReceiptClient:
    def __init__(self):
        self.channel = RecordingReceiptChannel()

    @property
    def edited_content(self):
        return self.channel.message.edited_content

    def get_channel(self, channel_id):
        return self.channel


class RecordingReceiptChannel:
    def __init__(self):
        self.message = RecordingReceiptMessage()

    async def fetch_message(self, message_id):
        return self.message


class RecordingReceiptMessage:
    def __init__(self):
        self.edited_content = None

    async def edit(self, *, content):
        self.edited_content = content


class FailingReceiptChannel:
    async def fetch_message(self, message_id):
        raise RuntimeError("receipt missing")


class ReplacementReceiptClient:
    def __init__(self):
        self.channel = ReplacementReceiptChannel()

    def get_channel(self, channel_id):
        return self.channel


class ReplacementReceiptChannel:
    def __init__(self):
        self.sent_contents = []

    async def fetch_message(self, message_id):
        raise RuntimeError("receipt missing")

    async def send(self, content):
        self.sent_contents.append(content)
        return SimpleNamespace(id=9002)


def insert_accepted_capture(ledger: Ledger, **kwargs):
    return ledger.insert_accepted_capture(**kwargs).capture


async def wait_for_status(ledger: Ledger, capture_id: str, status: str) -> None:
    for _ in range(50):
        if ledger.get_capture(capture_id).status == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"capture did not reach status {status}")


def event_types(ledger: Ledger, capture_id: str) -> list[str]:
    rows = ledger._connection.execute(
        "SELECT event_type FROM capture_events WHERE capture_id = ? ORDER BY id",
        (capture_id,),
    ).fetchall()
    return [row["event_type"] for row in rows]
