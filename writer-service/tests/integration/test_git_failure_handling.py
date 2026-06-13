"""Integration tests for Git failure handling (SB-116).

Uses a local bare Git repository as the remote.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from writerservice.main import app

CLIENT = TestClient(app, raise_server_exceptions=False)
_TOKEN = "test-token-abc123"
_HEADERS = {"X-Second-Brain-Writer-Token": _TOKEN}


def _base_payload(capture_id: str, title: str = "Test note", **overrides) -> dict:
    payload = {
        "capture_id": capture_id,
        "source_message_id": "111222333444555666",
        "created_at": "2026-06-12T18:00:00Z",
        "delivery_attempt": 1,
        "model": "gemini-3.5-flash",
        "prompt_version": "classifier-v1",
        "classification": {
            "folder": "projects",
            "project": "second-brain",
            "note_type": "note",
            "title": title,
            "tags": ["test"],
            "body": "Integration test body.",
            "actions": [],
            "needs_clarification": False,
            "clarifying_question": None,
            "confidence": 0.95,
        },
        "inbox_reason": None,
    }
    payload.update(overrides)
    return payload


def _init_bare_repo(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "bare.git"
    clone = tmp_path / "vault"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
    _cfg_git(clone)
    (clone / ".gitignore").write_text(".writer.lock\n")
    (clone / "99_log").mkdir()
    (clone / "99_log" / ".gitkeep").touch()
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: initial vault structure"], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=clone, check=True, capture_output=True)
    return bare, clone


def _cfg_git(path: Path) -> None:
    subprocess.run(["git", "config", "user.email", "writer@second-brain.local"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Second Brain Writer"], cwd=path, check=True, capture_output=True)


def _working_tree_is_clean(vault: Path) -> bool:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=vault, capture_output=True, text=True)
    return r.stdout.strip() == ""


# ── Simulated index lock ───────────────────────────────────────────────────────

def test_index_lock_returns_503(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    (vault / ".git" / "index.lock").touch()

    resp = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0101"), headers=_HEADERS)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_type"] == "git_index_locked"
    assert body["retryable"] is True


def test_index_lock_not_deleted_by_writer(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    lock = vault / ".git" / "index.lock"
    lock.touch()

    CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0102"), headers=_HEADERS)
    assert lock.exists()


def test_index_lock_no_note_written(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    (vault / ".git" / "index.lock").touch()
    resp = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0103"), headers=_HEADERS)
    assert resp.status_code == 503

    md_files = list(vault.rglob("*.md"))
    assert not any("SB-20260612-0103" in f.name for f in md_files)


# ── Simulated merge conflict ───────────────────────────────────────────────────

def test_merge_conflict_returns_409(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    # Commit directly on local clone to diverge from remote
    (vault / "local_only.md").write_text("not pushed")
    subprocess.run(["git", "add", "local_only.md"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local: unpushed commit"], cwd=vault, check=True, capture_output=True)

    resp = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0104"), headers=_HEADERS)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_type"] == "git_merge_conflict"
    assert body["retryable"] is False


def test_merge_conflict_no_new_file_written(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    (vault / "local_only.md").write_text("not pushed")
    subprocess.run(["git", "add", "local_only.md"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local: unpushed commit"], cwd=vault, check=True, capture_output=True)

    CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0105"), headers=_HEADERS)
    md_files = list(vault.rglob("*.md"))
    assert not any("SB-20260612-0105" in f.name for f in md_files)


# ── Push rejection (rollback) ─────────────────────────────────────────────────

def test_push_rejected_returns_409(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    # Use a threading.Event to inject a remote commit between writer's fetch and push
    from unittest.mock import patch
    import writerservice.git_ops as git_ops_mod

    original_push = git_ops_mod.git_push

    def push_that_fails(vault_path: Path) -> None:
        # Simulate a push rejection by calling git push to a non-existent remote path
        raise git_ops_mod.GitPushRejectedError("git push rejected: remote has advanced.")

    with patch.object(git_ops_mod, "git_push", side_effect=push_that_fails):
        resp = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0106"), headers=_HEADERS)

    assert resp.status_code == 409
    body = resp.json()
    assert body["error_type"] == "git_push_rejected"
    assert body["retryable"] is True


def test_push_rejected_rolls_back_note_file(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    from unittest.mock import patch
    import writerservice.git_ops as git_ops_mod

    def push_that_fails(vault_path: Path) -> None:
        raise git_ops_mod.GitPushRejectedError("rejected")

    with patch.object(git_ops_mod, "git_push", side_effect=push_that_fails):
        CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0107"), headers=_HEADERS)

    md_files = list(vault.rglob("*.md"))
    assert not any("SB-20260612-0107" in f.name for f in md_files)


def test_push_rejected_working_tree_clean_after_rollback(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    from unittest.mock import patch
    import writerservice.git_ops as git_ops_mod

    def push_that_fails(vault_path: Path) -> None:
        raise git_ops_mod.GitPushRejectedError("rejected")

    with patch.object(git_ops_mod, "git_push", side_effect=push_that_fails):
        CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0108"), headers=_HEADERS)

    assert _working_tree_is_clean(vault)


# ── Dirty working tree ────────────────────────────────────────────────────────

def test_dirty_tree_returns_503(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    (vault / "stale_orphan.md").write_text("crashed write")

    resp = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0109"), headers=_HEADERS)
    assert resp.status_code == 503
    assert resp.json()["error_type"] == "git_worktree_dirty"
    assert resp.json()["retryable"] is False


def test_dirty_tree_no_note_written(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    (vault / "stale_orphan.md").write_text("crashed write")
    CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0110"), headers=_HEADERS)

    md_files = list(vault.rglob("*.md"))
    assert not any("SB-20260612-0110" in f.name for f in md_files)


# ── Duplicate capture_id ──────────────────────────────────────────────────────

def test_duplicate_capture_id_returns_409(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    # Manually write two files with the same capture_id frontmatter
    for subdir in ["20_projects/a", "20_projects/b"]:
        d = vault / subdir
        d.mkdir(parents=True)
        (d / "note.md").write_text('---\ncapture_id: "SB-20260612-0111"\n---\n\n# Dup\n')

    resp = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0111"), headers=_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["error_type"] == "capture_id_duplicate"
    assert resp.json()["retryable"] is False


def test_duplicate_no_third_file_written(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    for subdir in ["20_projects/a", "20_projects/b"]:
        d = vault / subdir
        d.mkdir(parents=True)
        (d / "note.md").write_text('---\ncapture_id: "SB-20260612-0112"\n---\n\n# Dup\n')

    CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0112"), headers=_HEADERS)
    md_files = list(vault.rglob("*.md"))
    dup_files = [f for f in md_files if "SB-20260612-0112" in f.read_text()]
    assert len(dup_files) == 2


# ── Path traversal ────────────────────────────────────────────────────────────

def test_path_traversal_in_project_returns_safely(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "false")

    payload = _base_payload("SB-20260612-0113")
    payload["classification"]["project"] = "../../etc/passwd"
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    # sanitize_slug converts this to a safe slug; must succeed safely
    assert resp.status_code == 200
    note_path = resp.json()["note_path"]
    assert ".." not in note_path
    assert (vault / note_path).resolve().is_relative_to(vault.resolve())
