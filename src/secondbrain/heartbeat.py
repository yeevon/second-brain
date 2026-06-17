from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from secondbrain.observability import log_metadata


async def run_capture_service_heartbeat(
    *,
    ledger,
    instance_id: str,
    interval_seconds: int,
    reaper_liveness_threshold_s: int = 300,
    reconcile_liveness_threshold_s: int = 300,
) -> None:
    while True:
        try:
            updated = ledger.record_capture_service_heartbeat(
                instance_id=instance_id,
                now=datetime.now(UTC),
            )
            if not updated:
                log_metadata(
                    "capture_service_heartbeat_superseded",
                    instance_id=instance_id,
                )
                return
            _check_background_task_liveness(
                ledger=ledger,
                reaper_liveness_threshold_s=reaper_liveness_threshold_s,
                reconcile_liveness_threshold_s=reconcile_liveness_threshold_s,
            )
        except Exception as exc:
            log_metadata(
                "capture_service_heartbeat_failed",
                instance_id=instance_id,
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(interval_seconds)


def _check_background_task_liveness(
    *,
    ledger,
    reaper_liveness_threshold_s: int,
    reconcile_liveness_threshold_s: int,
) -> None:
    now = datetime.now(UTC)
    background_task_stale = False

    reaper_heartbeat_str = ledger.get_system_state("reaper_last_heartbeat_at")
    if reaper_heartbeat_str:
        reaper_age = (now - datetime.fromisoformat(reaper_heartbeat_str)).total_seconds()
        if reaper_age > reaper_liveness_threshold_s:
            background_task_stale = True
            log_metadata(
                "background_task_stale",
                task="reaper",
                age_seconds=int(reaper_age),
                threshold_seconds=reaper_liveness_threshold_s,
            )

    reconcile_heartbeat_str = ledger.get_system_state("reconcile_last_heartbeat_at")
    if reconcile_heartbeat_str:
        reconcile_age = (now - datetime.fromisoformat(reconcile_heartbeat_str)).total_seconds()
        if reconcile_age > reconcile_liveness_threshold_s:
            background_task_stale = True
            log_metadata(
                "background_task_stale",
                task="reconcile",
                age_seconds=int(reconcile_age),
                threshold_seconds=reconcile_liveness_threshold_s,
            )

    try:
        ledger.set_system_state("background_task_stale", "true" if background_task_stale else "false")
    except Exception as exc:
        log_metadata(
            "background_task_liveness_write_failed",
            error_type=type(exc).__name__,
        )
