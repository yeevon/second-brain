from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol

from secondbrain.observability import log_metadata


_RETRY_RECEIPT = (
    "⚠️ {capture_id} captured, but downstream processing was interrupted.\n"
    "Your original note is safe. The system will retry automatically."
)

_FAILED_RECEIPT = (
    "❌ {capture_id} captured, but filing failed after repeated retries.\n"
    "Your original note is safe in the local ledger.\n"
    "Manual review is required."
)


class DownstreamDeliveryClient(Protocol):
    async def forward_capture(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
    ) -> None:
        ...


async def run_delivery_dispatcher(
    *,
    settings,
    ledger,
    downstream_client: DownstreamDeliveryClient,
) -> None:
    while True:
        await asyncio.sleep(settings.delivery_dispatch_interval_seconds)
        await _run_one_dispatch_pass(
            settings=settings,
            ledger=ledger,
            downstream_client=downstream_client,
        )


async def _run_one_dispatch_pass(
    *,
    settings,
    ledger,
    downstream_client: DownstreamDeliveryClient,
) -> None:
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=settings.delivery_forward_lease_seconds)

    # Claim committed before any network call
    claimed = ledger.claim_due_deliveries(
        now=now,
        lease_until=lease_until,
        batch_size=settings.delivery_dispatch_batch_size,
    )

    for capture in claimed:
        attempt = capture.delivery_attempts
        processing_lease = now + timedelta(seconds=settings.delivery_processing_lease_seconds)
        try:
            await downstream_client.forward_capture(
                capture_id=capture.capture_id,
                delivery_attempt=attempt,
            )
        except Exception as exc:
            error_type = type(exc).__name__
            log_metadata(
                "delivery_webhook_failed",
                capture_id=capture.capture_id,
                delivery_attempt=attempt,
                error_type=error_type,
            )
            ledger.schedule_retry(
                capture_id=capture.capture_id,
                delivery_attempt=attempt,
                now=datetime.now(UTC),
                error_type=error_type,
                reason_type="webhook_failure",
                max_attempts=settings.delivery_max_attempts,
                base_delay_seconds=settings.delivery_retry_base_delay_seconds,
                max_delay_seconds=settings.delivery_retry_max_delay_seconds,
            )
            continue

        processing_lease = datetime.now(UTC) + timedelta(seconds=settings.delivery_processing_lease_seconds)
        ledger.mark_forwarded(
            capture_id=capture.capture_id,
            delivery_attempt=attempt,
            lease_until=processing_lease,
        )
        log_metadata(
            "capture_forwarded",
            capture_id=capture.capture_id,
            delivery_attempt=attempt,
        )


async def run_stale_lease_reaper(
    *,
    settings,
    ledger,
    receipt_client,
) -> None:
    while True:
        await asyncio.sleep(settings.delivery_reaper_interval_seconds)
        await _run_one_reaper_pass(
            settings=settings,
            ledger=ledger,
            receipt_client=receipt_client,
        )


async def _run_one_reaper_pass(
    *,
    settings,
    ledger,
    receipt_client,
    _now: datetime | None = None,
) -> None:
    now = _now if _now is not None else datetime.now(UTC)
    try:
        result = ledger.requeue_expired_leases(
            now=now,
            batch_size=settings.delivery_reaper_batch_size,
            max_attempts=settings.delivery_max_attempts,
            base_delay_seconds=settings.delivery_retry_base_delay_seconds,
            max_delay_seconds=settings.delivery_retry_max_delay_seconds,
        )
    except Exception as exc:
        log_metadata(
            "stale_lease_reaper_failed",
            error_type=type(exc).__name__,
        )
        return

    log_metadata(
        "stale_lease_reaper_completed",
        requeued=result.requeued,
        terminal_failures=result.terminal_failures,
    )

    # Send visible Discord alerts for terminal failures only after SQLite commits
    for capture_id in result.failed_capture_ids:
        await _send_failure_alert(capture_id, receipt_client, settings)


async def _send_failure_alert(capture_id: str, receipt_client, settings) -> None:
    try:
        capture = None
        try:
            # Try to edit the original receipt
            from secondbrain.ledger import Ledger  # avoid circular at module level
        except Exception:
            pass

        channel = receipt_client.get_channel(settings.discord_capture_channel_id)
        if channel is None:
            channel = await receipt_client.fetch_channel(settings.discord_capture_channel_id)
        content = _FAILED_RECEIPT.format(capture_id=capture_id)
        await channel.send(content)
    except Exception as exc:
        log_metadata(
            "delivery_failure_alert_send_failed",
            capture_id=capture_id,
            error_type=type(exc).__name__,
        )
