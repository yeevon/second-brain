# 01 · Discord bot setup

## 1. Create the application

1. Go to [discord dev apps](https://discord.com/developers/applications)
2. Click **New Application** → name it `second-brain`
3. Go to **Bot** in the left sidebar
4. Click **Add Bot** → confirm
5. Under **Token** click **Reset Token** → copy it → save as `DISCORD_BOT_TOKEN` in your `.env`
6. Under **Privileged Gateway Intents** enable:
   - Message Content Intent
   - Server Members Intent

## 2. Set bot permissions

1. Go to **OAuth2 → URL Generator**
2. Under **Scopes** check: `bot`
3. Under **Bot Permissions** check:
   - Read Messages / View Channels
   - Send Messages
   - Read Message History
   - Send Messages in Threads
4. Copy the generated URL → open it → add the bot to your private server

## 3. Get your server and channel IDs

Enable Developer Mode in Discord:

- Settings → Advanced → Developer Mode → ON

Then:

- Right-click your server name → **Copy Server ID** → save as `DISCORD_GUILD_ID`
- Right-click your `#brain-dump` channel → **Copy Channel ID** → save as `DISCORD_BRAIN_DUMP_CHANNEL_ID`
- Right-click your own username → **Copy User ID** → save as `DISCORD_DM_USER_ID`

## 4. Create the channel

In your private Discord server create a channel called `#brain-dump`.

This is the only channel the bot listens to. Keep it separate from everything else — it's your capture inbox, not a conversation channel.

## 5. Verify

At this point you should have four values in your `.env`:

```init
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
DISCORD_CHANNEL_ID=
DISCORD_DM_USER_ID=
```

Next: [EC2 and n8n setup](02-ec2-n8n.md)