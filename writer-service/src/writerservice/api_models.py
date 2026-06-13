from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


AllowedFolder = Literal["people", "projects", "ideas", "learning", "admin", "inbox"]
ActionStatus = Literal["open", "done"]

_CAPTURE_ID_RE = re.compile(r"^SB-\d{8}-\d{4}$")


class ClassifiedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    status: ActionStatus


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder: AllowedFolder
    project: str | None
    note_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    tags: list[str]
    body: str = Field(min_length=1)
    actions: list[ClassifiedAction]
    needs_clarification: bool
    clarifying_question: str | None
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str]) -> list[str]:
        return [tag.strip().lower() for tag in tags if tag.strip()]


class FileNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_id: str = Field(min_length=1, max_length=30)
    source_message_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    delivery_attempt: int = Field(ge=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    classification: Classification
    inbox_reason: str | None = None

    @field_validator("capture_id")
    @classmethod
    def validate_capture_id(cls, v: str) -> str:
        if not _CAPTURE_ID_RE.match(v):
            raise ValueError("capture_id must match ^SB-\\d{8}-\\d{4}$")
        return v


class FileNoteResponse(BaseModel):
    result: Literal["FILED"]
    note_path: str
    git_commit_hash: str | None
    idempotent: bool


class HealthResponse(BaseModel):
    status: Literal["ok"]
