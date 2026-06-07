from __future__ import annotations

import json
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
                systemInstruction=CLASSIFIER_SYSTEM_PROMPT,
                responseMimeType="application/json",
                responseSchema=Classification,
            ),
        )
        classification = parse_classification_response(response)
    except Exception as exc:
        return inbox_fallback(raw_text, reason=f"classifier failed: {type(exc).__name__}")

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


def _build_prompt(raw_text: str) -> str:
    return f"Classify this raw Discord capture:\n\n{raw_text}"
