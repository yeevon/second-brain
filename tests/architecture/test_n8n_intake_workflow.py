"""Architecture tests for the Second Brain - Intake workflow fixture."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(".")
FIXTURE_PATH = ROOT / "n8n" / "workflows" / "second-brain-intake.json"
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


def _all_urls(workflow: dict) -> list[str]:
    urls: list[str] = []
    for node in workflow.get("nodes", []):
        params = node.get("parameters", {})
        url = params.get("url", "")
        if url:
            urls.append(url)
    return urls


def _all_credential_ids(workflow: dict) -> list[str]:
    ids: list[str] = []
    for node in workflow.get("nodes", []):
        for cred in node.get("credentials", {}).values():
            if isinstance(cred, dict) and "id" in cred:
                ids.append(cred["id"])
    return ids


# ── Fixture validity ─────────────────────────────────────────────────────────


def test_intake_fixture_is_valid_json():
    data = _fixture()
    assert isinstance(data, dict)


def test_intake_fixture_has_no_top_level_id():
    assert "id" not in _fixture()


def test_intake_fixture_has_no_top_level_version_id():
    assert "versionId" not in _fixture()


def test_intake_fixture_is_inactive():
    assert _fixture().get("active") is False


def test_intake_fixture_has_name():
    assert _fixture()["name"] == "Second Brain - Intake"


# ── Webhook node ─────────────────────────────────────────────────────────────


def test_intake_webhook_node_path_is_second_brain_intake():
    wf = _fixture()
    webhook_nodes = [
        n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"
    ]
    assert len(webhook_nodes) >= 1
    paths = [n["parameters"].get("path", "") for n in webhook_nodes]
    assert any("second-brain-intake" in p for p in paths)


def test_intake_webhook_node_uses_header_auth():
    wf = _fixture()
    webhook_nodes = [
        n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"
    ]
    assert len(webhook_nodes) >= 1
    auth_values = [n["parameters"].get("authentication", "") for n in webhook_nodes]
    assert any(auth in ("headerAuth", "httpHeaderAuth") for auth in auth_values)


def test_intake_webhook_node_references_auth_credential():
    wf = _fixture()
    webhook_nodes = [
        n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"
    ]
    assert len(webhook_nodes) >= 1
    node = webhook_nodes[0]
    creds = node.get("credentials", {})
    assert len(creds) >= 1, "webhook node must reference a credential"


def test_intake_webhook_uses_response_node_mode():
    wf = _fixture()
    webhook_nodes = [
        n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"
    ]
    modes = [n["parameters"].get("responseMode", "") for n in webhook_nodes]
    assert any(m == "responseNode" for m in modes)


# ── Respond to Webhook node ───────────────────────────────────────────────────


def test_intake_workflow_has_respond_to_webhook_node():
    wf = _fixture()
    types = [n["type"] for n in wf["nodes"]]
    assert "n8n-nodes-base.respondToWebhook" in types


# ── Capture-service URLs ──────────────────────────────────────────────────────


def test_intake_all_capture_service_calls_use_internal_hostname():
    wf = _fixture()
    urls = _all_urls(wf)
    # Strip leading '=' (n8n expression prefix) for URL checks
    capture_urls = [u.lstrip("=") for u in urls if "capture-service" in u or ":8000" in u]
    assert len(capture_urls) >= 1
    for url in capture_urls:
        assert url.startswith("http://capture-service:8000"), (
            f"capture-service URL must use internal hostname, got: {url!r}"
        )


def test_intake_get_capture_url_is_correct():
    wf = _fixture()
    fixture_text = FIXTURE_PATH.read_text()
    assert "http://capture-service:8000/internal/downstream/captures/" in fixture_text


def test_intake_security_screen_url_is_correct():
    wf = _fixture()
    fixture_text = FIXTURE_PATH.read_text()
    assert "http://capture-service:8000/internal/security/screen" in fixture_text


def test_intake_validate_classification_url_is_correct():
    fixture_text = FIXTURE_PATH.read_text()
    assert "http://capture-service:8000/internal/contracts/classification/validate" in fixture_text


def test_intake_acknowledge_forwarded_url_is_correct():
    fixture_text = FIXTURE_PATH.read_text()
    assert "http://capture-service:8000/internal/downstream/acknowledge-forwarded" in fixture_text


# ── Writer-stub URLs ──────────────────────────────────────────────────────────


def test_intake_writer_stub_write_url_is_correct():
    fixture_text = FIXTURE_PATH.read_text()
    assert "http://writer-stub:8001/write" in fixture_text


def test_intake_writer_stub_inbox_url_is_correct():
    fixture_text = FIXTURE_PATH.read_text()
    assert "http://writer-stub:8001/inbox" in fixture_text


# ── Gemini URL ────────────────────────────────────────────────────────────────


def test_intake_gemini_url_uses_gemini_35_flash():
    fixture_text = FIXTURE_PATH.read_text()
    assert _GEMINI_URL_PREFIX in fixture_text


def test_intake_gemini_url_uses_https():
    wf = _fixture()
    urls = _all_urls(wf)
    gemini_urls = [u for u in urls if "generativelanguage.googleapis.com" in u]
    assert len(gemini_urls) >= 1
    for url in gemini_urls:
        assert url.startswith("https://"), f"Gemini URL must use HTTPS, got: {url!r}"


# ── Security invariants ───────────────────────────────────────────────────────


def test_intake_no_localhost_in_any_url():
    fixture_text = FIXTURE_PATH.read_text()
    assert "localhost" not in fixture_text
    assert "127.0.0.1" not in fixture_text


def test_intake_credential_ids_are_all_placeholders():
    ids = _all_credential_ids(_fixture())
    assert len(ids) >= 1
    for cred_id in ids:
        assert cred_id.startswith("PLACEHOLDER_"), (
            f"Committed credential ID must be PLACEHOLDER_*, got: {cred_id!r}"
        )


def test_intake_no_restricted_node_types():
    wf = _fixture()
    used_types = {n["type"] for n in wf["nodes"]}
    violations = used_types & _RESTRICTED_NODE_TYPES
    assert not violations, f"Restricted node types found: {violations}"


# ── Execution retention ───────────────────────────────────────────────────────


def test_intake_execution_retention_save_on_success_is_none():
    settings = _fixture().get("settings", {})
    assert settings.get("saveDataSuccessExecution") == "none"


def test_intake_execution_retention_save_on_error_is_none():
    settings = _fixture().get("settings", {})
    assert settings.get("saveDataErrorExecution") == "none"


def test_intake_execution_retention_save_manual_is_false():
    settings = _fixture().get("settings", {})
    assert settings.get("saveManualExecutions") is False


def test_intake_execution_retention_save_progress_is_false():
    settings = _fixture().get("settings", {})
    assert settings.get("saveExecutionProgress") is False


# ── Bootstrap integration ─────────────────────────────────────────────────────


def test_bootstrap_imports_intake_workflow():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "second-brain-intake.json" in bootstrap or "INTAKE_FIXTURE" in bootstrap


def test_bootstrap_checks_for_duplicate_intake_by_name():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "Second Brain - Intake" in bootstrap


def test_bootstrap_strips_ids_before_intake_import():
    assert "del(.id, .versionId)" in BOOTSTRAP_PATH.read_text()


def test_bootstrap_does_not_activate_intake():
    bootstrap = BOOTSTRAP_PATH.read_text()
    assert "--active=true" not in bootstrap
    assert "activate:workflow" not in bootstrap
