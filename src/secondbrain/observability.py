from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any


def log_metadata(event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
