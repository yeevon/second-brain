"""Regression tests for Compose service startup ordering (SB-155)."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent.parent


def _load_override() -> dict:
    return yaml.safe_load((ROOT / "compose.override.yaml").read_text())


def test_n8n_defines_healthcheck():
    svc = _load_override()["services"]["n8n"]
    assert "healthcheck" in svc, "n8n must define a healthcheck"


def test_local_n8n_init_depends_on_n8n_healthy():
    deps = _load_override()["services"]["local-n8n-init"]["depends_on"]
    assert deps["n8n"]["condition"] == "service_healthy"


def test_capture_service_depends_on_n8n_init_completed():
    deps = _load_override()["services"]["capture-service"]["depends_on"]
    assert deps["local-n8n-init"]["condition"] == "service_completed_successfully"


def test_capture_service_depends_on_writer_service_healthy():
    deps = _load_override()["services"]["capture-service"]["depends_on"]
    assert deps["writer-service"]["condition"] == "service_healthy"


def test_writer_service_depends_on_vault_init_completed():
    deps = _load_override()["services"]["writer-service"]["depends_on"]
    assert deps["local-vault-init"]["condition"] == "service_completed_successfully"
