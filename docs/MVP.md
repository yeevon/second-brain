# Second Brain — Local MVP Build Specification

**Status:** Approved for initial implementation  
**Last updated:** 2026-06-07  
**Revision:** Vertical-slice build order, background classifier worker, editable receipts, and crash-safe startup reconciliation.  
**Relationship to hardened design:** This document defines the first local vertical slice. The production target remains `docs/ARCHITECTURE.md`.

---

## 1. Purpose

Build the smallest end-to-end Second Brain that is genuinely useful for local testing:

```text
post a thought in Discord from any device
    ↓
local Python app receives the message through the Discord Gateway
    ↓
reject likely secrets before plaintext persistence
    ↓
persist an accepted raw capture in SQLite
    ↓
send an immediate saved receipt
    ↓
enqueue the durable capture for background processing
    ↓
classify with Gemini asynchronously using structured JSON
    ↓
render Markdown deterministically outside the LLM
    ↓
write the note into the local Obsidian vault
    ↓
send a final filing receipt
```

This MVP is intentionally local-first. It is usable while the local app is running and it recovers missed Discord messages when restarted.

It is **not** the always-on production deployment. EC2, n8n, GitHub push automation, encrypted backups, digests, and MCP remain later phases.

---

## 2. MVP success criteria

The MVP is successful when it can:

- Capture a Discord thought from mobile, laptop, or desktop.
- Accept messages only from the configured user, server, and channel.
- Ignore bot-authored and webhook-authored messages.
- Reject likely secrets before storing plaintext or calling Gemini.
- Persist accepted raw text before classification.
- Return an immediate saved receipt only after SQLite commit succeeds.
- Store the receipt message ID and edit that receipt as processing completes.
- Keep Discord Gateway handling responsive while Gemini classification runs in a background worker.
- Recover messages posted while the local app was stopped or when the process crashed before commit.
- Prevent duplicate notes during restart catch-up or duplicate event delivery.
- Classify accepted notes with one schema-constrained Gemini call.
- Route invalid, uncertain, or failed classifications into `00_inbox/`.
- Render filenames, frontmatter, and Markdown deterministically in application code.
- Write notes into a configurable local vault path.
- Append a readable audit event to `99_log/events.ndjson`.
- Edit the original Discord receipt to show the final filing location or failure state.
- Warn when a Discord message contains attachments that were not archived.

---

## 3. Scope

Implement one local Python application in the `yeevon/second-brain` repository.

### Runtime command

```bash
python -m secondbrain run
```

### Optional diagnostic command

```bash
python -m secondbrain status
```

### Runtime SQLite file

```text
.runtime/ledger.sqlite3
```

### Default local vault path

```text
~/prj/my-vault
```

The vault path must be configurable through `VAULT_PATH`.

---

## 4. Explicitly deferred

Do not include these in the MVP:

- EC2 deployment.
- n8n.
- Docker Compose.
- GitHub push automation.
- Automated Git commits.
- Daily or weekly digests.
- MCP.
- `project-memory` integration.
- Encrypted off-host backups.
- Full `fix:` correction workflow.
- Attachment archiving.
- Object storage.
- Two-way Obsidian sync.
- Multi-user support.
- Redis, Kafka, or a distributed queue.
- Separate Bouncer and Sorter model calls.
- Vector search or embeddings.

These are deferred deliberately. The MVP exists to prove the capture-to-vault loop before infrastructure is added.

---

## 5. Repository layout

```text
second-brain/
├── pyproject.toml
├── .env.example
├── .gitignore
├── src/
│   └── secondbrain/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py
│       ├── config.py
│       ├── discord_capture.py
│       ├── ledger.py
│       ├── secret_screen.py
│       ├── classifier.py
│       ├── worker.py
│       ├── models.py
│       ├── vault_writer.py
│       ├── receipts.py
│       ├── reconcile.py
│       └── audit.py
├── tests/
│   ├── unit/
│   └── integration/
├── docs/
│   ├── ARCHITECTURE.md
│   └── MVP.md
└── .runtime/
    └── ledger.sqlite3
```

### `.gitignore`

```gitignore
.env
.runtime/
__pycache__/
*.pyc
.pytest_cache/
.venv/
```

Do not commit the runtime ledger or secrets.

---

## 6. Dependencies

Use a small dependency set:

```text
discord.py
google-genai
pydantic
python-dotenv
pytest
pytest-asyncio
```

Use Python's built-in `sqlite3` module for the MVP.

Do not add an ORM.

---

## 7. Configuration

Load configuration from `.env`.

```dotenv
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
DISCORD_CAPTURE_CHANNEL_ID=
DISCORD_ALLOWED_USER_ID=

GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash
CLASSIFICATION_CONFIDENCE_THRESHOLD=0.75
CLASSIFIER_WORKER_COUNT=1
CLASSIFIER_QUEUE_MAXSIZE=100

VAULT_PATH=~/prj/my-vault
LEDGER_PATH=.runtime/ledger.sqlite3
STARTUP_RECONCILE_LIMIT=100
```

### Configuration rules

- Fail startup with a clear error when required values are missing.
- Never print tokens or `.env` values.
- Expand `~` in filesystem paths.
- Resolve `VAULT_PATH` to an absolute path once at startup.
- Pin `GEMINI_MODEL` to `gemini-3.5-flash`; do not use a floating alias.

---

## 8. Discord requirements

## 8.1 Bot behavior

The local app connects to the Discord Gateway and listens for `MESSAGE_CREATE`.

Enable the required message-content intent in the Discord Developer Portal.

### Required bot permissions

```text
View Channel
Send Messages
Read Message History
```

### Required filters

Ignore a message unless all conditions pass:

```text
message guild ID matches DISCORD_GUILD_ID
message channel ID matches DISCORD_CAPTURE_CHANNEL_ID
message author ID matches DISCORD_ALLOWED_USER_ID
message is not bot-authored
message is not webhook-authored
message has text content or a supported link
```

The bot must ignore its own receipts so it cannot create an ingestion loop.

### Gateway responsiveness rule

The Discord event handler must remain short:

```text
filter message
run secret screen
commit SQLite record
send immediate receipt
store receipt_message_id
enqueue capture_id
return control to Discord event loop
```

Do not perform Gemini classification inline inside the Discord event handler.

Discord Gateway connections require periodic heartbeats. A blocking HTTP request or other long-running synchronous operation on the event loop can delay those heartbeats and destabilize the connection.


## 8.2 Attachment policy

Attachments are not archived in the MVP.

Record attachment metadata in SQLite:

```text
filename
content_type
size
Discord attachment URL
```

Do not download the binary.

If a message includes text and attachments:

```text
capture the text
warn that attachments were not archived
```

If a message includes only attachments and no text:

```text
persist an accepted capture with an attachment warning
route a derived note to 00_inbox/
do not pretend the attachment content was classified
```

---

## 9. Event flow

## 9.1 Normal capture

```text
MESSAGE_CREATE
    ↓
validate guild, channel, author, and non-bot filters
    ↓
run pre-persistence secret screen
    ↓
insert accepted raw capture into SQLite idempotently
    ↓ commit succeeds
send immediate saved receipt
    ↓
store receipt_message_id
    ↓
enqueue capture_id for background worker
    ↓ return control to Discord event loop
background worker classifies with Gemini asynchronously
    ↓
validate structured result
    ↓
render deterministic Markdown
    ↓
write note to vault
    ↓
append audit event
    ↓
update SQLite status and note path
    ↓
send final receipt
```

## 9.2 Likely-sensitive message

```text
MESSAGE_CREATE
    ↓
secret screen flags likely sensitive content
    ↓
persist redacted rejection record only
    ↓
do not save original plaintext
do not call Gemini
    ↓
send rejection receipt
```

## 9.3 Classification failure or uncertainty

```text
accepted raw capture already safe in SQLite
    ↓
Gemini timeout, API error, invalid JSON, invalid folder, missing field, or low confidence
    ↓
render deterministic Inbox note from accepted raw text
    ↓
write note to 00_inbox/
    ↓
append audit event
    ↓
send Inbox receipt with reason
```

The note is never silently dropped because classification failed.

## 9.4 Vault-write failure

```text
accepted raw capture already safe in SQLite
    ↓
local vault write fails
    ↓
mark capture FAILED
    ↓
send visible failure receipt
```

The raw capture remains recoverable from SQLite.

---

## 10. Background classifier worker

Classification must not run inline inside the Discord Gateway callback.

### Queue model

Use:

```text
SQLite RECEIVED rows = durable work source
asyncio.Queue = in-memory wake-up signal
one background classifier worker = MVP consumer
```

The in-memory queue is not the source of truth. If the process exits after SQLite commit but before `queue.put()`, startup recovery finds the `RECEIVED` row and enqueues it again.

### Worker behavior

```text
wait for capture_id from asyncio.Queue
    ↓
atomically transition RECEIVED → CLASSIFYING
    ↓
load accepted raw capture
    ↓
call Gemini using the async SDK
    ↓
render and write deterministic Markdown
    ↓
update SQLite terminal state
    ↓
edit the original Discord receipt
```

Use the Google Gen AI SDK async client:

```python
response = await client.aio.models.generate_content(
    model=settings.gemini_model,
    contents=prompt,
    config=generate_config,
)
```

Do not call the synchronous Gemini client directly from the Discord event loop.

### Worker count

Start with:

```text
CLASSIFIER_WORKER_COUNT=1
```

One worker keeps local filing behavior predictable and avoids unnecessary vault-write races. The architecture may add controlled parallel classification later, but Markdown writes must still remain serialized.

### Startup recovery for unfinished work

After Discord-history reconciliation, enqueue ledger rows in:

```text
RECEIVED
CLASSIFYING
```

For the local MVP, reset stale `CLASSIFYING` rows to `RECEIVED` during startup before enqueuing them. The deterministic writer and `capture_id` checks prevent duplicate Markdown notes if a prior run wrote the file but crashed before updating SQLite.

---

## 11. Startup catch-up

A local app can be offline. The MVP must recover Discord messages posted while it was stopped.

### Behavior

On startup:

```text
load last_reconciled_discord_message_id from SQLite
    ↓
fetch up to STARTUP_RECONCILE_LIMIT newer messages from #brain-dump
    ↓
apply the same filters and secret screen used for live events
    ↓
insert missing Discord message IDs idempotently
    ↓
process newly recovered captures
    ↓
start normal Gateway listening
```

### Rules

- Use the Discord message ID as the idempotency key.
- Advance `last_reconciled_discord_message_id` only after each history message has a durable disposition: accepted row committed, redacted rejection row committed, or safely ignored by deterministic filters.
- Never advance the reconciliation high-water mark before an accepted or rejected message commit succeeds.
- A startup catch-up message must not produce a duplicate note.
- The bot's own old receipts must be filtered out.
- If more than `STARTUP_RECONCILE_LIMIT` messages are waiting, log and display a visible warning rather than silently skipping the excess.

Periodic reconciliation is deferred to the hardened architecture. Startup catch-up is required in the MVP.

A crash between Discord event receipt and SQLite commit is therefore a temporary interruption, not an accepted permanent-loss condition. The next startup reconciliation must recover the Discord message from channel history.

---

## 12. SQLite ledger

## 11.1 MVP database rule

Use one SQLite repository inside the local Python process.

Serialize mutations with one `asyncio.Lock` or a single repository write path.
Use `asyncio.Queue` only as a wake-up signal for background processing; SQLite `RECEIVED` rows remain the durable work source.

Keep transactions short:

```text
open transaction
insert or update rows
commit
release lock
perform Discord and Gemini network calls afterward
```

Never hold a SQLite transaction open across a network request.

The MVP does not need WAL mode. The monolithic local process has low write volume and one mutation path. WAL hardening belongs in the EC2 architecture.

## 11.2 Minimal schema

```sql
CREATE TABLE captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL UNIQUE,
    discord_message_id TEXT NOT NULL UNIQUE,
    discord_channel_id TEXT NOT NULL,
    discord_guild_id TEXT NOT NULL,
    discord_author_id TEXT NOT NULL,

    raw_text TEXT,
    redacted_text TEXT,
    is_sensitive INTEGER NOT NULL DEFAULT 0,
    sensitivity_flags TEXT,

    has_attachments INTEGER NOT NULL DEFAULT 0,
    attachment_metadata_json TEXT,

    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    classification_json TEXT,
    derived_note_path TEXT,
    receipt_message_id TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,

    CHECK (
        (is_sensitive = 0 AND raw_text IS NOT NULL)
        OR
        (is_sensitive = 1 AND raw_text IS NULL AND redacted_text IS NOT NULL)
    )
);

CREATE TABLE capture_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_payload_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
);

CREATE TABLE system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_captures_status ON captures(status);
CREATE INDEX idx_capture_events_capture_id ON capture_events(capture_id);
```

## 11.3 Capture statuses

```text
RECEIVED
CLASSIFYING
FILED
INBOX
REJECTED_SENSITIVE
FAILED
```

## 11.4 Capture IDs

Use the Discord message ID as the true idempotency key.

Generate a human-readable ID transactionally:

```text
SB-YYYYMMDD-NNNN
```

The exact counter implementation is internal. The database uniqueness constraint is the final guard.

---

## 13. Secret screen

Run the secret detector before plaintext persistence.

### Initial patterns

```text
-----BEGIN ... PRIVATE KEY-----
AWS access-key patterns
GitHub token patterns
Discord bot-token patterns
Bearer tokens
password=...
secret=...
api_key=...
SSN-like patterns
```

### Rules

- Do not log raw rejected content.
- Do not store raw rejected content.
- Do not send rejected content to Gemini.
- Store redacted text and sensitivity flags only.
- Send a visible rejection receipt.
- Treat this as a safety net, not a guarantee.

Do not submit employer secrets, client-sensitive information, controlled government information, credentials, or anything unsuitable for Discord or an external LLM API.

---

## 14. Gemini classifier

## 14.1 Model

```text
gemini-3.5-flash
```

## 14.2 Structured output contract

Use a Pydantic model and Gemini structured outputs.

```json
{
  "folder": "projects",
  "project": "halo",
  "note_type": "task",
  "title": "Review WebSocket reconnect handling",
  "tags": ["telemetry", "websocket"],
  "body": "Review reconnect handling in the HALO telemetry dashboard.",
  "actions": [
    {
      "text": "Review WebSocket reconnect handling",
      "status": "open"
    }
  ],
  "needs_clarification": false,
  "clarifying_question": null,
  "confidence": 0.91
}
```

### Allowed folders

```text
people
projects
ideas
learning
admin
inbox
```

### Validation rules

Route to `00_inbox/` when:

```text
Gemini API call fails
response is not schema-valid
folder is not allowlisted
required fields are missing
confidence is below CLASSIFICATION_CONFIDENCE_THRESHOLD
needs_clarification is true
```

`confidence` is a routing hint, not a calibrated probability.

---

## 15. Deterministic Markdown writer

Gemini returns data only. Application code owns the file output.

### Folder mapping

```text
inbox     → 00_inbox/
people    → 10_people/
projects  → 20_projects/<sanitized-project>/
ideas     → 30_ideas/
learning  → 40_learning/
admin     → 50_admin/
```

### Filename pattern

```text
YYYY-MM-DD--SB-YYYYMMDD-NNNN--sanitized-title.md
```

### Writer rules

- Validate folder against the allowlist.
- Sanitize title and project slug.
- Resolve the destination under `VAULT_PATH`.
- Reject path traversal.
- Create missing folders.
- Never overwrite an unrelated note.
- If a note already exists for the same `capture_id`, return the existing result rather than creating a duplicate.
- Append a readable event to `99_log/events.ndjson`.

### Frontmatter

```yaml
---
capture_id: SB-YYYYMMDD-NNNN
source_message_id: "DISCORD_MESSAGE_ID"
created_at: ISO_TIMESTAMP
area: projects
project: halo
note_type: task
tags:
  - telemetry
  - websocket
actions:
  - text: Review WebSocket reconnect handling
    status: open
lifecycle_status: active
model: gemini-3.5-flash
prompt_version: classifier-v1
schema_version: 1
---
```

### Body

```markdown
# Review WebSocket reconnect handling

Review reconnect handling in the HALO telemetry dashboard.

## Actions

- [ ] Review WebSocket reconnect handling
```

### Audit event

Append:

```text
99_log/events.ndjson
```

Example:

```json
{"capture_id":"SB-20260607-0042","event":"FILED","path":"20_projects/halo/2026-06-07--SB-20260607-0042--review-websocket-reconnect-handling.md","timestamp":"2026-06-07T09:14:05-05:00"}
```

---

## 16. Receipt behavior

## 16.1 Immediate saved receipt

Send only after SQLite commit succeeds. Store the returned Discord message ID in `captures.receipt_message_id`:

```text
⏳ SB-YYYYMMDD-NNNN received.
Your note is saved. Processing…
```

## 16.2 Successful filing receipt

Edit the original immediate receipt rather than sending a second bot message:

```text
✅ SB-YYYYMMDD-NNNN filed.
Location: 20_projects / halo
Type: task
Tags: telemetry, websocket
```

## 16.3 Inbox receipt

Edit the original immediate receipt:

```text
⚠️ SB-YYYYMMDD-NNNN saved to 00_inbox.
Reason: classification was uncertain.
```

Classifier failure variant:

```text
⚠️ SB-YYYYMMDD-NNNN saved to 00_inbox.
Reason: automatic classification failed. Your note is safe.
```

## 16.4 Sensitive rejection receipt

```text
⚠️ Message rejected.
It appears to contain a credential or sensitive identifier.
The original text was not saved or sent to Gemini.
```

## 16.5 Vault-write failure receipt

Edit the original immediate receipt:

```text
❌ SB-YYYYMMDD-NNNN captured but vault filing failed.
Your original note is safe in the local ledger.
```

## 16.6 Receipt fallback rule

If the initial receipt send fails, processing still continues because the capture is already durable.

If editing the stored receipt fails later:

```text
send one replacement final-state receipt
store the replacement receipt_message_id
append RECEIPT_REPLACED audit event
```

Do not send routine duplicate bot messages when the original receipt can be edited.

## 16.7 Attachment warning

Append when relevant:

```text
⚠️ Attachment detected but not archived in the MVP.
```

---

## 17. Observability

## 17.1 Do not log raw text by default

Log metadata:

```text
capture_id
discord_message_id
status transition
classification confidence
derived note path
error type
timestamp
```

Do not print:

```text
raw message text
rejected secret-like text
Discord token
Gemini API key
.env contents
```

## 17.2 Status command

```bash
python -m secondbrain status
```

Report:

```text
ledger path
vault path
total captures
captures filed
captures in inbox
captures rejected as sensitive
captures failed
last reconciled Discord message ID
last successful vault write
```

---

## 18. Test plan

## 18.1 Unit tests

- Allowlisted Discord message passes filters.
- Wrong user, wrong guild, and wrong channel are ignored.
- Bot-authored and webhook-authored messages are ignored.
- Duplicate Discord message ID creates one capture.
- Secret-like input is rejected before plaintext persistence.
- Rejected secret text is absent from SQLite and logs.
- Invalid classifier JSON routes to Inbox.
- Invalid folder routes to Inbox.
- Low confidence routes to Inbox.
- `needs_clarification = true` routes to Inbox.
- Classifier API failure routes to Inbox.
- Path traversal attempts are rejected.
- Folder mapping is correct.
- Markdown rendering is deterministic.
- Existing `capture_id` does not create a duplicate note.
- Attachment metadata is stored without downloading the binary.
- Frontmatter includes `prompt_version: classifier-v1`.

## 18.2 Integration tests with fake clients

- Capture persists before the classifier runs.
- Discord event callback enqueues background work and returns without awaiting Gemini completion.
- Gemini classification uses an async client or an isolated thread fallback; no synchronous HTTP call blocks the Discord event loop.
- Immediate receipt occurs only after SQLite commit.
- Successful classification writes one note.
- Failed classification writes one Inbox note.
- Vault-write failure marks the capture failed and sends a visible receipt.
- Final state edits the original receipt instead of sending a routine second bot message.
- Receipt-edit failure sends one replacement receipt and records the replacement ID.
- Receipt state changes from received to filed, Inbox, rejected, or failed.
- Audit event is appended to `99_log/events.ndjson`.
- Restart catch-up processes a missed message exactly once.
- Crash before SQLite commit is recovered by startup catch-up without skipping past the message.
- Startup recovery requeues unfinished `RECEIVED` and stale `CLASSIFYING` rows.
- Old bot receipts encountered during catch-up are ignored.
- Two rapid messages produce two rows and two notes without duplication.

## 18.3 Manual acceptance

1. Start the local app.
2. Post a normal note in Discord.
3. See the immediate saved receipt.
4. See the Markdown note in `VAULT_PATH`.
5. Verify the original receipt was edited into the final filed receipt rather than receiving a routine second bot message.
6. Stop the app.
7. Post another normal Discord note.
8. Restart the app.
9. Verify the missed note is recovered and filed exactly once.
10. Post a likely-secret test string.
11. Verify it is rejected and never sent to Gemini.
12. Post a message with a screenshot and text.
13. Verify the text is captured and the skipped-attachment warning appears.
14. Run `python -m secondbrain status`.
15. Verify counts and paths are correct.

---

## 19. MVP definition of done

The local MVP is complete when:

- One Python command starts the system.
- The bot captures allowlisted Discord messages.
- The bot ignores its own receipts.
- Accepted messages are committed to SQLite before the saved receipt.
- Gemini classification runs in a background worker and does not block Discord Gateway handling.
- The original receipt is edited into the final state whenever possible.
- Likely secrets are rejected before plaintext persistence or Gemini.
- Messages posted while the app is stopped or interrupted before commit are recovered after restart.
- Duplicate Discord events do not create duplicate ledger rows or notes.
- Gemini returns schema-constrained classification data.
- Classifier failures route notes to `00_inbox/`.
- Application code renders Markdown deterministically with `prompt_version: classifier-v1`.
- The vault writer cannot escape `VAULT_PATH`.
- Attachments generate a visible warning and are not silently dropped.
- Audit events are appended.
- Final receipts report filed, Inbox, rejected, or failed state.
- The status command reports operational counts.
- EC2, n8n, Git automation, digests, and MCP remain deferred.

---

## 20. Recommended build order — vertical slice

Build in this order. Each step leaves behind a working, testable piece.

### Step 1 — SQLite spine

Implement:

```text
config loading
SQLite migrations
captures insert
capture_events append
system_state read/write
transactional human-readable capture ID
duplicate Discord message ID handling
```

Add a development CLI:

```bash
python -m secondbrain capture "Review reconnect handling"
python -m secondbrain status
```

At this stage, `capture` writes a ledger row only. No Discord and no Gemini.

### Step 2 — Discord Gateway listener and saved receipt

Implement:

```text
Discord connection
message-content intent
guild/channel/user allowlists
bot and webhook filtering
SQLite insert through the existing repository
immediate saved receipt after commit
receipt_message_id persistence
startup catch-up
```

At this stage, a Discord note becomes a durable `RECEIVED` row and receives a saved receipt. No classifier yet.

### Step 3 — Pre-persistence secret screen

Wrap the shared ingestion path used by both:

```text
development CLI
live Gateway events
startup catch-up
```

Verify that likely secrets produce a redacted rejection record, no plaintext persistence, and no future classifier enqueue.

### Step 4 — Background classifier worker and deterministic vault writer

Implement:

```text
asyncio.Queue wake-up signal
SQLite RECEIVED rows as durable work source
one background worker
async Gemini structured-output call
Inbox fallback
deterministic Markdown renderer
capture_id dedupe
audit append
receipt edit
```

The Gateway callback enqueues work and returns. It never waits for Gemini.

### Step 5 — Hardening tests for the local slice

Run:

```text
duplicate event
rapid messages
restart catch-up
crash-before-commit recovery
unfinished CLASSIFYING recovery
Gemini timeout
invalid schema
vault-write failure
receipt-edit fallback
attachment warning
secret rejection
```

Do not move to EC2 until the local vertical slice passes.

---

## 21. Migration path after MVP

After the local MVP passes manual acceptance:

```text
local monolith
    ↓
extract capture-service
    ↓
deploy capture-service to EC2
    ↓
introduce n8n orchestration
    ↓
extract locked writer-service
    ↓
add GitHub vault sync and backups
    ↓
add digests
    ↓
add local pull wrapper and read-only MCP
```

The MVP should preserve clean module boundaries so extraction is straightforward, but it must remain one local application until the vertical slice works.
