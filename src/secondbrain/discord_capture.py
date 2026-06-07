import discord

_intents = discord.Intents.default()
_intents.message_content = True


def create_discord_client(settings, handle_capture):
    client = discord.Client(intents=_intents)

    @client.event
    async def on_message(message):
        if not should_capture_message(message, settings):
            return
        
        await handle_capture(message)
    
    return client
        

def should_capture_message(message: discord.Message, settings) -> bool:
    if message.guild is None:
        return False
    
    if message.guild.id != settings.discord_guild_id:
        return False
    
    if message.channel.id != settings.discord_capture_channel_id:
        return False
    
    if message.author.id != settings.discord_allowed_user_id:
        return False
    
    if message.author.bot:
        return False
    
    if message.webhook_id is not None:
        return False
    
    if not has_capturable_content(message):
        return False
    
    return True


def has_capturable_content(message: discord.Message) -> bool:
    return has_text_content_or_supported_link(message) or bool(message.attachments)


def has_text_content_or_supported_link(message) -> bool:
    return bool(message.content and message.content.strip())


def extract_attachment_metadata(message: discord.Message) -> list[dict]:
    return [
        {
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "size": attachment.size,
            "url": attachment.url,
        }
        for attachment in message.attachments
    ]
