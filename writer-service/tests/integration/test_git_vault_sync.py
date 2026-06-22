"""Integration tests for Git-backed vault sync (SB-115).

Uses a local bare Git repository as the remote to avoid network dependency.
"""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    """Create bare remote and a working clone with an initial commit."""
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


# ── Normal filing with Git sync ───────────────────────────────────────────────

def test_git_sync_files_note_and_pushes_commit(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    resp = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0001"), headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "FILED"
    assert body["git_commit_hash"] is not None
    assert len(body["git_commit_hash"]) == 40

    note_path = vault / body["note_path"]
    assert note_path.is_file()

    audit = (vault / "99_log" / "events.ndjson").read_text()
    assert "SB-20260612-0001" in audit

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=vault, capture_output=True, text=True
    )
    assert "SB-20260612-0001" in log.stdout

    # Commit must exist in bare remote
    bare_log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=bare, capture_output=True, text=True
    )
    assert "SB-20260612-0001" in bare_log.stdout


# ── Idempotent replay ─────────────────────────────────────────────────────────

def test_git_sync_idempotent_replay_returns_existing_hash(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    resp1 = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0002"), headers=_HEADERS)
    assert resp1.status_code == 200
    hash1 = resp1.json()["git_commit_hash"]

    payload2 = _base_payload("SB-20260612-0002")
    payload2["delivery_attempt"] = 2
    resp2 = CLIENT.post("/internal/notes/file", json=payload2, headers=_HEADERS)
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["idempotent"] is True
    assert body2["git_commit_hash"] == hash1
    assert body2["git_commit_hash"] is not None

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=vault, capture_output=True, text=True
    )
    assert log.stdout.count("SB-20260612-0002") == 1


# ── Concurrent near-simultaneous writes ───────────────────────────────────────

def test_git_sync_concurrent_writes_produce_linear_history(tmp_path, monkeypatch):
    bare, vault = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    payloads = [
        _base_payload("SB-20260612-0003", title="Concurrent note A"),
        _base_payload("SB-20260612-0004", title="Concurrent note B"),
    ]

    results = []

    def post(p):
        return CLIENT.post("/internal/notes/file", json=p, headers=_HEADERS)

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(post, p) for p in payloads]
        for f in as_completed(futures):
            results.append(f.result())

    assert all(r.status_code == 200 for r in results), [r.json() for r in results]
    hashes = [r.json()["git_commit_hash"] for r in results]
    assert hashes[0] != hashes[1]

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=vault, capture_output=True, text=True
    )
    lines = [l for l in log.stdout.strip().splitlines() if "SB-20260612-000" in l]
    assert len(lines) == 2

    merge_commits = subprocess.run(
        ["git", "log", "--merges", "--oneline"], cwd=vault, capture_output=True, text=True
    )
    assert merge_commits.stdout.strip() == "", "no merge commits expected"


# ── Fetch-before-write ────────────────────────────────────────────────────────

def test_git_sync_fetches_before_write(tmp_path, monkeypatch):
    bare, clone1 = _init_bare_repo(tmp_path)
    monkeypatch.setenv("VAULT_PATH", str(clone1))
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")

    # Advance the remote from a second clone
    clone2 = tmp_path / "clone2"
    subprocess.run(["git", "clone", str(bare), str(clone2)], check=True, capture_output=True)
    _cfg_git(clone2)
    (clone2 / "remote_advance.md").write_text("pushed from clone2")
    subprocess.run(["git", "add", "remote_advance.md"], cwd=clone2, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: remote advance"], cwd=clone2, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=clone2, check=True, capture_output=True)

    resp = CLIENT.post("/internal/notes/file", json=_base_payload("SB-20260612-0005"), headers=_HEADERS)
    assert resp.status_code == 200

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=clone1, capture_output=True, text=True
    )
    assert "remote advance" in log.stdout
    assert "SB-20260612-0005" in log.stdout
