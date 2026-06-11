from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from secondbrain.observability import log_metadata


_RETRY_RECEIPT = (
    "⏳ {capture_id} — downstream processing was interrupted.\n"
    "Your original note is safe. The system will retry automatically."
)

_FAILED_RECEIPT = (
    "❌ {capture_id} — filing failed after repeated retries.\n"
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


class ReceiptEditClient(Protocol):
    async def edit_receipt(self, *, capture_id: str, content: str) -> Any:
        ...


async def run_delivery_dispatcher(
    *,
    settings,
    ledger,
    downstream_client: DownstreamDeliveryClient,
    receipt_edit_client: ReceiptEditClient | None = None,
) -> None:
    while True:
        await asyncio.sleep(settings.delivery_dispatch_interval_seconds)
        try:
            await _run_one_dispatch_pass(
                settings=settings,
                ledger=ledger,
                downstream_client=downstream_client,
                receipt_edit_client=receipt_edit_client,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_metadata(
                "delivery_dispatch_pass_failed",
                error_type=type(exc).__name__,
            )


async def _run_one_dispatch_pass(
    *,
    settings,
    ledger,
    downstream_client: DownstreamDeliveryClient,
    receipt_edit_client: ReceiptEditClient | None = None,
    _now: datetime | None = None,
) -> None:
    now = _now if _now is not None else datetime.now(UTC)
    lease_until = now + timedelta(seconds=settings.delivery_forward_lease_seconds)

    # Claim committed before any network call
    claimed = ledger.claim_due_deliveries(
        now=now,
        lease_until=lease_until,
        batch_size=settings.delivery_dispatch_batch_size,
    )

    for capture in claimed:
        attempt = capture.delivery_attempts
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
            try:
                disposition = ledger.schedule_retry(
                    capture_id=capture.capture_id,
                    delivery_attempt=attempt,
                    now=datetime.now(UTC),
                    error_type=error_type,
                    reason_type="webhook_failure",
                    max_attempts=settings.delivery_retry_max_attempts,
                    base_delay_seconds=settings.delivery_retry_base_delay_seconds,
                    max_delay_seconds=settings.delivery_retry_max_delay_seconds,
                )
            except ValueError:
                log_metadata(
                    "stale_delivery_retry_ignored",
                    capture_id=capture.capture_id,
                    delivery_attempt=attempt,
                )
                continue

            if receipt_edit_client is not None:
                if disposition.failed_terminally:
                    await _edit_receipt_best_effort(
                        receipt_edit_client,
                        capture_id=capture.capture_id,
                        content=_FAILED_RECEIPT.format(capture_id=capture.capture_id),
                    )
                elif disposition.retry_scheduled:
                    await _edit_receipt_best_effort(
                        receipt_edit_client,
                        capture_id=capture.capture_id,
                        content=_RETRY_RECEIPT.format(capture_id=capture.capture_id),
                    )
            continue

        processing_lease = datetime.now(UTC) + timedelta(seconds=settings.delivery_processing_lease_seconds)
        result = ledger.mark_forwarded(
            capture_id=capture.capture_id,
            delivery_attempt=attempt,
            lease_until=processing_lease,
        )
        if result.changed:
            log_metadata(
                "capture_forwarded",
                capture_id=capture.capture_id,
                delivery_attempt=attempt,
            )
        elif result.outcome == "idempotent_replay":
            log_metadata(
                "duplicate_delivery_acceptance_ignored",
                capture_id=capture.capture_id,
                delivery_attempt=attempt,
            )
        else:
            log_metadata(
                "stale_delivery_acceptance_ignored",
                capture_id=capture.capture_id,
                delivery_attempt=attempt,
                outcome=result.outcome,
            )


async def _edit_receipt_best_effort(
    receipt_edit_client: ReceiptEditClient,
    *,
    capture_id: str,
    content: str,
) -> None:
    try:
        await receipt_edit_client.edit_receipt(capture_id=capture_id, content=content)
    except Exception as exc:
        log_metadata(
            "delivery_receipt_edit_failed",
            capture_id=capture_id,
            error_type=type(exc).__name__,
        )
