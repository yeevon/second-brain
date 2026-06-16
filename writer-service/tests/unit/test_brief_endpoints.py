"""Tests for the /internal/vault/brief/daily and /weekly endpoints."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from writerservice.main import app

CLIENT = TestClient(app, raise_server_exceptions=False)

_WRITER_TOKEN = "test-token"
_AUTH = {"X-Second-Brain-Writer-Token": _WRITER_TOKEN}


def _make_vault(tmp_path, notes: list[tuple[str, str]]) -> str:
    """Create a vault with (filename, content) pairs."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for fname, content in notes:
        (vault / fname).write_text(content, encoding="utf-8")
    return str(vault)


_TODAY = date.today().isoformat()
_TOMORROW = (date.today() + timedelta(days=1)).isoformat()
_LAST_WEEK = (date.today() - timedelta(days=3)).isoformat()

_NOTE_HIGH_PRIORITY = f"""\
---
capture_id: "SB-001"
note_type: "task"
title: "Fix the pipeline"
project: "second-brain"
created_at: "{_LAST_WEEK}T10:00:00+00:00"
actions:
  - text: "Fix the pipeline"
    status: "open"
    priority: "high"
    project: "second-brain"
---
# Fix the pipeline

body
"""

_NOTE_DUE_TODAY = f"""\
---
capture_id: "SB-002"
note_type: "task"
title: "Submit form"
project: "admin"
created_at: "{_LAST_WEEK}T10:00:00+00:00"
actions:
  - text: "Submit the form"
    status: "open"
    due: "{_TODAY}"
---
# Submit form

body
"""

_NOTE_DUE_TOMORROW = f"""\
---
capture_id: "SB-003"
note_type: "task"
title: "Prep for meeting"
project: "admin"
created_at: "{_LAST_WEEK}T10:00:00+00:00"
actions:
  - text: "Prep for meeting"
    status: "open"
    due: "{_TOMORROW}"
---
# Prep for meeting

body
"""

_NOTE_BIRTHDAY = f"""\
---
capture_id: "SB-004"
note_type: "birthday"
title: "Mom birthday"
note_date: "{_TOMORROW}"
created_at: "{_LAST_WEEK}T10:00:00+00:00"
actions:
  []
---
# Mom birthday
"""

_NOTE_ACCOMPLISHED_THIS_WEEK = f"""\
---
capture_id: "SB-005"
note_type: "note"
title: "Shipped M6 digest redesign"
project: "second-brain"
created_at: "{_LAST_WEEK}T10:00:00+00:00"
actions:
  []
---
# Shipped M6 digest redesign

body
"""

_NOTE_DECISION = f"""\
---
capture_id: "SB-006"
note_type: "decision"
title: "Use /internal/brief over digest"
project: "second-brain"
created_at: "{_LAST_WEEK}T10:00:00+00:00"
actions:
  []
---
# Use /internal/brief over digest
"""

_NOTE_OPEN_TASK_NO_DUE = f"""\
---
capture_id: "SB-007"
note_type: "task"
title: "Backlog item"
project: "math"
created_at: "{_LAST_WEEK}T10:00:00+00:00"
actions:
  - text: "Finish algebra review"
    status: "open"
---
# Backlog item
"""


class TestDailyBriefEndpoint:
    def test_requires_auth(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/daily")
        assert resp.status_code == 401

    def test_returns_today_field(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/daily", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["today"] == _TODAY

    def test_high_priority_task_in_focus_items(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [("high_pri.md", _NOTE_HIGH_PRIORITY)])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/daily", headers=_AUTH)
        data = resp.json()
        assert any(t["title"] == "Fix the pipeline" for t in data["focus_items"])

    def test_task_due_today_in_due_today(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [("due_today.md", _NOTE_DUE_TODAY)])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/daily", headers=_AUTH)
        data = resp.json()
        assert any(t["title"] == "Submit the form" for t in data["due_today"])

    def test_task_due_tomorrow_in_coming_up(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [("due_tomorrow.md", _NOTE_DUE_TOMORROW)])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/daily", headers=_AUTH)
        data = resp.json()
        assert any(item.get("title") == "Prep for meeting" for item in data["coming_up"])

    def test_birthday_note_in_birthdays(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [("birthday.md", _NOTE_BIRTHDAY)])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/daily", headers=_AUTH)
        data = resp.json()
        assert any(b["name"] == "Mom birthday" for b in data["birthdays"])

    def test_plain_open_task_in_pending(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [("backlog.md", _NOTE_OPEN_TASK_NO_DUE)])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/daily", headers=_AUTH)
        data = resp.json()
        assert any(t["title"] == "Finish algebra review" for t in data["pending_tasks"])

    def test_empty_vault_returns_all_empty_lists(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/daily", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        for key in ("focus_items", "due_today", "coming_up", "birthdays", "pending_tasks", "stale_tasks"):
            assert data[key] == [], f"{key} should be empty"


class TestWeeklyBriefEndpoint:
    def test_requires_auth(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/weekly")
        assert resp.status_code == 401

    def test_returns_week_start_and_end(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/weekly", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "week_start" in data
        assert "week_end" in data

    def test_note_created_this_week_appears_in_accomplished(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [("accomplished.md", _NOTE_ACCOMPLISHED_THIS_WEEK)])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/weekly", headers=_AUTH)
        data = resp.json()
        assert any(a["title"] == "Shipped M6 digest redesign" for a in data["accomplished"])

    def test_decision_note_appears_in_decisions(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [("decision.md", _NOTE_DECISION)])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/weekly", headers=_AUTH)
        data = resp.json()
        assert any(d["title"] == "Use /internal/brief over digest" for d in data["decisions"])

    def test_open_task_appears_in_still_open(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [("backlog.md", _NOTE_OPEN_TASK_NO_DUE)])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/weekly", headers=_AUTH)
        data = resp.json()
        assert any(t["title"] == "Finish algebra review" for t in data["still_open"])

    def test_empty_vault_returns_all_empty_lists(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path, [])
        monkeypatch.setenv("VAULT_PATH", vault)
        monkeypatch.setenv("WRITER_SERVICE_TOKEN", _WRITER_TOKEN)

        resp = CLIENT.get("/internal/vault/brief/weekly", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        for key in ("accomplished", "completed_tasks", "decisions", "still_open", "study_progress"):
            assert data[key] == [], f"{key} should be empty"
