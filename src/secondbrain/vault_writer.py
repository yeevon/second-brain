from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re

from secondbrain.classifier import CLASSIFIER_PROMPT_VERSION
from secondbrain.models import Classification


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
        self.vault_path = Path(vault_path)
        if not self.vault_path.is_absolute():
            raise ValueError("vault_path must be absolute")

    def write_note(
        self,
        *,
        capture_id: str,
        source_message_id: str,
        created_at: datetime,
        classification: Classification,
        model: str,
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
        )
        note_path.write_text(markdown, encoding="utf-8")

        relative_path = _relative_posix(note_path, self.vault_path)
        self._append_audit_event(
            {
                "capture_id": capture_id,
                "event": "FILED" if classification.folder != "inbox" else "INBOX",
                "path": relative_path,
                "timestamp": _iso(_now()),
            }
        )

        return WriteResult(
            note_path=relative_path,
            absolute_path=note_path,
            created=True,
        )

    def find_note_by_capture_id(self, capture_id: str) -> Path | None:
        if not self.vault_path.exists():
            return None

        needle = f"capture_id: {capture_id}"
        for path in self.vault_path.rglob("*.md"):
            if path.is_file() and needle in path.read_text(encoding="utf-8"):
                return path
        return None

    def _note_path(
        self,
        *,
        capture_id: str,
        created_at: datetime,
        classification: Classification,
    ) -> Path:
        if classification.folder not in FOLDER_MAPPING:
            raise ValueError(f"unsupported folder: {classification.folder}")

        folder = FOLDER_MAPPING[classification.folder]
        relative_parts = [folder]
        if classification.folder == "projects":
            relative_parts.append(sanitize_slug(classification.project or "general"))

        filename = (
            f"{created_at.strftime('%Y-%m-%d')}--"
            f"{capture_id}--"
            f"{sanitize_slug(classification.title)}.md"
        )
        relative_parts.append(filename)

        path = (self.vault_path / Path(*relative_parts)).resolve()
        if not path.is_relative_to(self.vault_path):
            raise ValueError("note path escaped vault root")
        return path

    def _append_audit_event(self, event: dict) -> None:
        log_dir = self.vault_path / "99_log"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "events.ndjson"
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, separators=(",", ":"), sort_keys=True))
            file.write("\n")


def render_markdown(
    *,
    capture_id: str,
    source_message_id: str,
    created_at: datetime,
    classification: Classification,
    model: str,
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
    lines.extend(f"  - {yaml_scalar(tag)}" for tag in classification.tags)

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
            f"prompt_version: {yaml_scalar(CLASSIFIER_PROMPT_VERSION)}",
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
        lines.extend(f"- [ ] {action.text}" for action in classification.actions if action.status == "open")

    return "\n".join(lines).rstrip() + "\n"


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    slug = slug.strip("-")
    return slug or "untitled"


def yaml_scalar(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./@+\- ]*", value):
        return value
    return json.dumps(value)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()
