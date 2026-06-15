from __future__ import annotations

from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Header

from secondbrain.api_models import (
    AcknowledgeClassifyingRequest,
    AcknowledgeDeliveryFailedRequest,
    AcknowledgeFiledRequest,
    AcknowledgeForwardedRequest,
    AcknowledgeInboxRequest,
    CaptureResponse,
    ClarificationRequest,
    ClarificationResponse,
    ClassificationValidationRequest,
    ClassificationValidationResponse,
    CorrectionRequest,
    CorrectionResponse,
    DeliveryTransitionResponse,
    DownstreamCaptureResponse,
    EditReceiptRequest,
    HealthResponse,
    ReceiptDeliveryResponse,
    RenewLeaseRequest,
    ReportWorkflowErrorRequest,
    ScheduleRetryRequest,
    SecurityScreenRequest,
    SecurityScreenResponse,
    WorkflowErrorResponse,
)
from secondbrain.capture_service import (
    CaptureNotFoundError,
    CaptureService,
    ReceiptDeliveryError,
)
from secondbrain.capture_models import CaptureRecord, DeliveryMutationResult


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

    # ------------------------------------------------------------------
    # Minimal downstream capture envelope (n8n fetch)
    # Returns only the 7 fields n8n needs — never exposes audit fields.
    # ------------------------------------------------------------------

    @app.get(
        "/internal/downstream/captures/{capture_id}",
        response_model=DownstreamCaptureResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def get_downstream_capture(capture_id: str):
        capture = _get_capture(capture_service, capture_id)
        return DownstreamCaptureResponse(
            capture_id=capture.capture_id,
            raw_text=None if capture.is_sensitive else capture.raw_text,
            is_sensitive=capture.is_sensitive,
            has_attachments=capture.has_attachments,
            delivery_attempt=capture.delivery_attempts,
            status=capture.status,
            delivery_status=capture.delivery_status,
            source_message_id=capture.discord_message_id,
            created_at=capture.received_at,
        )

    # ------------------------------------------------------------------
    # Security screen (defence-in-depth re-screen by n8n before filing)
    # ------------------------------------------------------------------

    @app.post(
        "/internal/security/screen",
        response_model=SecurityScreenResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def security_screen(request: SecurityScreenRequest):
        from secondbrain.secret_screen import screen_text
        result = screen_text(request.text)
        return SecurityScreenResponse(
            is_sensitive=result.is_sensitive,
            safe_category_list=list(result.flags),
        )

    # ------------------------------------------------------------------
    # Classification contract validation
    # Validates Gemini output against our schema and applies the
    # confidence threshold to return an authoritative routing decision.
    # ------------------------------------------------------------------

    @app.post(
        "/internal/contracts/classification/validate",
        response_model=ClassificationValidationResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def validate_classification(request: ClassificationValidationRequest):
        from secondbrain.models import Classification
        from pydantic import ValidationError

        errors: list[str] = []
        route: str | None = None
        inbox_reason: str | None = None
        valid = False
        confidence_met = False

        try:
            classification = Classification.model_validate(request.classification)
            valid = True
            threshold = capture_service.settings.classification_confidence_threshold
            if threshold is None:
                threshold = 0.75
            confidence_met = classification.confidence >= threshold
            if classification.folder == "inbox":
                route = "inbox"
                inbox_reason = "classifier_selected_inbox"
            elif classification.needs_clarification:
                route = "inbox"
                inbox_reason = "needs_clarification"
            elif not confidence_met:
                route = "inbox"
                inbox_reason = "low_confidence"
            else:
                route = "file"
        except ValidationError as exc:
            for err in exc.errors():
                loc = " -> ".join(str(p) for p in err["loc"])
                errors.append(f"{loc}: {err['msg']}")

        return ClassificationValidationResponse(
            valid=valid,
            route=route,
            confidence_met=confidence_met,
            inbox_reason=inbox_reason,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Attempt-aware downstream delivery callback routes
    # ------------------------------------------------------------------

    @app.post(
        "/internal/captures/{capture_id}/delivery/acknowledge-forwarded",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def acknowledge_delivery_forwarded(capture_id: str, request: AcknowledgeForwardedRequest):
        _get_capture(capture_service, capture_id)
        result = capture_service.acknowledge_delivery_forwarded(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
        )
        # Atomic winner gate: HTTP 200 for all 4 outcomes
        return _acknowledge_forwarded_response(capture_service, capture_id, result)

    @app.post(
        "/internal/captures/{capture_id}/delivery/acknowledge-classifying",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def acknowledge_delivery_classifying(capture_id: str, request: AcknowledgeClassifyingRequest):
        _get_capture(capture_service, capture_id)
        result = capture_service.acknowledge_delivery_classifying(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
        )
        return _non_terminal_delivery_response(capture_service, capture_id, result)

    @app.post(
        "/internal/captures/{capture_id}/delivery/renew-lease",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def renew_delivery_lease(capture_id: str, request: RenewLeaseRequest):
        _get_capture(capture_service, capture_id)
        result = capture_service.renew_delivery_lease(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
        )
        return _non_terminal_delivery_response(capture_service, capture_id, result)

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
        disposition = await capture_service.schedule_delivery_retry(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
            error_type=request.error_type,
            reason_type=request.reason_type,
        )
        capture = _get_capture(capture_service, capture_id)
        outcome = disposition.outcome or (
            "retry_scheduled" if disposition.retry_scheduled else "terminal_failure"
        )
        return DeliveryTransitionResponse(
            capture_id=capture_id,
            delivery_status=capture.delivery_status,
            delivery_attempts=capture.delivery_attempts,
            retry_attempts=capture.retry_attempts,
            changed=not outcome.startswith("ignored_"),
            outcome=outcome,
        )

    @app.post(
        "/internal/captures/{capture_id}/delivery/acknowledge-failed",
        response_model=DeliveryTransitionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def acknowledge_delivery_failed(capture_id: str, request: AcknowledgeDeliveryFailedRequest):
        _get_capture(capture_service, capture_id)
        result = await capture_service.acknowledge_delivery_failed(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
            reason_type=request.reason_type,
        )
        return _acknowledge_failed_response(capture_service, capture_id, result)

    @app.post(
        "/internal/captures/{capture_id}/delivery/report-workflow-error",
        response_model=WorkflowErrorResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def report_workflow_error(capture_id: str, request: ReportWorkflowErrorRequest):
        _get_capture(capture_service, capture_id)
        outcome = await capture_service.report_workflow_error(
            capture_id=capture_id,
            delivery_attempt=request.delivery_attempt,
            disposition=request.disposition,
            error_type=request.error_type,
            reason_type=request.reason_type,
            workflow_id=request.workflow_id,
            workflow_name=request.workflow_name,
            execution_id=request.execution_id,
            stage=request.stage,
        )
        return WorkflowErrorResponse(
            capture_id=outcome.capture_id,
            delivery_attempt=outcome.delivery_attempt,
            delivery_status=outcome.delivery_status,
            retry_attempts=outcome.retry_attempts,
            outcome=outcome.outcome,
        )

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

    # ------------------------------------------------------------------
    # SB-117: Clarification handling
    # ------------------------------------------------------------------

    @app.post(
        "/internal/clarifications/{capture_id}",
        response_model=ClarificationResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def record_clarification(capture_id: str, request: ClarificationRequest):
        _get_capture(capture_service, capture_id)
        recorded = await capture_service.record_clarification(
            capture_id=capture_id,
            question=request.question,
        )
        if not recorded:
            raise HTTPException(
                status_code=409,
                detail="capture is not in INBOX status; clarification cannot be recorded",
            )
        capture = _get_capture(capture_service, capture_id)
        return ClarificationResponse(
            capture_id=capture_id,
            clarification_status=capture.clarification_status or "NEEDS_CLARIFICATION",
            question_sent=True,
        )

    # ------------------------------------------------------------------
    # SB-118: Correction handling
    # ------------------------------------------------------------------

    @app.post(
        "/internal/captures/{capture_id}/corrections",
        response_model=CorrectionResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def apply_correction(capture_id: str, request: CorrectionRequest):
        capture = _get_capture(capture_service, capture_id)
        result = await capture_service.apply_correction(
            capture_id=capture_id,
            new_folder=request.new_folder,
            correction_reason=request.correction_reason,
        )
        if result is None:
            raise HTTPException(
                status_code=409,
                detail="correction could not be applied; capture may not have a filed note",
            )
        return CorrectionResponse(
            capture_id=capture_id,
            correction_id=result["correction_id"],
            old_note_path=result["old_note_path"],
            new_note_path=result["new_note_path"],
            git_commit_hash=result.get("git_commit_hash"),
        )

    return app


def _get_capture(capture_service: CaptureService, capture_id: str) -> CaptureRecord:
    try:
        return capture_service.get_capture(capture_id)
    except CaptureNotFoundError as exc:
        raise HTTPException(status_code=404, detail="capture not found") from exc


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
        retry_attempts=capture.retry_attempts,
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


def _acknowledge_forwarded_response(
    capture_service: CaptureService,
    capture_id: str,
    result: DeliveryMutationResult,
) -> DeliveryTransitionResponse:
    """Atomic winner gate: HTTP 200 for all 4 outcomes."""
    capture = _get_capture(capture_service, capture_id)
    return DeliveryTransitionResponse(
        capture_id=capture_id,
        delivery_status=capture.delivery_status,
        delivery_attempts=capture.delivery_attempts,
        retry_attempts=capture.retry_attempts,
        changed=result.changed,
        outcome=result.outcome,
        ignored_reason=result.outcome if result.outcome != "changed" else None,
    )


def _non_terminal_delivery_response(
    capture_service: CaptureService,
    capture_id: str,
    result: DeliveryMutationResult,
) -> DeliveryTransitionResponse:
    if result.outcome == "invalid_state":
        raise HTTPException(status_code=409, detail="capture not in valid state for this callback")
    capture = _get_capture(capture_service, capture_id)
    return DeliveryTransitionResponse(
        capture_id=capture_id,
        delivery_status=capture.delivery_status,
        delivery_attempts=capture.delivery_attempts,
        retry_attempts=capture.retry_attempts,
        changed=result.changed,
        outcome=result.outcome,
        ignored_reason="stale_attempt" if result.outcome == "stale_attempt" else None,
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
        retry_attempts=capture.retry_attempts,
        changed=result.changed,
        outcome=result.outcome,
        ignored_reason="stale_attempt" if result.outcome == "stale_attempt" else None,
    )


def _acknowledge_failed_response(
    capture_service: CaptureService,
    capture_id: str,
    result: DeliveryMutationResult,
) -> DeliveryTransitionResponse:
    """HTTP 409 only for conflicting_replay; HTTP 200 for everything else."""
    if result.outcome == "conflicting_replay":
        raise HTTPException(status_code=409, detail="conflicting terminal failure reason")
    capture = _get_capture(capture_service, capture_id)
    return DeliveryTransitionResponse(
        capture_id=capture_id,
        delivery_status=capture.delivery_status,
        delivery_attempts=capture.delivery_attempts,
        retry_attempts=capture.retry_attempts,
        changed=result.changed,
        outcome=result.outcome,
        ignored_reason=result.outcome if result.outcome != "changed" else None,
    )
