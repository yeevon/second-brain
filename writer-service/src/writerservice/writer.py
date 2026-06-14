from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from writerservice.api_models import Classification
from writerservice.git_errors import CaptureDuplicateError, PathTraversalError

logger = logging.getLogger(__name__)


class DuplicateCaptureError(Exception):
    pass


FOLDER_MAPPING = {
    "inbox": "00_inbox",
    "people": "10_people",
    "projects": "20_projects",
    "ideas": "30_ideas",
    "learning": "40_learning",
    "admin": "50_admin",
}

# Patterns that signal traversal-shaped input regardless of eventual sanitization.
# Checked on raw field values before any slug conversion.
_TRAVERSAL_RE = re.compile(r"\.\.|/|\\|\x00")


@dataclass(frozen=True)
class WriteResult:
    note_path: str
    absolute_path: Path
    created: bool
    git_commit_hash: str | None = None


class VaultWriter:
    def __init__(self, vault_path: Path | str, audit_log_path: Path | str | None = None) -> None:
        configured_path = Path(vault_path)
        if not configured_path.is_absolute():
            raise ValueError("vault_path must be absolute")
        self.vault_path = configured_path.resolve()
        if audit_log_path is not None:
            self.audit_log_path: Path = Path(audit_log_path)
        else:
            self.audit_log_path = self.vault_path / "99_log" / "events.ndjson"

    def write_note(
        self,
        *,
        capture_id: str,
        source_message_id: str,
        created_at: datetime,
        classification: Classification,
        model: str,
        prompt_version: str,
        delivery_attempt: int,
        inbox_reason: str | None,
        git_sync_enabled: bool = False,
    ) -> WriteResult:
        from writerservice.flock import vault_write_lock

        lock_path = self.vault_path / ".writer.lock"
        with vault_write_lock(lock_path):
            return self._write_under_lock(
                capture_id=capture_id,
                source_message_id=source_message_id,
                created_at=created_at,
                classification=classification,
                model=model,
                prompt_version=prompt_version,
                delivery_attempt=delivery_attempt,
                inbox_reason=inbox_reason,
                git_sync_enabled=git_sync_enabled,
            )

    def _write_under_lock(
        self,
        *,
        capture_id: str,
        source_message_id: str,
        created_at: datetime,
        classification: Classification,
        model: str,
        prompt_version: str,
        delivery_attempt: int,
        inbox_reason: str | None,
        git_sync_enabled: bool,
    ) -> WriteResult:
        from writerservice.git_ops import (
            check_index_lock,
            check_working_tree_clean,
            git_add,
            git_commit,
            git_fetch,
            git_log_hash_for_path,
            git_merge_ff_only,
            git_push,
            git_rev_parse_head,
        )

        if git_sync_enabled:
            check_index_lock(self.vault_path)
            git_fetch(self.vault_path)
            git_merge_ff_only(self.vault_path)

        existing_path = self.find_note_by_capture_id(capture_id)
        if existing_path is not None:
            commit_hash: str | None = None
            if git_sync_enabled:
                commit_hash = git_log_hash_for_path(self.vault_path, existing_path)
            return WriteResult(
                note_path=_relative_posix(existing_path, self.vault_path),
                absolute_path=existing_path,
                created=False,
                git_commit_hash=commit_hash,
            )

        if git_sync_enabled:
            check_working_tree_clean(self.vault_path)

        note_path = self._note_path(
            capture_id=capture_id,
            created_at=created_at,
            classification=classification,
            inbox_reason=inbox_reason,
        )
        note_path.parent.mkdir(parents=True, exist_ok=True)

        if note_path.exists():
            raise FileExistsError(f"refusing to overwrite unrelated note: {note_path}")

        pre_write_head: str | None = None
        audit_log_existed = self.audit_log_path.exists()
        if git_sync_enabled:
            pre_write_head = git_rev_parse_head(self.vault_path)

        try:
            markdown = render_markdown(
                capture_id=capture_id,
                source_message_id=source_message_id,
                created_at=created_at,
                classification=classification,
                model=model,
                prompt_version=prompt_version,
            )
            note_path.write_text(markdown, encoding="utf-8")

            relative_path = _relative_posix(note_path, self.vault_path)
            self._append_audit_event(
                capture_id=capture_id,
                note_path=relative_path,
                delivery_attempt=delivery_attempt,
                idempotent=False,
            )

            git_hash: str | None = None
            if git_sync_enabled:
                git_add(self.vault_path, relative_path, "99_log/events.ndjson")
                git_commit(
                    self.vault_path,
                    f"note: {capture_id} via writer-service",
                )
                git_hash = git_rev_parse_head(self.vault_path)
                git_push(self.vault_path)

        except Exception:
            if git_sync_enabled and pre_write_head is not None:
                _rollback_to_head(
                    self.vault_path,
                    pre_write_head,
                    note_path,
                    audit_log_existed,
                    self.audit_log_path,
                )
            raise

        return WriteResult(
            note_path=relative_path,
            absolute_path=note_path,
            created=True,
            git_commit_hash=git_hash,
        )

    def find_note_by_capture_id(self, capture_id: str) -> Path | None:
        matches = self._find_all_notes_by_capture_id(capture_id)
        if len(matches) == 0:
            return None
        if len(matches) == 1:
            return matches[0]
        raise CaptureDuplicateError(
            f"Found {len(matches)} vault files with capture_id {capture_id!r}. "
            "Manual deduplication required. "
            f"Files: {', '.join(str(p.relative_to(self.vault_path)) for p in matches)}"
        )

    def _find_all_notes_by_capture_id(self, capture_id: str) -> list[Path]:
        if not self.vault_path.exists():
            return []
        found: list[Path] = []
        for path in self.vault_path.rglob("*.md"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if _frontmatter_capture_id(text) == capture_id:
                found.append(path)
        return found

    def _note_path(
        self,
        *,
        capture_id: str,
        created_at: datetime,
        classification: Classification,
        inbox_reason: str | None,
    ) -> Path:
        if inbox_reason is not None:
            folder = "00_inbox"
            relative_parts = [folder]
        else:
            if classification.folder not in FOLDER_MAPPING:
                raise ValueError(f"unsupported folder: {classification.folder}")
            folder = FOLDER_MAPPING[classification.folder]
            relative_parts = [folder]
            if classification.folder == "projects":
                raw_project = classification.project or "general"
                _reject_traversal_input(raw_project, "project")
                relative_parts.append(sanitize_slug(raw_project))

        raw_title = classification.title
        _reject_traversal_input(raw_title, "title")

        filename = (
            f"{created_at.strftime('%Y-%m-%d')}--"
            f"{capture_id}--"
            f"{sanitize_slug(raw_title)}.md"
        )
        relative_parts.append(filename)

        path = (self.vault_path / Path(*relative_parts)).resolve()
        if not path.is_relative_to(self.vault_path):
            raise ValueError("note path escaped vault root")
        return path

    def _append_audit_event(
        self,
        *,
        capture_id: str,
        note_path: str,
        delivery_attempt: int,
        idempotent: bool,
    ) -> None:
        from writerservice.audit import append_audit_event

        append_audit_event(
            log_path=self.audit_log_path,
            capture_id=capture_id,
            note_path=note_path,
            delivery_attempt=delivery_attempt,
            idempotent=idempotent,
        )


def _reject_traversal_input(value: str, field: str) -> None:
    """Reject raw user-supplied strings that contain traversal-shaped characters."""
    if _TRAVERSAL_RE.search(value) or value.startswith("."):
        raise PathTraversalError(
            f"Traversal-shaped input rejected in {field!r}"
        )


def _frontmatter_capture_id(text: str) -> str | None:
    """Return the capture_id from YAML frontmatter, or None if absent/unparseable."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    for line in text[4:end].splitlines():
        if line.startswith("capture_id:"):
            raw = line[len("capture_id:"):].strip()
            if raw.startswith('"') and raw.endswith('"'):
                return raw[1:-1]
            return raw
    return None


def _rollback_to_head(
    vault_path: Path,
    pre_write_head: str,
    written_note: Path,
    audit_log_existed: bool,
    audit_log_path: Path,
) -> None:
    rel_note = str(written_note.relative_to(vault_path))
    reset_result = subprocess.run(
        ["git", "reset", "--hard", pre_write_head],
        cwd=vault_path,
        check=False,
        capture_output=True,
        timeout=15,
    )
    clean_result = subprocess.run(
        ["git", "clean", "-f", rel_note],
        cwd=vault_path,
        check=False,
        capture_output=True,
        timeout=15,
    )
    if reset_result.returncode != 0 or clean_result.returncode != 0:
        logger.error(
            json.dumps({
                "event": "writer_git_rollback_failed",
                "reset_returncode": reset_result.returncode,
                "clean_returncode": clean_result.returncode,
            })
        )
    if not audit_log_existed and audit_log_path.exists():
        audit_log_path.unlink(missing_ok=True)


def render_markdown(
    *,
    capture_id: str,
    source_message_id: str,
    created_at: datetime,
    classification: Classification,
    model: str,
    prompt_version: str,
) -> str:
    lines = [
        "---",
        f"capture_id: {yaml_scalar(capture_id)}",
        f"source_message_id: {json.dumps(source_message_id)}",
        f"created_at: {yaml_scalar(_iso(created_at))}",
        f"area: {yaml_scalar(classification.folder)}",
    ]

    if classification.project:
        lines.append(f"project: {yaml_scalar(sanitize_slug(classification.project))}")

    lines.extend(
        [
            f"note_type: {yaml_scalar(classification.note_type)}",
            "tags:",
        ]
    )
    if classification.tags:
        lines.extend(f"  - {yaml_scalar(tag)}" for tag in classification.tags)
    else:
        lines.append("  []")

    lines.append("actions:")
    if classification.actions:
        for action in classification.actions:
            lines.append(f"  - text: {yaml_scalar(action.text)}")
            lines.append(f"    status: {yaml_scalar(action.status)}")
    else:
        lines.append("  []")

    lines.extend(
        [
            "lifecycle_status: active",
            f"model: {yaml_scalar(model)}",
            f"prompt_version: {yaml_scalar(prompt_version)}",
            "schema_version: 1",
            "---",
            "",
            f"# {classification.title}",
            "",
            classification.body,
        ]
    )

    if classification.actions:
        lines.extend(["", "## Actions", ""])
        lines.extend(
            f"- [ ] {action.text}"
            for action in classification.actions
            if action.status == "open"
        )

    return "\n".join(lines).rstrip() + "\n"


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    slug = slug.strip("-")
    return slug or "untitled"


def yaml_scalar(value: str) -> str:
    return json.dumps(value)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _iso(value: datetime) -> str:
    return value.isoformat()
