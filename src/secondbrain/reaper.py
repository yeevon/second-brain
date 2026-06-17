from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

from secondbrain.capture_models import LeaseReaperResult
from secondbrain.observability import log_metadata
from secondbrain.receipts import (
    format_delivery_retry_exhausted_receipt,
    format_delivery_retry_scheduled_receipt,
)


class ReceiptEditClient(Protocol):
    async def edit_receipt(self, *, capture_id: str, content: str) -> Any:
        ...


class StaleLeaseReaper:
    def __init__(self, *, settings, ledger) -> None:
        self._settings = settings
        self._ledger = ledger
        self._lock = asyncio.Lock()

    async def run_once(
        self,
        receipt_client: ReceiptEditClient | None = None,
        _now: datetime | None = None,
    ) -> LeaseReaperResult:
        if self._lock.locked():
            log_metadata("stale_lease_reaper_overlap_skipped")
            return LeaseReaperResult(scanned=0, requeued=(), failed=())
        async with self._lock:
            return await run_stale_lease_reaper_once(
                settings=self._settings,
                ledger=self._ledger,
                receipt_client=receipt_client,
                _now=_now,
            )


async def run_stale_lease_reaper(
    *,
    settings,
    ledger,
    receipt_client: ReceiptEditClient | None = None,
) -> None:
    reaper = StaleLeaseReaper(settings=settings, ledger=ledger)
    while True:
        try:
            await reaper.run_once(receipt_client)
            ledger.set_system_state(
                "reaper_last_heartbeat_at",
                datetime.now(UTC).isoformat(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_metadata("stale_lease_reaper_failed", error_type=type(exc).__name__)
        await asyncio.sleep(settings.stale_lease_reaper_interval_seconds)


async def run_stale_lease_reaper_once(
    *,
    settings,
    ledger,
    receipt_client: ReceiptEditClient | None = None,
    _now: datetime | None = None,
) -> LeaseReaperResult:
    now = _now if _now is not None else datetime.now(UTC)
    log_metadata("stale_lease_reaper_started")
    result = ledger.reap_expired_processing_leases(
        now=now,
        batch_size=settings.stale_lease_reaper_batch_size,
        retry_max_attempts=settings.delivery_retry_max_attempts,
        retry_base_delay_seconds=settings.delivery_retry_base_delay_seconds,
        retry_max_delay_seconds=settings.delivery_retry_max_delay_seconds,
    )
    log_metadata(
        "stale_lease_reaper_completed",
        scanned=result.scanned,
        requeued_count=len(result.requeued),
        failed_count=len(result.failed),
    )
    for item in result.requeued:
        log_metadata(
            "stale_lease_requeued",
            capture_id=item.capture_id,
            previous_delivery_status=item.previous_delivery_status,
            delivery_attempts=item.delivery_attempts,
            retry_attempts=item.retry_attempts,
            next_attempt_at=item.next_attempt_at.isoformat(),
        )
    for item in result.failed:
        log_metadata(
            "retry_limit_exceeded",
            capture_id=item.capture_id,
            previous_delivery_status=item.previous_delivery_status,
            delivery_attempts=item.delivery_attempts,
            retry_attempts=item.retry_attempts,
        )
    if receipt_client is not None:
        for requeued in result.requeued:
            try:
                await receipt_client.edit_receipt(
                    capture_id=requeued.capture_id,
                    content=format_delivery_retry_scheduled_receipt(
                        requeued.capture_id,
                        retry_attempts=requeued.retry_attempts,
                        next_attempt_at=requeued.next_attempt_at,
                    ),
                )
            except Exception as exc:
                log_metadata(
                    "delivery_retry_receipt_failed",
                    capture_id=requeued.capture_id,
                    error_type=type(exc).__name__,
                )
        for failed in result.failed:
            try:
                await receipt_client.edit_receipt(
                    capture_id=failed.capture_id,
                    content=format_delivery_retry_exhausted_receipt(failed.capture_id),
                )
            except Exception as exc:
                log_metadata(
                    "delivery_failed_receipt_failed",
                    capture_id=failed.capture_id,
                    error_type=type(exc).__name__,
                )
    return result
