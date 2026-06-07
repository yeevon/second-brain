import asyncio
from dataclasses import dataclass
from typing import Any

from secondbrain.classifier import classify_capture
from secondbrain.ledger import FAILED, FILED, INBOX, Ledger
from secondbrain.models import Classification
from secondbrain.observability import log_metadata
from secondbrain.receipts import (
    deliver_final_receipt,
    format_filed_receipt,
    format_inbox_receipt,
    format_vault_failure_receipt,
)
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
    note_path: str | None
    inbox_reason: str | None


async def process_capture_once(
    *,
    capture_id: str,
    settings: Any,
    ledger: Ledger,
    vault_writer: VaultWriter,
    classifier_client: Any | None = None,
    receipt_client: Any | None = None,
) -> ProcessingResult | None:
    if not ledger.mark_classifying(capture_id):
        return None

    capture = ledger.get_capture(capture_id)
    log_metadata(
        "capture_classifying",
        capture_id=capture.capture_id,
        discord_message_id=capture.discord_message_id,
        status_transition="RECEIVED->CLASSIFYING",
    )
    if capture.raw_text is None:
        raise ValueError(f"capture has no raw text: {capture_id}")

    if not capture.raw_text.strip() and capture.has_attachments:
        outcome = attachment_only_inbox_outcome()
    else:
        outcome = await classify_capture(
            capture.raw_text,
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            confidence_threshold=settings.classification_confidence_threshold,
            client=classifier_client,
        )
    try:
        write_result = vault_writer.write_note(
            capture_id=capture.capture_id,
            source_message_id=capture.discord_message_id,
            created_at=capture.received_at,
            classification=outcome.classification,
            model=settings.gemini_model,
        )
    except Exception as exc:
        failure_reason = f"vault write failed: {type(exc).__name__}: {exc}"
        ledger.update_capture(
            capture.capture_id,
            status=FAILED,
            classification_json=outcome.classification.model_dump(mode="json"),
            last_error=failure_reason,
            event_type="CAPTURE_FAILED",
            event_payload={"reason": failure_reason},
        )
        print(f"{capture.capture_id} failed: vault write failed")
        print(f"  reason: {failure_reason}")
        log_metadata(
            "capture_failed",
            capture_id=capture.capture_id,
            discord_message_id=capture.discord_message_id,
            status_transition=f"CLASSIFYING->{FAILED}",
            classification_confidence=outcome.classification.confidence,
            error_type=type(exc).__name__,
        )
        if receipt_client is not None:
            await try_deliver_final_receipt(
                receipt_client,
                ledger,
                ledger.get_capture(capture.capture_id),
                format_vault_failure_receipt(capture.capture_id),
            )
        return ProcessingResult(
            capture_id=capture.capture_id,
            status=FAILED,
            note_path=None,
            inbox_reason=failure_reason,
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
        log_metadata(
            "capture_inbox",
            capture_id=capture.capture_id,
            discord_message_id=capture.discord_message_id,
            status_transition=f"CLASSIFYING->{INBOX}",
            classification_confidence=outcome.classification.confidence,
            derived_note_path=write_result.note_path,
        )
        receipt_content = format_inbox_receipt(
            capture_id=capture.capture_id,
            note_path=write_result.note_path,
            reason=outcome.inbox_reason,
            has_attachments=capture.has_attachments,
        )
    else:
        print(f"{capture.capture_id} filed: {write_result.note_path}")
        log_metadata(
            "capture_filed",
            capture_id=capture.capture_id,
            discord_message_id=capture.discord_message_id,
            status_transition=f"CLASSIFYING->{FILED}",
            classification_confidence=outcome.classification.confidence,
            derived_note_path=write_result.note_path,
        )
        receipt_content = format_filed_receipt(
            capture_id=capture.capture_id,
            note_path=write_result.note_path,
            classification=outcome.classification,
            has_attachments=capture.has_attachments,
        )

    if receipt_client is not None:
        await try_deliver_final_receipt(
            receipt_client,
            ledger,
            ledger.get_capture(capture.capture_id),
            receipt_content,
        )

    return ProcessingResult(
        capture_id=capture.capture_id,
        status=status,
        note_path=write_result.note_path,
        inbox_reason=outcome.inbox_reason,
    )


def unfinished_capture_ids(ledger: Ledger) -> list[str]:
    ledger.reset_classifying_to_received()
    return ledger.enqueueable_capture_ids()


async def enqueue_capture_ids(capture_ids: list[str], queue: CaptureQueue) -> list[str]:
    for capture_id in capture_ids:
        await queue.enqueue(capture_id)
    return capture_ids


async def run_capture_worker(
    *,
    settings: Any,
    ledger: Ledger,
    queue: CaptureQueue,
    vault_writer: VaultWriter,
    classifier_client: Any | None = None,
    receipt_client: Any | None = None,
) -> None:
    while True:
        capture_id = await queue.get()
        try:
            await process_capture_once(
                capture_id=capture_id,
                settings=settings,
                ledger=ledger,
                vault_writer=vault_writer,
                classifier_client=classifier_client,
                receipt_client=receipt_client,
            )
        except Exception as exc:
            failure_reason = f"worker error: {type(exc).__name__}: {exc}"
            try:
                ledger.update_capture(
                    capture_id,
                    status=FAILED,
                    last_error=failure_reason,
                    event_type="CAPTURE_FAILED",
                    event_payload={"reason": failure_reason},
                )
                if receipt_client is not None:
                    await try_deliver_final_receipt(
                        receipt_client,
                        ledger,
                        ledger.get_capture(capture_id),
                        format_vault_failure_receipt(capture_id),
                    )
            except Exception as update_exc:
                print(
                    f"{capture_id} failed to mark worker error: "
                    f"{type(update_exc).__name__}: {update_exc}"
                )
            print(f"{capture_id} worker error: {type(exc).__name__}: {exc}")
            log_metadata(
                "capture_worker_error",
                capture_id=capture_id,
                status_transition=f"CLASSIFYING->{FAILED}",
                error_type=type(exc).__name__,
            )
        finally:
            queue.task_done()


class ClassificationOutcomeLike:
    def __init__(self, classification: Classification, route: str, inbox_reason: str | None) -> None:
        self.classification = classification
        self.route = route
        self.inbox_reason = inbox_reason


def attachment_only_inbox_outcome() -> ClassificationOutcomeLike:
    reason = "attachment-only capture; attachment content was not archived or classified"
    return ClassificationOutcomeLike(
        classification=Classification(
            folder="inbox",
            project=None,
            note_type="attachment",
            title="Attachment-only capture",
            tags=["inbox", "attachment"],
            body=reason,
            actions=[],
            needs_clarification=True,
            clarifying_question=None,
            confidence=0.0,
        ),
        route="inbox",
        inbox_reason=reason,
    )


async def try_deliver_final_receipt(receipt_client: Any, ledger: Ledger, capture, content: str) -> None:
    try:
        await deliver_final_receipt(receipt_client, ledger, capture, content)
    except Exception as exc:
        print(f"{capture.capture_id} final receipt failed: {type(exc).__name__}: {exc}")
