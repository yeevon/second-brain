"""Architecture tests for the Second Brain - Error Handler and Error Harness fixtures."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(".")
ERROR_HANDLER_PATH = ROOT / "n8n" / "workflows" / "second-brain-error-handler.json"
ERROR_HARNESS_PATH = ROOT / "n8n" / "workflows" / "test" / "second-brain-error-harness.json"
INTAKE_PATH = ROOT / "n8n" / "workflows" / "second-brain-intake.json"
BOOTSTRAP_PATH = ROOT / "deploy" / "bootstrap-n8n.sh"

_RESTRICTED_NODE_TYPES = {
    "n8n-nodes-base.executeCommand",
    "n8n-nodes-base.readWriteFile",
    "n8n-nodes-base.filesFromUrl",
    "n8n-nodes-base.writeBinaryFile",
    "n8n-nodes-base.sshCommand",
    "n8n-nodes-base.executeWorkflow",
    "n8n-nodes-base.discord",
    "n8n-nodes-base.discordWebhook",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _node_types(workflow: dict) -> set[str]:
    return {n["type"] for n in workflow.get("nodes", [])}


def _node_names(workflow: dict) -> set[str]:
    return {n["name"] for n in workflow.get("nodes", [])}


def _all_code(workflow: dict) -> list[str]:
    return [
        n["parameters"].get("jsCode", "")
        for n in workflow.get("nodes", [])
        if n["type"] == "n8n-nodes-base.code"
    ]


def _node_code(workflow: dict, node_name: str) -> str:
    for node in workflow.get("nodes", []):
        if node["name"] == node_name and node["type"] == "n8n-nodes-base.code":
            return node["parameters"].get("jsCode", "")
    return ""


def _error_handler_js(node_name: str) -> str:
    return _node_code(_load(ERROR_HANDLER_PATH), node_name)


def _all_urls(workflow: dict) -> list[str]:
    return [
        n["parameters"].get("url", "")
        for n in workflow.get("nodes", [])
        if "url" in n.get("parameters", {})
    ]


def _fixture_text(path: Path) -> str:
    return path.read_text()


# ── Error Handler fixture validity ────────────────────────────────────────────


def test_error_handler_fixture_is_valid_json():
    data = _load(ERROR_HANDLER_PATH)
    assert isinstance(data, dict)


def test_error_handler_has_no_top_level_id():
    assert "id" not in _load(ERROR_HANDLER_PATH)


def test_error_handler_has_no_top_level_version_id():
    assert "versionId" not in _load(ERROR_HANDLER_PATH)


def test_error_handler_is_inactive():
    assert _load(ERROR_HANDLER_PATH).get("active") is False


def test_error_handler_has_correct_name():
    assert _load(ERROR_HANDLER_PATH)["name"] == "Second Brain - Error Handler"


# ── Error Handler starts with Error Trigger ───────────────────────────────────


def test_error_handler_starts_with_error_trigger():
    wf = _load(ERROR_HANDLER_PATH)
    types = [n["type"] for n in wf["nodes"]]
    assert "n8n-nodes-base.errorTrigger" in types


def test_error_handler_error_trigger_is_first_or_root_node():
    wf = _load(ERROR_HANDLER_PATH)
    trigger_nodes = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.errorTrigger"]
    assert len(trigger_nodes) >= 1


# ── Error Handler no dangerous nodes ─────────────────────────────────────────


def test_error_handler_has_no_discord_node():
    types = _node_types(_load(ERROR_HANDLER_PATH))
    discord_types = {t for t in types if "discord" in t.lower()}
    assert not discord_types, f"Discord node types found: {discord_types}"


def test_error_handler_has_no_gemini_node():
    fixture_text = _fixture_text(ERROR_HANDLER_PATH)
    assert "generativelanguage.googleapis.com" not in fixture_text


def test_error_handler_has_no_writer_stub_call():
    fixture_text = _fixture_text(ERROR_HANDLER_PATH)
    assert "writer-stub" not in fixture_text


def test_error_handler_does_not_allow_writer_stub_stage():
    """No stale writer_stub taxonomy in the error-handler allowlists (SB-116 cleanup)."""
    all_js = "\n".join(_all_code(_load(ERROR_HANDLER_PATH)))
    assert "writer_stub" not in all_js, (
        "error-handler JS still references writer_stub taxonomy — remove it"
    )
    assert "writer_stub_timeout" not in all_js
    assert "writer_stub_unavailable" not in all_js


def test_error_handler_node_stage_map_has_no_chained_object_keys():
    """NODE_STAGE_MAP must not contain JS-invalid chained key syntax from malformed writer-stub removal."""
    js = _error_handler_js("Normalize Safe Error Metadata")
    assert js, "Normalize Safe Error Metadata node not found or has no jsCode"
    assert "'Write to Vault': 'Write to Inbox':" not in js, (
        "Malformed chained object key detected in NODE_STAGE_MAP"
    )
    assert "'Write to Inbox': 'Inbox (classified)':" not in js
    assert "'Inbox (classified)': 'Submit to Writer Service':" not in js


def test_error_handler_has_no_filesystem_node():
    types = _node_types(_load(ERROR_HANDLER_PATH))
    fs_types = {"n8n-nodes-base.readWriteFile", "n8n-nodes-base.writeBinaryFile", "n8n-nodes-base.filesFromUrl"}
    assert not (types & fs_types)


def test_error_handler_has_no_execute_command_node():
    assert "n8n-nodes-base.executeCommand" not in _node_types(_load(ERROR_HANDLER_PATH))


def test_error_handler_has_no_git_node():
    fixture_text = _fixture_text(ERROR_HANDLER_PATH)
    assert "n8n-nodes-base.git" not in fixture_text


def test_error_handler_has_no_restricted_node_types():
    types = _node_types(_load(ERROR_HANDLER_PATH))
    violations = types & _RESTRICTED_NODE_TYPES
    assert not violations, f"Restricted node types found: {violations}"


# ── Error Handler calls capture-service only ─────────────────────────────────


def test_error_handler_calls_capture_service_via_internal_hostname():
    urls = _all_urls(_load(ERROR_HANDLER_PATH))
    capture_urls = [u for u in urls if "capture-service" in u or ":8000" in u]
    assert len(capture_urls) >= 1
    for url in capture_urls:
        assert "capture-service:8000" in url, f"URL must use internal hostname: {url!r}"


def test_error_handler_report_workflow_error_url_present():
    fixture_text = _fixture_text(ERROR_HANDLER_PATH)
    assert "delivery/report-workflow-error" in fixture_text


def test_error_handler_does_not_use_localhost():
    fixture_text = _fixture_text(ERROR_HANDLER_PATH)
    assert "localhost" not in fixture_text
    assert "127.0.0.1" not in fixture_text


def test_error_handler_credential_ids_are_all_placeholders():
    wf = _load(ERROR_HANDLER_PATH)
    for node in wf.get("nodes", []):
        for cred in node.get("credentials", {}).values():
            if isinstance(cred, dict) and "id" in cred:
                assert cred["id"].startswith("PLACEHOLDER_"), (
                    f"Credential ID must be PLACEHOLDER_*, got: {cred['id']!r}"
                )


# ── Error Handler does not forward raw error content ─────────────────────────


def test_error_handler_code_does_not_forward_raw_error_message():
    code_snippets = _all_code(_load(ERROR_HANDLER_PATH))
    for code in code_snippets:
        # Must not forward execution.error.message or stack directly to capture-service
        assert "error.stack" not in code, "Code must not forward stack traces"


def test_error_handler_does_not_contain_hardcoded_secrets():
    fixture_text = _fixture_text(ERROR_HANDLER_PATH)
    # No real-looking tokens (40+ hex chars)
    import re
    long_hex = re.findall(r'[0-9a-f]{40,}', fixture_text)
    assert not long_hex, f"Potential hardcoded secrets found: {long_hex}"


# ── Error Handler terminates with No Operation ───────────────────────────────


def test_error_handler_has_no_operation_node():
    assert "n8n-nodes-base.noOp" in _node_types(_load(ERROR_HANDLER_PATH))


# ── Error Handler execution retention settings ────────────────────────────────


def test_error_handler_save_on_success_is_none():
    settings = _load(ERROR_HANDLER_PATH).get("settings", {})
    assert settings.get("saveDataSuccessExecution") == "none"


def test_error_handler_save_on_error_is_none():
    settings = _load(ERROR_HANDLER_PATH).get("settings", {})
    assert settings.get("saveDataErrorExecution") == "none"


def test_error_handler_save_manual_is_false():
    settings = _load(ERROR_HANDLER_PATH).get("settings", {})
    assert settings.get("saveManualExecutions") is False


# ── Error Harness fixture ─────────────────────────────────────────────────────


def test_error_harness_is_stored_under_test_directory():
    assert ERROR_HARNESS_PATH.exists(), f"Harness fixture not found: {ERROR_HARNESS_PATH}"
    assert "test" in str(ERROR_HARNESS_PATH)


def test_error_harness_fixture_is_valid_json():
    data = _load(ERROR_HARNESS_PATH)
    assert isinstance(data, dict)


def test_error_harness_has_correct_name():
    assert _load(ERROR_HARNESS_PATH)["name"] == "Second Brain - Error Harness"


def test_error_harness_is_inactive():
    assert _load(ERROR_HARNESS_PATH).get("active") is False


def test_error_harness_references_error_handler():
    settings = _load(ERROR_HARNESS_PATH).get("settings", {})
    assert settings.get("errorWorkflow") == "PLACEHOLDER_SECOND_BRAIN_ERROR_HANDLER"


def test_error_harness_accepts_only_allowlisted_test_cases():
    fixture_text = _fixture_text(ERROR_HARNESS_PATH)
    # The four allowed test cases must appear in the fixture
    for test_case in ("gemini_timeout", "classification_validation_failure", "contract_violation", "orphan_unhandled_exception"):
        assert test_case in fixture_text, f"Missing allowlisted test case: {test_case}"
    # ALLOWED_TEST_CASES should be restricted — check it doesn't accept arbitrary strings
    assert "ALLOWED_TEST_CASES" in fixture_text or "allowlisted" in fixture_text.lower()


def test_error_harness_has_webhook_trigger():
    types = _node_types(_load(ERROR_HARNESS_PATH))
    assert "n8n-nodes-base.webhook" in types


def test_error_harness_has_stop_and_error_node():
    types = _node_types(_load(ERROR_HARNESS_PATH))
    assert "n8n-nodes-base.stopAndError" in types


def test_error_harness_has_no_top_level_id():
    assert "id" not in _load(ERROR_HARNESS_PATH)


# ── Intake workflow error workflow reference ──────────────────────────────────


def test_intake_references_error_handler_as_error_workflow():
    settings = _load(INTAKE_PATH).get("settings", {})
    assert settings.get("errorWorkflow") == "PLACEHOLDER_SECOND_BRAIN_ERROR_HANDLER", (
        "Intake workflow must reference the error handler via errorWorkflow setting"
    )


def test_intake_successful_execution_retention_disabled():
    settings = _load(INTAKE_PATH).get("settings", {})
    assert settings.get("saveDataSuccessExecution") == "none"


def test_intake_failed_execution_retention_disabled():
    settings = _load(INTAKE_PATH).get("settings", {})
    assert settings.get("saveDataErrorExecution") == "none"


def test_intake_manual_execution_retention_disabled():
    settings = _load(INTAKE_PATH).get("settings", {})
    assert settings.get("saveManualExecutions") is False


def test_intake_execution_progress_retention_disabled():
    settings = _load(INTAKE_PATH).get("settings", {})
    assert settings.get("saveExecutionProgress") is False


# ── Bootstrap EC2 deployment exclusions ──────────────────────────────────────


def test_bootstrap_does_not_reference_error_harness_fixture():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "second-brain-error-harness.json" not in bootstrap


def test_bootstrap_does_not_import_from_test_directory():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "n8n/workflows/test/" not in bootstrap


def test_bootstrap_imports_error_handler():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "Second Brain - Error Handler" in bootstrap
    assert "second-brain-error-handler.json" in bootstrap


def test_bootstrap_strips_ids_before_import():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "del(.id, .versionId)" in bootstrap
