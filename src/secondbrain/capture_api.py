from __future__ import annotations

from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException

from secondbrain.api_models import (
    AcknowledgeClassifyingRequest,
    AcknowledgeDeliveryFailedRequest,
    AcknowledgeFiledRequest,
    AcknowledgeForwardedRequest,
    AcknowledgeInboxRequest,
    CaptureResponse,
    DeliveryTransitionResponse,
    EditReceiptRequest,
    HealthResponse,
    MarkFailedRequest,
    ReceiptDeliveryResponse,
    RenewLeaseRequest,
    ScheduleRetryRequest,
    TransitionResponse,
)
from secondbrain.capture_service import (
    CaptureNotFoundError,
    CaptureService,
    ConflictingReplayError,
    InvalidCaptureTransitionError,
    ReceiptDeliveryError,
)
from secondbrain.capture_models import CaptureRecord, DeliveryMutationResult, TransitionResult


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
        "/internal/captures/{capture_id}/retry",
        response_model=TransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def retry(capture_id: str):
        return _transition_response(_transition(lambda: capture_service.retry(capture_id)))

    # ------------------------------------------------------------------
    # Attempt-aware downstream delivery callback routes
    # All routes validate delivery_attempt before mutating state.
    # ------------------------------------------------------------------

    @app.post(
        "/internal/captures/{capture_id}/delivery/acknowledge-forwarded",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def acknowledge_delivery_forwarded(capture_id: str, request: AcknowledgeForwardedRequest):
        changed = capture_service.acknowledge_delivery_forwarded(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
        )
        return _delivery_response(capture_service, capture_id, changed=changed,
                                  outcome="changed" if changed else "stale_attempt")

    @app.post(
        "/internal/captures/{capture_id}/delivery/acknowledge-classifying",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def acknowledge_delivery_classifying(capture_id: str, request: AcknowledgeClassifyingRequest):
        changed = capture_service.acknowledge_delivery_classifying(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
        )
        return _delivery_response(capture_service, capture_id, changed=changed,
                                  outcome="changed" if changed else "stale_attempt")

    @app.post(
        "/internal/captures/{capture_id}/delivery/renew-lease",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def renew_delivery_lease(capture_id: str, request: RenewLeaseRequest):
        changed = capture_service.renew_delivery_lease(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
        )
        return _delivery_response(capture_service, capture_id, changed=changed,
                                  outcome="changed" if changed else "stale_attempt")

    @app.post(
        "/internal/captures/{capture_id}/delivery/acknowledge-filed",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def acknowledge_delivery_filed(capture_id: str, request: AcknowledgeFiledRequest):
        _get_capture(capture_service, capture_id)
        result = await capture_service.acknowledge_delivery_filed(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
            derived_note_path=request.note_path,
            git_commit_hash=request.git_commit_hash,
        )
        return _delivery_mutation_response(capture_service, capture_id, result)

    @app.post(
        "/internal/captures/{capture_id}/delivery/acknowledge-inbox",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def acknowledge_delivery_inbox(capture_id: str, request: AcknowledgeInboxRequest):
        _get_capture(capture_service, capture_id)
        result = await capture_service.acknowledge_delivery_inbox(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
            derived_note_path=request.note_path,
            git_commit_hash=request.git_commit_hash,
            reason_type=request.reason_type,
        )
        return _delivery_mutation_response(capture_service, capture_id, result)

    @app.post(
        "/internal/captures/{capture_id}/delivery/schedule-retry",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def schedule_delivery_retry(capture_id: str, request: ScheduleRetryRequest):
        _get_capture(capture_service, capture_id)
        try:
            disposition = await capture_service.schedule_delivery_retry(
                capture_id=capture_id,
                delivery_attempt=request.delivery_attempt,
                error_type=request.error_type,
                reason_type=request.reason_type,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="stale delivery attempt") from exc
        capture = _get_capture(capture_service, capture_id)
        return DeliveryTransitionResponse(
            capture_id=capture_id,
            delivery_status=capture.delivery_status,
            delivery_attempts=capture.delivery_attempts,
            changed=True,
            outcome="retry_scheduled" if disposition.retry_scheduled else "terminal_failure",
        )

    @app.post(
        "/internal/captures/{capture_id}/delivery/acknowledge-failed",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def acknowledge_delivery_failed(capture_id: str, request: AcknowledgeDeliveryFailedRequest):
        _get_capture(capture_service, capture_id)
        changed = await capture_service.acknowledge_delivery_failed(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
            reason_type=request.reason_type,
        )
        return _delivery_response(capture_service, capture_id, changed=changed,
                                  outcome="changed" if changed else "stale_attempt")

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
        delivery_status=capture.delivery_status,
        delivery_attempts=capture.delivery_attempts,
        processing_lease_until=capture.processing_lease_until,
        next_attempt_at=capture.next_attempt_at,
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


def _delivery_response(
    capture_service: CaptureService,
    capture_id: str,
    *,
    changed: bool,
    outcome: str,
    ignored_reason: str | None = None,
) -> DeliveryTransitionResponse:
    capture = _get_capture(capture_service, capture_id)
    return DeliveryTransitionResponse(
        capture_id=capture_id,
        delivery_status=capture.delivery_status,
        delivery_attempts=capture.delivery_attempts,
        changed=changed,
        outcome=outcome,
        ignored_reason=ignored_reason,
    )


def _delivery_mutation_response(
    capture_service: CaptureService,
    capture_id: str,
    result: DeliveryMutationResult,
) -> DeliveryTransitionResponse:
    if result.outcome == "conflicting_replay":
        raise HTTPException(status_code=409, detail="conflicting terminal callback")
    if result.outcome == "invalid_state":
        raise HTTPException(status_code=409, detail="capture not in valid state for terminal callback")
    capture = _get_capture(capture_service, capture_id)
    return DeliveryTransitionResponse(
        capture_id=capture_id,
        delivery_status=capture.delivery_status,
        delivery_attempts=capture.delivery_attempts,
        changed=result.changed,
        outcome=result.outcome,
        ignored_reason="stale_attempt" if result.outcome == "stale_attempt" else None,
    )


def _transition_response(transition: TransitionResult) -> TransitionResponse:
    return TransitionResponse(
        capture_id=transition.capture_id,
        previous_status=transition.previous_status,
        status=transition.status,
        changed=transition.changed,
    )
