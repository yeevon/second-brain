from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


AllowedFolder = Literal["people", "projects", "ideas", "learning", "admin", "inbox"]
ActionStatus = Literal["open", "done"]


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


class ClassificationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: Classification
    route: Literal["file", "inbox"]
    inbox_reason: str | None = None
