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
    assert "/opt/second-brain/data:/var/lib/second-brain" in service["volumes"]


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
    assert compose["services"]["capture-service"]["env_file"] == [
        "/opt/second-brain/config/capture-service.env"
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
