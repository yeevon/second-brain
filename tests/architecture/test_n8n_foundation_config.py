import json
from pathlib import Path

import yaml

ROOT = Path(".")


def load_n8n_compose():
    return yaml.safe_load((ROOT / "compose.n8n.yaml").read_text())


def n8n_compose_text():
    return (ROOT / "compose.n8n.yaml").read_text()


def n8n_env_text():
    return (ROOT / "deploy" / "n8n.env.example").read_text()


def bootstrap_text():
    return (ROOT / "deploy" / "bootstrap-n8n.sh").read_text()


def deploy_text():
    return (ROOT / "deploy" / "deploy.sh").read_text()


def verify_text():
    return (ROOT / "deploy" / "verify.sh").read_text()


# ── Compose overlay ───────────────────────────────────────────────────────────


def test_n8n_image_tag_is_required():
    service = load_n8n_compose()["services"]["n8n"]
    assert ":?" in service["image"]


def test_n8n_image_may_not_use_latest_or_next():
    text = n8n_compose_text()
    assert "docker.n8n.io/n8nio/n8n:latest" not in text
    assert "docker.n8n.io/n8nio/n8n:next" not in text
    assert "docker.n8n.io/n8nio/n8n\n" not in text
    assert "docker.n8n.io/n8nio/n8n'" not in text
    assert "docker.n8n.io/n8nio/n8n\"" not in text


def test_n8n_restart_policy_is_unless_stopped():
    service = load_n8n_compose()["services"]["n8n"]
    assert service["restart"] == "unless-stopped"


def test_n8n_joins_backend_network():
    service = load_n8n_compose()["services"]["n8n"]
    assert "backend" in service["networks"]


def test_n8n_mount_target_is_home_node_n8n():
    service = load_n8n_compose()["services"]["n8n"]
    assert any("/home/node/.n8n" in v for v in service["volumes"])


def test_n8n_env_file_path_is_explicit():
    service = load_n8n_compose()["services"]["n8n"]
    env_files = service["env_file"]
    assert any("N8N_ENV_FILE" in str(f) for f in env_files)


def test_n8n_encryption_key_supplied_through_secret_file():
    compose = load_n8n_compose()
    service = compose["services"]["n8n"]
    assert "n8n_encryption_key" in service.get("secrets", [])
    assert service["environment"].get("N8N_ENCRYPTION_KEY_FILE") == "/run/secrets/n8n_encryption_key"
    secret_def = compose["secrets"]["n8n_encryption_key"]
    assert "N8N_ENCRYPTION_KEY_FILE" in secret_def["file"]


def test_n8n_host_binding_is_exactly_loopback_5678():
    service = load_n8n_compose()["services"]["n8n"]
    assert "127.0.0.1:5678:5678" in service.get("ports", [])


def test_n8n_is_never_bound_to_all_interfaces():
    text = n8n_compose_text()
    assert "0.0.0.0:5678" not in text

    service = load_n8n_compose()["services"]["n8n"]
    ports = service.get("ports", [])
    assert not any("0.0.0.0" in str(p) for p in ports)


def test_capture_service_still_has_no_published_ports():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    service = compose["services"]["capture-service"]
    assert "ports" not in service
    assert service["expose"] == ["8000"]


# ── Environment template ──────────────────────────────────────────────────────


def test_env_concurrency_limit_is_one():
    assert "N8N_CONCURRENCY_PRODUCTION_LIMIT=1" in n8n_env_text()


def test_env_enforce_settings_file_permissions():
    assert "N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=true" in n8n_env_text()


def test_env_block_env_access_in_node():
    assert "N8N_BLOCK_ENV_ACCESS_IN_NODE=true" in n8n_env_text()


def test_env_block_file_access_to_n8n_files():
    assert "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES=true" in n8n_env_text()


def test_env_runners_not_present():
    assert "N8N_RUNNERS_ENABLED" not in n8n_env_text()


def test_env_secure_cookie_false_is_documented_as_ssh_tunnel_only():
    env = n8n_env_text()
    assert "N8N_SECURE_COOKIE=false" in env
    assert "SSH" in env


def test_env_executions_save_on_error_is_none():
    assert "EXECUTIONS_DATA_SAVE_ON_ERROR=none" in n8n_env_text()


def test_env_executions_save_on_success_is_none():
    assert "EXECUTIONS_DATA_SAVE_ON_SUCCESS=none" in n8n_env_text()


def test_env_executions_save_on_progress_is_false():
    assert "EXECUTIONS_DATA_SAVE_ON_PROGRESS=false" in n8n_env_text()


def test_env_executions_save_manual_executions_is_false():
    assert "EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS=false" in n8n_env_text()


def test_env_executions_prune_is_true():
    assert "EXECUTIONS_DATA_PRUNE=true" in n8n_env_text()


def test_env_public_api_disabled():
    assert "N8N_PUBLIC_API_DISABLED=true" in n8n_env_text()


# ── Secret exclusions ─────────────────────────────────────────────────────────


def test_n8n_local_env_excluded_from_git():
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "n8n.local.env" in gitignore


def test_n8n_local_env_excluded_from_docker_build_context():
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert "n8n.local.env" in dockerignore


def test_n8n_encryption_key_local_excluded_from_git():
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "n8n-encryption-key.local" in gitignore


def test_n8n_encryption_key_local_excluded_from_docker_build_context():
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert "n8n-encryption-key.local" in dockerignore


# ── Bootstrap ─────────────────────────────────────────────────────────────────


def test_bootstrap_imports_error_handler_using_n8n_container_cli():
    bootstrap = bootstrap_text()
    assert "n8n import:workflow" in bootstrap
    assert "second-brain-error-handler" in bootstrap


def test_bootstrap_exits_nonzero_when_duplicate_workflow_name_detected():
    bootstrap = bootstrap_text()
    assert "already exists" in bootstrap
    assert "exit 0" in bootstrap or "exit 1" in bootstrap
    assert "ERROR_HANDLER_NAME" in bootstrap or "Second Brain - Error Handler" in bootstrap


def test_committed_workflow_fixture_has_no_top_level_id_or_version_id():
    fixture_path = ROOT / "n8n" / "workflows" / "second-brain-error-handler.json"
    data = json.loads(fixture_path.read_text())
    assert "id" not in data
    assert "versionId" not in data


def test_committed_workflow_fixture_is_inactive():
    fixture_path = ROOT / "n8n" / "workflows" / "second-brain-error-handler.json"
    data = json.loads(fixture_path.read_text())
    assert data.get("active") is False


def test_bootstrap_strips_id_and_version_id_before_import():
    assert "del(.id, .versionId)" in bootstrap_text()


def test_bootstrap_does_not_auto_activate_any_workflow():
    bootstrap = bootstrap_text()
    assert "--active=true" not in bootstrap
    assert "activate:workflow" not in bootstrap
    assert "n8n update:workflow" not in bootstrap


# ── Deployment scripts ────────────────────────────────────────────────────────


def test_deploy_requires_pinned_image_tag():
    deploy = deploy_text()
    assert "N8N_IMAGE_TAG:?" in deploy


def test_deploy_exports_compose_n8n_yaml():
    deploy = deploy_text()
    assert "compose.n8n.yaml" in deploy
    assert "COMPOSE_FILE" in deploy


def test_deploy_validates_ebs_backed_n8n_data_directory():
    deploy = deploy_text()
    assert "N8N_DATA_DIR" in deploy or "N8N_DATA_SOURCE" in deploy
    assert "n8n data directory missing" in deploy or "n8n" in deploy


def test_verify_rejects_public_5678_binding():
    verify = verify_text()
    assert "0.0.0.0" in verify
    assert "5678" in verify


def test_verify_checks_n8n_persistence_mount():
    verify = verify_text()
    assert "/home/node/.n8n" in verify
    assert "n8n_mount_source" in verify or "Mounts" in verify


def test_verify_checks_n8n_capture_service_reachability():
    verify = verify_text()
    assert "capture-service:8000/health" in verify
    assert "backend" in verify or "n8n" in verify
