from __future__ import annotations

import json
import re
from typing import Any

from google import genai
from google.genai import types
from pydantic import ValidationError

from secondbrain.models import Classification, ClassificationOutcome


CLASSIFIER_PROMPT_VERSION = "classifier-v1"

CLASSIFIER_SYSTEM_PROMPT = """You classify raw Discord captures for a personal Second Brain.

Return structured data only. Do not return file paths, filenames, Markdown
frontmatter, shell commands, or Git commands.

Allowed folders:
- people
- projects
- ideas
- learning
- admin
- inbox

Use inbox when the capture is too vague to file confidently.
Keep the body close to the original thought. Do not invent facts.

note_type should be one of: note, task, idea, decision, birthday, reminder, event, study

For birthday captures (e.g. "Mom's birthday June 22"):
  - Set note_type to "birthday"
  - Set note_date to the ISO date (YYYY-MM-DD); use current year if no year given

For reminder or due-date captures (e.g. "remember to submit form by Friday"):
  - Set note_type to "reminder"
  - Set note_date to the ISO due date

For event captures (e.g. "ENGR 110 starts June 18"):
  - Set note_type to "event"
  - Set note_date to the ISO date of the event

For study captures (e.g. "finished algebra section"):
  - Set note_type to "study"
  - Use folder "learning"

For action items in any note:
  - Set due to "YYYY-MM-DD" if a due date is mentioned
  - Set priority to "high", "medium", or "low" if urgency is explicit or strongly implied
  - Set project to the project slug if the action belongs to a specific project different from the note's project
"""


async def classify_capture(
    raw_text: str,
    *,
    api_key: str,
    model: str,
    confidence_threshold: float,
    client: Any | None = None,
) -> ClassificationOutcome:
    client = client or genai.Client(api_key=api_key)

    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=_build_prompt(raw_text),
            config=types.GenerateContentConfig(
                system_instruction=CLASSIFIER_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=gemini_classification_schema(),
            ),
        )
        classification = parse_classification_response(response)
    except Exception as exc:
        return inbox_fallback(raw_text, reason=_classifier_failure_reason(exc, api_key=api_key))

    return route_classification(
        classification,
        confidence_threshold=confidence_threshold,
        raw_text=raw_text,
    )


def parse_classification_response(response: Any) -> Classification:
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        if isinstance(parsed, Classification):
            return parsed
        return Classification.model_validate(parsed)

    text = getattr(response, "text", None)
    if not text:
        raise ValueError("Gemini response did not include parsed data or text")

    return Classification.model_validate(json.loads(text))


def route_classification(
    classification: Classification,
    *,
    confidence_threshold: float,
    raw_text: str,
) -> ClassificationOutcome:
    if classification.folder == "inbox":
        return ClassificationOutcome(
            classification=classification,
            route="inbox",
            inbox_reason="classifier selected inbox",
        )

    if classification.needs_clarification:
        return inbox_fallback(raw_text, reason="classification needs clarification")

    if classification.confidence < confidence_threshold:
        return inbox_fallback(raw_text, reason="classification confidence below threshold")

    return ClassificationOutcome(
        classification=classification,
        route="file",
        inbox_reason=None,
    )


def inbox_fallback(raw_text: str, *, reason: str) -> ClassificationOutcome:
    return ClassificationOutcome(
        classification=Classification(
            folder="inbox",
            project=None,
            note_type="note",
            note_date=None,
            title="Unclassified capture",
            tags=["inbox"],
            body=raw_text,
            actions=[],
            needs_clarification=True,
            clarifying_question=None,
            confidence=0.0,
        ),
        route="inbox",
        inbox_reason=reason,
    )


def gemini_classification_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "folder": {
                "type": "string",
                "enum": ["people", "projects", "ideas", "learning", "admin", "inbox"],
            },
            "project": {
                "type": "string",
                "nullable": True,
            },
            "note_type": {
                "type": "string",
            },
            "note_date": {
                "type": "string",
                "nullable": True,
            },
            "title": {
                "type": "string",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
            },
            "body": {
                "type": "string",
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "status": {"type": "string", "enum": ["open", "done"]},
                        "due": {"type": "string", "nullable": True},
                        "priority": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "nullable": True,
                        },
                        "project": {"type": "string", "nullable": True},
                    },
                    "required": ["text", "status"],
                },
            },
            "needs_clarification": {
                "type": "boolean",
            },
            "clarifying_question": {
                "type": "string",
                "nullable": True,
            },
            "confidence": {
                "type": "number",
            },
        },
        "required": [
            "folder",
            "project",
            "note_type",
            "note_date",
            "title",
            "tags",
            "body",
            "actions",
            "needs_clarification",
            "clarifying_question",
            "confidence",
        ],
        "property_ordering": [
            "folder",
            "project",
            "note_type",
            "note_date",
            "title",
            "tags",
            "body",
            "actions",
            "needs_clarification",
            "clarifying_question",
            "confidence",
        ],
    }


def _build_prompt(raw_text: str) -> str:
    return f"Classify this raw Discord capture:\n\n{raw_text}"


def _classifier_failure_reason(exc: Exception, *, api_key: str) -> str:
    message = _safe_exception_message(exc, api_key=api_key)
    if not message:
        return f"classifier failed: {type(exc).__name__}"
    return f"classifier failed: {type(exc).__name__}: {message}"


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# Matches key=value / key: value pairs where the key name looks like a credential,
# and also Bearer <token> scheme values.
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:"
    r"(password|passwd|token|secret|api[_-]?key|authorization|access[_-]?key"
    r"|private[_-]?key|x-api-key)\s*[=:]\s*\S+"
    r"|Bearer\s+\S+"
    r")",
)


def _safe_exception_message(exc: Exception, *, api_key: str) -> str:
    message = str(exc).replace("\n", " ").strip()
    if api_key:
        message = message.replace(api_key, "[REDACTED_API_KEY]")
    message = _URL_RE.sub("[REDACTED_URL]", message)
    message = _CREDENTIAL_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]" if m.group(1) else "Bearer [REDACTED]", message)
    if len(message) > 200:
        message = f"{message[:197]}..."
    return message
