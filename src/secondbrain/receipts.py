from __future__ import annotations

from secondbrain.ledger import CaptureRecord


async def send_saved_receipt(message, capture: CaptureRecord, *, has_attachments: bool) -> str:
    content = f"{capture.capture_id} received. Your note is saved. Processing..."
    if has_attachments:
        content += "\nAttachment detected but not archived in the MVP."

    receipt = await message.channel.send(content)
    return str(receipt.id)


async def send_rejection_receipt(message, capture: CaptureRecord, *, flags: tuple[str, ...]) -> str:
    reason = ", ".join(flags) if flags else "likely sensitive content"
    receipt = await message.channel.send(
        f"{capture.capture_id} rejected. It appears to contain sensitive content.\nReason: {reason}"
    )
    return str(receipt.id)


async def edit_final_receipt(client, capture: CaptureRecord, content: str) -> None:
    if not capture.receipt_message_id:
        return

    channel = client.get_channel(int(capture.discord_channel_id))
    if channel is None:
        channel = await client.fetch_channel(int(capture.discord_channel_id))

    receipt = await channel.fetch_message(int(capture.receipt_message_id))
    await receipt.edit(content=content)
