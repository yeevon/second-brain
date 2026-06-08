from __future__ import annotations

from dataclasses import dataclass

import discord


LAST_RECONCILED_MESSAGE_ID = "last_reconciled_discord_message_id"


@dataclass(frozen=True)
class ReconcileResult:
    seen: int
    handled: int
    ignored: int
    warning: str | None


async def fetch_discord_history(
    *,
    client: discord.Client,
    settings,
    last_message_id: str | None,
) -> tuple[list, str | None]:
    channel = client.get_channel(settings.discord_capture_channel_id)
    if channel is None:
        channel = await client.fetch_channel(settings.discord_capture_channel_id)

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

    return messages, warning
