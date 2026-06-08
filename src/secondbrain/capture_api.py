from __future__ import annotations

from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException

from secondbrain.api_models import (
    CaptureResponse,
    EditReceiptRequest,
    HealthResponse,
    MarkFailedRequest,
    MarkFiledRequest,
    MarkInboxRequest,
    ReceiptDeliveryResponse,
    TransitionResponse,
)
from secondbrain.capture_service import (
    CaptureNotFoundError,
    CaptureService,
    ConflictingReplayError,
    InvalidCaptureTransitionError,
    ReceiptDeliveryError,
)
from secondbrain.capture_models import CaptureRecord, TransitionResult


INTERNAL_TOKEN_HEADER = "X-Second-Brain-Internal-Token"


def build_require_internal_token(expected_token: str):
    async def require_internal_token(
        supplied_token: Annotated[str | None, Header(alias=INTERNAL_TOKEN_HEADER)] = None,
    ) -> None:
        if supplied_token is None or not compare_digest(supplied_token, expected_token):
            raise HTTPException(status_code=401, detail="unauthorized")

    return require_internal_token


def create_capture_api(*, capture_service: CaptureService, internal_token: str) -> FastAPI:
    require_internal_token = build_require_internal_token(internal_token)
    app = FastAPI(
        title="Second Brain Capture Service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health", response_model=HealthResponse)
    async def health():
        try:
            capture_service.assert_healthy()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="capture-service unavailable") from exc
        return HealthResponse(status="ok", service="capture-service")

    @app.get(
        "/internal/captures/{capture_id}",
        response_model=CaptureResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def get_capture(capture_id: str):
        return _capture_response(_get_capture(capture_service, capture_id))

    @app.post(
        "/internal/captures/{capture_id}/mark-forwarded",
        response_model=TransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def mark_forwarded(capture_id: str):
        return _transition_response(_transition(lambda: capture_service.mark_forwarded(capture_id)))

    @app.post(
        "/internal/captures/{capture_id}/mark-classifying",
        response_model=TransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def mark_classifying(capture_id: str):
        return _transition_response(_transition(lambda: capture_service.mark_classifying(capture_id)))

    @app.post(
        "/internal/captures/{capture_id}/mark-filed",
        response_model=TransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def mark_filed(capture_id: str, request: MarkFiledRequest):
        return _transition_response(
            _transition(
                lambda: capture_service.mark_filed(
                    capture_id=capture_id,
                    note_path=request.note_path,
                    classification=request.classification,
                )
            )
        )

    @app.post(
        "/internal/captures/{capture_id}/mark-inbox",
        response_model=TransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def mark_inbox(capture_id: str, request: MarkInboxRequest):
        return _transition_response(
            _transition(
                lambda: capture_service.mark_inbox(
                    capture_id=capture_id,
                    note_path=request.note_path,
                    classification=request.classification,
                    reason=request.reason,
                )
            )
        )

    @app.post(
        "/internal/captures/{capture_id}/mark-failed",
        response_model=TransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def mark_failed(capture_id: str, request: MarkFailedRequest):
        return _transition_response(
            _transition(
                lambda: capture_service.mark_failed(
                    capture_id=capture_id,
                    reason=request.reason,
                )
            )
        )

    @app.post(
        "/internal/captures/{capture_id}/retry",
        response_model=TransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def retry(capture_id: str):
        return _transition_response(_transition(lambda: capture_service.retry(capture_id)))

    @app.post(
        "/internal/receipts/{capture_id}/edit",
        response_model=ReceiptDeliveryResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def edit_receipt(capture_id: str, request: EditReceiptRequest):
        try:
            delivery = await capture_service.edit_receipt(
                capture_id=capture_id,
                content=request.content,
            )
        except CaptureNotFoundError as exc:
            raise HTTPException(status_code=404, detail="capture not found") from exc
        except ReceiptDeliveryError as exc:
            raise HTTPException(status_code=503, detail="receipt delivery failed") from exc
        return ReceiptDeliveryResponse(
            capture_id=capture_id,
            delivered=delivery.delivered,
            replaced=delivery.replaced,
            receipt_message_id=delivery.receipt_message_id,
        )

    return app


def _get_capture(capture_service: CaptureService, capture_id: str) -> CaptureRecord:
    try:
        return capture_service.get_capture(capture_id)
    except CaptureNotFoundError as exc:
        raise HTTPException(status_code=404, detail="capture not found") from exc


def _transition(operation) -> TransitionResult:
    try:
        return operation()
    except CaptureNotFoundError as exc:
        raise HTTPException(status_code=404, detail="capture not found") from exc
    except (InvalidCaptureTransitionError, ConflictingReplayError) as exc:
        raise HTTPException(status_code=409, detail="capture transition conflict") from exc


def _capture_response(capture: CaptureRecord) -> CaptureResponse:
    return CaptureResponse(
        capture_id=capture.capture_id,
        discord_message_id=capture.discord_message_id,
        discord_channel_id=capture.discord_channel_id,
        discord_guild_id=capture.discord_guild_id,
        discord_author_id=capture.discord_author_id,
        status=capture.status,
        raw_text=capture.raw_text,
        redacted_text=capture.redacted_text,
        is_sensitive=capture.is_sensitive,
        has_attachments=capture.has_attachments,
        attachment_metadata=capture.attachment_metadata,
        received_at=capture.received_at,
        receipt_message_id=capture.receipt_message_id,
        derived_note_path=capture.derived_note_path,
        last_error=capture.last_error,
    )


def _transition_response(transition: TransitionResult) -> TransitionResponse:
    return TransitionResponse(
        capture_id=transition.capture_id,
        previous_status=transition.previous_status,
        status=transition.status,
        changed=transition.changed,
    )
