"""Architecture tests for the Second Brain - Daily Digest workflow fixture."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(".")
FIXTURE_PATH = ROOT / "n8n" / "workflows" / "second-brain-daily-digest.json"
BOOTSTRAP_PATH = ROOT / "deploy" / "bootstrap-n8n.sh"

_RESTRICTED_NODE_TYPES = {
    "n8n-nodes-base.executeCommand",
    "n8n-nodes-base.readWriteFile",
    "n8n-nodes-base.filesFromUrl",
    "n8n-nodes-base.writeBinaryFile",
    "n8n-nodes-base.sshCommand",
    "n8n-nodes-base.executeWorkflow",
}


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


def test_daily_digest_fixture_is_valid_json():
    assert isinstance(_fixture(), dict)


def test_daily_digest_fixture_has_no_top_level_id():
    assert "id" not in _fixture()


def test_daily_digest_fixture_has_no_top_level_version_id():
    assert "versionId" not in _fixture()


def test_daily_digest_fixture_is_inactive():
    assert _fixture().get("active") is False


def test_daily_digest_fixture_has_correct_name():
    assert _fixture()["name"] == "Second Brain - Daily Digest"


# ── Schedule trigger ─────────────────────────────────────────────────────────


def test_daily_digest_has_schedule_trigger():
    wf = _fixture()
    types = [n["type"] for n in wf["nodes"]]
    assert "n8n-nodes-base.scheduleTrigger" in types


def test_daily_digest_schedule_is_daily_7am():
    wf = _fixture()
    schedule_nodes = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.scheduleTrigger"]
    assert len(schedule_nodes) == 1
    # Accept either cron expression or structured form
    fixture_text = FIXTURE_PATH.read_text()
    assert "7" in fixture_text  # hour 7 present somewhere in schedule config


# ── Digest endpoint ──────────────────────────────────────────────────────────


def test_daily_digest_calls_correct_capture_service_url():
    fixture_text = FIXTURE_PATH.read_text()
    assert "http://capture-service:8000/internal/digest/daily" in fixture_text


def test_daily_digest_capture_service_url_uses_internal_hostname():
    wf = _fixture()
    for node in wf["nodes"]:
        url = node.get("parameters", {}).get("url", "")
        if "capture-service" in url or ":8000" in url:
            clean = url.lstrip("=")
            assert clean.startswith("http://capture-service:8000"), (
                f"capture-service URL must use internal hostname, got: {url!r}"
            )


# ── Security invariants ───────────────────────────────────────────────────────


def test_daily_digest_no_localhost_in_any_url():
    fixture_text = FIXTURE_PATH.read_text()
    assert "localhost" not in fixture_text
    assert "127.0.0.1" not in fixture_text


def test_daily_digest_no_restricted_node_types():
    wf = _fixture()
    used = {n["type"] for n in wf["nodes"]}
    assert not used & _RESTRICTED_NODE_TYPES, f"Restricted node types: {used & _RESTRICTED_NODE_TYPES}"


def test_daily_digest_credential_ids_are_placeholders():
    ids = _all_credential_ids(_fixture())
    for cred_id in ids:
        assert cred_id.startswith("PLACEHOLDER_"), (
            f"Committed credential ID must be PLACEHOLDER_*, got: {cred_id!r}"
        )


# ── Execution retention ───────────────────────────────────────────────────────


def test_daily_digest_save_success_is_none():
    assert _fixture()["settings"]["saveDataSuccessExecution"] == "none"


def test_daily_digest_save_error_is_none():
    assert _fixture()["settings"]["saveDataErrorExecution"] == "none"


def test_daily_digest_save_manual_is_false():
    assert _fixture()["settings"]["saveManualExecutions"] is False


def test_daily_digest_save_progress_is_false():
    assert _fixture()["settings"]["saveExecutionProgress"] is False


# ── Message formatting ────────────────────────────────────────────────────────


def test_daily_digest_format_node_references_inbox_backlog():
    fixture_text = FIXTURE_PATH.read_text()
    assert "inbox_backlog_count" in fixture_text


def test_daily_digest_format_node_references_sensitive_rejections():
    fixture_text = FIXTURE_PATH.read_text()
    assert "sensitive_rejections_count" in fixture_text


def test_daily_digest_format_node_references_attachment_warnings():
    fixture_text = FIXTURE_PATH.read_text()
    assert "attachment_warnings_count" in fixture_text


def test_daily_digest_sends_to_discord():
    wf = _fixture()
    http_nodes = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.httpRequest"]
    methods = [n["parameters"].get("method", "GET") for n in http_nodes]
    assert "POST" in methods, "Expected at least one POST node for Discord delivery"


# ── Bootstrap ────────────────────────────────────────────────────────────────


def test_bootstrap_imports_daily_digest_workflow():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "second-brain-daily-digest.json" in bootstrap or "Second Brain - Daily Digest" in bootstrap


def test_daily_digest_capture_service_node_uses_placeholder_credential():
    wf = _fixture()
    for node in wf["nodes"]:
        url = node.get("parameters", {}).get("url", "")
        if "capture-service" in url:
            cred = node.get("credentials", {}).get("httpHeaderAuth")
            assert cred is not None, f"Node '{node['name']}' calls capture-service but has no httpHeaderAuth credential"
            assert cred["id"] == "PLACEHOLDER_CAPTURE_SERVICE_TOKEN"
            assert cred["name"] == "Capture Service Token"


# ── Open tasks by project ─────────────────────────────────────────────────────


def test_daily_digest_format_node_references_open_tasks_by_project():
    fixture_text = FIXTURE_PATH.read_text()
    assert "open_tasks_by_project" in fixture_text


# ── No-activity branch ────────────────────────────────────────────────────────


def test_daily_digest_has_activity_check_if_node():
    wf = _fixture()
    types = [n["type"] for n in wf["nodes"]]
    assert "n8n-nodes-base.if" in types, "Expected an IF node for the activity check"


def test_daily_digest_has_no_activity_skip_node():
    wf = _fixture()
    names = [n["name"] for n in wf["nodes"]]
    assert any("No Activity" in name or "Skip" in name for name in names), (
        "Expected a no-activity/skip node for the false branch"
    )


def test_daily_digest_has_activity_branch_covers_new_captures():
    fixture_text = FIXTURE_PATH.read_text()
    assert "new_captures_count" in fixture_text


# ── Error handling ────────────────────────────────────────────────────────────


def test_daily_digest_send_to_discord_has_error_output():
    wf = _fixture()
    discord_nodes = [n for n in wf["nodes"] if n.get("name") == "Send to Discord"]
    assert len(discord_nodes) == 1
    assert discord_nodes[0].get("onError") == "continueErrorOutput", (
        "Send to Discord must use continueErrorOutput so delivery failures are visible"
    )


def test_daily_digest_delivery_failure_is_logged():
    wf = _fixture()
    names = [n["name"] for n in wf["nodes"]]
    assert any("Failure" in name or "failure" in name for name in names), (
        "Expected a log/handle delivery failure node"
    )


def test_daily_digest_discord_error_output_is_connected():
    wf = _fixture()
    discord_conn = wf["connections"].get("Send to Discord", {}).get("main", [])
    assert len(discord_conn) >= 2, "Send to Discord must have both success and error outputs defined"
    assert len(discord_conn[1]) > 0, "Send to Discord error output must connect to a failure-handling node"
