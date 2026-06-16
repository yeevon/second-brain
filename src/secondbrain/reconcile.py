from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import discord

from secondbrain.observability import log_metadata


LAST_RECONCILED_MESSAGE_ID = "last_reconciled_discord_message_id"

_RECONCILE_FAILURE_WARNING = (
    "⚠️ Second Brain reconciliation failed.\n"
    "Some Discord captures may be delayed.\n"
    "The next scheduled scan will retry automatically."
)

_RECONCILE_LIMIT_WARNING = (
    "⚠️ Second Brain reconciliation reached its scan limit.\n"
    "A backlog remains. Recovery will continue during the next scheduled scan."
)


@dataclass(frozen=True)
class CaptureDisposition:
    capture_id: str
    created: bool
    status: str
    queued: bool


@dataclass(frozen=True)
class ReconcileResult:
    mode: str = "startup"
    seen: int = 0
    handled: int = 0
    recovered: int = 0
    duplicates: int = 0
    ignored: int = 0
    limit_exceeded: bool = False
    warning: str | None = None
    high_water_message_id: str | None = None


async def reconcile_discord_history(
    *,
    client,
    settings,
    ledger,
    handle_capture,
    mode: str,
    scan_limit: int,
) -> ReconcileResult:
    channel = client.get_channel(settings.discord_capture_channel_id)
    if channel is None:
        channel = await client.fetch_channel(settings.discord_capture_channel_id)

    last_message_id = ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID)
    after = discord.Object(id=int(last_message_id)) if last_message_id else None

    messages = [
        message
        async for message in channel.history(
            limit=scan_limit + 1,
            after=after,
            oldest_first=True,
        )
    ]

    limit_exceeded = len(messages) > scan_limit
    if limit_exceeded:
        messages = messages[:scan_limit]

    warning: str | None = None
    if limit_exceeded:
        if mode == "startup":
            warning = (
                f"startup reconciliation reached scan limit of {scan_limit} messages; "
                "backlog remains and periodic reconciliation will continue recovery"
            )
        else:
            warning = (
                f"periodic reconciliation reached scan limit of {scan_limit} messages; "
                "backlog remains and the next scan will continue"
            )

    handled = 0
    recovered = 0
    duplicates = 0
    ignored = 0
    high_water_message_id: str | None = None

    for message in messages:
        disposition = await handle_capture(message)
        if disposition is None:
            ignored += 1
        else:
            handled += 1
            if disposition.created:
                recovered += 1
            else:
                duplicates += 1
        # Advance marker only after message reaches a durable disposition.
        # If handle_capture raises, this line is not reached and the marker
        # remains unchanged so the next pass retries the same message.
        ledger.advance_system_state_snowflake(LAST_RECONCILED_MESSAGE_ID, str(message.id))
        high_water_message_id = str(message.id)

    return ReconcileResult(
        mode=mode,
        seen=len(messages),
        handled=handled,
        recovered=recovered,
        duplicates=duplicates,
        ignored=ignored,
        limit_exceeded=limit_exceeded,
        warning=warning,
        high_water_message_id=high_water_message_id,
    )


def _record_periodic_failure_best_effort(*, ledger, exc: Exception) -> None:
    operations = (
        (
            "periodic_reconcile_failures_total",
            lambda: ledger.increment_system_counter("periodic_reconcile_failures_total"),
        ),
        (
            "periodic_reconcile_last_warning",
            lambda: ledger.set_system_state("periodic_reconcile_last_warning", "scan_failed"),
        ),
        (
            "periodic_reconcile_last_error_type",
            lambda: ledger.set_system_state("periodic_reconcile_last_error_type", type(exc).__name__),
        ),
    )
    for key, operation in operations:
        try:
            operation()
        except Exception as persist_exc:
            log_metadata(
                "discord_reconcile_failure_state_write_failed",
                metric_key=key,
                error_type=type(persist_exc).__name__,
            )


async def run_periodic_reconciliation(
    *,
    client,
    settings,
    ledger,
    handle_capture,
) -> None:
    scan_lock = asyncio.Lock()
    while True:
        await asyncio.sleep(settings.periodic_reconcile_interval_seconds)
        async with scan_lock:
            try:
                await _run_one_periodic_pass(
                    client=client,
                    settings=settings,
                    ledger=ledger,
                    handle_capture=handle_capture,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_metadata(
                    "discord_reconcile_loop_error",
                    mode="periodic",
                    error_type=type(exc).__name__,
                )
                _record_periodic_failure_best_effort(ledger=ledger, exc=exc)
                await _send_reconcile_warning(client, settings, _RECONCILE_FAILURE_WARNING)


async def _run_one_periodic_pass(
    *,
    client,
    settings,
    ledger,
    handle_capture,
) -> None:
    now = datetime.now(UTC).isoformat()
    ledger.set_system_state("periodic_reconcile_last_run_at", now)
    ledger.increment_system_counter("periodic_reconcile_runs_total")

    try:
        result = await reconcile_discord_history(
            client=client,
            settings=settings,
            ledger=ledger,
            handle_capture=handle_capture,
            mode="periodic",
            scan_limit=settings.periodic_reconcile_limit,
        )
    except Exception as exc:
        log_metadata(
            "discord_reconcile_failed",
            mode="periodic",
            error_type=type(exc).__name__,
        )
        _record_periodic_failure_best_effort(ledger=ledger, exc=exc)
        await _send_reconcile_warning(client, settings, _RECONCILE_FAILURE_WARNING)
        return

    ledger.increment_system_counter("periodic_reconcile_recovered_total", result.recovered)
    ledger.increment_system_counter("periodic_reconcile_duplicates_total", result.duplicates)
    ledger.increment_system_counter("periodic_reconcile_ignored_total", result.ignored)

    success_now = datetime.now(UTC)
    success_now_iso = success_now.isoformat()
    ledger.set_system_state("periodic_reconcile_last_success_at", success_now_iso)
    ledger.set_system_state("periodic_reconcile_last_recovered_count", str(result.recovered))
    ledger.record_successful_reconciliation(mode="periodic", now=success_now)

    if result.limit_exceeded:
        ledger.increment_system_counter("periodic_reconcile_limit_exceeded_total")
        ledger.set_system_state("periodic_reconcile_last_warning", "scan_limit_reached")
        log_metadata(
            "discord_reconcile_limit_exceeded",
            mode="periodic",
            scan_limit=settings.periodic_reconcile_limit,
            high_water_message_id=result.high_water_message_id,
        )
        await _send_reconcile_warning(client, settings, _RECONCILE_LIMIT_WARNING)
    else:
        ledger.set_system_state("periodic_reconcile_last_warning", "none")

    log_metadata(
        "discord_reconcile_completed",
        mode=result.mode,
        seen=result.seen,
        handled=result.handled,
        recovered=result.recovered,
        duplicates=result.duplicates,
        ignored=result.ignored,
        limit_exceeded=result.limit_exceeded,
        high_water_message_id=result.high_water_message_id,
    )


async def _send_reconcile_warning(client, settings, content: str) -> None:
    try:
        channel = client.get_channel(settings.discord_capture_channel_id)
        if channel is None:
            channel = await client.fetch_channel(settings.discord_capture_channel_id)
        await channel.send(content)
    except Exception as exc:
        log_metadata(
            "reconcile_warning_delivery_failed",
            error_type=type(exc).__name__,
        )
