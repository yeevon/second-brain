from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def append_audit_event(
    *,
    log_path: Path,
    capture_id: str,
    note_path: str,
    delivery_attempt: int,
    idempotent: bool,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "event": "NOTE_FILED",
        "capture_id": capture_id,
        "note_path": note_path,
        "delivery_attempt": delivery_attempt,
        "idempotent": idempotent,
        "timestamp": _now_iso(),
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
