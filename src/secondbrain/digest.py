"""Vault scanning utilities shared by digest endpoints and MCP server."""
from __future__ import annotations

from pathlib import Path


def scan_open_tasks(vault_path: Path) -> int:
    """Count action items with status: open across all vault notes."""
    count = 0
    for note_path in vault_path.rglob("*.md"):
        if not note_path.is_file():
            continue
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        count += parts[1].count("    status: open")
    return count


def scan_open_task_list(
    vault_path: Path,
    *,
    project: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return structured list of open action items from vault notes."""
    results: list[dict] = []
    for note_path in vault_path.rglob("*.md"):
        if len(results) >= limit:
            break
        if not note_path.is_file():
            continue
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        frontmatter = parts[1]
        if "    status: open" not in frontmatter:
            continue
        note_project = _extract_frontmatter_field(frontmatter, "project")
        if project is not None and note_project != project:
            continue
        capture_id = _extract_frontmatter_field(frontmatter, "capture_id")
        open_actions = _parse_open_actions(frontmatter)
        if not open_actions:
            continue
        results.append({
            "note_path": note_path.relative_to(vault_path).as_posix(),
            "project": note_project,
            "capture_id": capture_id,
            "open_actions": open_actions,
        })
    return results


def _extract_frontmatter_field(frontmatter: str, field: str) -> str | None:
    prefix = f"{field}: "
    for line in frontmatter.splitlines():
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            # Strip surrounding quotes (yaml_scalar uses json.dumps which adds quotes)
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            return value or None
    return None


def _parse_open_actions(frontmatter: str) -> list[str]:
    """Extract action text strings with status: open from frontmatter."""
    lines = frontmatter.splitlines()
    open_actions: list[str] = []
    in_actions = False
    pending_text: str | None = None

    for line in lines:
        stripped = line.strip()
        if stripped == "actions:":
            in_actions = True
            continue
        if not in_actions:
            continue
        # Exit actions block if we hit a top-level key
        if stripped and not stripped.startswith("-") and not stripped.startswith("text:") and not stripped.startswith("status:") and ":" in stripped and not stripped.startswith("#"):
            if not line.startswith(" ") and not line.startswith("\t"):
                in_actions = False
                continue
        if stripped.startswith("- text:"):
            pending_text = stripped[7:].strip().strip('"')
        elif stripped.startswith("status: open") and pending_text is not None:
            open_actions.append(pending_text)
            pending_text = None
        elif stripped.startswith("status:") and pending_text is not None:
            pending_text = None  # done or other status — discard

    return open_actions
