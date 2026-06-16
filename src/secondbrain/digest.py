"""Vault scanning utilities shared by digest endpoints and MCP server."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Matches both unquoted (`status: open`) and quoted (`status: "open"`) forms.
# writer-service renders status via yaml_scalar = json.dumps, so real notes
# contain the quoted form; unquoted is supported for hand-authored notes.
_OPEN_STATUS_LINE_RE = re.compile(r'^    status: "?open"?\s*$', re.MULTILINE)


def scan_open_tasks_by_project(vault_path: Path) -> dict[str, int]:
    """Count open tasks grouped by project slug across all vault notes.

    Returns a dict mapping project slug (or '__none__' for notes without a project)
    to the number of open action items in that project.
    """
    by_project: dict[str, int] = {}
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
        frontmatter = parts[1]
        open_count = len(_OPEN_STATUS_LINE_RE.findall(frontmatter))
        if open_count == 0:
            continue
        project = _extract_frontmatter_field(frontmatter, "project") or "__none__"
        by_project[project] = by_project.get(project, 0) + open_count
    return by_project


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
        count += len(_OPEN_STATUS_LINE_RE.findall(parts[1]))
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
        if not _OPEN_STATUS_LINE_RE.search(frontmatter):
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


def _action_status_is_open(stripped: str) -> bool:
    return stripped in ("status: open", 'status: "open"')


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
        elif _action_status_is_open(stripped) and pending_text is not None:
            open_actions.append(pending_text)
            pending_text = None
        elif stripped.startswith("status:") and pending_text is not None:
            pending_text = None  # done or other status — discard

    return open_actions


def _parse_actions_full(frontmatter: str) -> list[dict]:
    """Parse all action items with full metadata (text, status, due, priority, project)."""
    lines = frontmatter.splitlines()
    results: list[dict] = []
    in_actions = False
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if stripped == "actions:":
            in_actions = True
            continue
        if not in_actions:
            continue
        # Exit on a top-level YAML key (no leading whitespace, not a list item)
        if line and not line[0].isspace() and not line.startswith("-"):
            if current is not None:
                results.append(current)
                current = None
            in_actions = False
            continue
        if stripped.startswith("- text:"):
            if current is not None:
                results.append(current)
            current = {
                "text": stripped[7:].strip().strip('"'),
                "status": "open",
                "due": None,
                "priority": None,
                "project": None,
            }
        elif current is not None:
            if stripped.startswith("status:"):
                current["status"] = stripped[7:].strip().strip('"')
            elif stripped.startswith("due:"):
                v = stripped[4:].strip().strip('"')
                current["due"] = v or None
            elif stripped.startswith("priority:"):
                v = stripped[9:].strip().strip('"')
                current["priority"] = v or None
            elif stripped.startswith("project:"):
                v = stripped[8:].strip().strip('"')
                current["project"] = v or None

    if current is not None:
        results.append(current)
    return results


def _extract_body_title(body: str) -> str | None:
    """Return the text of the first H1 heading in a note body."""
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


# ── Daily brief ───────────────────────────────────────────────────────────────

_STALE_DAYS = 7
_DUE_WINDOW_DAYS = 7
_BIRTHDAY_WINDOW_DAYS = 14


def scan_daily_brief(vault_path: Path, *, today: date | None = None) -> dict:
    """Scan the vault and return structured data for the daily brief.

    Returns a dict with keys: today, focus_items, due_today, coming_up,
    birthdays, pending_tasks, stale_tasks.
    """
    today = today or date.today()
    due_window = today + timedelta(days=_DUE_WINDOW_DAYS)
    birthday_window = today + timedelta(days=_BIRTHDAY_WINDOW_DAYS)
    stale_cutoff = time.time() - (_STALE_DAYS * 86400)

    focus_items: list[dict] = []
    due_today: list[dict] = []
    coming_up: list[dict] = []
    birthdays: list[dict] = []
    pending_tasks: list[dict] = []
    stale_tasks: list[dict] = []

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
        frontmatter, body = parts[1], parts[2]

        note_project = _extract_frontmatter_field(frontmatter, "project")
        note_type = _extract_frontmatter_field(frontmatter, "note_type") or "note"
        note_date_str = _extract_frontmatter_field(frontmatter, "note_date")
        title_field = _extract_frontmatter_field(frontmatter, "title")
        note_title = title_field or _extract_body_title(body) or note_path.stem
        rel_path = note_path.relative_to(vault_path).as_posix()

        # Birthday notes
        if note_type == "birthday" and note_date_str:
            try:
                bday = date.fromisoformat(note_date_str)
                this_year = bday.replace(year=today.year)
                if this_year < today:
                    this_year = bday.replace(year=today.year + 1)
                if today <= this_year <= birthday_window:
                    birthdays.append({
                        "name": note_title,
                        "date": this_year.isoformat(),
                        "note_path": rel_path,
                    })
            except (ValueError, AttributeError):
                pass
            continue

        # Event / reminder notes
        if note_type in ("event", "reminder") and note_date_str:
            try:
                event_date = date.fromisoformat(note_date_str)
                if today <= event_date <= due_window:
                    coming_up.append({
                        "title": note_title,
                        "date": event_date.isoformat(),
                        "source": note_type,
                        "note_path": rel_path,
                    })
            except (ValueError, AttributeError):
                pass

        # Action items
        actions = _parse_actions_full(frontmatter)
        open_actions = [a for a in actions if a.get("status") == "open"]
        if not open_actions:
            continue

        try:
            is_stale = note_path.stat().st_mtime < stale_cutoff
        except OSError:
            is_stale = False

        for action in open_actions:
            action_project = action.get("project") or note_project
            due_str = action.get("due")
            priority = action.get("priority")

            item = {
                "title": action["text"],
                "project": action_project,
                "source": "task",
                "due": due_str,
                "priority": priority,
                "note_path": rel_path,
            }

            placed = False
            if due_str:
                try:
                    due_date = date.fromisoformat(due_str)
                    if due_date == today:
                        due_today.append(item)
                        placed = True
                    elif today < due_date <= due_window:
                        coming_up.append({
                            "title": action["text"],
                            "date": due_str,
                            "source": "task",
                            "note_path": rel_path,
                        })
                        placed = True
                except ValueError:
                    pass

            if not placed and priority == "high":
                focus_items.append(item)
                placed = True

            if not placed and is_stale:
                stale_tasks.append(item)
                placed = True

            if not placed:
                pending_tasks.append(item)

    return {
        "today": today.isoformat(),
        "focus_items": focus_items,
        "due_today": due_today,
        "coming_up": sorted(coming_up, key=lambda x: x.get("date", "")),
        "birthdays": sorted(birthdays, key=lambda x: x.get("date", "")),
        "pending_tasks": pending_tasks,
        "stale_tasks": stale_tasks,
    }


# ── Weekly brief ──────────────────────────────────────────────────────────────


def scan_weekly_brief(
    vault_path: Path,
    *,
    week_start: date | None = None,
    week_end: date | None = None,
) -> dict:
    """Scan the vault and return structured data for the weekly review.

    Returns a dict with keys: week_start, week_end, accomplished,
    completed_tasks, decisions, still_open, study_progress.
    """
    today = date.today()
    week_end = week_end or today
    week_start = week_start or (today - timedelta(days=7))

    accomplished: list[dict] = []
    completed_tasks: list[dict] = []
    decisions: list[dict] = []
    still_open: list[dict] = []
    study_progress: list[dict] = []

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
        frontmatter, body = parts[1], parts[2]

        note_project = _extract_frontmatter_field(frontmatter, "project")
        note_type = _extract_frontmatter_field(frontmatter, "note_type") or "note"
        created_at_str = _extract_frontmatter_field(frontmatter, "created_at")
        title_field = _extract_frontmatter_field(frontmatter, "title")
        note_title = title_field or _extract_body_title(body) or note_path.stem
        rel_path = note_path.relative_to(vault_path).as_posix()

        # Determine if note was created this week
        created_this_week = False
        if created_at_str:
            try:
                created_dt = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
                created_this_week = week_start <= created_dt.date() <= week_end
            except (ValueError, AttributeError):
                pass

        if created_this_week:
            if note_type == "decision":
                decisions.append({
                    "title": note_title,
                    "project": note_project,
                    "note_path": rel_path,
                })
            elif note_type == "study":
                study_progress.append({
                    "track": note_project or note_title,
                    "status": note_title,
                    "note_path": rel_path,
                })
            else:
                accomplished.append({
                    "title": note_title,
                    "source": note_type,
                    "project": note_project,
                    "note_path": rel_path,
                })

        # Scan all actions regardless of creation date
        actions = _parse_actions_full(frontmatter)
        for action in actions:
            action_project = action.get("project") or note_project
            if action.get("status") == "done" and created_this_week:
                completed_tasks.append({
                    "title": action["text"],
                    "project": action_project,
                    "note_path": rel_path,
                })
            elif action.get("status") == "open":
                still_open.append({
                    "title": action["text"],
                    "project": action_project,
                    "due": action.get("due"),
                    "priority": action.get("priority"),
                    "note_path": rel_path,
                })

    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "accomplished": accomplished,
        "completed_tasks": completed_tasks,
        "decisions": decisions,
        "still_open": still_open,
        "study_progress": study_progress,
    }
