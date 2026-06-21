from types import SimpleNamespace

import pytest

from secondbrain.classifier import classify_capture, gemini_classification_schema, parse_classification_response
from secondbrain.models import Classification


VALID_CLASSIFICATION = {
    "folder": "projects",
    "project": "halo",
    "note_type": "task",
    "title": "Review WebSocket reconnect handling",
    "tags": ["Telemetry", " websocket "],
    "body": "Review reconnect handling in the HALO telemetry dashboard.",
    "actions": [{"text": "Review WebSocket reconnect handling", "status": "open"}],
    "needs_clarification": False,
    "clarifying_question": None,
    "confidence": 0.91,
}


@pytest.mark.asyncio
async def test_classify_capture_returns_file_route_for_valid_high_confidence_response():
    outcome = await classify_capture(
        "Review reconnect handling in the HALO telemetry dashboard.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    assert outcome.route == "file"
    assert outcome.inbox_reason is None
    assert outcome.classification.folder == "projects"
    assert outcome.classification.project == "halo"
    assert outcome.classification.tags == ["telemetry", "websocket"]


@pytest.mark.asyncio
async def test_classify_capture_preserves_model_selected_inbox_route():
    payload = {
        **VALID_CLASSIFICATION,
        "folder": "inbox",
        "project": None,
        "confidence": 0.9,
    }

    outcome = await classify_capture(
        "This is too vague to place.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(parsed=payload),
    )

    assert outcome.route == "inbox"
    assert outcome.classification.folder == "inbox"
    assert outcome.inbox_reason == "classifier selected inbox"


@pytest.mark.asyncio
async def test_classify_capture_routes_low_confidence_to_inbox():
    payload = {**VALID_CLASSIFICATION, "confidence": 0.2}

    outcome = await classify_capture(
        "Maybe check something.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(parsed=payload),
    )

    assert outcome.route == "inbox"
    assert outcome.classification.folder == "inbox"
    assert outcome.inbox_reason == "classification confidence below threshold"


@pytest.mark.asyncio
async def test_classify_capture_routes_clarification_to_inbox():
    payload = {
        **VALID_CLASSIFICATION,
        "needs_clarification": True,
        "clarifying_question": "Which project is this for?",
    }

    outcome = await classify_capture(
        "Check reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(parsed=payload),
    )

    assert outcome.route == "inbox"
    assert outcome.classification.folder == "inbox"
    assert outcome.inbox_reason == "classification needs clarification"


@pytest.mark.asyncio
async def test_classify_capture_routes_invalid_response_to_inbox():
    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(parsed={"folder": "not-real"}),
    )

    assert outcome.route == "inbox"
    assert outcome.classification.folder == "inbox"
    assert outcome.inbox_reason.startswith("classifier failed: ValidationError:")


@pytest.mark.asyncio
async def test_classify_capture_routes_malformed_json_text_to_inbox():
    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeTextClient(text='{"folder":'),
    )

    assert outcome.route == "inbox"
    assert outcome.classification.folder == "inbox"
    assert outcome.inbox_reason.startswith("classifier failed: JSONDecodeError:")


@pytest.mark.asyncio
async def test_classify_capture_routes_missing_required_field_to_inbox():
    payload = dict(VALID_CLASSIFICATION)
    payload.pop("tags")

    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(parsed=payload),
    )

    assert outcome.route == "inbox"
    assert outcome.classification.folder == "inbox"
    assert outcome.inbox_reason.startswith("classifier failed: ValidationError:")


@pytest.mark.asyncio
async def test_classify_capture_routes_action_missing_status_to_inbox():
    payload = {**VALID_CLASSIFICATION, "actions": [{"text": "Review reconnect handling"}]}

    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(parsed=payload),
    )

    assert outcome.route == "inbox"
    assert outcome.classification.folder == "inbox"
    assert outcome.inbox_reason.startswith("classifier failed: ValidationError:")


@pytest.mark.asyncio
async def test_classify_capture_routes_api_failure_to_inbox():
    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(error=RuntimeError("timeout")),
    )

    assert outcome.route == "inbox"
    assert outcome.classification.folder == "inbox"
    assert outcome.inbox_reason == "classifier failed: RuntimeError: timeout"


@pytest.mark.asyncio
async def test_classify_capture_redacts_api_key_from_failure_reason():
    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="secret-api-key",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(error=RuntimeError("bad key secret-api-key")),
    )

    assert "secret-api-key" not in outcome.inbox_reason
    assert "[REDACTED_API_KEY]" in outcome.inbox_reason


@pytest.mark.asyncio
async def test_classify_capture_redacts_generic_credential_patterns_from_failure_reason():
    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(error=RuntimeError(
            "call failed: password=hunter2 token=abc123 Bearer supersecrettoken"
        )),
    )

    assert "hunter2" not in outcome.inbox_reason
    assert "abc123" not in outcome.inbox_reason
    assert "supersecrettoken" not in outcome.inbox_reason
    assert "[REDACTED]" in outcome.inbox_reason


@pytest.mark.asyncio
async def test_classify_capture_strips_url_from_failure_reason():
    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(error=RuntimeError("request failed: https://api.example.com/v1/secret")),
    )

    assert "https://" not in outcome.inbox_reason
    assert "[REDACTED_URL]" in outcome.inbox_reason


@pytest.mark.asyncio
async def test_classify_capture_truncates_long_failure_reason():
    long_message = "x" * 500
    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(error=RuntimeError(long_message)),
    )

    assert len(outcome.inbox_reason) <= 300
    assert "..." in outcome.inbox_reason


@pytest.mark.asyncio
async def test_classify_capture_failure_reason_contains_only_error_type_and_bounded_summary():
    outcome = await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=FakeClient(error=ValueError("some internal detail")),
    )

    assert outcome.inbox_reason.startswith("classifier failed: ValueError")
    assert len(outcome.inbox_reason) <= 300


def test_parse_classification_response_accepts_json_text_response():
    response = SimpleNamespace(text="""
    {
      "folder": "learning",
      "project": null,
      "note_type": "note",
      "title": "Learn SQLite WAL",
      "tags": ["sqlite"],
      "body": "Learn how SQLite WAL works.",
      "actions": [],
      "needs_clarification": false,
      "clarifying_question": null,
      "confidence": 0.8
    }
    """)

    classification = parse_classification_response(response)

    assert isinstance(classification, Classification)
    assert classification.folder == "learning"
    assert classification.title == "Learn SQLite WAL"


def test_gemini_schema_omits_unsupported_additional_properties():
    schema = gemini_classification_schema()

    assert "additionalProperties" not in str(schema)
    assert "additional_properties" not in str(schema)
    assert schema["properties"]["actions"]["items"]["required"] == ["text", "status"]
    assert schema["required"] == [
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
    ]


@pytest.mark.asyncio
async def test_classify_capture_sends_structured_output_config_to_gemini():
    client = FakeClient(parsed=VALID_CLASSIFICATION)

    await classify_capture(
        "Review reconnect handling.",
        api_key="fake",
        model="gemini-test",
        confidence_threshold=0.75,
        client=client,
    )

    call = client.aio.models.calls[0]
    assert call["model"] == "gemini-test"
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].response_schema == gemini_classification_schema()


class FakeClient:
    def __init__(self, *, parsed=None, error=None):
        self.aio = SimpleNamespace(models=FakeModels(parsed=parsed, error=error))


class FakeModels:
    def __init__(self, *, parsed, error):
        self.parsed = parsed
        self.error = error
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(parsed=self.parsed)


class FakeTextClient:
    def __init__(self, *, text):
        self.aio = SimpleNamespace(models=FakeTextModels(text=text))


class FakeTextModels:
    def __init__(self, *, text):
        self.text = text

    async def generate_content(self, **kwargs):
        return SimpleNamespace(text=self.text)
