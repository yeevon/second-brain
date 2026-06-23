from __future__ import annotations

import json
import logging
import re as _re
import sys
from datetime import UTC, datetime
from typing import Any


class _LiveStdoutHandler(logging.StreamHandler):
    """StreamHandler that always resolves sys.stdout at emit time.

    This makes it compatible with pytest's capsys fixture, which temporarily
    replaces sys.stdout per test. A regular StreamHandler caches the stream
    object at construction time and would miss the capsys redirect.
    """

    @property  # type: ignore[override]
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value) -> None:
        pass  # always use sys.stdout


_log = logging.getLogger(__name__)
_log.setLevel(logging.INFO)
_log.propagate = False
_handler = _LiveStdoutHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
_log.addHandler(_handler)


_TRACEBACK_PATTERN = _re.compile(r"Traceback \(most recent call last\)", _re.IGNORECASE)
_EXCEPTION_BODY_PATTERN = _re.compile(r"[A-Za-z]+Error: .{40,}", _re.IGNORECASE)
_MAX_FIELD_VALUE_LEN = 500


def _safe_field_value(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if _TRACEBACK_PATTERN.search(value):
        return f"[traceback redacted — use error_type instead, key={key!r}]"
    if _EXCEPTION_BODY_PATTERN.search(value):
        return f"[exception body redacted — use error_type instead, key={key!r}]"
    if len(value) > _MAX_FIELD_VALUE_LEN:
        return value[:_MAX_FIELD_VALUE_LEN] + "…[truncated]"
    return value


def log_metadata(event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "level": "INFO",
        "logger": __name__,
        "message": event,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    payload.update({
        key: _safe_field_value(key, value)
        for key, value in fields.items()
        if value is not None
    })
    _log.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))
