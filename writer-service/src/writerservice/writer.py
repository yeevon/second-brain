from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from writerservice.api_models import Classification


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


@dataclass(frozen=True)
class WriteResult:
    note_path: str
    absolute_path: Path
    created: bool


class VaultWriter:
    def __init__(self, vault_path: Path | str) -> None:
        configured_path = Path(vault_path)
        if not configured_path.is_absolute():
            raise ValueError("vault_path must be absolute")
        self.vault_path = configured_path.resolve()

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
    ) -> WriteResult:
        existing_path = self.find_note_by_capture_id(capture_id)
        if existing_path is not None:
            return WriteResult(
                note_path=_relative_posix(existing_path, self.vault_path),
                absolute_path=existing_path,
                created=False,
            )

        note_path = self._note_path(
            capture_id=capture_id,
            created_at=created_at,
            classification=classification,
            inbox_reason=inbox_reason,
        )
        note_path.parent.mkdir(parents=True, exist_ok=True)

        if note_path.exists():
            raise FileExistsError(f"refusing to overwrite unrelated note: {note_path}")

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

        return WriteResult(
            note_path=relative_path,
            absolute_path=note_path,
            created=True,
        )

    def find_note_by_capture_id(self, capture_id: str) -> Path | None:
        """Return path if exactly one match, None if zero. Raises DuplicateCaptureError on 2+."""
        matches = self._find_all_notes_by_capture_id(capture_id)
        if len(matches) == 0:
            return None
        if len(matches) == 1:
            return matches[0]
        raise DuplicateCaptureError(
            f"multiple notes found for capture_id {capture_id!r}: {matches}"
        )

    def _find_all_notes_by_capture_id(self, capture_id: str) -> list[Path]:
        if not self.vault_path.exists():
            return []

        needles = {
            f"capture_id: {capture_id}",
            f"capture_id: {yaml_scalar(capture_id)}",
        }
        found: list[Path] = []
        for path in self.vault_path.rglob("*.md"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
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
                relative_parts.append(sanitize_slug(classification.project or "general"))

        _validate_path_components(relative_parts)

        filename = (
            f"{created_at.strftime('%Y-%m-%d')}--"
            f"{capture_id}--"
            f"{sanitize_slug(classification.title)}.md"
        )
        _validate_path_components([filename])
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
            log_path=Path(self.vault_path / "99_log" / "events.ndjson"),
            capture_id=capture_id,
            note_path=note_path,
            delivery_attempt=delivery_attempt,
            idempotent=idempotent,
        )


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


def _validate_path_components(parts: list[str]) -> None:
    for part in parts:
        if not part:
            raise ValueError("empty path component")
        if "\x00" in part:
            raise ValueError("path component contains null byte")
        if part.startswith("."):
            raise ValueError("path component starts with dot")
        if ".." in part:
            raise ValueError("path component contains ..")
        if "/" in part:
            raise ValueError("path component contains /")
        if part.startswith("/"):
            raise ValueError("absolute path component")


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _iso(value: datetime) -> str:
    return value.isoformat()
