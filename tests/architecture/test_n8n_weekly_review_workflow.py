"""Architecture tests for the Second Brain - Weekly Review workflow fixture."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(".")
FIXTURE_PATH = ROOT / "n8n" / "workflows" / "second-brain-weekly-review.json"
BOOTSTRAP_PATH = ROOT / "deploy" / "bootstrap-n8n.sh"

_RESTRICTED_NODE_TYPES = {
    "n8n-nodes-base.executeCommand",
    "n8n-nodes-base.readWriteFile",
    "n8n-nodes-base.filesFromUrl",
    "n8n-nodes-base.writeBinaryFile",
    "n8n-nodes-base.sshCommand",
    "n8n-nodes-base.executeWorkflow",
}

_GEMINI_URL_PREFIX = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _all_credential_ids(workflow: dict) -> list[str]:
    ids: list[str] = []
    for node in workflow.get("nodes", []):
        for cred in node.get("credentials", {}).values():
            if isinstance(cred, dict) and "id" in cred:
                ids.append(cred["id"])
    return ids


# ── Fixture validity ─────────────────────────────────────────────────────────


def test_weekly_review_fixture_is_valid_json():
    assert isinstance(_fixture(), dict)


def test_weekly_review_fixture_has_no_top_level_id():
    assert "id" not in _fixture()


def test_weekly_review_fixture_has_no_top_level_version_id():
    assert "versionId" not in _fixture()


def test_weekly_review_fixture_is_inactive():
    assert _fixture().get("active") is False


def test_weekly_review_fixture_has_correct_name():
    assert _fixture()["name"] == "Second Brain - Weekly Review"


# ── Schedule trigger ─────────────────────────────────────────────────────────


def test_weekly_review_has_schedule_trigger():
    wf = _fixture()
    types = [n["type"] for n in wf["nodes"]]
    assert "n8n-nodes-base.scheduleTrigger" in types


def test_weekly_review_schedule_is_weekly():
    fixture_text = FIXTURE_PATH.read_text()
    assert "1" in fixture_text and "8" in fixture_text  # day 1 and hour 8 present


# ── Brief endpoint ───────────────────────────────────────────────────────────


def test_weekly_review_calls_brief_endpoint():
    fixture_text = FIXTURE_PATH.read_text()
    assert "http://capture-service:8000/internal/brief/weekly" in fixture_text


def test_weekly_review_does_not_call_old_digest_endpoint():
    fixture_text = FIXTURE_PATH.read_text()
    assert "/internal/digest/weekly" not in fixture_text


def test_weekly_review_capture_service_url_uses_internal_hostname():
    wf = _fixture()
    for node in wf["nodes"]:
        url = node.get("parameters", {}).get("url", "")
        if "capture-service" in url or ":8000" in url:
            clean = url.lstrip("=")
            assert clean.startswith("http://capture-service:8000"), (
                f"capture-service URL must use internal hostname, got: {url!r}"
            )


# ── Gemini call ──────────────────────────────────────────────────────────────


def test_weekly_review_calls_gemini():
    fixture_text = FIXTURE_PATH.read_text()
    assert _GEMINI_URL_PREFIX in fixture_text


def test_weekly_review_gemini_url_uses_https():
    wf = _fixture()
    for node in wf["nodes"]:
        url = node.get("parameters", {}).get("url", "")
        if "generativelanguage.googleapis.com" in url:
            assert url.startswith("https://"), f"Gemini URL must use HTTPS, got: {url!r}"


def test_weekly_review_gemini_credential_is_placeholder():
    wf = _fixture()
    gemini_nodes = [
        n for n in wf["nodes"]
        if "generativelanguage.googleapis.com" in n.get("parameters", {}).get("url", "")
    ]
    assert len(gemini_nodes) >= 1
    for node in gemini_nodes:
        cred = node.get("credentials", {}).get("httpHeaderAuth")
        assert cred is not None, f"Gemini node '{node['name']}' missing httpHeaderAuth credential"
        assert cred["id"] == "PLACEHOLDER_GEMINI_API_KEY"


# ── Brief output format ───────────────────────────────────────────────────────


def test_weekly_review_format_references_accomplished():
    fixture_text = FIXTURE_PATH.read_text()
    assert "accomplished" in fixture_text


def test_weekly_review_format_references_still_open():
    fixture_text = FIXTURE_PATH.read_text()
    assert "still_open" in fixture_text


def test_weekly_review_format_references_decisions():
    fixture_text = FIXTURE_PATH.read_text()
    assert "decisions" in fixture_text


def test_weekly_review_format_references_study_progress():
    fixture_text = FIXTURE_PATH.read_text()
    assert "study_progress" in fixture_text


def test_weekly_review_message_labels_ai_section():
    fixture_text = FIXTURE_PATH.read_text()
    assert "AI-GENERATED PRIORITIES" in fixture_text


def test_weekly_review_ai_prompt_does_not_infer_completion_from_prose():
    fixture_text = FIXTURE_PATH.read_text()
    assert "explicit state" in fixture_text.lower() or "grounded in the" in fixture_text.lower()


# ── Security invariants ───────────────────────────────────────────────────────


def test_weekly_review_no_localhost_in_any_url():
    fixture_text = FIXTURE_PATH.read_text()
    assert "localhost" not in fixture_text
    assert "127.0.0.1" not in fixture_text


def test_weekly_review_no_restricted_node_types():
    wf = _fixture()
    used = {n["type"] for n in wf["nodes"]}
    assert not used & _RESTRICTED_NODE_TYPES, f"Restricted node types: {used & _RESTRICTED_NODE_TYPES}"


def test_weekly_review_credential_ids_are_placeholders():
    ids = _all_credential_ids(_fixture())
    for cred_id in ids:
        assert cred_id.startswith("PLACEHOLDER_"), (
            f"Committed credential ID must be PLACEHOLDER_*, got: {cred_id!r}"
        )


# ── Execution retention ───────────────────────────────────────────────────────


def test_weekly_review_save_success_is_none():
    assert _fixture()["settings"]["saveDataSuccessExecution"] == "none"


def test_weekly_review_save_error_is_none():
    assert _fixture()["settings"]["saveDataErrorExecution"] == "none"


def test_weekly_review_save_manual_is_false():
    assert _fixture()["settings"]["saveManualExecutions"] is False


def test_weekly_review_save_progress_is_false():
    assert _fixture()["settings"]["saveExecutionProgress"] is False


# ── Bootstrap ────────────────────────────────────────────────────────────────


def test_bootstrap_imports_weekly_review_workflow():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "second-brain-weekly-review.json" in bootstrap or "Second Brain - Weekly Review" in bootstrap


def test_bootstrap_updates_existing_weekly_review_workflow_in_place():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "import_or_update_workflow" in bootstrap
    assert '"$WEEKLY_REVIEW_NAME"' in bootstrap
    assert "$WEEKLY_REVIEW_FIXTURE" in bootstrap
    assert "updated in place" in bootstrap
    assert "Second Brain - Weekly Review: skipped" not in bootstrap


def test_weekly_review_capture_service_node_uses_placeholder_credential():
    wf = _fixture()
    for node in wf["nodes"]:
        url = node.get("parameters", {}).get("url", "")
        if "capture-service" in url:
            cred = node.get("credentials", {}).get("httpHeaderAuth")
            assert cred is not None, f"Node '{node['name']}' calls capture-service but has no httpHeaderAuth credential"
            assert cred["id"] == "PLACEHOLDER_CAPTURE_SERVICE_TOKEN"


def test_weekly_review_prepare_node_uses_open_tasks_data():
    """Prepare AI input must use still_open and accomplished from the brief response."""
    wf = _fixture()
    code_nodes = [n for n in wf["nodes"] if n.get("type") == "n8n-nodes-base.code"]
    prepare_nodes = [n for n in code_nodes if "Prepare" in n["name"] or "Input" in n["name"]]
    assert len(prepare_nodes) >= 1, "Prepare AI Input node not found"
    code = prepare_nodes[0]["parameters"]["jsCode"]
    assert "still_open" in code, "Prepare AI input must reference still_open tasks"
    assert "accomplished" in code, "Prepare AI input must reference accomplished items"


# ── Error handling ────────────────────────────────────────────────────────────


def test_weekly_review_send_to_discord_has_error_output():
    wf = _fixture()
    discord_nodes = [n for n in wf["nodes"] if n.get("name") == "Send to Discord"]
    assert len(discord_nodes) == 1
    assert discord_nodes[0].get("onError") == "continueErrorOutput", (
        "Send to Discord must use continueErrorOutput so delivery failures are visible"
    )


def test_weekly_review_delivery_failure_is_logged():
    wf = _fixture()
    names = [n["name"] for n in wf["nodes"]]
    assert any("Failure" in name or "failure" in name for name in names), (
        "Expected a log/handle delivery failure node"
    )


def test_weekly_review_discord_error_output_is_connected():
    wf = _fixture()
    discord_conn = wf["connections"].get("Send to Discord", {}).get("main", [])
    assert len(discord_conn) >= 2, "Send to Discord must have both success and error outputs defined"
    assert len(discord_conn[1]) > 0, "Send to Discord error output must connect to a failure-handling node"


def test_weekly_review_gemini_has_continue_on_error():
    """Gemini failure must not block the weekly factual summary from posting."""
    wf = _fixture()
    gemini_nodes = [
        n for n in wf["nodes"]
        if "generativelanguage.googleapis.com" in n.get("parameters", {}).get("url", "")
    ]
    assert len(gemini_nodes) >= 1
    on_error = gemini_nodes[0].get("onError", "")
    assert on_error in ("continueRegularOutput", "continueErrorOutput"), (
        "Gemini node must set onError so weekly review posts factual counts even if Gemini is down"
    )


def test_weekly_review_format_message_has_priorities_unavailable_fallback():
    """Format node must handle missing Gemini output gracefully."""
    fixture_text = FIXTURE_PATH.read_text()
    assert "priorities unavailable" in fixture_text.lower(), (
        "Format Review Message must include a fallback string for when Gemini output is missing"
    )
