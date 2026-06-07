import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from secondbrain.ledger import FAILED, FILED, INBOX, RECEIVED, Ledger
from secondbrain.vault_writer import VaultWriter
import secondbrain.worker as worker_module
from secondbrain.worker import CaptureQueue, enqueue_unfinished_captures, process_capture_once, run_capture_worker


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


def insert_capture(ledger: Ledger):
    return ledger.insert_accepted_capture(
        discord_message_id="1513233540316266517",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
        received_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
    )


def insert_attachment_only_capture(ledger: Ledger):
    return ledger.insert_accepted_capture(
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
        ledger=ledger,
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
    assert "area: inbox" in markdown
    assert "# Unclassified capture" in markdown
    assert "Review reconnect handling." in markdown


@pytest.mark.asyncio
async def test_process_capture_routes_low_confidence_to_inbox(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    writer = VaultWriter(tmp_path / "vault")
    payload = {**VALID_CLASSIFICATION, "confidence": 0.2}

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        ledger=ledger,
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
        ledger=ledger,
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
async def test_process_capture_marks_failed_when_vault_write_fails(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        ledger=ledger,
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
    assert f"{capture.capture_id} failed: vault write failed" in output
    assert "vault unavailable" in output


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
            ledger=ledger,
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
async def test_enqueue_unfinished_captures_resets_classifying_and_queues_work(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    first = insert_capture(ledger)
    second = ledger.insert_accepted_capture(
        discord_message_id="1513233540316266518",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Second capture.",
        received_at=datetime(2026, 6, 7, 12, 30, 0, tzinfo=UTC),
    )
    ledger.mark_classifying(second.capture_id)
    queue = CaptureQueue()

    queued = await enqueue_unfinished_captures(ledger, queue)

    assert queued == [first.capture_id, second.capture_id]
    assert ledger.get_capture(second.capture_id).status == RECEIVED
    assert await queue.get() == first.capture_id
    assert await queue.get() == second.capture_id


@pytest.mark.asyncio
async def test_run_capture_worker_consumes_queue_and_files_capture(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    queue = CaptureQueue()
    writer = VaultWriter(tmp_path / "vault")
    worker = asyncio.create_task(
        run_capture_worker(
            settings=make_settings(),
            ledger=ledger,
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
        ledger=ledger,
        vault_writer=writer,
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
        receipt_client=FailingReceiptClient(),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert result is not None
    assert result.status == FILED
    assert updated.status == FILED
    assert updated.last_error is None


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


class FailingReceiptChannel:
    async def fetch_message(self, message_id):
        raise RuntimeError("receipt missing")


async def wait_for_status(ledger: Ledger, capture_id: str, status: str) -> None:
    for _ in range(50):
        if ledger.get_capture(capture_id).status == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"capture did not reach status {status}")
