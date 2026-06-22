from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from writerservice.api_models import AttachmentMetadata, Classification
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


class RawHashMismatchError(Exception):
    """Raised when an existing raw file has a different hash than the incoming raw body."""


@dataclass(frozen=True)
class WriteResult:
    note_path: str
    absolute_path: Path
    created: bool
    git_commit_hash: str | None = None
    raw_capture_path: str = ""
    raw_sha256: str = ""


@dataclass(frozen=True)
class MoveResult:
    old_note_path: str
    new_note_path: str
    git_commit_hash: str | None
    moved: bool


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
        raw_text: str = "",
        attachments: list[AttachmentMetadata] | None = None,
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
                raw_text=raw_text,
                attachments=attachments or [],
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
        raw_text: str,
        attachments: list[AttachmentMetadata],
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

        created_at = created_at.astimezone(UTC)

        if git_sync_enabled:
            check_index_lock(self.vault_path)
            git_fetch(self.vault_path)
            git_merge_ff_only(self.vault_path)

        existing_path = self.find_note_by_capture_id(capture_id)
        if existing_path is not None:
            commit_hash: str | None = None
            if git_sync_enabled:
                commit_hash = git_log_hash_for_path(self.vault_path, existing_path)
            # Compute incoming hash and verify raw file integrity even on idempotent replay
            incoming_raw_body = build_raw_body(raw_text, attachments)
            incoming_raw_hash = compute_raw_sha256(incoming_raw_body)
            rel_raw = raw_capture_path(capture_id, created_at)
            raw_abs = self.vault_path / rel_raw
            raw_hash = incoming_raw_hash
            if raw_abs.exists():
                _, existing_hash = parse_raw_file(raw_abs)
                if existing_hash != incoming_raw_hash:
                    raise RawHashMismatchError(
                        f"raw file hash mismatch for {capture_id}: "
                        f"existing={existing_hash!r} incoming={incoming_raw_hash!r}"
                    )
            return WriteResult(
                note_path=_relative_posix(existing_path, self.vault_path),
                absolute_path=existing_path,
                created=False,
                git_commit_hash=commit_hash,
                raw_capture_path=rel_raw,
                raw_sha256=raw_hash,
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

        # --- raw substrate ---
        raw_body = build_raw_body(raw_text, attachments)
        raw_hash = compute_raw_sha256(raw_body)
        rel_raw = raw_capture_path(capture_id, created_at)
        raw_abs = self.vault_path / rel_raw

        # write_or_verify raises RawHashMismatchError on hash collision
        raw_created = write_or_verify_raw_capture(
            raw_abs=raw_abs,
            capture_id=capture_id,
            source_message_id=source_message_id,
            created_at=created_at,
            raw_body=raw_body,
            raw_hash=raw_hash,
        )

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
                raw_capture_path=rel_raw,
                raw_sha256=raw_hash,
            )
            note_path.write_text(markdown, encoding="utf-8")

            relative_path = _relative_posix(note_path, self.vault_path)
            self._append_audit_event(
                capture_id=capture_id,
                note_path=relative_path,
                raw_capture_path=rel_raw,
                raw_sha256=raw_hash,
                delivery_attempt=delivery_attempt,
                idempotent=False,
            )

            git_hash: str | None = None
            if git_sync_enabled:
                git_add(self.vault_path, relative_path, rel_raw, "99_log/events.ndjson")
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
                    raw_abs if raw_created else None,
                    audit_log_existed,
                    self.audit_log_path,
                )
            raise

        return WriteResult(
            note_path=relative_path,
            absolute_path=note_path,
            created=True,
            git_commit_hash=git_hash,
            raw_capture_path=rel_raw,
            raw_sha256=raw_hash,
        )

    def move_note(
        self,
        *,
        capture_id: str,
        new_folder: str,
        new_project: str | None = None,
        correction_reason: str,
        git_sync_enabled: bool = False,
    ) -> "MoveResult":
        from writerservice.flock import vault_write_lock

        lock_path = self.vault_path / ".writer.lock"
        with vault_write_lock(lock_path):
            return self._move_under_lock(
                capture_id=capture_id,
                new_folder=new_folder,
                new_project=new_project,
                correction_reason=correction_reason,
                git_sync_enabled=git_sync_enabled,
            )

    def _move_under_lock(
        self,
        *,
        capture_id: str,
        new_folder: str,
        new_project: str | None,
        correction_reason: str,
        git_sync_enabled: bool,
    ) -> "MoveResult":
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

        if git_sync_enabled:
            check_index_lock(self.vault_path)
            git_fetch(self.vault_path)
            git_merge_ff_only(self.vault_path)

        current_path = self.find_note_by_capture_id(capture_id)
        if current_path is None:
            raise FileNotFoundError(f"no vault note found for capture_id {capture_id!r}")

        if git_sync_enabled:
            check_working_tree_clean(self.vault_path)

        if new_folder not in FOLDER_MAPPING:
            raise ValueError(f"unsupported folder: {new_folder}")
        target_folder = FOLDER_MAPPING[new_folder]
        relative_parts = [target_folder]
        if new_folder == "projects":
            raw_project = new_project or "general"
            _reject_traversal_input(raw_project, "project")
            relative_parts.append(sanitize_slug(raw_project))

        filename = current_path.name
        new_absolute = (self.vault_path / Path(*relative_parts) / filename).resolve()
        if not new_absolute.is_relative_to(self.vault_path):
            raise ValueError("destination path escaped vault root")

        old_relative = _relative_posix(current_path, self.vault_path)
        new_relative = _relative_posix(new_absolute, self.vault_path)

        if current_path == new_absolute:
            return MoveResult(
                old_note_path=old_relative,
                new_note_path=new_relative,
                git_commit_hash=None,
                moved=False,
            )

        new_absolute.parent.mkdir(parents=True, exist_ok=True)

        pre_move_head: str | None = None
        if git_sync_enabled:
            pre_move_head = git_rev_parse_head(self.vault_path)

        try:
            if git_sync_enabled:
                subprocess.run(
                    ["git", "mv", old_relative, new_relative],
                    cwd=self.vault_path,
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
            else:
                current_path.rename(new_absolute)

            self._append_audit_event(
                capture_id=capture_id,
                note_path=new_relative,
                delivery_attempt=0,
                idempotent=False,
            )

            git_hash: str | None = None
            if git_sync_enabled:
                git_add(self.vault_path, new_relative, "99_log/events.ndjson")
                git_commit(
                    self.vault_path,
                    f"correction: {capture_id} moved to {target_folder}",
                )
                git_hash = git_rev_parse_head(self.vault_path)
                git_push(self.vault_path)

        except Exception:
            if git_sync_enabled and pre_move_head is not None:
                subprocess.run(
                    ["git", "reset", "--hard", pre_move_head],
                    cwd=self.vault_path,
                    check=False,
                    capture_output=True,
                    timeout=15,
                )
            raise

        return MoveResult(
            old_note_path=old_relative,
            new_note_path=new_relative,
            git_commit_hash=git_hash,
            moved=True,
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
        raw_dir = self.vault_path / "00_raw"
        found: list[Path] = []
        for path in self.vault_path.rglob("*.md"):
            if not path.is_file():
                continue
            # Skip raw substrate files — they are not sanitized notes
            try:
                path.relative_to(raw_dir)
                continue
            except ValueError:
                pass
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
        raw_capture_path: str = "",
        raw_sha256: str = "",
        delivery_attempt: int,
        idempotent: bool,
    ) -> None:
        from writerservice.audit import append_audit_event

        append_audit_event(
            log_path=self.audit_log_path,
            capture_id=capture_id,
            note_path=note_path,
            raw_capture_path=raw_capture_path,
            raw_sha256=raw_sha256,
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
    written_raw: Path | None,
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
    files_to_clean = [rel_note]
    if written_raw is not None:
        files_to_clean.append(str(written_raw.relative_to(vault_path)))
    clean_result = subprocess.run(
        ["git", "clean", "-f", *files_to_clean],
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
    raw_capture_path: str = "",
    raw_sha256: str = "",
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
        ]
    )

    if raw_capture_path:
        lines.append(f"raw_capture_path: {yaml_scalar(raw_capture_path)}")
    if raw_sha256:
        lines.append(f"raw_sha256: {yaml_scalar(raw_sha256)}")
    if raw_capture_path:
        lines.append(f"derived_from_capture_id: {yaml_scalar(capture_id)}")

    lines.extend(
        [
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


def build_raw_body(raw_text: str, attachments: list[AttachmentMetadata]) -> str:
    has_text = bool(raw_text)
    has_attachments = bool(attachments)

    parts: list[str] = []
    if has_text:
        parts.append(raw_text)

    if has_attachments:
        att_lines = ["## Attachments", ""]
        for att in attachments:
            if att.content_type is not None:
                att_lines.append(f'- filename: "{att.filename}", content_type: "{att.content_type}"')
            else:
                att_lines.append(f'- filename: "{att.filename}", content_type: null')
        att_block = "\n".join(att_lines)
        if has_text:
            parts.append("\n\n" + att_block)
        else:
            parts.append(att_block)

    return "".join(parts)


def raw_capture_path(capture_id: str, created_at: datetime) -> str:
    """Return the deterministic vault-relative posix path for a raw capture file."""
    return f"00_raw/{created_at.strftime('%Y/%m')}/{capture_id}.md"


def compute_raw_sha256(raw_body: str) -> str:
    return hashlib.sha256(raw_body.encode("utf-8")).hexdigest()


def render_raw_markdown(
    *,
    capture_id: str,
    source_message_id: str,
    created_at: datetime,
    raw_body: str,
    raw_hash: str,
) -> str:
    frontmatter = "\n".join([
        "---",
        f"capture_id: {yaml_scalar(capture_id)}",
        f"created_at: {yaml_scalar(_iso(created_at))}",
        f"source_message_id: {json.dumps(source_message_id)}",
        f"raw_sha256: {yaml_scalar(raw_hash)}",
        "schema_version: 1",
        "---",
    ])
    # Preserve raw_body verbatim — no rstrip, no normalization.
    return frontmatter + "\n\n" + raw_body + "\n"


def parse_raw_file(path: Path) -> tuple[str, str]:
    """Return (raw_body, raw_sha256) parsed from an existing raw file.

    The raw file format is:
        ---
        <frontmatter>
        ---

        <raw_body>
    render_raw_markdown appends a trailing newline via .rstrip() + "\n".
    parse_raw_file reverses that by stripping the exact leading blank line and
    trailing newline that render_raw_markdown adds.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return text, ""
    end = text.find("\n---\n", 4)
    if end == -1:
        return text, ""
    raw_sha256 = ""
    for line in text[4:end].splitlines():
        if line.startswith("raw_sha256:"):
            raw_sha256 = line[len("raw_sha256:"):].strip().strip('"')
            break
    # After "\n---\n" there is a blank line ("\n") before the body.
    after_fence = text[end + 5:]  # skip "\n---\n"
    if after_fence.startswith("\n"):
        after_fence = after_fence[1:]  # skip the blank separator line
    # render_raw_markdown ends with ".rstrip() + '\n'" so we strip that trailing newline
    if after_fence.endswith("\n"):
        after_fence = after_fence[:-1]
    return after_fence, raw_sha256


def write_or_verify_raw_capture(
    *,
    raw_abs: Path,
    capture_id: str,
    source_message_id: str,
    created_at: datetime,
    raw_body: str,
    raw_hash: str,
) -> bool:
    """Write raw capture file atomically, or verify existing file hash matches.

    Returns True if a new file was created, False if idempotent (already exists with matching hash).
    Raises RawHashMismatchError if the file exists with a different hash.
    """
    if raw_abs.exists():
        existing_body, existing_hash = parse_raw_file(raw_abs)
        recomputed = compute_raw_sha256(existing_body)
        incoming_hash = raw_hash
        if existing_hash == incoming_hash and recomputed == incoming_hash:
            return False
        raise RawHashMismatchError(
            f"raw file hash mismatch for {capture_id}: "
            f"existing={existing_hash!r} recomputed={recomputed!r} incoming={incoming_hash!r}"
        )

    raw_abs.parent.mkdir(parents=True, exist_ok=True)
    content = render_raw_markdown(
        capture_id=capture_id,
        source_message_id=source_message_id,
        created_at=created_at,
        raw_body=raw_body,
        raw_hash=raw_hash,
    )
    # Atomic write: temp file + rename
    dir_ = raw_abs.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".raw_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, raw_abs)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True


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
