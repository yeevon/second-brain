"""Tests for the expanded /internal/vault/stats/open-tasks endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from writerservice.main import app

CLIENT = TestClient(app, raise_server_exceptions=False)

_WRITER_TOKEN = "test-token"
_AUTH = {"X-Second-Brain-Writer-Token": _WRITER_TOKEN}


def _make_vault(tmp_path, notes: list[str]) -> str:
    vault = tmp_path / "vault"
    vault.mkdir()
    for i, content in enumerate(notes):
        (vault / f"note_{i}.md").write_text(content, encoding="utf-8")
    return str(vault)


_NOTE_WITH_OPEN_TASK_PROJECT_A = """\
---
capture_id: "SB-20260615-0001"
project: "project-a"
actions:
  - text: "Do the thing"
    status: "open"
---
body text
"""

_NOTE_WITH_OPEN_TASK_PROJECT_A_2 = """\
---
capture_id: "SB-20260615-0002"
project: "project-a"
actions:
  - text: "Another thing"
    status: "open"
---
body text
"""

_NOTE_WITH_OPEN_TASK_PROJECT_B = """\
---
capture_id: "SB-20260615-0003"
project: "project-b"
actions:
  - text: "Task in B"
    status: "open"
---
body text
"""

_NOTE_WITH_OPEN_TASK_NO_PROJECT = """\
---
capture_id: "SB-20260615-0004"
actions:
  - text: "Unassigned task"
    status: "open"
---
body text
"""

_NOTE_WITH_DONE_TASK = """\
---
capture_id: "SB-20260615-0005"
project: "project-a"
actions:
  - text: "Finished"
    status: "done"
---
body text
"""


class TestOpenTasksEndpoint:
    def test_returns_open_tasks_count(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [_NOTE_WITH_OPEN_TASK_PROJECT_A, _NOTE_WITH_DONE_TASK])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/stats/open-tasks", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["open_tasks_count"] == 1

    def test_returns_open_tasks_by_project(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [
            _NOTE_WITH_OPEN_TASK_PROJECT_A,
            _NOTE_WITH_OPEN_TASK_PROJECT_A_2,
            _NOTE_WITH_OPEN_TASK_PROJECT_B,
            _NOTE_WITH_OPEN_TASK_NO_PROJECT,
        ])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/stats/open-tasks", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "open_tasks_by_project" in data
        by_project = data["open_tasks_by_project"]
        assert by_project["project-a"] == 2
        assert by_project["project-b"] == 1
        assert by_project["__none__"] == 1

    def test_done_tasks_excluded_from_by_project(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [_NOTE_WITH_DONE_TASK])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/stats/open-tasks", headers=_AUTH)
        data = resp.json()
        assert data["open_tasks_count"] == 0
        assert data["open_tasks_by_project"] == {}

    def test_requires_auth(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/stats/open-tasks")
        assert resp.status_code == 401

    def test_empty_vault_returns_zero_and_empty_dict(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/stats/open-tasks", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["open_tasks_count"] == 0
        assert data["open_tasks_by_project"] == {}
