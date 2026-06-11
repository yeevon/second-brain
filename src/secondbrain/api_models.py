from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from secondbrain.models import Classification


_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


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


class AcknowledgeForwardedRequest(BaseModel):
    delivery_attempt: int = Field(ge=1)


class AcknowledgeClassifyingRequest(BaseModel):
    delivery_attempt: int = Field(ge=1)


class RenewLeaseRequest(BaseModel):
    delivery_attempt: int = Field(ge=1)


class AcknowledgeFiledRequest(BaseModel):
    delivery_attempt: int = Field(ge=1)
    note_path: str = Field(min_length=1, max_length=1000)
    git_commit_hash: str | None = Field(default=None, max_length=100)


class AcknowledgeInboxRequest(BaseModel):
    delivery_attempt: int = Field(ge=1)
    note_path: str = Field(min_length=1, max_length=1000)
    git_commit_hash: str | None = Field(default=None, max_length=100)
    reason_type: str = Field(default="", max_length=100)

    @field_validator("reason_type")
    @classmethod
    def validate_reason_type(cls, v: str) -> str:
        if v:
            return _validate_safe_slug(v)
        return v


class ScheduleRetryRequest(BaseModel):
    delivery_attempt: int = Field(ge=1)
    error_type: str = Field(min_length=1, max_length=100)
    reason_type: str = Field(default="webhook_failure", max_length=100)

    @field_validator("error_type", "reason_type")
    @classmethod
    def validate_safe_slugs(cls, v: str) -> str:
        return _validate_safe_slug(v)


class AcknowledgeDeliveryFailedRequest(BaseModel):
    delivery_attempt: int = Field(ge=1)
    reason_type: str = Field(default="", max_length=100)

    @field_validator("reason_type")
    @classmethod
    def validate_reason_type(cls, v: str) -> str:
        if v:
            return _validate_safe_slug(v)
        return v


class EditReceiptRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1900)


class ReceiptDeliveryResponse(BaseModel):
    capture_id: str
    delivered: bool
    replaced: bool
    receipt_message_id: str | None
