from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def writer_service_env(tmp_path, monkeypatch):
    """Provide required env vars for all writer-service tests."""
    monkeypatch.setenv("WRITER_SERVICE_TOKEN", "test-token-abc123")
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "vault" / "99_log" / "events.ndjson"))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "false")
    # Reset cached settings between tests
    import writerservice.config as cfg
    cfg._settings = None
    yield
    cfg._settings = None
