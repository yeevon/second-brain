"""SB-123: Read-only MCP server for the Second Brain vault.

Exposes vault and ledger data as MCP tools with path-root enforcement,
result limits, no mutation tools, and a sync preflight before queries.

Run via: brain-mcp (configured in pyproject.toml)
Required env vars: LEDGER_PATH, VAULT_PATH (for vault tools)
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from secondbrain.digest import scan_open_task_list

_RESULT_LIMIT_MAX = 100
_RESULT_LIMIT_DEFAULT = 20

server = Server("second-brain-vault")


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    raw = os.getenv("LEDGER_PATH", "").strip()
    if not raw:
        raise RuntimeError("LEDGER_PATH is required")
    return Path(raw)


def _vault_path() -> Path | None:
    raw = os.getenv("VAULT_PATH", "").strip()
    return Path(raw) if raw else None


def _clamp_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = _RESULT_LIMIT_DEFAULT
    return max(1, min(limit, _RESULT_LIMIT_MAX))


# ---------------------------------------------------------------------------
# Path-root enforcement
# ---------------------------------------------------------------------------


def _enforce_path(vault_path: Path, note_path: str) -> Path:
    """Resolve note_path relative to vault root; raise on traversal."""
    vault_root = vault_path.resolve()
    resolved = (vault_root / note_path).resolve()
    if not resolved.is_relative_to(vault_root):
        raise ValueError(f"path traversal detected: {note_path!r}")
    return resolved


# ---------------------------------------------------------------------------
# Sync preflight
# ---------------------------------------------------------------------------


def _vault_preflight(vault_path: Path | None) -> str | None:
    """Return a warning string if vault may be stale or dirty; None if ok.

    Checks: path configured → path exists → git worktree clean.
    A dirty worktree means vault-pull has not been run or local changes
    were made outside the sync flow — results could be stale.
    """
    if vault_path is None:
        return "VAULT_PATH not configured"
    if not vault_path.exists():
        return f"vault path does not exist: {vault_path}"
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=vault_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return f"vault git status failed (exit {result.returncode}) — run vault-pull first"
        if result.stdout.strip():
            return (
                "vault has uncommitted local changes — results may be stale; "
                "run vault-pull to sync before querying"
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "vault git status timed out or git is unavailable"
    return None


# ---------------------------------------------------------------------------
# Ledger read helpers (read-only SQLite connection)
# ---------------------------------------------------------------------------


def _open_ledger(ledger_path: Path) -> sqlite3.Connection:
    if not ledger_path.exists():
        raise RuntimeError(f"ledger does not exist: {ledger_path}")
    conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 1000")
    return conn


def _ledger_system_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _do_search_notes(
    vault_path: Path,
    *,
    query: str,
    folder: str | None,
    project: str | None,
    tags: list[str] | None,
    limit: int,
) -> list[dict]:
    query_lower = query.lower()
    results: list[dict] = []
    for note_path in vault_path.rglob("*.md"):
        if len(results) >= limit:
            break
        if not note_path.is_file():
            continue
        relative = note_path.relative_to(vault_path).as_posix()
        # Folder filter: first path component maps to vault area folders
        if folder is not None and not relative.startswith(folder):
            continue
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if query_lower not in text.lower():
            continue
        # Parse frontmatter for metadata
        note_project: str | None = None
        note_tags: list[str] = []
        title: str | None = None
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                fm = parts[1]
                for line in fm.splitlines():
                    if line.startswith("project: "):
                        note_project = line[9:].strip().strip('"')
                    elif line.startswith("  - ") and "tags:" in fm:
                        note_tags.append(line.strip().strip('"').lstrip("- "))
                if len(parts) >= 3 and parts[2]:
                    for body_line in parts[2].splitlines():
                        if body_line.startswith("# "):
                            title = body_line[2:].strip()
                            break
        if project is not None and note_project != project:
            continue
        if tags:
            if not any(t in note_tags for t in tags):
                continue
        results.append({
            "note_path": relative,
            "title": title,
            "project": note_project,
            "tags": note_tags,
        })
    return results


def _do_read_note(vault_path: Path, note_path: str) -> str:
    resolved = _enforce_path(vault_path, note_path)
    if not resolved.exists():
        raise FileNotFoundError(f"note not found: {note_path!r}")
    if not resolved.is_file():
        raise ValueError(f"not a file: {note_path!r}")
    return resolved.read_text(encoding="utf-8", errors="replace")


def _do_list_recent_notes(
    vault_path: Path,
    *,
    days: int,
    folder: str | None,
    limit: int,
) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()
    results: list[dict] = []
    candidates = []
    for note_path in vault_path.rglob("*.md"):
        if not note_path.is_file():
            continue
        relative = note_path.relative_to(vault_path).as_posix()
        if folder is not None and not relative.startswith(folder):
            continue
        try:
            mtime = note_path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff_ts:
            candidates.append((mtime, relative))
    candidates.sort(key=lambda x: x[0], reverse=True)
    for mtime, relative in candidates[:limit]:
        results.append({
            "note_path": relative,
            "modified_at": datetime.fromtimestamp(mtime, UTC).isoformat(),
        })
    return results


def _do_list_open_tasks(
    vault_path: Path,
    *,
    project: str | None,
    limit: int,
) -> list[dict]:
    return scan_open_task_list(vault_path, project=project, limit=limit)


def _do_get_sync_status(ledger_path: Path, vault_path: Path | None) -> dict:
    result: dict[str, Any] = {
        "ledger_path": str(ledger_path),
        "vault_path": str(vault_path) if vault_path else None,
        "ledger_exists": ledger_path.exists(),
    }

    # Read last sync timestamps from ledger
    if ledger_path.exists():
        try:
            conn = _open_ledger(ledger_path)
            result["last_successful_reconciliation_at"] = _ledger_system_state(
                conn, "last_successful_reconciliation_at"
            )
            result["last_successful_backup_at"] = _ledger_system_state(
                conn, "last_successful_backup_at"
            )
            result["capture_service_state"] = _ledger_system_state(
                conn, "capture_service_state"
            )
            conn.close()
        except Exception as exc:
            result["ledger_error"] = str(exc)

    # Check vault git status (no shell injection — fixed args only)
    if vault_path is not None and vault_path.exists():
        try:
            git_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=vault_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            result["vault_dirty"] = bool(git_status.stdout.strip())
            result["vault_git_status_exit_code"] = git_status.returncode

            git_log = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=vault_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            result["vault_head_commit"] = git_log.stdout.strip() or None
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            result["vault_git_error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# MCP server handlers
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_notes",
            description="Search vault notes by keyword. Returns matching note paths and metadata.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or phrase to search for"},
                    "folder": {"type": "string", "description": "Filter by vault folder prefix (e.g. '20_projects')"},
                    "project": {"type": "string", "description": "Filter by project slug"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags (OR match)"},
                    "limit": {"type": "integer", "default": _RESULT_LIMIT_DEFAULT, "description": f"Max results (1-{_RESULT_LIMIT_MAX})"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="read_note",
            description="Read the full content of a vault note. Path is relative to the vault root.",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_path": {"type": "string", "description": "Path relative to vault root (e.g. '20_projects/my-project/2025-01-01--SB-20250101-0001--title.md')"},
                },
                "required": ["note_path"],
            },
        ),
        Tool(
            name="list_recent_notes",
            description="List recently modified vault notes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 7, "description": "Look back this many days (default 7)"},
                    "folder": {"type": "string", "description": "Filter by vault folder prefix"},
                    "limit": {"type": "integer", "default": _RESULT_LIMIT_DEFAULT, "description": f"Max results (1-{_RESULT_LIMIT_MAX})"},
                },
            },
        ),
        Tool(
            name="list_open_tasks",
            description="List vault notes that have open (not done) action items.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Filter by project slug"},
                    "limit": {"type": "integer", "default": _RESULT_LIMIT_DEFAULT, "description": f"Max results (1-{_RESULT_LIMIT_MAX})"},
                },
            },
        ),
        Tool(
            name="get_sync_status",
            description="Check vault git sync status and capture service health from the ledger.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    vault = _vault_path()
    ledger = _ledger_path()

    if name == "get_sync_status":
        data = _do_get_sync_status(ledger, vault)
        return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]

    # Sync preflight for all vault tools
    preflight_error = _vault_preflight(vault)
    if preflight_error:
        return [TextContent(type="text", text=f"ERROR: {preflight_error}")]

    assert vault is not None  # preflight would have caught None

    if name == "search_notes":
        query = arguments.get("query", "")
        if not query:
            return [TextContent(type="text", text="ERROR: query is required")]
        results = _do_search_notes(
            vault,
            query=query,
            folder=arguments.get("folder"),
            project=arguments.get("project"),
            tags=arguments.get("tags"),
            limit=_clamp_limit(arguments.get("limit", _RESULT_LIMIT_DEFAULT)),
        )
        return [TextContent(type="text", text=json.dumps(results, indent=2))]

    if name == "read_note":
        note_path = arguments.get("note_path", "")
        if not note_path:
            return [TextContent(type="text", text="ERROR: note_path is required")]
        try:
            content = _do_read_note(vault, note_path)
        except (ValueError, FileNotFoundError) as exc:
            return [TextContent(type="text", text=f"ERROR: {exc}")]
        return [TextContent(type="text", text=content)]

    if name == "list_recent_notes":
        days = max(1, int(arguments.get("days", 7)))
        results = _do_list_recent_notes(
            vault,
            days=days,
            folder=arguments.get("folder"),
            limit=_clamp_limit(arguments.get("limit", _RESULT_LIMIT_DEFAULT)),
        )
        return [TextContent(type="text", text=json.dumps(results, indent=2))]

    if name == "list_open_tasks":
        results = _do_list_open_tasks(
            vault,
            project=arguments.get("project"),
            limit=_clamp_limit(arguments.get("limit", _RESULT_LIMIT_DEFAULT)),
        )
        return [TextContent(type="text", text=json.dumps(results, indent=2))]

    return [TextContent(type="text", text=f"ERROR: unknown tool: {name!r}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    asyncio.run(_run())


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    main()
