"""SB-137: Vault update proposal apply operations.

All operations receive a validated absolute file path and operation-specific
parameters extracted from change_json. Operations are surgical — they modify
specific lines or frontmatter fields, never rewrite the entire file.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from writerservice.git_errors import PathTraversalError

logger = logging.getLogger(__name__)

_FRONTMATTER_FENCE_RE = re.compile(r"^---\s*$", re.MULTILINE)
_TASK_OPEN_RE = re.compile(r"^(\s*-\s*\[) \](\s+.*)$")
_TASK_DONE_RE = re.compile(r"^(\s*-\s*\[)x\](\s+.*)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def validate_vault_path(vault_root: Path, target_note_path: str) -> Path:
    """Resolve target_note_path relative to vault_root; raise on traversal."""
    if ".." in target_note_path.replace("\\", "/").split("/"):
        raise PathTraversalError(f"path traversal detected: {target_note_path!r}")
    if target_note_path.startswith("/"):
        raise PathTraversalError(f"absolute paths not allowed: {target_note_path!r}")
    parts = target_note_path.replace("\\", "/").split("/")
    if any(p.startswith(".") for p in parts if p):
        raise PathTraversalError(f"hidden paths not allowed: {target_note_path!r}")
    resolved = (vault_root / target_note_path).resolve()
    if not resolved.is_relative_to(vault_root.resolve()):
        raise PathTraversalError(f"path escapes vault root: {target_note_path!r}")
    return resolved


# ---------------------------------------------------------------------------
# Lifecycle status check
# ---------------------------------------------------------------------------


def check_lifecycle_status(file_path: Path) -> None:
    """Raise ValueError if the note has an archived or superseded lifecycle_status."""
    text = file_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return
    parts = text.split("---", 2)
    if len(parts) < 3:
        return
    frontmatter = parts[1]
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if stripped.startswith("lifecycle_status:"):
            value = stripped[len("lifecycle_status:"):].strip().strip('"').strip("'")
            if value in ("archived", "superseded"):
                raise ValueError(f"note has lifecycle_status: {value} — apply rejected")


# ---------------------------------------------------------------------------
# Anchor verification
# ---------------------------------------------------------------------------


def verify_anchor(file_path: Path, anchor_text: str) -> None:
    """Raise ValueError if anchor_text is not present in the file."""
    text = file_path.read_text(encoding="utf-8")
    if anchor_text not in text:
        raise ValueError(f"anchor not found in file: {anchor_text!r}")


# ---------------------------------------------------------------------------
# Operation implementations
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[str, str, str]:
    """Return (before_fence, frontmatter_body, rest) or raise ValueError."""
    if not text.startswith("---"):
        raise ValueError("note has no frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("note frontmatter is unclosed")
    return "---", parts[1], parts[2]


def _set_frontmatter_field(text: str, field: str, value: str) -> str:
    """Set or replace a frontmatter field; add it if absent."""
    _, fm_body, rest = _parse_frontmatter(text)
    field_re = re.compile(rf"^({re.escape(field)}:\s*).*$", re.MULTILINE)
    if field_re.search(fm_body):
        new_fm = field_re.sub(rf"\g<1>{value}", fm_body)
    else:
        new_fm = fm_body.rstrip("\n") + f"\n{field}: {value}\n"
    return f"---{new_fm}---{rest}"


def _append_tag(text: str, tag: str) -> str:
    """Append tag to tags: list in frontmatter."""
    _, fm_body, rest = _parse_frontmatter(text)
    tags_re = re.compile(r"^(tags:\s*\[)([^\]]*)\]", re.MULTILINE)
    m = tags_re.search(fm_body)
    if m:
        existing = m.group(2).strip()
        if tag in [t.strip().strip('"').strip("'") for t in existing.split(",") if t.strip()]:
            return text  # idempotent
        new_tags = f"{existing}, {tag}" if existing else tag
        new_fm = fm_body[: m.start()] + f"{m.group(1)}{new_tags}]" + fm_body[m.end():]
    else:
        new_fm = fm_body.rstrip("\n") + f"\ntags: [{tag}]\n"
    return f"---{new_fm}---{rest}"


def op_mark_task_done(file_path: Path, task_text: str) -> None:
    text = file_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if task_text in stripped:
            m = _TASK_OPEN_RE.match(stripped)
            if m:
                lines[i] = f"{m.group(1)}x]{m.group(2)}\n"
                changed = True
                break
    if changed:
        file_path.write_text("".join(lines), encoding="utf-8")


def op_mark_task_open(file_path: Path, task_text: str) -> None:
    text = file_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if task_text in stripped:
            m = _TASK_DONE_RE.match(stripped)
            if m:
                lines[i] = f"{m.group(1)} ]{m.group(2)}\n"
                changed = True
                break
    if changed:
        file_path.write_text("".join(lines), encoding="utf-8")


def op_set_task_due_date(file_path: Path, due_date: str) -> None:
    text = file_path.read_text(encoding="utf-8")
    new_text = _set_frontmatter_field(text, "due_date", due_date)
    file_path.write_text(new_text, encoding="utf-8")


def op_set_task_priority(file_path: Path, priority: str) -> None:
    text = file_path.read_text(encoding="utf-8")
    new_text = _set_frontmatter_field(text, "priority", priority)
    file_path.write_text(new_text, encoding="utf-8")


def op_append_task(file_path: Path, task_text: str) -> None:
    text = file_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    actions_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## Actions":
            actions_idx = i
            break
    new_task = f"- [ ] {task_text}\n"
    if actions_idx is not None:
        lines.insert(actions_idx + 1, new_task)
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(new_task)
    file_path.write_text("".join(lines), encoding="utf-8")


def op_append_note_section(file_path: Path, section_heading: str, section_body: str) -> None:
    text = file_path.read_text(encoding="utf-8")
    addition = f"\n## {section_heading}\n\n{section_body}\n"
    file_path.write_text(text.rstrip("\n") + "\n" + addition, encoding="utf-8")


def op_move_note_to_folder(vault_root: Path, file_path: Path, new_folder: str) -> Path:
    """Move file to new_folder under vault_root via git mv; return new absolute path."""
    new_folder_path = validate_vault_path(vault_root, new_folder)
    new_folder_path.mkdir(parents=True, exist_ok=True)
    dest = new_folder_path / file_path.name
    result = subprocess.run(
        ["git", "mv", str(file_path), str(dest)],
        cwd=str(vault_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git mv failed: {result.stderr}")
    return dest


def op_add_project_tag(file_path: Path, tag: str) -> None:
    text = file_path.read_text(encoding="utf-8")
    new_text = _append_tag(text, tag)
    file_path.write_text(new_text, encoding="utf-8")


def op_add_weekly_review_entry(file_path: Path, entry: str) -> None:
    text = file_path.read_text(encoding="utf-8")
    addition = f"\n{entry}\n"
    file_path.write_text(text.rstrip("\n") + "\n" + addition, encoding="utf-8")


# ---------------------------------------------------------------------------
# Apply result
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    changed_path: str
    commit_hash: str | None
    audit_record: dict


# ---------------------------------------------------------------------------
# Main apply entry point
# ---------------------------------------------------------------------------


def apply_proposal(
    vault_root: Path,
    proposal_id: str,
    operation: str,
    target_note_path: str,
    target_anchor_json: str | None,
    change_json: str,
    audit_log_path: Path,
    git_sync_enabled: bool,
) -> ApplyResult:
    """Apply a single approved proposal under the existing flock writer lock."""
    from writerservice.git_ops import (
        check_index_lock,
        check_working_tree_clean,
        git_add,
        git_commit,
        git_fetch,
        git_merge_ff_only,
        git_push,
        git_rev_parse_head,
    )

    change = json.loads(change_json)

    # Resolve and validate path
    file_path = validate_vault_path(vault_root, target_note_path)

    if not file_path.exists():
        raise FileNotFoundError(f"target note not found: {target_note_path!r}")

    # Lifecycle protection
    check_lifecycle_status(file_path)

    # Anchor verification (if provided)
    if target_anchor_json:
        anchor_data = json.loads(target_anchor_json)
        anchor_text = anchor_data.get("task_text") or anchor_data.get("anchor_text") or ""
        if anchor_text:
            verify_anchor(file_path, anchor_text)

    # Sync before mutation — all Git helpers raise on failure
    if git_sync_enabled:
        check_index_lock(vault_root)
        check_working_tree_clean(vault_root)
        git_fetch(vault_root)
        git_merge_ff_only(vault_root)

    # Execute operation
    final_path = file_path
    op = operation
    if op == "mark_task_done":
        op_mark_task_done(file_path, change["task_text"])
    elif op == "mark_task_open":
        op_mark_task_open(file_path, change["task_text"])
    elif op == "set_task_due_date":
        op_set_task_due_date(file_path, change["due_date"])
    elif op == "set_task_priority":
        op_set_task_priority(file_path, change["priority"])
    elif op == "append_task":
        op_append_task(file_path, change["task_text"])
    elif op == "append_note_section":
        op_append_note_section(file_path, change["section_heading"], change.get("section_body", ""))
    elif op == "move_note_to_folder":
        final_path = op_move_note_to_folder(vault_root, file_path, change["new_folder"])
    elif op == "add_project_tag":
        op_add_project_tag(file_path, change["tag"])
    elif op == "add_weekly_review_entry":
        op_add_weekly_review_entry(file_path, change["entry"])
    else:
        raise ValueError(f"unsupported operation: {op!r}")

    # Relative path for audit / return
    changed_rel_path = str(final_path.relative_to(vault_root))

    # Build and persist audit record before commit so it is included in the commit
    now = datetime.now(UTC).isoformat()
    audit_record: dict[str, Any] = {
        "event": "VAULT_UPDATE_APPLIED",
        "proposal_id": proposal_id,
        "operation": op,
        "target_path": changed_rel_path,
        "timestamp": now,
    }
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(audit_record, separators=(",", ":")) + "\n")

    # Stage and commit — all helpers raise on failure
    git_add(vault_root, str(final_path), str(audit_log_path))
    msg = f"vault update: {op} on {changed_rel_path} [{proposal_id}]"
    git_commit(vault_root, msg)
    commit_hash = git_rev_parse_head(vault_root)

    if git_sync_enabled:
        git_push(vault_root)

    return ApplyResult(
        changed_path=changed_rel_path,
        commit_hash=commit_hash,
        audit_record=audit_record,
    )
