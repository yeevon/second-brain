from __future__ import annotations

import logging
import os

from secondbrain.observability import _LiveStdoutHandler


def configure_logging() -> None:
    """Configure application logging from LOG_LEVEL environment variable (default INFO).

    All secondbrain operational events flow through log_metadata() in observability.py,
    which routes through logging.getLogger('secondbrain.observability'). This function:
    - Sets the log level for the secondbrain logger tree from LOG_LEVEL.
    - Adds a stdout StreamHandler to the root 'secondbrain' logger if not already set.
    - Quiets noisy third-party loggers that default to DEBUG/INFO.
    """
    level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)

    logger = logging.getLogger("secondbrain")
    logger.setLevel(level)
    if not logger.handlers:
        handler = _LiveStdoutHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    for noisy in ("discord", "httpx", "httpcore", "uvicorn", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
