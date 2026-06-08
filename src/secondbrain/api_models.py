from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from secondbrain.models import Classification


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


class MarkFiledRequest(BaseModel):
    note_path: str = Field(min_length=1, max_length=1000)
    classification: Classification


class MarkInboxRequest(BaseModel):
    note_path: str = Field(min_length=1, max_length=1000)
    classification: Classification
    reason: str | None = Field(default=None, max_length=500)


class MarkFailedRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class EditReceiptRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1900)


class ReceiptDeliveryResponse(BaseModel):
    capture_id: str
    delivered: bool
    replaced: bool
    receipt_message_id: str | None
