import asyncio
from dataclasses import dataclass
from typing import Any

from secondbrain.classifier import classify_capture
from secondbrain.ledger import FILED, INBOX, Ledger
from secondbrain.vault_writer import VaultWriter


class CaptureQueue:
    def __init__(self, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)

    async def enqueue(self, capture_id: str) -> None:
        await self._queue.put(capture_id)

    async def get(self) -> str:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()


@dataclass(frozen=True)
class ProcessingResult:
    capture_id: str
    status: str
    note_path: str
    inbox_reason: str | None


async def process_capture_once(
    *,
    capture_id: str,
    settings: Any,
    ledger: Ledger,
    vault_writer: VaultWriter,
    classifier_client: Any | None = None,
) -> ProcessingResult | None:
    if not ledger.mark_classifying(capture_id):
        return None

    capture = ledger.get_capture(capture_id)
    if capture.raw_text is None:
        raise ValueError(f"capture has no raw text: {capture_id}")

    outcome = await classify_capture(
        capture.raw_text,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        confidence_threshold=settings.classification_confidence_threshold,
        client=classifier_client,
    )
    write_result = vault_writer.write_note(
        capture_id=capture.capture_id,
        source_message_id=capture.discord_message_id,
        created_at=capture.received_at,
        classification=outcome.classification,
        model=settings.gemini_model,
    )

    status = INBOX if outcome.route == "inbox" else FILED
    event_type = "CAPTURE_INBOX" if status == INBOX else "CAPTURE_FILED"
    event_payload = {"path": write_result.note_path}
    if outcome.inbox_reason is not None:
        event_payload["reason"] = outcome.inbox_reason

    ledger.update_capture(
        capture.capture_id,
        status=status,
        classification_json=outcome.classification.model_dump(mode="json"),
        derived_note_path=write_result.note_path,
        last_error=outcome.inbox_reason,
        event_type=event_type,
        event_payload=event_payload,
    )

    if status == INBOX:
        print(f"{capture.capture_id} filed to Inbox: {write_result.note_path}")
        print(f"  reason: {outcome.inbox_reason}")
    else:
        print(f"{capture.capture_id} filed: {write_result.note_path}")

    return ProcessingResult(
        capture_id=capture.capture_id,
        status=status,
        note_path=write_result.note_path,
        inbox_reason=outcome.inbox_reason,
    )
