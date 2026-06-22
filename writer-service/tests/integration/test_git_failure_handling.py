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
        "raw_text": "",
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
# A true --ff-only failure requires the local and remote to DIVERGE.
# We achieve this by:
#   1. Advancing the remote from a second clone (commit B).
#   2. Making a local commit C in the vault clone (on top of the shared base A).
# After fetch, origin/main = B but vault HEAD = C → merge --ff-only fails.

def _make_diverging_vault(tmp_path: "Path") -> tuple["Path", "Path"]:
    bare, vault = _init_bare_repo(tmp_path)
    # Advance remote from a second clone
    clone2 = tmp_path / "clone2"
    subprocess.run(["git", "clone", str(bare), str(clone2)], check=True, capture_output=True)
    _cfg_git(clone2)
    (clone2 / "remote_only.md").write_text("remote advance")
    subprocess.run(["git", "add", "remote_only.md"], cwd=clone2, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: remote advance"], cwd=clone2, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=clone2, check=True, capture_output=True)
    # Make a diverging local commit in the vault clone
    (vault / "local_only.md").write_text("local diverge")
    subprocess.run(["git", "add", "local_only.md"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local: diverging commit"], cwd=vault, check=True, capture_output=True)
    return bare, vault


def test_merge_conflict_returns_409(tmp_path, monkeypatch):
    bare, vault = _make_diverging_vault(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    resp = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0104"), headers=_HEADERS)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_type"] == "git_merge_conflict"
    assert body["retryable"] is False


def test_merge_conflict_no_new_file_written(tmp_path, monkeypatch):
    bare, vault = _make_diverging_vault(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

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

def test_path_traversal_in_project_rejected(tmp_path, monkeypatch):
    # SB-116 contract: traversal-shaped input is rejected before sanitization.
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "false")

    payload = _base_payload("SB-20260612-0113")
    payload["classification"]["project"] = "../../etc/passwd"
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    assert resp.status_code == 422
    assert resp.json()["error_type"] == "path_traversal_attempt"
    assert resp.json()["retryable"] is False
    assert not any(vault.rglob("*.md"))


# ── Idempotent recovery push failure (rollback) ───────────────────────────────

def test_recovery_push_failure_rolls_back_raw_file_and_commit(tmp_path, monkeypatch):
    # Setup: sanitized note in git, raw file missing (never committed).
    # Recovery creates raw + commits locally, then push fails.
    # Rollback must reset to pre-recovery HEAD and remove the raw file from disk.
    from unittest.mock import patch
    import writerservice.git_ops as git_ops_mod

    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    # Plant sanitized note directly in git with no raw file.
    note_folder = vault / "20_projects" / "second-brain"
    note_folder.mkdir(parents=True, exist_ok=True)
    note_content = (
        "---\n"
        "capture_id: \"SB-20260612-0120\"\n"
        "source_message_id: \"111222333444555666\"\n"
        "created_at: \"2026-06-12T18:00:00+00:00\"\n"
        "area: \"projects\"\n"
        "project: \"second-brain\"\n"
        "note_type: \"note\"\n"
        "title: \"Test note\"\n"
        "tags:\n  - \"test\"\n"
        "actions:\n  []\n"
        "lifecycle_status: active\n"
        "model: \"gemini-3.5-flash\"\n"
        "prompt_version: \"classifier-v1\"\n"
        "schema_version: 1\n"
        "---\n\n# Test note\n\nIntegration test body.\n"
    )
    note_file = note_folder / "2026-06-12--SB-20260612-0120--test-note.md"
    note_file.write_text(note_content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "note: SB-20260612-0120 (no raw)"],
        cwd=vault, check=True, capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=vault, check=True, capture_output=True)
    pre_recovery_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=vault, capture_output=True, text=True
    ).stdout.strip()

    raw_abs = vault / "00_raw" / "2026" / "06" / "SB-20260612-0120.md"
    assert not raw_abs.exists()

    def push_that_fails(vault_path: Path) -> None:
        raise git_ops_mod.GitPushRejectedError("rejected")

    with patch.object(git_ops_mod, "git_push", side_effect=push_that_fails):
        resp = CLIENT.post(
            "/internal/notes/file",
            json=_base_payload("SB-20260612-0120", delivery_attempt=2),
            headers=_HEADERS,
        )

    assert resp.status_code == 409

    # Raw file must be gone after rollback.
    assert not raw_abs.exists(), "raw file must be removed on push failure rollback"

    # Local HEAD must be back at pre-recovery commit.
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=vault, capture_output=True, text=True
    ).stdout.strip()
    assert head_after == pre_recovery_head, "HEAD must be reset to pre-recovery commit after rollback"

    # Working tree must be clean.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=vault, capture_output=True, text=True
    )
    assert status.stdout.strip() == "", "working tree must be clean after rollback"
