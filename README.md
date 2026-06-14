# Second Brain

A Discord bot that durably captures messages and, in local mode, automatically files them into an Obsidian vault using Gemini AI classification.

Send a message to a designated Discord channel. The bot screens it for secrets, persists it to SQLite, sends an immediate receipt, and — depending on the runtime mode — either stops there or classifies and files it into your vault.

## Runtime modes

Two explicit modes are supported via `CAPTURE_PROCESSING_MODE`:

| Mode | Where it runs | What it does |
| --- | --- | --- |
| `local-full` | Desktop | Capture → screen → persist → classify (Gemini) → file (Obsidian) → receipt |
| `capture-only` | EC2 | Capture → screen → persist → receipt. Downstream filing disabled. |
| `capture-only` | Local Docker | Same container image as EC2, validated locally via `deploy/local-up.sh`. |

The mode must be set explicitly. Startup fails if it is missing or unsupported.

## What it does

### Both modes

1. **Captures** — Monitors a single Discord channel for messages from one allowed user.
2. **Screens** — Rejects messages containing secrets (API keys, tokens, passwords) before any persistence.
3. **Persists** — Writes the capture durably to SQLite before doing anything else.
4. **Receipts** — Sends an immediate Discord confirmation. `local-full` says "Processing…"; `capture-only` says "Downstream filing is not enabled yet."
5. **Reconciles** — On startup, replays Discord history to recover any messages missed while the service was offline.
6. **Internal API** — Exposes an authenticated HTTP API inside the process for state transitions and health checks.

### `local-full` only

1. **Classifies** — Sends the text to Gemini in a background worker. Gemini returns structured JSON (folder, title, tags, body, actions).
2. **Files** — Writes a deterministic Markdown note into the Obsidian vault under the correct folder.
3. **Final receipt** — Edits the original Discord message to show the filed location or inbox reason.

## Project layout

```text
src/secondbrain/
  app.py              # runtime orchestration and CLI entry point
  config.py           # settings and mode validation
  capture_service.py  # CaptureService facade — sole SQLite owner
  capture_api.py      # internal HTTP API route definitions
  api_server.py       # embedded Uvicorn server (EmbeddedUvicornServer)
  api_models.py       # internal API request/response models
  capture_models.py   # capture record and status types
  ledger.py           # SQLite repository (source of truth)
  sqlite_runtime.py   # dedicated SQLite worker thread with bounded retries
  migrations.py       # schema migrations applied at startup
  delivery.py         # delivery attempt tracking and backoff
  n8n_delivery.py     # N8nWebhookDeliveryClient — posts envelope to intake webhook
  heartbeat.py        # periodic capture-service liveness signal
  reaper.py           # stale-lease watchdog loop
  status.py           # operational status snapshot and formatting
  classifier.py       # Gemini API call and response parsing
  vault_writer.py     # Markdown rendering and file writing
  worker.py           # background classifier worker loop
  receipts.py         # Discord receipt formatting and delivery
  reconcile.py        # startup and periodic Discord history catch-up
  discord_capture.py  # Discord client wiring
  secret_screen.py    # pre-persistence secret detection
  models.py           # Pydantic models
  observability.py    # structured JSON logging to stdout
  audit.py            # append-only audit log

writer-service/
  Dockerfile                         # Python 3.13-slim, UID 10002, port 8001
  src/writerservice/
    main.py                          # FastAPI app, auth middleware, /health and /internal/notes/file
    config.py                        # GIT_SYNC_ENABLED, VAULT_PATH, WRITER_SERVICE_TOKEN
    api_models.py                    # FileNoteRequest, FileNoteResponse, Classification
    writer.py                        # deterministic Markdown generation and vault write dispatch
    vault.py                         # folder enum → physical directory mapping, path validation
    audit.py                         # append-only 99_log/events.ndjson writes
    flock.py                         # WriterLock — kernel-managed OS advisory flock (fcntl.LOCK_EX)
    git_ops.py                       # GitVaultOps — fetch, merge, write, add, commit, push sequence
    git_errors.py                    # typed Git-layer error classes
  tests/
    unit/                            # auth, validation, Markdown generation, idempotency, flock, git_ops
    integration/                     # filing, idempotent replay, inbox routing, Git sync, Git failures

deploy/
  container-entrypoint.sh           # EBS sentinel check before container start
  provision-host.sh                  # EC2 host setup (includes vault clone and SSH key setup)
  deploy.sh                          # build and start on EC2
  verify.sh                          # post-deployment checks (includes writer-service checks)
  local-stack-up.sh                  # build, start full stack, wait for healthy
  local-stack-down.sh                # stop all services (volumes preserved)
  test-container-packaging.sh        # container packaging regression suite
  test-writer-service.sh             # writer-service regression: health, filing, idempotency, audit
  test-writer-safe-failure.sh        # Git failure injection regression suite
  test-n8n-error-workflow.sh         # n8n error workflow regression suite
  capture-service.env.example
  writer-service.env.example
  README.md                          # EC2 provisioning and deployment guide
```

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- A Discord bot token with Message Content Intent enabled
- **`local-full` only:** a Gemini API key and an Obsidian vault directory
- **`capture-only` (EC2):** Docker Engine and Docker Compose plugin
- **`capture-only` (local validation):** Docker Desktop or Docker Engine with Compose plugin

## Setup — local-full (desktop)

### 1. Create a Discord bot

1. Go to the Discord Developer Portal and create a new application.
2. Under **Bot**, enable **Message Content Intent**.
3. Copy the bot token.
4. Invite the bot with `Send Messages`, `Read Message History`, and `Read Messages/View Channels` permissions.
5. Note the Guild ID and capture channel ID (Developer Mode → right-click → Copy ID).

### 2. Get a Gemini API key

Get a key from Google AI Studio.

### 3. Install dependencies

```bash
uv sync
```

### 4. Configure environment

```bash
cp .env.example .env
```

Required variables for `local-full`:

```env
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
DISCORD_CAPTURE_CHANNEL_ID=
DISCORD_ALLOWED_USER_ID=

CAPTURE_PROCESSING_MODE=local-full

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
CLASSIFICATION_CONFIDENCE_THRESHOLD=0.75
CLASSIFIER_WORKER_COUNT=1
CLASSIFIER_QUEUE_MAXSIZE=100

VAULT_PATH=/absolute/path/to/your/obsidian/vault
LEDGER_PATH=.runtime/ledger.sqlite3
STARTUP_RECONCILE_LIMIT=100

CAPTURE_SERVICE_INTERNAL_TOKEN=<at least 32 random characters>
CAPTURE_API_HOST=127.0.0.1
CAPTURE_API_PORT=8000
```

All paths must be absolute (except `LEDGER_PATH` during local development).

### 5. Run

```bash
uv run python -m secondbrain run
```

Check status:

```bash
uv run python -m secondbrain status
```

## Setup — capture-only (local Docker validation)

Fill in credentials, then use standard Docker Compose commands:

```bash
cp .env.example .env                # fill in Discord credentials and internal token
docker compose up -d                # build images, start all services
docker compose down                 # stop services (named volumes preserved)
docker compose logs -f              # follow logs
docker compose ps                   # check status
```

`compose.override.yaml` is auto-loaded and provides local-safe defaults for all required variables. The EBS sentinel marker is created automatically on first container start.

For a first-run convenience wrapper that also validates env files and waits for healthy containers:

```bash
deploy/local-stack-up.sh
```

Container packaging regression:

```bash
deploy/test-container-packaging.sh   # Python version, env overrides, invariants, SIGTERM
deploy/local-reset.sh --confirm-delete-local-test-data   # wipe volumes and start fresh
```

`test-container-packaging.sh` runs four tests. The SIGTERM test stops the container — run `docker compose up -d` again before the next test cycle.

## Setup — capture-only (EC2)

See [deploy/README.md](deploy/README.md) for the full EC2 provisioning, deployment, and verification guide.

The short version:

1. Provision the EC2 host: `deploy/provision-host.sh`
2. Mount and format the EBS data volume at `/opt/second-brain/data`
3. Create the EBS sentinel marker on the mounted volume
4. Create `/opt/second-brain/config/capture-service.env` from `deploy/capture-service.env.example`
5. Deploy: `deploy/deploy.sh`
6. Verify: `deploy/verify.sh`

Do not run the desktop `local-full` listener while the EC2 `capture-only` service owns Discord intake.

## Running tests

```bash
uv run pytest
```

## Vault structure (local-full)

Notes are filed into numbered folders:

```text
vault/
  00_inbox/          # unclassified, low-confidence, or failed captures
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

## n8n orchestration (SB-111+)

n8n runs alongside capture-service in the `compose.n8n.yaml` overlay. Key facts:

- Persistent state on the EBS-backed volume at `/opt/second-brain/data/n8n`.
- Single instance, `N8N_CONCURRENCY_PRODUCTION_LIMIT=1`, SQLite during the foundation phase.
- UI accessible only through an SSH tunnel (`deploy/open-n8n-tunnel.sh`). Port 5678 is never publicly exposed.
- Credentials encrypted with an explicit external key; key stored outside the repository.
- Execution payloads not retained globally — raw capture text must never appear in n8n storage.
- `Second Brain - Error Handler` and `Second Brain - Intake` workflows are bootstrapped once via `deploy/bootstrap-n8n.sh`.

### Writer-service (SB-114+)

`writer-service` is a standalone FastAPI container (UID 10002, port 8001, never published to the host) that is the sole vault writer. n8n sends classified captures to `POST /internal/notes/file`; writer-service renders a deterministic Markdown note, writes it to the vault, appends an audit event to `99_log/events.ndjson`, and returns the real filesystem note path. capture-service then edits the Discord receipt to show the filed location.

Key filing behavior:

- Filenames follow `YYYY-MM-DD--<capture_id>--<title-slug>.md`. The `capture_id` is embedded in the filename.
- Idempotency is enforced by the `capture_id` frontmatter field. A duplicate request for the same `capture_id` returns the existing path without overwriting the note.
- When `inbox_reason` is non-null (`classifier_selected_inbox`, `needs_clarification`, `low_confidence`), the note is always written to `00_inbox/` regardless of the classification folder.
- `stub://` paths are no longer produced. All note paths are real vault filesystem paths.

Downstream delivery is enabled with `DOWNSTREAM_DELIVERY_ENABLED=true`. See `.env.example` and `deploy/writer-service.env.example` for all required variables.

### Git-backed vault sync (SB-115+)

When `GIT_SYNC_ENABLED=true`, every successful note write is committed and pushed to the private vault GitHub repository. An OS-level advisory `flock` (`fcntl.LOCK_EX`) on `/opt/vault/.writer.lock` serializes all writes — the kernel releases the lock automatically if the process terminates, so no stale application lock is possible.

The write sequence:

```text
acquire OS advisory flock on /opt/vault/.writer.lock
git fetch origin
git merge --ff-only origin/main
idempotency check (capture_id in frontmatter)
render Markdown note
write file to vault
append audit event to 99_log/events.ndjson
git add <note_path> 99_log/events.ndjson
git commit -m "note: <capture_id> via writer-service"
git push origin main
release flock
return { note_path, git_commit_hash }
```

Locally, `GIT_SYNC_ENABLED` defaults to `true` via `compose.override.yaml`. The `local-vault-init` one-shot service (alpine/git) initializes both the vault working tree and a bare fake remote before writer-service starts, so `docker compose up -d` works with no manual Git setup.

Git failure handling (SB-116): every bad state returns a typed error and preserves the raw SQLite capture. `git_push_rejected` is retryable; `git_merge_conflict`, `git_index_locked`, and `capture_id_duplicate` are terminal and require operator inspection. `.git/index.lock` is never deleted automatically.

### Error workflow (SB-113+)

When an n8n execution fails, `Second Brain - Error Handler` is invoked automatically via n8n's `errorWorkflow` setting. It reports the failure back to capture-service:

```text
POST /internal/captures/{id}/delivery/report-workflow-error
```

The endpoint accepts `disposition` (`retryable` or `terminal`), `error_type`, `reason_type`, `stage`, and execution metadata. Retryable reports schedule a capped-backoff retry; terminal reports mark the capture `DELIVERY_FAILED`. All values are validated as safe slugs — no raw exception text enters the ledger. Duplicate and stale reports are handled idempotently.

capture-service remains the sole owner of the capture ledger. n8n and writer-service reach it over the private Compose backend network at `http://capture-service:8000`.

See [deploy/README.md](deploy/README.md) for provisioning, bootstrap, and verification steps.

## Behavior notes

- Messages containing secrets are rejected before SQLite and before Gemini.
- Attachment-only messages are saved to inbox without calling Gemini.
- If Gemini returns low-confidence results, the note goes to `00_inbox/` — never silently dropped.
- The internal API is not published outside the container. n8n and writer-service reach it over the Compose bridge network.
- The container entrypoint refuses to start if the EBS sentinel file is absent, preventing SQLite writes to the root filesystem after a failed EBS remount.
- SIGTERM is owned by the outer runtime, not Uvicorn. The shutdown sequence records `capture_service_state = STOPPED` and closes SQLite before the process exits with code 0.
