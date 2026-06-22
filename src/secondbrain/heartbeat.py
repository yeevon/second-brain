from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Callable, Literal

from secondbrain.observability import log_metadata


TaskStatus = Literal["running", "completed_unexpectedly", "degraded", "not_applicable"]

_TASK_KEYS = {
    "reaper": {
        "heartbeat": "reaper_last_heartbeat_at",
        "status": "reaper_task_status",
        "last_error": "reaper_last_error_type",
    },
    "reconcile": {
        "heartbeat": "reconcile_last_heartbeat_at",
        "status": "reconcile_task_status",
        "last_error": "reconcile_last_error_type",
    },
    "delivery": {
        "heartbeat": "delivery_last_heartbeat_at",
        "status": "delivery_task_status",
        "last_error": "delivery_last_error_type",
    },
    "classifier": {
        "heartbeat": "classifier_last_heartbeat_at",
        "status": "classifier_task_status",
        "last_error": "classifier_last_error_type",
    },
}


async def run_capture_service_heartbeat(
    *,
    ledger,
    instance_id: str,
    interval_seconds: int,
    reaper_liveness_threshold_s: int = 300,
    reconcile_liveness_threshold_s: int = 300,
    delivery_liveness_threshold_s: int = 300,
    classifier_liveness_threshold_s: int = 300,
    # SB-137: callable so handles are resolved dynamically each tick, picking up
    # tasks that are started after the heartbeat coroutine is created.
    get_task_handles: "Callable[[], dict[str, asyncio.Task]] | None" = None,
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
            handles = get_task_handles() if get_task_handles is not None else None
            _check_background_task_liveness(
                ledger=ledger,
                reaper_liveness_threshold_s=reaper_liveness_threshold_s,
                reconcile_liveness_threshold_s=reconcile_liveness_threshold_s,
                delivery_liveness_threshold_s=delivery_liveness_threshold_s,
                classifier_liveness_threshold_s=classifier_liveness_threshold_s,
                task_handles=handles,
            )
        except Exception as exc:
            log_metadata(
                "capture_service_heartbeat_failed",
                instance_id=instance_id,
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(interval_seconds)


def mark_task_not_applicable(ledger, task: str) -> None:
    """Call once at startup for tasks that are deliberately not started in this mode."""
    keys = _TASK_KEYS.get(task)
    if keys is None:
        return
    try:
        ledger.set_system_state(keys["status"], "not_applicable")
    except Exception as exc:
        log_metadata(
            "background_task_liveness_write_failed",
            task=task,
            error_type=type(exc).__name__,
        )


def _check_background_task_liveness(
    *,
    ledger,
    reaper_liveness_threshold_s: int,
    reconcile_liveness_threshold_s: int,
    delivery_liveness_threshold_s: int = 300,
    classifier_liveness_threshold_s: int = 300,
    task_handles: dict[str, asyncio.Task] | None = None,
) -> None:
    now = datetime.now(UTC)
    background_task_stale = False
    handles = task_handles or {}

    thresholds = {
        "reaper": reaper_liveness_threshold_s,
        "reconcile": reconcile_liveness_threshold_s,
        "delivery": delivery_liveness_threshold_s,
        "classifier": classifier_liveness_threshold_s,
    }

    for task, threshold_s in thresholds.items():
        keys = _TASK_KEYS[task]
        status_val = ledger.get_system_state(keys["status"])
        if status_val == "not_applicable":
            continue

        # SB-137: check asyncio task handle for unexpected completion first
        handle = handles.get(task)
        if handle is not None and handle.done():
            exc = handle.exception() if not handle.cancelled() else None
            error_type = type(exc).__name__ if exc else ("CancelledError" if handle.cancelled() else None)
            log_metadata(
                "background_task_completed_unexpectedly",
                task=task,
                cancelled=handle.cancelled(),
                error_type=error_type,
            )
            _safe_set(ledger, keys["status"], "completed_unexpectedly")
            if error_type is not None:
                _safe_set(ledger, keys["last_error"], error_type)
            background_task_stale = True
            continue

        heartbeat_str = ledger.get_system_state(keys["heartbeat"])
        if not heartbeat_str:
            continue

        age = (now - datetime.fromisoformat(heartbeat_str)).total_seconds()
        if age > threshold_s:
            background_task_stale = True
            log_metadata(
                "background_task_stale",
                task=task,
                age_seconds=int(age),
                threshold_seconds=threshold_s,
            )
            _safe_set(ledger, keys["status"], "degraded")
        else:
            _safe_set(ledger, keys["status"], "running")

    try:
        ledger.set_system_state("background_task_stale", "true" if background_task_stale else "false")
    except Exception as exc:
        log_metadata(
            "background_task_liveness_write_failed",
            error_type=type(exc).__name__,
        )


def _safe_set(ledger, key: str, value: str) -> None:
    try:
        ledger.set_system_state(key, value)
    except Exception as exc:
        log_metadata(
            "background_task_liveness_write_failed",
            error_type=type(exc).__name__,
        )
