from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from secondbrain.models import Classification


_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


class StrictInternalRequest(BaseModel):
    """Base class for all n8n-facing callback request bodies. Rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


def _validate_safe_slug(value: str) -> str:
    if not _SAFE_SLUG_RE.match(value):
        raise ValueError(
            "value must match ^[A-Za-z0-9_.:-]{1,100}$ "
            "(safe category identifiers only, no free-form messages)"
        )
    return value


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: Literal["capture-service"]


class CaptureResponse(BaseModel):
    capture_id: str
    discord_message_id: str
    discord_channel_id: str
    discord_guild_id: str
    discord_author_id: str
    status: str
    delivery_status: str
    delivery_attempts: int
    retry_attempts: int
    processing_lease_until: datetime | None
    next_attempt_at: datetime | None
    raw_text: str | None
    redacted_text: str | None
    is_sensitive: bool
    has_attachments: bool
    attachment_metadata: list[dict[str, Any]]
    received_at: datetime
    receipt_message_id: str | None
    derived_note_path: str | None
    last_error: str | None


class TransitionResponse(BaseModel):
    capture_id: str
    previous_status: str
    status: str
    changed: bool


class DeliveryTransitionResponse(BaseModel):
    capture_id: str
    delivery_status: str
    delivery_attempts: int
    retry_attempts: int
    changed: bool
    outcome: str
    ignored_reason: str | None = None


class MarkFiledRequest(BaseModel):
    note_path: str = Field(min_length=1, max_length=1000)
    classification: Classification


class MarkInboxRequest(BaseModel):
    note_path: str = Field(min_length=1, max_length=1000)
    classification: Classification
    reason: str | None = Field(default=None, max_length=500)


class MarkFailedRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AcknowledgeForwardedRequest(StrictInternalRequest):
    delivery_attempt: int = Field(ge=1)


class AcknowledgeClassifyingRequest(StrictInternalRequest):
    delivery_attempt: int = Field(ge=1)


class RenewLeaseRequest(StrictInternalRequest):
    delivery_attempt: int = Field(ge=1)


class AcknowledgeFiledRequest(StrictInternalRequest):
    delivery_attempt: int = Field(ge=1)
    note_path: str = Field(min_length=1, max_length=1000)
    git_commit_hash: str | None = Field(default=None, max_length=100)
    classification: dict | None = Field(default=None)


class AcknowledgeInboxRequest(StrictInternalRequest):
    delivery_attempt: int = Field(ge=1)
    note_path: str = Field(min_length=1, max_length=1000)
    git_commit_hash: str | None = Field(default=None, max_length=100)
    reason_type: str = Field(default="", max_length=100)
    classification: dict | None = Field(default=None)

    @field_validator("reason_type")
    @classmethod
    def validate_reason_type(cls, v: str) -> str:
        if v:
            return _validate_safe_slug(v)
        return v


class ScheduleRetryRequest(StrictInternalRequest):
    delivery_attempt: int = Field(ge=1)
    error_type: str = Field(min_length=1, max_length=100)
    reason_type: str = Field(default="webhook_failure", max_length=100)

    @field_validator("error_type", "reason_type")
    @classmethod
    def validate_safe_slugs(cls, v: str) -> str:
        return _validate_safe_slug(v)


class AcknowledgeDeliveryFailedRequest(StrictInternalRequest):
    delivery_attempt: int = Field(ge=1)
    reason_type: str = Field(default="", max_length=100)

    @field_validator("reason_type")
    @classmethod
    def validate_reason_type(cls, v: str) -> str:
        if v:
            return _validate_safe_slug(v)
        return v


class ReportWorkflowErrorRequest(StrictInternalRequest):
    delivery_attempt: int = Field(ge=1)
    disposition: Literal["retryable", "terminal"]
    error_type: str = Field(min_length=1, max_length=100)
    reason_type: str = Field(default="workflow_error", max_length=100)
    workflow_id: str = Field(min_length=1, max_length=100)
    workflow_name: str = Field(min_length=1, max_length=100)
    execution_id: str | None = Field(default=None, max_length=100)
    stage: str = Field(min_length=1, max_length=100)

    @field_validator("error_type", "reason_type", "workflow_id", "workflow_name", "stage")
    @classmethod
    def validate_safe_slug_fields(cls, v: str) -> str:
        return _validate_safe_slug(v)

    @field_validator("execution_id")
    @classmethod
    def validate_execution_id(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_safe_slug(v)
        return v

    @model_validator(mode="after")
    def validate_disposition_and_stage(self) -> "ReportWorkflowErrorRequest":
        from secondbrain.downstream_errors import (
            ALLOWED_STAGES,
            RETRYABLE_DOWNSTREAM_ERRORS,
            TERMINAL_DOWNSTREAM_ERRORS,
        )
        if self.disposition == "retryable" and self.error_type not in RETRYABLE_DOWNSTREAM_ERRORS:
            raise ValueError(
                f"error_type {self.error_type!r} is not a known retryable error"
            )
        if self.disposition == "terminal" and self.error_type not in TERMINAL_DOWNSTREAM_ERRORS:
            raise ValueError(
                f"error_type {self.error_type!r} is not a known terminal error"
            )
        if self.stage not in ALLOWED_STAGES:
            raise ValueError(f"stage {self.stage!r} is not an allowlisted stage")
        return self


class WorkflowErrorResponse(BaseModel):
    capture_id: str
    delivery_attempt: int
    delivery_status: str
    retry_attempts: int
    outcome: str


class DownstreamCaptureResponse(BaseModel):
    """Minimal capture envelope exposed to n8n — no raw secrets, no audit fields."""

    capture_id: str
    raw_text: str | None  # null when is_sensitive=True
    is_sensitive: bool
    has_attachments: bool
    delivery_attempt: int
    status: str
    delivery_status: str
    source_message_id: str
    created_at: datetime


class SecurityScreenRequest(StrictInternalRequest):
    text: str = Field(min_length=1, max_length=10000)


class SecurityScreenResponse(BaseModel):
    is_sensitive: bool
    safe_category_list: list[str]


class ClassificationValidationRequest(StrictInternalRequest):
    classification: dict  # raw dict from Gemini — validated by the endpoint
    delivery_attempt: int = Field(ge=1)


class ClassificationValidationResponse(BaseModel):
    valid: bool
    route: Literal["file", "inbox"] | None
    confidence_met: bool
    inbox_reason: str | None = None
    errors: list[str]


class EditReceiptRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1900)


class ReceiptDeliveryResponse(BaseModel):
    capture_id: str
    delivered: bool
    replaced: bool
    receipt_message_id: str | None


class ClarificationRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class ClarificationResponse(BaseModel):
    capture_id: str
    clarification_status: str
    question_sent: bool


class CorrectionRequest(BaseModel):
    new_folder: str = Field(min_length=1, max_length=50)
    correction_reason: str = Field(min_length=1, max_length=500)
    receipt_message_id: str | None = Field(default=None)


class CorrectionResponse(BaseModel):
    capture_id: str
    correction_id: str
    old_note_path: str
    new_note_path: str
    git_commit_hash: str | None


# ── Brief response models (SB-120 / SB-121 redesign) ─────────────────────────


class BriefActionItem(BaseModel):
    title: str
    project: str | None
    source: str
    due: str | None
    priority: str | None
    note_path: str


class BriefDateItem(BaseModel):
    title: str
    date: str
    source: str
    note_path: str


class BriefBirthdayItem(BaseModel):
    name: str
    date: str
    note_path: str


class DailyBriefResponse(BaseModel):
    generated_at: datetime
    today: str
    focus_items: list[BriefActionItem]
    due_today: list[BriefActionItem]
    coming_up: list[BriefDateItem]
    birthdays: list[BriefBirthdayItem]
    pending_tasks: list[BriefActionItem]
    stale_tasks: list[BriefActionItem]


class WeeklyAccomplishedItem(BaseModel):
    title: str
    source: str
    project: str | None
    note_path: str


class WeeklyCompletedTask(BaseModel):
    title: str
    project: str | None
    note_path: str


class WeeklyDecision(BaseModel):
    title: str
    project: str | None
    note_path: str


class WeeklyOpenItem(BaseModel):
    title: str
    project: str | None
    due: str | None
    priority: str | None
    note_path: str


class WeeklyStudyProgress(BaseModel):
    track: str
    status: str
    note_path: str


class WeeklyBriefResponse(BaseModel):
    generated_at: datetime
    week_start: str
    week_end: str
    accomplished: list[WeeklyAccomplishedItem]
    completed_tasks: list[WeeklyCompletedTask]
    decisions: list[WeeklyDecision]
    still_open: list[WeeklyOpenItem]
    study_progress: list[WeeklyStudyProgress]


class DailyDigestResponse(BaseModel):
    generated_at: datetime
    window_hours: int
    new_captures_count: int
    filed_notes_count: int
    inbox_backlog_count: int
    awaiting_clarification_count: int
    open_tasks_count: int | None
    open_tasks_by_project: dict[str, int] | None
    failed_captures_count: int
    retry_events_count: int
    sensitive_rejections_count: int
    attachment_warnings_count: int


class WeeklyDigestResponse(BaseModel):
    generated_at: datetime
    since: datetime
    window_days: int
    new_captures_count: int
    filed_notes_count: int
    created_tasks_count: int
    completed_actions_count: int
    decisions_count: int
    outstanding_tasks_count: int | None
    inbox_backlog_count: int
    corrections_count: int
    failures_count: int
    retries_count: int
    sensitive_rejections_count: int


# ── Vault update proposal models (SB-136) ─────────────────────────────────────


class CreateProposalRequest(BaseModel):
    source: str = Field(min_length=1, max_length=100)
    requested_by: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=100)
    target_note_path: str = Field(min_length=1, max_length=1000)
    target_anchor_json: str | None = Field(default=None, max_length=2000)
    change_json: str = Field(min_length=2, max_length=10000)
    reason: str | None = Field(default=None, max_length=500)
    requires_approval: bool = True

    @field_validator("change_json")
    @classmethod
    def validate_change_json(cls, v: str) -> str:
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"change_json must be valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("change_json must be a JSON object, not an array or scalar")
        return v

    @field_validator("target_anchor_json")
    @classmethod
    def validate_target_anchor_json(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"target_anchor_json must be valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("target_anchor_json must be a JSON object, not an array or scalar")
        return v


class ProposalResponse(BaseModel):
    proposal_id: str
    source: str
    requested_by: str
    operation: str
    target_note_path: str
    target_anchor_json: str | None
    change_json: str
    reason: str | None
    status: str
    requires_approval: bool
    submitted_at: datetime
    reviewed_at: datetime | None
    reviewed_by: str | None
    applied_at: datetime | None
    rejected_reason: str | None
    git_commit_hash: str | None
    last_error: str | None
    approval_message_id: str | None


class UpdateProposalStatusRequest(BaseModel):
    status: str = Field(min_length=1, max_length=50)
    reviewed_by: str | None = Field(default=None, max_length=200)
    reviewed_at: datetime | None = None
    rejected_reason: str | None = Field(default=None, max_length=500)
    applied_at: datetime | None = None
    git_commit_hash: str | None = Field(default=None, max_length=100)
    last_error: str | None = Field(default=None, max_length=500)
    approval_message_id: str | None = Field(default=None, max_length=100)
