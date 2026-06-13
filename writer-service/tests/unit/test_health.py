"""Health endpoint tests for writer-service."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from writerservice.main import app

CLIENT = TestClient(app, raise_server_exceptions=False)


def test_health_returns_200_when_vault_writable(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    resp = CLIENT.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_returns_503_when_vault_not_writable(tmp_path, monkeypatch):
    vault = tmp_path / "nonexistent_vault"
    monkeypatch.setenv("VAULT_PATH", str(vault))
    resp = CLIENT.get("/health")
    assert resp.status_code == 503
