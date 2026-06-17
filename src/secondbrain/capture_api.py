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
    CreateProposalRequest,
    DailyBriefResponse,
    DailyDigestResponse,
    DeliveryTransitionResponse,
    DownstreamCaptureResponse,
    EditReceiptRequest,
    HealthResponse,
    ProposalResponse,
    ReceiptDeliveryResponse,
    RenewLeaseRequest,
    ReportWorkflowErrorRequest,
    ScheduleRetryRequest,
    SecurityScreenRequest,
    SecurityScreenResponse,
    UpdateProposalStatusRequest,
    WeeklyBriefResponse,
    WeeklyDigestResponse,
    WorkflowErrorResponse,
)
from secondbrain.capture_service import (
    CaptureNotFoundError,
    CaptureService,
    ReceiptDeliveryError,
)
from secondbrain.capture_models import (
    ALLOWED_PROPOSAL_OPERATIONS,
    ALL_PROPOSAL_STATUSES,
    CaptureRecord,
    DeliveryMutationResult,
)


INTERNAL_TOKEN_HEADER = "X-Second-Brain-Internal-Token"


def _fetch_open_task_count(capture_service: CaptureService) -> int | None:
    """Return open task count from writer-service or direct vault scan.

    Priority order:
    1. Call writer-service GET /internal/vault/stats/open-tasks if writer_service_url is set.
    2. Fall back to direct vault scan if vault_path is set (local-full mode).
    3. Return None if neither is available (capture-only mode with no vault access).
    """
    import urllib.request
    from secondbrain.digest import scan_open_tasks

    writer_url = getattr(capture_service.settings, "writer_service_url", None)
    writer_token = getattr(capture_service.settings, "writer_service_token", None)
    if writer_url and writer_token:
        try:
            req = urllib.request.Request(
                f"{writer_url}/internal/vault/stats/open-tasks",
                headers={"X-Second-Brain-Writer-Token": writer_token},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                import json
                data = json.loads(resp.read())
                return data.get("open_tasks_count")
        except Exception:
            pass  # fall through to direct scan

    vault_path = getattr(capture_service.settings, "vault_path", None)
    if vault_path is not None:
        try:
            return scan_open_tasks(vault_path)
        except Exception:
            pass
    return None


def _fetch_open_tasks_by_project(capture_service: CaptureService) -> dict[str, int] | None:
    """Return open task counts grouped by project.

    Priority order:
    1. Call writer-service GET /internal/vault/stats/open-tasks (returns both count + by_project)
       if writer_service_url and writer_service_token are configured.
    2. Fall back to direct vault scan if vault_path is set (local-full mode).
    3. Return None if neither is available (capture-only mode with no vault access).
    """
    import urllib.request
    from secondbrain.digest import scan_open_tasks_by_project

    writer_url = getattr(capture_service.settings, "writer_service_url", None)
    writer_token = getattr(capture_service.settings, "writer_service_token", None)
    if writer_url and writer_token:
        try:
            req = urllib.request.Request(
                f"{writer_url}/internal/vault/stats/open-tasks",
                headers={"X-Second-Brain-Writer-Token": writer_token},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                import json
                data = json.loads(resp.read())
                by_project = data.get("open_tasks_by_project")
                if isinstance(by_project, dict):
                    return by_project
        except Exception:
            pass  # fall through to direct scan

    vault_path = getattr(capture_service.settings, "vault_path", None)
    if vault_path is not None:
        try:
            return scan_open_tasks_by_project(vault_path)
        except Exception:
            pass
    return None


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
            classification_json=request.classification,
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
            classification_json=request.classification,
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

    # ------------------------------------------------------------------
    # SB-120 / SB-121: Digest endpoints
    # ------------------------------------------------------------------

    @app.get(
        "/internal/digest/daily",
        response_model=DailyDigestResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def get_daily_digest():
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        since = now - timedelta(hours=24)
        snapshot = capture_service.daily_digest_snapshot(since=since, now=now)

        return DailyDigestResponse(
            generated_at=now,
            window_hours=24,
            open_tasks_count=_fetch_open_task_count(capture_service),
            open_tasks_by_project=_fetch_open_tasks_by_project(capture_service),
            **snapshot,
        )

    @app.get(
        "/internal/digest/weekly",
        response_model=WeeklyDigestResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def get_weekly_digest():
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        since = now - timedelta(days=7)
        snapshot = capture_service.weekly_digest_snapshot(since=since, now=now)

        return WeeklyDigestResponse(
            generated_at=now,
            since=since,
            window_days=7,
            outstanding_tasks_count=_fetch_open_task_count(capture_service),
            **snapshot,
        )

    # ------------------------------------------------------------------
    # SB-120 / SB-121: Actionable brief endpoints
    # Replace count-based digest with vault-scanned brief data.
    # ------------------------------------------------------------------

    @app.get(
        "/internal/brief/daily",
        response_model=DailyBriefResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def get_daily_brief():
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        data = _fetch_brief(capture_service, "daily")
        return DailyBriefResponse(
            generated_at=now,
            today=data.get("today", now.date().isoformat()),
            focus_items=data.get("focus_items", []),
            due_today=data.get("due_today", []),
            coming_up=data.get("coming_up", []),
            birthdays=data.get("birthdays", []),
            pending_tasks=data.get("pending_tasks", []),
            stale_tasks=data.get("stale_tasks", []),
        )

    @app.get(
        "/internal/brief/weekly",
        response_model=WeeklyBriefResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def get_weekly_brief():
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        today = now.date()
        data = _fetch_brief(capture_service, "weekly")
        return WeeklyBriefResponse(
            generated_at=now,
            week_start=data.get("week_start", (today - timedelta(days=7)).isoformat()),
            week_end=data.get("week_end", today.isoformat()),
            accomplished=data.get("accomplished", []),
            completed_tasks=data.get("completed_tasks", []),
            decisions=data.get("decisions", []),
            still_open=data.get("still_open", []),
            study_progress=data.get("study_progress", []),
        )

    # ------------------------------------------------------------------
    # SB-136: Vault update proposal routes
    # ------------------------------------------------------------------

    @app.post(
        "/internal/vault/proposals",
        response_model=ProposalResponse,
        status_code=201,
        dependencies=[Depends(require_internal_token)],
    )
    async def create_proposal(request: CreateProposalRequest):
        if request.operation not in ALLOWED_PROPOSAL_OPERATIONS:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported operation: {request.operation!r}; "
                       f"allowed: {sorted(ALLOWED_PROPOSAL_OPERATIONS)}",
            )
        _validate_proposal_path(request.target_note_path)
        try:
            proposal = capture_service.create_proposal(
                source=request.source,
                requested_by=request.requested_by,
                operation=request.operation,
                target_note_path=request.target_note_path,
                target_anchor_json=request.target_anchor_json,
                change_json=request.change_json,
                reason=request.reason,
                requires_approval=request.requires_approval,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail="failed to create proposal") from exc

        if proposal.requires_approval:
            channel_id = getattr(capture_service.settings, "discord_capture_channel_id", None)
            if channel_id:
                msg_id = await capture_service.post_proposal_approval_message(
                    proposal, int(channel_id)
                )
                if msg_id:
                    proposal = capture_service.update_proposal(
                        proposal.proposal_id, approval_message_id=msg_id
                    )

        return _proposal_response(proposal)

    @app.get(
        "/internal/vault/proposals/{proposal_id}",
        response_model=ProposalResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def get_proposal(proposal_id: str):
        try:
            proposal = capture_service.get_proposal(proposal_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="proposal not found")
        return _proposal_response(proposal)

    @app.get(
        "/internal/vault/proposals",
        response_model=list[ProposalResponse],
        dependencies=[Depends(require_internal_token)],
    )
    async def list_proposals(status: str | None = None):
        if status is not None and status not in ALL_PROPOSAL_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown proposal status: {status!r}",
            )
        proposals = capture_service.list_proposals(status=status)
        return [_proposal_response(p) for p in proposals]

    @app.patch(
        "/internal/vault/proposals/{proposal_id}",
        response_model=ProposalResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def update_proposal(proposal_id: str, request: UpdateProposalStatusRequest):
        if request.status not in ALL_PROPOSAL_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown proposal status: {request.status!r}",
            )
        try:
            proposal = capture_service.update_proposal(
                proposal_id,
                status=request.status,
                reviewed_at=request.reviewed_at,
                reviewed_by=request.reviewed_by,
                applied_at=request.applied_at,
                rejected_reason=request.rejected_reason,
                git_commit_hash=request.git_commit_hash,
                last_error=request.last_error,
                approval_message_id=request.approval_message_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="proposal not found")
        return _proposal_response(proposal)

    return app


def _resolve_vault_path(capture_service: CaptureService):
    """Return a Path to the vault, or None if not accessible from this service instance."""
    from pathlib import Path
    vault_path_setting = getattr(capture_service.settings, "vault_path", None)
    if vault_path_setting is not None:
        return Path(vault_path_setting)
    return None


def _fetch_brief(capture_service: CaptureService, period: str) -> dict:
    """Return brief data for period ('daily' or 'weekly').

    Priority order:
    1. Call writer-service GET /internal/vault/brief/{period} if configured.
    2. Fall back to direct vault scan if vault_path is set (local-full mode).
    3. Return empty structure if neither is available (capture-only mode).
    """
    import json
    import urllib.request
    from secondbrain.digest import scan_daily_brief, scan_weekly_brief
    from datetime import date, timedelta

    writer_url = getattr(capture_service.settings, "writer_service_url", None)
    writer_token = getattr(capture_service.settings, "writer_service_token", None)
    if writer_url and writer_token:
        try:
            req = urllib.request.Request(
                f"{writer_url}/internal/vault/brief/{period}",
                headers={"X-Second-Brain-Writer-Token": writer_token},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception:
            pass

    vault_path = _resolve_vault_path(capture_service)
    if vault_path is not None:
        try:
            if period == "daily":
                return scan_daily_brief(vault_path)
            today = date.today()
            return scan_weekly_brief(vault_path, week_start=today - timedelta(days=7), week_end=today)
        except Exception:
            pass

    today = date.today()
    if period == "daily":
        return {"today": today.isoformat(), "focus_items": [], "due_today": [], "coming_up": [], "birthdays": [], "pending_tasks": [], "stale_tasks": []}
    return {"week_start": (today - timedelta(days=7)).isoformat(), "week_end": today.isoformat(), "accomplished": [], "completed_tasks": [], "decisions": [], "still_open": [], "study_progress": []}


def _get_capture(capture_service: CaptureService, capture_id: str) -> CaptureRecord:
    try:
        return capture_service.get_capture(capture_id)
    except CaptureNotFoundError as exc:
        raise HTTPException(status_code=404, detail="capture not found") from exc


def _validate_proposal_path(target_note_path: str) -> None:
    """Reject paths that could escape the vault root."""
    import posixpath
    if ".." in target_note_path.replace("\\", "/").split("/"):
        raise HTTPException(status_code=422, detail="path traversal detected in target_note_path")
    if target_note_path.startswith("/"):
        raise HTTPException(status_code=422, detail="absolute paths are not allowed in target_note_path")
    normalized = posixpath.normpath(target_note_path)
    if normalized.startswith("..") or normalized.startswith("/."):
        raise HTTPException(status_code=422, detail="path traversal detected in target_note_path")
    parts = target_note_path.replace("\\", "/").split("/")
    if any(part.startswith(".") for part in parts if part):
        raise HTTPException(status_code=422, detail="hidden paths are not allowed in target_note_path")


def _proposal_response(proposal) -> ProposalResponse:
    return ProposalResponse(
        proposal_id=proposal.proposal_id,
        source=proposal.source,
        requested_by=proposal.requested_by,
        operation=proposal.operation,
        target_note_path=proposal.target_note_path,
        target_anchor_json=proposal.target_anchor_json,
        change_json=proposal.change_json,
        reason=proposal.reason,
        status=proposal.status,
        requires_approval=proposal.requires_approval,
        submitted_at=proposal.submitted_at,
        reviewed_at=proposal.reviewed_at,
        reviewed_by=proposal.reviewed_by,
        applied_at=proposal.applied_at,
        rejected_reason=proposal.rejected_reason,
        git_commit_hash=proposal.git_commit_hash,
        last_error=proposal.last_error,
        approval_message_id=proposal.approval_message_id,
    )


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


