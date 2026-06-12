import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(".")


def load_compose():
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_compose_does_not_publish_ports():
    service = load_compose()["services"]["capture-service"]

    assert "ports" not in service
    assert service["expose"] == ["8000"]


def test_compose_uses_restart_policy_and_persistent_ledger_mount():
    service = load_compose()["services"]["capture-service"]

    assert service["restart"] == "unless-stopped"
    assert any("/var/lib/second-brain" in v for v in service["volumes"])


def test_compose_defines_internal_healthcheck():
    service = load_compose()["services"]["capture-service"]
    command = " ".join(service["healthcheck"]["test"])

    assert "http://127.0.0.1:8000/health" in command


def test_compose_uses_non_internal_backend_bridge_network():
    compose = load_compose()

    assert compose["services"]["capture-service"]["networks"] == ["backend"]
    assert compose["networks"]["backend"]["driver"] == "bridge"
    assert compose["networks"]["backend"].get("internal") is not True


def test_compose_drops_capabilities_and_enables_no_new_privileges():
    service = load_compose()["services"]["capture-service"]

    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["read_only"] is True
    assert "/tmp" in service["tmpfs"]


def test_compose_config_does_not_contain_real_secrets():
    compose = load_compose()
    serialized = repr(compose)

    assert "DISCORD_BOT_TOKEN=" not in serialized
    assert "CAPTURE_SERVICE_INTERNAL_TOKEN=" not in serialized

    env_files = compose["services"]["capture-service"]["env_file"]

    assert env_files == [
        "${CAPTURE_SERVICE_ENV_FILE:-.env}"
    ]


def test_compose_enforces_capture_only_container_runtime():
    service = load_compose()["services"]["capture-service"]

    assert service["environment"] == {
        "CAPTURE_PROCESSING_MODE": "capture-only",
        "CAPTURE_API_HOST": "0.0.0.0",
        "CAPTURE_API_PORT": "8000",
        "LEDGER_PATH": "/var/lib/second-brain/ledger.sqlite3",
    }

    assert service["volumes"] == [
        "${CAPTURE_DATA_SOURCE:-./second-brain-local-data}:/var/lib/second-brain"
    ]


def test_dockerfile_runs_as_non_root_user():
    dockerfile = (ROOT / "Dockerfile").read_text().splitlines()

    assert any(line == "USER secondbrain" for line in dockerfile)
    assert any("useradd --uid 10001" in line for line in dockerfile)


def test_dockerfile_does_not_copy_env_or_runtime_sqlite_files():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "COPY .env" not in dockerfile
    assert "ledger.sqlite3" not in dockerfile
    assert ".runtime" not in dockerfile


def test_dockerignore_excludes_secrets_runtime_and_tests():
    ignored = set((ROOT / ".dockerignore").read_text().splitlines())

    assert ".env" in ignored
    assert ".env.*" in ignored
    assert "deploy/*.env" in ignored
    assert "capture-service.local.env" in ignored
    assert ".runtime" in ignored
    assert "tests" in ignored
    assert "docs" in ignored
    assert "!.env.example" in ignored


def test_deployment_env_example_is_capture_only_without_gemini_or_vault():
    env_example = (ROOT / "deploy" / "capture-service.env.example").read_text()

    assert "CAPTURE_PROCESSING_MODE=capture-only" in env_example
    assert "LEDGER_PATH=/var/lib/second-brain/ledger.sqlite3" in env_example
    assert "GEMINI_API_KEY" not in env_example
    assert "VAULT_PATH" not in env_example


def test_deploy_script_refuses_unmounted_data_directory():
    deploy = (ROOT / "deploy" / "deploy.sh").read_text()

    assert "mountpoint -q" in deploy
    assert 'DATA_DIR' in deploy


def test_verify_script_refuses_unmounted_data_directory():
    verify = (ROOT / "deploy" / "verify.sh").read_text()

    assert "mountpoint -q" in verify
    assert 'DATA_DIR' in verify


def test_verify_script_checks_expected_ledger_bind_mount():
    verify = (ROOT / "deploy" / "verify.sh").read_text()

    assert "/var/lib/second-brain" in verify
    assert "mount_source" in verify
    assert "Mounts" in verify


def test_verify_script_checks_persistent_ledger_file():
    verify = (ROOT / "deploy" / "verify.sh").read_text()

    assert "ledger.sqlite3" in verify
    assert "-f " in verify


def test_container_entrypoint_refuses_missing_ebs_marker(tmp_path):
    result = subprocess.run(
        ["sh", str(ROOT / "deploy" / "container-entrypoint.sh"), "true"],
        env={**os.environ, "SECOND_BRAIN_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "persistent EBS volume marker missing" in result.stderr


def test_container_entrypoint_runs_command_when_ebs_marker_exists(tmp_path):
    (tmp_path / ".second-brain-ebs-volume").touch()

    result = subprocess.run(
        ["sh", str(ROOT / "deploy" / "container-entrypoint.sh"), "true"],
        env={**os.environ, "SECOND_BRAIN_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


def test_dockerfile_uses_persistent_volume_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "secondbrain-entrypoint" in dockerfile
    assert "ENTRYPOINT" in dockerfile
    assert "container-entrypoint.sh" in dockerfile


def test_deploy_script_checks_ebs_marker():
    deploy = (ROOT / "deploy" / "deploy.sh").read_text()

    assert ".second-brain-ebs-volume" in deploy
    assert "MARKER" in deploy


def test_verify_script_checks_ebs_marker():
    verify = (ROOT / "deploy" / "verify.sh").read_text()

    assert ".second-brain-ebs-volume" in verify
    assert "MARKER" in verify
