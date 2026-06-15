"""Architecture tests for writer-service compose configuration (SB-115, SB-116)."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(".")
COMPOSE_N8N = ROOT / "compose.n8n.yaml"
COMPOSE_OVERRIDE = ROOT / "compose.override.yaml"
INTAKE_FIXTURE = ROOT / "n8n" / "workflows" / "second-brain-intake.json"
ERROR_HANDLER_FIXTURE = ROOT / "n8n" / "workflows" / "second-brain-error-handler.json"
WRITER_SRC = ROOT / "writer-service" / "src" / "writerservice"
VERIFY_SH = ROOT / "deploy" / "verify.sh"
GITIGNORE = ROOT / ".gitignore"
DOCKERIGNORE = ROOT / ".dockerignore"


def _n8n_compose():
    return yaml.safe_load(COMPOSE_N8N.read_text())


def _override_compose():
    return yaml.safe_load(COMPOSE_OVERRIDE.read_text())


def _intake():
    return json.loads(INTAKE_FIXTURE.read_text())


def _error_handler():
    return json.loads(ERROR_HANDLER_FIXTURE.read_text())


# ── writer-service image ──────────────────────────────────────────────────────


def test_writer_service_uses_build_not_latest_image():
    svc = _n8n_compose()["services"]["writer-service"]
    assert "build" in svc
    assert svc.get("image", "build") != "latest"


# ── Network isolation ─────────────────────────────────────────────────────────


def test_writer_service_joins_backend_network_only():
    svc = _n8n_compose()["services"]["writer-service"]
    networks = svc.get("networks", [])
    assert networks == ["backend"]


def test_writer_service_has_no_published_host_ports():
    svc = _n8n_compose()["services"]["writer-service"]
    assert "ports" not in svc


# ── Vault volume ──────────────────────────────────────────────────────────────


def test_writer_service_mounts_vault_volume():
    svc = _n8n_compose()["services"]["writer-service"]
    volumes = svc.get("volumes", [])
    assert any("/opt/vault" in str(v) for v in volumes)


def test_override_defines_second_brain_local_vault_volume():
    compose = _override_compose()
    volumes = compose.get("volumes", {})
    assert "second-brain-local-vault" in volumes


# ── Security hardening ────────────────────────────────────────────────────────


def test_writer_service_uses_cap_drop_all():
    svc = _n8n_compose()["services"]["writer-service"]
    assert svc.get("cap_drop") == ["ALL"]


def test_writer_service_uses_no_new_privileges():
    svc = _n8n_compose()["services"]["writer-service"]
    assert "no-new-privileges:true" in svc.get("security_opt", [])


# ── writer-stub removed ───────────────────────────────────────────────────────


def test_compose_n8n_does_not_contain_writer_stub():
    content = COMPOSE_N8N.read_text()
    assert "writer-stub" not in content


def test_compose_override_does_not_define_writer_stub():
    content = COMPOSE_OVERRIDE.read_text()
    assert "writer-stub" not in content


# ── SB-115: Git vault sync compose config ────────────────────────────────────


def test_compose_n8n_vault_volume_is_bind_mount():
    svc = _n8n_compose()["services"]["writer-service"]
    volumes = svc.get("volumes", [])
    assert any("/opt/vault" in str(v) for v in volumes)
    # Bind mount uses a variable path (not a bare named-volume reference)
    vault_vol = next(str(v) for v in volumes if "/opt/vault" in str(v))
    assert "${WRITER_VAULT_SOURCE" in vault_vol


def test_compose_n8n_vault_deploy_key_secret_defined():
    compose = _n8n_compose()
    secrets = compose.get("secrets", {})
    assert "vault_deploy_key" in secrets


def test_compose_n8n_github_known_hosts_secret_defined():
    compose = _n8n_compose()
    secrets = compose.get("secrets", {})
    assert "github_known_hosts" in secrets


def test_writer_service_git_ssh_command_contains_strict_host_checking_yes():
    svc = _n8n_compose()["services"]["writer-service"]
    env = svc.get("environment", {})
    git_ssh = str(env.get("GIT_SSH_COMMAND", ""))
    assert "StrictHostKeyChecking=yes" in git_ssh


def test_writer_service_git_ssh_command_does_not_contain_strict_host_checking_no():
    svc = _n8n_compose()["services"]["writer-service"]
    env = svc.get("environment", {})
    git_ssh = str(env.get("GIT_SSH_COMMAND", ""))
    assert "StrictHostKeyChecking=no" not in git_ssh


def test_writer_service_git_ssh_command_contains_known_hosts_file():
    svc = _n8n_compose()["services"]["writer-service"]
    env = svc.get("environment", {})
    git_ssh = str(env.get("GIT_SSH_COMMAND", ""))
    assert "UserKnownHostsFile=/run/secrets/github_known_hosts" in git_ssh


def test_gitignore_contains_writer_lock():
    assert ".writer.lock" in GITIGNORE.read_text()


def test_gitignore_contains_vault_deploy_key():
    assert "vault-deploy-key" in GITIGNORE.read_text()


def test_dockerignore_contains_vault_deploy_key():
    assert "vault-deploy-key" in DOCKERIGNORE.read_text()


def test_verify_sh_checks_vault_remote_url():
    verify = VERIFY_SH.read_text()
    assert "VAULT_REMOTE" in verify or "vault remote" in verify.lower()


def test_verify_sh_checks_deploy_key_permissions():
    verify = VERIFY_SH.read_text()
    assert "vault-deploy-key" in verify or "DEPLOY_KEY" in verify or "deploy_key" in verify


def test_verify_sh_checks_github_known_hosts_file():
    verify = VERIFY_SH.read_text()
    assert "github_known_hosts" in verify or "known_hosts" in verify.lower()


# ── SB-116: Git error hierarchy ──────────────────────────────────────────────


def _load_git_errors():
    spec_path = str(WRITER_SRC)
    if spec_path not in sys.path:
        sys.path.insert(0, str(ROOT / "writer-service" / "src"))
    import writerservice.git_errors as ge
    return ge


def test_git_errors_defines_git_merge_conflict_error():
    ge = _load_git_errors()
    assert hasattr(ge, "GitMergeConflictError")


def test_git_errors_defines_git_push_rejected_error():
    ge = _load_git_errors()
    assert hasattr(ge, "GitPushRejectedError")


def test_git_errors_defines_git_index_locked_error():
    ge = _load_git_errors()
    assert hasattr(ge, "GitIndexLockedError")


def test_git_errors_defines_git_workdir_dirty_error():
    ge = _load_git_errors()
    assert hasattr(ge, "GitWorkdirDirtyError")


def test_git_errors_defines_capture_duplicate_error():
    ge = _load_git_errors()
    assert hasattr(ge, "CaptureDuplicateError")


def test_git_errors_defines_path_traversal_error():
    ge = _load_git_errors()
    assert hasattr(ge, "PathTraversalError")


def test_all_writer_errors_have_error_type_http_status_retryable():
    ge = _load_git_errors()
    for name in [
        "GitMergeConflictError", "GitPushRejectedError", "GitIndexLockedError",
        "GitWorkdirDirtyError", "CaptureDuplicateError", "PathTraversalError",
    ]:
        cls = getattr(ge, name)
        assert hasattr(cls, "error_type"), f"{name} missing error_type"
        assert hasattr(cls, "http_status"), f"{name} missing http_status"
        assert hasattr(cls, "retryable"), f"{name} missing retryable"
        assert issubclass(cls, ge.WriterError)


def test_git_merge_conflict_error_not_retryable():
    ge = _load_git_errors()
    assert ge.GitMergeConflictError.retryable is False


def test_git_workdir_dirty_error_not_retryable():
    ge = _load_git_errors()
    assert ge.GitWorkdirDirtyError.retryable is False


# ── SB-116: downstream_errors.py ────────────────────────────────────────────


def _load_downstream_errors():
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    import secondbrain.downstream_errors as de
    return de


def test_retryable_errors_contains_writer_git_push_rejected():
    de = _load_downstream_errors()
    assert "writer_git_push_rejected" in de.RETRYABLE_DOWNSTREAM_ERRORS


def test_retryable_errors_contains_writer_git_index_locked():
    de = _load_downstream_errors()
    assert "writer_git_index_locked" in de.RETRYABLE_DOWNSTREAM_ERRORS


def test_terminal_errors_contains_writer_git_conflict():
    de = _load_downstream_errors()
    assert "writer_git_conflict" in de.TERMINAL_DOWNSTREAM_ERRORS


def test_terminal_errors_contains_writer_git_worktree_dirty():
    de = _load_downstream_errors()
    assert "writer_git_worktree_dirty" in de.TERMINAL_DOWNSTREAM_ERRORS


def test_terminal_errors_contains_writer_path_traversal():
    de = _load_downstream_errors()
    assert "writer_path_traversal" in de.TERMINAL_DOWNSTREAM_ERRORS


def test_terminal_errors_contains_writer_capture_duplicate():
    de = _load_downstream_errors()
    assert "writer_capture_duplicate" in de.TERMINAL_DOWNSTREAM_ERRORS


def test_allowed_stages_contains_writer_service():
    de = _load_downstream_errors()
    assert "writer_service" in de.ALLOWED_STAGES


def test_allowed_stages_does_not_contain_writer_stub():
    de = _load_downstream_errors()
    assert "writer_stub" not in de.ALLOWED_STAGES


# ── SB-116: n8n error handler workflow ──────────────────────────────────────


def _error_handler_js(node_name: str) -> str:
    wf = _error_handler()
    for node in wf["nodes"]:
        if node["name"] == node_name:
            return node["parameters"].get("jsCode", "")
    return ""


def test_error_handler_maps_submit_to_writer_service_stage():
    js = _error_handler_js("Normalize Safe Error Metadata")
    assert "Submit to Writer Service" in js
    assert "writer_service" in js


def test_error_handler_retryable_set_contains_writer_git_push_rejected():
    js = _error_handler_js("Extract Safe Correlation Context")
    assert "writer_git_push_rejected" in js


def test_error_handler_retryable_set_contains_writer_git_index_locked():
    js = _error_handler_js("Extract Safe Correlation Context")
    assert "writer_git_index_locked" in js


def test_error_handler_terminal_set_contains_writer_git_conflict():
    js = _error_handler_js("Extract Safe Correlation Context")
    assert "writer_git_conflict" in js


def test_error_handler_terminal_set_contains_writer_git_worktree_dirty():
    js = _error_handler_js("Extract Safe Correlation Context")
    assert "writer_git_worktree_dirty" in js


def test_error_handler_allowed_stages_contains_writer_service():
    js = _error_handler_js("Extract Safe Correlation Context")
    assert "writer_service" in js


# ── SB-116: n8n intake workflow error routing ─────────────────────────────────


def _writer_nodes():
    wf = _intake()
    return [
        n for n in wf["nodes"]
        if n["name"] in ("Submit to Writer Service", "Submit to Writer Service (inbox)")
    ]


def test_intake_writer_service_nodes_have_never_error():
    for node in _writer_nodes():
        opts = node["parameters"].get("options", {})
        inner = opts.get("response", {}).get("response", {})
        assert inner.get("neverError") is True, (
            f"Node {node['name']!r} missing neverError: true"
        )


def test_intake_writer_service_nodes_have_full_response():
    for node in _writer_nodes():
        opts = node["parameters"].get("options", {})
        inner = opts.get("response", {}).get("response", {})
        assert inner.get("fullResponse") is True, (
            f"Node {node['name']!r} missing fullResponse: true"
        )


def _intake_conns():
    return _intake().get("connections", {})


def test_intake_writer_service_routes_to_evaluate_node():
    conns = _intake_conns()
    file_targets = [c["node"] for c in conns.get("Submit to Writer Service", {}).get("main", [[]])[0]]
    assert "Evaluate Writer Response" in file_targets

    inbox_targets = [c["node"] for c in conns.get("Submit to Writer Service (inbox)", {}).get("main", [[]])[0]]
    assert "Evaluate Writer Response (inbox)" in inbox_targets


def test_intake_routes_git_push_rejected_to_schedule_retry():
    conns = _intake_conns()
    # Writer Error Retryable? output 0 (true) → Schedule Retry (writer)
    retryable_conns = conns.get("Writer Error Retryable?", {}).get("main", [])
    retry_targets = [c["node"] for c in retryable_conns[0]] if retryable_conns else []
    assert any("Schedule Retry" in t for t in retry_targets)


def test_intake_routes_git_index_locked_to_schedule_retry():
    # Both git_push_rejected and git_index_locked are retryable → same Schedule Retry node
    conns = _intake_conns()
    retryable_conns = conns.get("Writer Error Retryable?", {}).get("main", [])
    retry_targets = [c["node"] for c in retryable_conns[0]] if retryable_conns else []
    assert any("Schedule Retry" in t for t in retry_targets)


def test_intake_routes_git_merge_conflict_to_acknowledge_failed():
    conns = _intake_conns()
    # Writer Error Retryable? output 1 (false) → Acknowledge Failed (writer)
    retryable_conns = conns.get("Writer Error Retryable?", {}).get("main", [])
    terminal_targets = [c["node"] for c in retryable_conns[1]] if len(retryable_conns) > 1 else []
    assert any("Acknowledge Failed" in t for t in terminal_targets)


def test_intake_routes_git_worktree_dirty_to_acknowledge_failed():
    conns = _intake_conns()
    retryable_conns = conns.get("Writer Error Retryable?", {}).get("main", [])
    terminal_targets = [c["node"] for c in retryable_conns[1]] if len(retryable_conns) > 1 else []
    assert any("Acknowledge Failed" in t for t in terminal_targets)


def test_intake_routes_path_traversal_to_acknowledge_failed():
    conns = _intake_conns()
    retryable_conns = conns.get("Writer Error Retryable?", {}).get("main", [])
    terminal_targets = [c["node"] for c in retryable_conns[1]] if len(retryable_conns) > 1 else []
    assert any("Acknowledge Failed" in t for t in terminal_targets)


def test_intake_routes_capture_id_duplicate_to_acknowledge_failed():
    conns = _intake_conns()
    retryable_conns = conns.get("Writer Error Retryable?", {}).get("main", [])
    terminal_targets = [c["node"] for c in retryable_conns[1]] if len(retryable_conns) > 1 else []
    assert any("Acknowledge Failed" in t for t in terminal_targets)


# ── SB-116: complete retryable taxonomy ─────────────────────────────────────


def test_retryable_errors_contains_writer_git_fetch_error():
    de = _load_downstream_errors()
    assert "writer_git_fetch_error" in de.RETRYABLE_DOWNSTREAM_ERRORS


def test_retryable_errors_contains_writer_git_add_error():
    de = _load_downstream_errors()
    assert "writer_git_add_error" in de.RETRYABLE_DOWNSTREAM_ERRORS


def test_retryable_errors_contains_writer_git_commit_error():
    de = _load_downstream_errors()
    assert "writer_git_commit_error" in de.RETRYABLE_DOWNSTREAM_ERRORS


def test_retryable_errors_contains_writer_git_push_error():
    de = _load_downstream_errors()
    assert "writer_git_push_error" in de.RETRYABLE_DOWNSTREAM_ERRORS


# ── SB-116: Evaluate Writer Response uses body.retryable from response ────────


def test_evaluate_writer_response_uses_body_retryable():
    """Retryability is derived from the writer-service response, not a hardcoded set."""
    wf = _intake()
    for node in wf["nodes"]:
        if node["name"] in ("Evaluate Writer Response", "Evaluate Writer Response (inbox)"):
            js = node["parameters"].get("jsCode", "")
            assert "body.retryable === true" in js, (
                f"Node {node['name']!r} must use body.retryable from writer response"
            )


# ── SB-116: verify.sh checks vault .gitignore ───────────────────────────────


def test_verify_sh_checks_vault_gitignore_contains_writer_lock():
    verify = VERIFY_SH.read_text()
    assert ".writer.lock" in verify


# ── SB-116: path traversal contract ─────────────────────────────────────────


def test_git_errors_defines_path_traversal_error():
    ge = _load_git_errors()
    assert hasattr(ge, "PathTraversalError")


def test_path_traversal_error_is_422_not_retryable():
    ge = _load_git_errors()
    assert ge.PathTraversalError.http_status == 422
    assert ge.PathTraversalError.retryable is False


# ── local-vault-init compose contract (SB-115) ───────────────────────────────


def test_override_defines_local_vault_init_service():
    compose = _override_compose()
    assert "local-vault-init" in compose.get("services", {})


def test_override_defines_local_vault_remote_volume():
    compose = _override_compose()
    volumes = compose.get("volumes", {})
    assert "second-brain-local-vault-remote" in volumes


def test_override_writer_service_depends_on_local_vault_init():
    svc = _override_compose()["services"]["writer-service"]
    depends_on = svc.get("depends_on", {})
    assert "local-vault-init" in depends_on
    assert depends_on["local-vault-init"].get("condition") == "service_completed_successfully"


def test_override_writer_service_defaults_git_sync_enabled_true():
    svc = _override_compose()["services"]["writer-service"]
    env = svc.get("environment", {})
    assert env.get("GIT_SYNC_ENABLED") == "${GIT_SYNC_ENABLED:-true}"


def test_override_writer_service_mounts_local_vault_remote():
    svc = _override_compose()["services"]["writer-service"]
    volumes = [str(v) for v in svc.get("volumes", [])]
    assert any("second-brain-local-vault-remote:/remote" in v for v in volumes)


def test_override_local_vault_init_bootstraps_git_contract():
    svc = _override_compose()["services"]["local-vault-init"]
    command = str(svc.get("command", ""))

    assert "git init --bare -b main /remote/repo.git" in command
    assert "git -C /vault init -b main" in command
    assert ".writer.lock" in command
    assert "remote add origin /remote/repo.git" in command
    assert "remote set-url origin /remote/repo.git" in command
    assert "chown -R 10003:10003 /vault /remote" in command


def test_override_local_vault_init_does_not_auto_stage_entire_vault():
    svc = _override_compose()["services"]["local-vault-init"]
    command = str(svc.get("command", ""))
    assert "git -C /vault add -A" not in command
    assert "git -C /vault add .gitignore 99_log/.gitkeep" in command


def test_override_local_vault_init_self_verifies_git_repo():
    svc = _override_compose()["services"]["local-vault-init"]
    command = str(svc.get("command", ""))

    assert "test -d /vault/.git" in command
    assert "git -C /vault rev-parse --is-inside-work-tree" in command
    assert 'grep -qxF "/remote/repo.git"' in command
    assert 'grep -qxF ".writer.lock" /vault/.gitignore' in command


def test_override_writer_service_healthcheck_verifies_git_vault():
    svc = _override_compose()["services"]["writer-service"]
    healthcheck = str(svc.get("healthcheck", {}))

    assert "git -C /opt/vault rev-parse --is-inside-work-tree" in healthcheck
    assert "git -C /opt/vault remote get-url origin" in healthcheck
    assert ".writer.lock" in healthcheck


# ── local-n8n-init compose contract ──────────────────────────────────────────

N8N_INIT_SCRIPT = ROOT / "deploy" / "local-n8n-init.py"


def _n8n_init_script() -> str:
    return N8N_INIT_SCRIPT.read_text()


def test_override_defines_local_n8n_init_service():
    compose = _override_compose()
    assert "local-n8n-init" in compose.get("services", {})


def test_override_local_n8n_init_depends_on_n8n_healthy():
    svc = _override_compose()["services"]["local-n8n-init"]
    depends = svc.get("depends_on", {})
    assert "n8n" in depends
    assert depends["n8n"].get("condition") == "service_healthy"


def test_override_local_n8n_init_mounts_workflows_readonly():
    svc = _override_compose()["services"]["local-n8n-init"]
    vols = [str(v) for v in svc.get("volumes", [])]
    assert any("n8n/workflows" in v and "ro" in v for v in vols)


def test_override_local_n8n_init_mounts_init_script_readonly():
    svc = _override_compose()["services"]["local-n8n-init"]
    vols = [str(v) for v in svc.get("volumes", [])]
    assert any("local-n8n-init.py" in v and "ro" in v for v in vols)


def test_local_n8n_init_creates_all_required_credentials():
    script = _n8n_init_script()
    assert "Capture Service Token" in script
    assert "Second Brain - Writer Service Header" in script
    assert "Intake Webhook Token" in script
    assert "Gemini API Key" in script


def test_local_n8n_init_patches_all_placeholder_ids():
    script = _n8n_init_script()
    assert "PLACEHOLDER_CAPTURE_SERVICE_TOKEN" in script
    assert "PLACEHOLDER_WRITER_SERVICE_TOKEN" in script
    assert "PLACEHOLDER_INTAKE_WEBHOOK_TOKEN" in script
    assert "PLACEHOLDER_GEMINI_API_KEY" in script
    assert "PLACEHOLDER_SECOND_BRAIN_ERROR_HANDLER" in script


def test_local_n8n_init_activates_intake_workflow():
    script = _n8n_init_script()
    assert "/activate" in script
    assert "Intake" in script


def test_local_n8n_init_activates_error_handler_before_intake():
    script = _n8n_init_script()
    assert "Activating Error Handler workflow" in script
    assert "activate_workflow(eh_id)" in script
    assert script.index("activate_workflow(eh_id)") < script.index("activate_workflow(intake_wf_id)")


def test_local_n8n_init_verifies_webhook_registration():
    script = _n8n_init_script()
    assert "second-brain-intake" in script
    assert "404" in script


def test_override_local_n8n_init_requires_gemini_api_key():
    svc = _override_compose()["services"]["local-n8n-init"]
    assert svc["environment"]["GEMINI_API_KEY"] == "${GEMINI_API_KEY:?GEMINI_API_KEY is required for local-n8n-init}"


def test_local_n8n_init_fails_on_webhook_auth_errors():
    script = _n8n_init_script()
    assert "401" in script
    assert "403" in script
    assert "credential binding or Intake token configuration is broken" in script


def test_n8n_healthcheck_probes_rest_login_not_root():
    """Healthcheck must wait for REST readiness, not just the web server.

    A 400 from POST /rest/login (missing body fields) proves the REST API is
    up. A 200 from / only proves the static-file server is answering.
    """
    svc = _override_compose()["services"]["n8n"]
    healthcheck_cmd = " ".join(str(p) for p in svc["healthcheck"]["test"])
    assert "/rest/login" in healthcheck_cmd, (
        "n8n healthcheck must probe /rest/login, not /"
    )
    assert "status === 400" in healthcheck_cmd or "status==400" in healthcheck_cmd, (
        "n8n healthcheck must accept HTTP 400 (REST ready) as the passing signal"
    )


def test_override_capture_service_waits_for_n8n_init():
    """capture-service must not start until local-n8n-init completes.

    Prevents Discord messages being captured before the intake webhook exists.
    """
    svc = _override_compose()["services"]["capture-service"]
    depends = svc.get("depends_on", {})
    assert "local-n8n-init" in depends, (
        "capture-service must depend on local-n8n-init"
    )
    assert depends["local-n8n-init"].get("condition") == "service_completed_successfully", (
        "capture-service must wait for local-n8n-init to complete successfully"
    )


def test_override_capture_service_waits_for_writer_service():
    """capture-service must not start until writer-service is healthy."""
    svc = _override_compose()["services"]["capture-service"]
    depends = svc.get("depends_on", {})
    assert "writer-service" in depends, (
        "capture-service must depend on writer-service"
    )
    assert depends["writer-service"].get("condition") == "service_healthy", (
        "capture-service must wait for writer-service to be healthy"
    )


def test_local_n8n_init_setup_owner_rejects_404():
    """setup_owner must treat 404 as 'REST not ready', not 'already configured'."""
    script = _n8n_init_script()
    assert "404" in script
    assert "REST API not ready" in script or "not ready" in script.lower(), (
        "setup_owner must raise on 404, not silently treat it as already-configured"
    )
    assert "assuming already configured" not in script, (
        "loose 'assuming already configured' fallback must be removed from setup_owner"
    )
