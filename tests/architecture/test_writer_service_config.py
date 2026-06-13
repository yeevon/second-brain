"""Architecture tests for writer-service compose configuration."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(".")
COMPOSE_N8N = ROOT / "compose.n8n.yaml"
COMPOSE_OVERRIDE = ROOT / "compose.override.yaml"
INTAKE_FIXTURE = ROOT / "n8n" / "workflows" / "second-brain-intake.json"


def _n8n_compose():
    return yaml.safe_load(COMPOSE_N8N.read_text())


def _override_compose():
    return yaml.safe_load(COMPOSE_OVERRIDE.read_text())


def _intake():
    return json.loads(INTAKE_FIXTURE.read_text())


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
