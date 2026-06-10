from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from secondbrain.observability import log_metadata


async def run_capture_service_heartbeat(
    *,
    ledger,
    instance_id: str,
    interval_seconds: int,
) -> None:
    while True:
        try:
            ledger.record_capture_service_heartbeat(
                instance_id=instance_id,
                now=datetime.now(UTC),
            )
        except Exception as exc:
            log_metadata(
                "capture_service_heartbeat_failed",
                instance_id=instance_id,
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(interval_seconds)
