# Second Brain

A Discord bot that captures messages and automatically files them into an Obsidian vault using Gemini AI classification.

Send a message to a designated Discord channel and the bot saves it to SQLite, classifies it with Gemini, renders a Markdown note, and files it into your vault — all without blocking the Discord event loop.

## What it does

1. **Captures** — Monitors a single Discord channel for messages from one allowed user.
2. **Screens** — Rejects messages containing secrets (API keys, tokens, passwords) before any persistence.
3. **Persists** — Writes the capture durably to SQLite before doing anything else.
4. **Classifies** — Sends the text to Gemini in a background worker. Gemini returns structured JSON (folder, title, tags, body, actions).
5. **Files** — Writes a deterministic Markdown note into the Obsidian vault under the correct folder (`projects/`, `people/`, `ideas/`, `learning/`, `admin/`, or `inbox/`).
6. **Receipts** — Edits the original Discord confirmation message to show the final filed location or reason for inbox routing.
7. **Reconciles** — On startup, replays Discord channel history to recover any messages missed while the bot was offline.

## Project layout

```text
src/secondbrain/
  app.py            # orchestration and entry point
  config.py         # settings loaded from .env
  ledger.py         # SQLite repository (source of truth)
  classifier.py     # Gemini API call and response parsing
  vault_writer.py   # Markdown rendering and file writing
  worker.py         # background classifier worker loop
  receipts.py       # Discord receipt formatting and delivery
  reconcile.py      # startup Discord history catch-up
  discord_capture.py# Discord client wiring
  secret_screen.py  # pre-persistence secret detection
  models.py         # Pydantic models
  observability.py  # structured JSON logging to stdout
```

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- A Discord bot token with Message Content Intent enabled
- A Gemini API key
- An Obsidian vault directory (absolute path)

## Setup

### 1. Create a Discord bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications) and create a new application.
2. Under **Bot**, enable **Message Content Intent**.
3. Copy the bot token.
4. Invite the bot to your server with `Send Messages`, `Read Message History`, and `Read Messages/View Channels` permissions. Use OAuth2 → URL Generator with `bot` scope.
5. Note the Guild ID (server ID) and the ID of the capture channel (enable Developer Mode → right-click → Copy ID).

### 2. Get a Gemini API key

Get a key from [Google AI Studio](https://aistudio.google.com/app/apikey).

### 3. Install dependencies

```bash
uv sync
```

Or with pip:

```bash
pip install -e .
```

### 4. Configure environment

Copy the example and fill in your values:

```bash
cp .env.example .env
```

`.env` variables:

```env
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_GUILD_ID=123456789012345678
DISCORD_CAPTURE_CHANNEL_ID=123456789012345678
DISCORD_ALLOWED_USER_ID=123456789012345678

GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.0-flash
CLASSIFICATION_CONFIDENCE_THRESHOLD=0.75
CLASSIFIER_WORKER_COUNT=1
CLASSIFIER_QUEUE_MAXSIZE=100

VAULT_PATH=/absolute/path/to/your/obsidian/vault
LEDGER_PATH=/absolute/path/to/ledger.sqlite3
STARTUP_RECONCILE_LIMIT=50
```

All paths must be absolute.

## Running

### Start the bot

```bash
python -m secondbrain run
```

### Check status

```bash
python -m secondbrain status
```

Reports total captures, filed/inbox/failed counts, and last reconciled Discord message ID.

## Running tests

```bash
uv run pytest
```

## Vault structure

Notes are filed into numbered folders matching Obsidian conventions:

```text
vault/
  00_inbox/          # unclassified, low-confidence, or attachment-only captures
  10_people/
  20_projects/
    project-name/
  30_ideas/
  40_learning/
  50_admin/
  99_log/
    events.ndjson    # append-only audit log
```

Note filenames follow the pattern: `YYYY-MM-DD--CAPTURE_ID--sanitized-title.md`

## Notes on behavior

- Messages containing secrets are rejected before SQLite and before Gemini.
- Attachment-only messages (no text) are saved to inbox without calling Gemini.
- If Gemini is unavailable or returns low-confidence results, the note goes to `00_inbox/` — it is never silently dropped.
- If the bot restarts, it replays history from the last processed message ID to recover any missed captures.
- The Discord receipt message is edited in place from "received" to the final filed/inbox/failed state.
