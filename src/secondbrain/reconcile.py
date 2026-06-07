from __future__ import annotations

from dataclasses import dataclass

import discord

from secondbrain.discord_capture import should_capture_message
from secondbrain.ledger import Ledger


LAST_RECONCILED_MESSAGE_ID = "last_reconciled_discord_message_id"


@dataclass(frozen=True)
class ReconcileResult:
    seen: int
    handled: int
    ignored: int
    warning: str | None


async def reconcile_discord_history(
    *,
    client: discord.Client,
    settings,
    ledger: Ledger,
    handle_capture,
) -> ReconcileResult:
    channel = client.get_channel(settings.discord_capture_channel_id)
    if channel is None:
        channel = await client.fetch_channel(settings.discord_capture_channel_id)

    last_message_id = ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID)
    after = discord.Object(id=int(last_message_id)) if last_message_id else None
    fetch_limit = settings.startup_reconcile_limit + 1
    messages = [
        message
        async for message in channel.history(
            limit=fetch_limit,
            after=after,
            oldest_first=True,
        )
    ]

    warning = None
    if len(messages) > settings.startup_reconcile_limit:
        warning = (
            "startup reconcile limit reached; "
            f"processed first {settings.startup_reconcile_limit} messages"
        )
        messages = messages[: settings.startup_reconcile_limit]

    handled = 0
    ignored = 0
    for message in messages:
        if should_capture_message(message, settings):
            await handle_capture(message)
            handled += 1
        else:
            ignored += 1

        ledger.set_system_state(LAST_RECONCILED_MESSAGE_ID, str(message.id))

    return ReconcileResult(
        seen=len(messages),
        handled=handled,
        ignored=ignored,
        warning=warning,
    )
