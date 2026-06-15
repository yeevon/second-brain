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
    # Cron expression for every Monday at 8am: "0 8 * * 1"
    assert "1" in fixture_text and "8" in fixture_text  # day 1 and hour 8 present


# ── Digest endpoint ──────────────────────────────────────────────────────────


def test_weekly_review_calls_correct_capture_service_url():
    fixture_text = FIXTURE_PATH.read_text()
    assert "http://capture-service:8000/internal/digest/weekly" in fixture_text


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


# ── AI priorities label ───────────────────────────────────────────────────────


def test_weekly_review_message_labels_ai_section():
    fixture_text = FIXTURE_PATH.read_text()
    assert "AI-GENERATED PRIORITIES" in fixture_text


def test_weekly_review_ai_prompt_does_not_infer_completion_from_prose():
    fixture_text = FIXTURE_PATH.read_text()
    assert "explicit state" in fixture_text.lower() or "grounded in the numbers" in fixture_text.lower()


# ── Message content ───────────────────────────────────────────────────────────


def test_weekly_review_references_corrections():
    fixture_text = FIXTURE_PATH.read_text()
    assert "corrections_count" in fixture_text


def test_weekly_review_references_failures():
    fixture_text = FIXTURE_PATH.read_text()
    assert "failures_count" in fixture_text


def test_weekly_review_references_created_tasks():
    fixture_text = FIXTURE_PATH.read_text()
    assert "created_tasks_count" in fixture_text


def test_weekly_review_references_completed_actions():
    fixture_text = FIXTURE_PATH.read_text()
    assert "completed_actions_count" in fixture_text


def test_weekly_review_references_decisions():
    fixture_text = FIXTURE_PATH.read_text()
    assert "decisions_count" in fixture_text


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


def test_weekly_review_capture_service_node_uses_placeholder_credential():
    wf = _fixture()
    for node in wf["nodes"]:
        url = node.get("parameters", {}).get("url", "")
        if "capture-service" in url:
            cred = node.get("credentials", {}).get("httpHeaderAuth")
            assert cred is not None, f"Node '{node['name']}' calls capture-service but has no httpHeaderAuth credential"
            assert cred["id"] == "PLACEHOLDER_CAPTURE_SERVICE_TOKEN"


def test_weekly_review_prepare_node_uses_let_not_const_for_week_summary():
    """weekSummary must be `let` so outstanding_tasks_count can be appended."""
    wf = _fixture()
    code_nodes = [n for n in wf["nodes"] if n.get("type") == "n8n-nodes-base.code"]
    prepare_nodes = [n for n in code_nodes if "Prepare" in n["name"] or "Priority" in n["name"].lower()]
    assert len(prepare_nodes) >= 1, "Prepare AI Priorities Input node not found"
    code = prepare_nodes[0]["parameters"]["jsCode"]
    assert "let weekSummary" in code, "weekSummary must be declared with 'let' so += assignment works"
    assert "weekSummary +=" in code, "outstanding_tasks_count must be appended via += not discarded"
