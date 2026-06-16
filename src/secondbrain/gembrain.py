"""SB-124: gembrain CLI — vault-pull preflight + MCP sub-commands + Gemini CLI integration.

Commands:
  gembrain status                           — vault sync status (no preflight)
  gembrain recent [--days N] [--folder F] [--limit N]
  gembrain tasks  [--project P] [--limit N]
  gembrain ask "<question>"                 — Gemini CLI query via brain-mcp

All commands except `status` run vault-pull preflight before executing.
VAULT_PATH is required for all commands (status degrades gracefully without it).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _vault_path() -> Path | None:
    raw = os.getenv("VAULT_PATH", "").strip()
    return Path(raw) if raw else None


def _ledger_path() -> Path | None:
    raw = os.getenv("LEDGER_PATH", "").strip()
    return Path(raw) if raw else None


def _require_vault_path() -> Path | None:
    vault = _vault_path()
    if vault is None:
        print("ERROR: VAULT_PATH environment variable is required", file=sys.stderr)
    return vault


# ---------------------------------------------------------------------------
# Vault-pull preflight
# ---------------------------------------------------------------------------


def _run_preflight(vault_path: Path) -> int:
    """Run vault-pull preflight. Prints error to stderr and returns non-zero on failure."""
    from secondbrain.vault_pull import VaultPullError, pull_vault

    try:
        pull_vault(vault_path)
        return 0
    except VaultPullError as exc:
        print(f"ERROR: vault-pull preflight failed: {exc}", file=sys.stderr)
        return exc.exit_code


# ---------------------------------------------------------------------------
# gembrain status
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a git command; return stdout string or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _ledger_state(ledger: Path, key: str) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row["value"] if row else None
    except Exception:
        return None


def cmd_status() -> int:
    """Report vault sync status without running a preflight.

    Exit 0 if vault is clean and in sync, 1 otherwise.
    """
    vault = _vault_path()
    ledger = _ledger_path()

    status: dict = {
        "vault_path": str(vault) if vault else None,
        "vault_configured": vault is not None,
    }

    if vault and vault.exists():
        status["vault_exists"] = True

        # Local HEAD
        status["local_head"] = _git(["log", "--oneline", "-1"], vault)

        # Remote HEAD
        status["remote_head"] = _git(["log", "--oneline", "-1", "origin/main"], vault)

        # Commits behind
        behind_str = _git(["rev-list", "--count", "HEAD..origin/main"], vault)
        if behind_str is not None and behind_str.isdigit():
            behind = int(behind_str)
            status["commits_behind"] = behind
            status["up_to_date"] = behind == 0
        else:
            status["commits_behind"] = None
            status["up_to_date"] = None

        # Dirty worktree (tracked files only — ignores Obsidian metadata)
        dirty_out = _git(["status", "--porcelain", "--untracked-files=no"], vault)
        status["dirty_worktree"] = bool(dirty_out)

        # Pending merge conflict
        merge_head = vault / ".git" / "MERGE_HEAD"
        status["pending_conflict"] = merge_head.exists()
    else:
        status["vault_exists"] = False

    # Ledger timestamps (optional)
    if ledger and ledger.exists():
        status["last_successful_pull_at"] = _ledger_state(ledger, "last_vault_pull_at")
        status["last_mcp_query_at"] = _ledger_state(ledger, "last_mcp_query_at")
    else:
        status["last_successful_pull_at"] = None
        status["last_mcp_query_at"] = None

    print(json.dumps(status, indent=2))

    up_to_date = status.get("up_to_date")
    dirty = status.get("dirty_worktree")
    conflict = status.get("pending_conflict", False)
    if up_to_date is False or dirty or conflict:
        return 1
    return 0


# ---------------------------------------------------------------------------
# gembrain recent
# ---------------------------------------------------------------------------


def cmd_recent(*, days: int, folder: str | None, limit: int) -> int:
    """List recently modified vault notes (after vault-pull preflight)."""
    vault = _require_vault_path()
    if vault is None:
        return 1

    exit_code = _run_preflight(vault)
    if exit_code != 0:
        return exit_code

    from secondbrain.mcp_server import _do_list_recent_notes

    try:
        results = _do_list_recent_notes(vault, days=days, folder=folder, limit=limit)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not results:
        print(f"No notes modified in the last {days} day(s).")
        return 0

    print(f"Recent notes (last {days} day(s), {len(results)} result(s)):")
    for item in results:
        modified = item["modified_at"][:10]
        print(f"  {modified}  {item['note_path']}")
    return 0


# ---------------------------------------------------------------------------
# gembrain tasks
# ---------------------------------------------------------------------------


def cmd_tasks(*, project: str | None, limit: int) -> int:
    """List open tasks from the vault (after vault-pull preflight)."""
    vault = _require_vault_path()
    if vault is None:
        return 1

    exit_code = _run_preflight(vault)
    if exit_code != 0:
        return exit_code

    from secondbrain.digest import scan_open_task_list

    try:
        results = scan_open_task_list(vault, project=project, limit=limit)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not results:
        filter_msg = f" in project '{project}'" if project else ""
        print(f"No open tasks found{filter_msg}.")
        return 0

    filter_msg = f" (project: {project})" if project else ""
    print(f"Open tasks{filter_msg} — {len(results)} note(s):")
    for item in results:
        print(f"\n  {item['note_path']}")
        for action in item.get("open_actions", []):
            print(f"    - {action}")
    return 0


# ---------------------------------------------------------------------------
# gembrain ask
# ---------------------------------------------------------------------------


def cmd_ask(question: str) -> int:
    """Ask a natural-language question via Gemini CLI + brain-mcp (after preflight)."""
    vault = _require_vault_path()
    if vault is None:
        return 1

    exit_code = _run_preflight(vault)
    if exit_code != 0:
        return exit_code

    gemini_bin = shutil.which("gemini")
    if gemini_bin is None:
        print(
            "ERROR: 'gemini' CLI not found on PATH.\n"
            "Install it from: https://github.com/google-gemini/gemini-cli",
            file=sys.stderr,
        )
        return 1

    # Forward VAULT_PATH and LEDGER_PATH to the brain-mcp subprocess
    env = os.environ.copy()
    vault_str = os.getenv("VAULT_PATH", "")
    ledger_str = os.getenv("LEDGER_PATH", "")
    if vault_str:
        env["VAULT_PATH"] = vault_str
    if ledger_str:
        env["LEDGER_PATH"] = ledger_str

    result = subprocess.run(
        [gemini_bin, "--mcp-server", "brain-mcp", question],
        env=env,
    )
    return result.returncode


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="gembrain",
        description="Vault-aware CLI for querying the Second Brain via MCP and Gemini.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Report vault sync status (no preflight)")

    recent_p = sub.add_parser("recent", help="List recently modified notes")
    recent_p.add_argument("--days", type=int, default=7, metavar="N")
    recent_p.add_argument("--folder", default=None, metavar="F")
    recent_p.add_argument("--limit", type=int, default=20, metavar="N")

    tasks_p = sub.add_parser("tasks", help="List open tasks from the vault")
    tasks_p.add_argument("--project", default=None, metavar="P")
    tasks_p.add_argument("--limit", type=int, default=20, metavar="N")

    ask_p = sub.add_parser("ask", help="Ask a question via Gemini CLI + brain-mcp")
    ask_p.add_argument("question", help="Natural-language question")

    args = parser.parse_args()

    if args.command == "status":
        return cmd_status()
    if args.command == "recent":
        return cmd_recent(days=args.days, folder=args.folder, limit=args.limit)
    if args.command == "tasks":
        return cmd_tasks(project=args.project, limit=args.limit)
    if args.command == "ask":
        return cmd_ask(args.question)

    parser.print_help(file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
