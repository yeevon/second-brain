import asyncio
from dataclasses import dataclass
from typing import Any

from secondbrain.capture_models import FAILED, FILED, INBOX
from secondbrain.classifier import classify_capture
from secondbrain.models import Classification
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

    async def join(self) -> None:
        await self._queue.join()

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
    capture_service: Any,
    vault_writer: VaultWriter,
    classifier_client: Any | None = None,
) -> ProcessingResult | None:
    capture = capture_service.claim_for_processing(capture_id)
    if capture is None:
        return None

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
        await capture_service.complete_failed(
            capture_id=capture.capture_id,
            reason=failure_reason,
            classification=outcome.classification,
        )
        return ProcessingResult(
            capture_id=capture.capture_id,
            status=FAILED,
            note_path=None,
            inbox_reason=failure_reason,
        )

    status = INBOX if outcome.route == "inbox" else FILED
    if status == INBOX:
        await capture_service.complete_inbox(
            capture_id=capture.capture_id,
            classification=outcome.classification,
            note_path=write_result.note_path,
            reason=outcome.inbox_reason,
        )
    else:
        await capture_service.complete_filed(
            capture_id=capture.capture_id,
            classification=outcome.classification,
            note_path=write_result.note_path,
        )

    return ProcessingResult(
        capture_id=capture.capture_id,
        status=status,
        note_path=write_result.note_path,
        inbox_reason=outcome.inbox_reason,
    )


async def run_capture_worker(
    *,
    settings: Any,
    capture_service: Any,
    queue: CaptureQueue,
    vault_writer: VaultWriter,
    classifier_client: Any | None = None,
) -> None:
    while True:
        capture_id = await queue.get()
        try:
            await process_capture_once(
                capture_id=capture_id,
                settings=settings,
                capture_service=capture_service,
                vault_writer=vault_writer,
                classifier_client=classifier_client,
            )
        except Exception as exc:
            failure_reason = f"worker error: {type(exc).__name__}: {exc}"
            try:
                await capture_service.complete_failed(
                    capture_id=capture_id,
                    reason=failure_reason,
                )
            except Exception as update_exc:
                from secondbrain.observability import log_metadata

                log_metadata(
                    "capture_worker_error_update_failed",
                    capture_id=capture_id,
                    error_type=type(update_exc).__name__,
                )
            from secondbrain.observability import log_metadata

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
