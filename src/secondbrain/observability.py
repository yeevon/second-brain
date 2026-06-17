from __future__ import annotations

import json
import logging
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


def log_metadata(event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "level": "INFO",
        "logger": __name__,
        "message": event,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    _log.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))
