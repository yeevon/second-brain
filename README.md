# Second Brain

A Discord bot that durably captures messages and, in local mode, automatically files them into an Obsidian vault using Gemini AI classification.

Send a message to a designated Discord channel. The bot screens it for secrets, persists it to SQLite, sends an immediate receipt, and — depending on the runtime mode — either stops there or classifies and files it into your vault.

## Release status

V3 (Milestone 7) is the current development branch. V3 adds a proposal-only vault-write path: an LLM client proposes structured changes via `brain-mcp-propose`, the user approves or rejects via Discord, and `writer-service` applies approved changes under the existing Git lock. The read-only `brain-mcp` profile is unchanged. EC2 production deployment of V3 is deferred to Milestone 9; V3 is built and validated locally in this milestone.

V2 local/full-stack validation work is complete and was released as `v2.0.0`. V2 validated Discord capture, n8n orchestration, writer-service-owned vault writes, Daily/Weekly vault-backed briefs, and host-visible Obsidian bind-mount behavior. EC2 production deployment is intentionally deferred until Milestone 9, after V3 and the post-V3 tech-debt cleanup.

## Runtime modes

Two explicit modes are supported via `CAPTURE_PROCESSING_MODE`:

| Mode | Where it runs | What it does |
| --- | --- | --- |
| `local-full` | Desktop | Capture → screen → persist → classify (Gemini) → file (Obsidian) → receipt. No Docker required. |
| `capture-only` | EC2 | Capture → screen → persist → receipt. Downstream filing handled by n8n + writer-service. |
| `capture-only` | Local Docker full stack | Same capture-service image as EC2, plus n8n, writer-service, and Git-backed local vault — all started by `docker compose up -d`. |

The mode must be set explicitly. Startup fails if it is missing or unsupported.

## What it does

### Both modes

1. **Captures** — Monitors a single Discord channel for messages from one allowed user.
2. **Screens** — Rejects messages containing secrets (API keys, tokens, passwords) before any persistence.
3. **Persists** — Writes the capture durably to SQLite before doing anything else.
4. **Receipts** — Sends an immediate Discord confirmation. Receipt text reflects actual delivery state: "Queued for downstream filing" when `DOWNSTREAM_DELIVERY_ENABLED=true`, or "Downstream filing is not enabled yet" when disabled.
5. **Reconciles** — On startup, replays Discord history to recover any messages missed while the service was offline.
6. **Corrections** — In writer-service-backed modes, `fix: <reason>` as a reply to a receipt, or `fix SB-YYYYMMDD-NNNN: <reason>` as a standalone message, moves a filed note to a new folder. Bare `fix:` with no target is rejected. Correction history is append-only.
7. **Internal API** — Exposes an authenticated HTTP API inside the process for state transitions and health checks.

### Downstream classification and filing

Both `local-full` (in-process Gemini worker) and the local Docker full stack / EC2 (`capture-only` + n8n + writer-service) support the steps below. The difference is where the work runs: `local-full` runs classification inside the capture-service process; the `capture-only` modes hand off to n8n and writer-service via webhook.

1. **Classifies** — Gemini returns structured JSON (folder, title, tags, body, actions).
2. **Files** — Writes a deterministic Markdown note into the vault under the correct folder.
3. **Clarifications** — When Gemini flags `needs_clarification: true`, the note is filed to `00_inbox/` and a follow-up question is sent via the Discord receipt. The capture remains `INBOX` with `clarification_status = NEEDS_CLARIFICATION` until a user reply resolves it. `secondbrain status` reports unresolved clarifications as a separate count.
4. **Final receipt** — Edits the original Discord message to show the filed location, inbox reason, or clarification question.

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
  mcp_server.py       # brain-mcp stdio server — five read-only vault and ledger tools
  mcp_propose_server.py  # brain-mcp-propose stdio server — eight proposal-only tools (V3)
  vault_pull.py       # vault-pull CLI — pull-only git sync of the Obsidian vault
  digest.py           # open-task vault scanner used by daily/weekly digest endpoints

writer-service/
  Dockerfile                         # Python 3.13-slim, gosu, port 8001 (runtime UID from LOCAL_UID)
  docker-entrypoint.sh               # creates runtime user, copies SSH secrets, execs gosu
  src/writerservice/
    main.py                          # FastAPI app, auth middleware, /health and /internal/notes/file
    config.py                        # GIT_SYNC_ENABLED, VAULT_PATH, WRITER_SERVICE_TOKEN
    api_models.py                    # FileNoteRequest, FileNoteResponse, Classification
    writer.py                        # deterministic Markdown generation and vault write dispatch
    vault.py                         # folder enum → physical directory mapping, path validation
    audit.py                         # append-only 99_log/events.ndjson writes
    flock.py                         # WriterLock — kernel-managed OS advisory flock (fcntl.LOCK_EX)
    git_ops.py                       # fetch, merge, write, add, commit, push sequence
    git_errors.py                    # typed Git-layer error classes
  tests/
    unit/                            # auth, validation, Markdown generation, idempotency, flock, git_ops
    integration/                     # filing, idempotent replay, inbox routing, Git sync, Git failures

writer-service/
  Dockerfile                         # Python 3.13-slim, gosu, port 8001 (runtime UID from LOCAL_UID)
  docker-entrypoint.sh               # creates runtime user, copies SSH secrets, execs gosu
  src/writerservice/
    main.py                          # FastAPI app, auth middleware, /health and /internal/notes/file
    config.py                        # GIT_SYNC_ENABLED, VAULT_PATH, WRITER_SERVICE_TOKEN
    api_models.py                    # FileNoteRequest, FileNoteResponse, Classification
    writer.py                        # deterministic Markdown generation and vault write dispatch
    vault.py                         # folder enum → physical directory mapping, path validation
    audit.py                         # append-only 99_log/events.ndjson writes
    flock.py                         # WriterLock — kernel-managed OS advisory flock (fcntl.LOCK_EX)
    git_ops.py                       # fetch, merge, write, add, commit, push sequence
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
  backup.sh                          # nightly encrypted snapshot (SQLite, vault, n8n data volume)
  restore-validate.sh                # weekly restore validation into a temporary directory
  backup.env.example                 # backup environment template
  second-brain-backup.cron           # cron schedule for backup and restore-validate jobs
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
- **`capture-only` (local Docker full stack):** Docker Desktop or Docker Engine with Compose plugin; a Gemini API key (required by `local-n8n-init`)

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

## Setup — local Docker full stack

`docker compose up -d` starts the complete local stack and enforces a safe boot order automatically:

- `local-vault-init` — one-shot alpine/git service that initializes the vault working tree and a bare fake remote
- `n8n` — classification orchestration; healthcheck probes `POST /rest/login` (HTTP 400 = REST ready), not just the static web server
- `local-n8n-init` — one-shot Python service that seeds all n8n state (owner account, credentials, workflows, webhook); starts only after n8n REST is ready; `capture-service` does not start until this completes
- `writer-service` — Markdown generation and Git-backed vault writes; `capture-service` waits for it to be healthy
- `capture-service` — Discord intake and SQLite ledger; starts last, after the webhook is registered and writer-service is ready

`compose.override.yaml` is auto-loaded and provides local-safe defaults. In default named-volume mode, the EBS sentinel marker and local fake vault remote are created automatically on first start. When using `LOCAL_VAULT_PATH`, the host vault must already be a Git repository with `origin` configured — see the bind-mount section below.

### Required env files

```bash
cp .env.example .env                      # Discord credentials, internal tokens, GEMINI_API_KEY
cp deploy/n8n.env.example n8n.local.env   # n8n environment (encryption key path, etc.)
printf '%s' "$(openssl rand -hex 32)" > n8n-encryption-key.local
```

The `.env` file must include these variables for `local-n8n-init`:

```env
CAPTURE_SERVICE_INTERNAL_TOKEN=<at least 32 random characters>
WRITER_SERVICE_TOKEN=<at least 32 random characters>
N8N_INTAKE_WEBHOOK_TOKEN=<at least 32 random characters>
GEMINI_API_KEY=<your Gemini API key>    # required — local-n8n-init fails without it
```

### Optional: bind-mount a host vault (`LOCAL_VAULT_PATH`)

By default, the stack uses a Docker named volume for the vault. To make the vault visible to Obsidian and `brain-mcp` on the host without any Docker volume copy, point `LOCAL_VAULT_PATH` at a local git repository instead:

```env
LOCAL_VAULT_PATH=/home/your-name/prj/my-vault   # must be a git repo with origin configured
LOCAL_UID=1000                                   # your host uid (run: id -u)
LOCAL_GID=1000                                   # your host gid (run: id -g)

GIT_SYNC_ENABLED=true
VAULT_DEPLOY_KEY_FILE=/home/your-name/.ssh/second-brain/my-vault-deploy-key
GITHUB_KNOWN_HOSTS_FILE=deploy/github_known_hosts   # default; shipped in the repo
```

Prerequisites before setting `LOCAL_VAULT_PATH`:

1. The directory must already be a git repository with an `origin` remote configured. The init container will fail loudly if `origin` is missing.
2. Add a write-access deploy key to the GitHub repository and point `VAULT_DEPLOY_KEY_FILE` at the private key file on your host.

`deploy/github_known_hosts` contains pinned SSH host keys for `github.com` and is the default value of `GITHUB_KNOWN_HOSTS_FILE`.

The writer-service entrypoint creates the runtime user at `LOCAL_UID`/`LOCAL_GID` on each container start and copies the deploy key and known_hosts from Docker secrets to `~/.ssh/`. It fails fast on startup if `GIT_SYNC_ENABLED=true` and either file is missing.

### Start and stop

```bash
docker compose up -d                # build images, start all services, seed n8n automatically
docker compose logs -f              # follow logs
docker compose ps                   # check status
docker compose down                 # stop services — named volumes preserved
docker compose down -v              # destructive reset — deletes all named volumes
```

Use `docker compose down` for normal shutdown. Use `docker compose down -v` only for a destructive local reset or acceptance testing — it permanently deletes all SQLite, n8n, and vault volume data.

### Convenience wrapper

```bash
deploy/local-stack-up.sh            # validates env files, starts stack, waits for healthy, verifies vault
```

### Regression scripts

```bash
deploy/test-container-packaging.sh   # Python version, env overrides, invariants, SIGTERM
deploy/test-writer-service.sh        # writer-service health, filing, idempotency, audit log
deploy/test-writer-safe-failure.sh   # Git failure injection: merge conflict, push rejected, index lock
deploy/test-n8n-error-workflow.sh    # n8n error workflow end-to-end regression
```

`test-container-packaging.sh` runs four tests. The SIGTERM test stops the container — run `docker compose up -d` again before the next test cycle.

## Setup — capture-only (EC2)

This EC2 path is documented for staging/non-production validation. Do not treat it as production deployment until Milestone 9 is complete.

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
- `Second Brain - Error Handler`, `Second Brain - Intake`, `Second Brain - Daily Digest`, and `Second Brain - Weekly Review` are bootstrapped via `deploy/bootstrap-n8n.sh`. Intake, Daily Digest, and Weekly Review update in place by existing ID on EC2/staging; local `local-n8n-init` updates all four workflows in place.

### Intake pipeline reliability

The intake workflow includes explicit error handling at every external boundary:

- **Gemini failures** — HTTP 429/403/5xx and timeouts route to `Schedule Retry (Gemini error)` with `error_type: gemini_http_error` instead of leaking a stale lease. `Classify with Gemini` uses `temperature: 0` and `maxOutputTokens: 2048` for deterministic, compact output.
- **Invalid classification** — `Valid Classification?` IF node catches empty route or `valid: false` and routes to `Schedule Retry (classifier)` with `error_type: invalid_classifier_output`.
- **Clarification branch** — `Needs Clarification?` IF node routes to `Record Clarification` after inbox filing, setting `clarification_status = NEEDS_CLARIFICATION` on the capture in capture-service.

### Daily and weekly briefs (SB-120/SB-121)

Milestone 6 adds scheduled n8n review workflows that post Discord summaries from vault state, not raw capture-count rollups:

- **Daily Brief** — `Second Brain - Daily Digest` calls `GET http://capture-service:8000/internal/brief/daily` and formats Today's Focus, Coming Up, Upcoming Birthdays, Pending Tasks, and Stale / Neglected.
- **Weekly Review** — `Second Brain - Weekly Review` calls `GET http://capture-service:8000/internal/brief/weekly`, formats accomplished notes, completed tasks, decisions, still-open work, and study progress, then asks Gemini for a clearly labelled AI-generated priorities section.
- **Data source** — capture-service first proxies to writer-service (`GET /internal/vault/brief/{daily,weekly}`) so the brief is grounded in the current vault. If writer-service is unavailable and `VAULT_PATH` is configured, capture-service falls back to a local vault scan.
- **Activation** — both scheduled workflows are imported inactive. Bind the Capture Service Token credential and configure `DISCORD_DIGEST_WEBHOOK_URL` in n8n before activating them.
- **No-wipe updates** — local `docker compose up -d --build` runs `local-n8n-init`, which updates all four local workflows in place by existing ID. EC2/staging bootstrap updates Intake, Daily Digest, and Weekly Review in place; Error Handler is imported only when missing.

### Intake pipeline reliability

The intake workflow includes explicit error handling at every external boundary:

- **Gemini failures** — HTTP 429/403/5xx and timeouts route to `Schedule Retry (Gemini error)` with `error_type: gemini_http_error` instead of leaking a stale lease. `Classify with Gemini` uses `temperature: 0` and `maxOutputTokens: 2048` for deterministic, compact output.
- **Invalid classification** — `Valid Classification?` IF node catches empty route or `valid: false` and routes to `Schedule Retry (classifier)` with `error_type: invalid_classifier_output`.
- **Clarification branch** — `Needs Clarification?` IF node routes to `Record Clarification` after inbox filing, setting `clarification_status = NEEDS_CLARIFICATION` on the capture in capture-service.

### Daily and weekly briefs (SB-120/SB-121)

Milestone 6 adds scheduled n8n review workflows that post Discord summaries from vault state, not raw capture-count rollups:

- **Daily Brief** — `Second Brain - Daily Digest` calls `GET http://capture-service:8000/internal/brief/daily` and formats Today's Focus, Coming Up, Upcoming Birthdays, Pending Tasks, and Stale / Neglected.
- **Weekly Review** — `Second Brain - Weekly Review` calls `GET http://capture-service:8000/internal/brief/weekly`, formats accomplished notes, completed tasks, decisions, still-open work, and study progress, then asks Gemini for a clearly labelled AI-generated priorities section.
- **Data source** — capture-service first proxies to writer-service (`GET /internal/vault/brief/{daily,weekly}`) so the brief is grounded in the current vault. If writer-service is unavailable and `VAULT_PATH` is configured, capture-service falls back to a local vault scan.
- **Activation** — both scheduled workflows are imported inactive. Bind the Capture Service Token credential and configure `DISCORD_DIGEST_WEBHOOK_URL` in n8n before activating them.
- **No-wipe updates** — local `docker compose up -d --build` runs `local-n8n-init`, which updates all four local workflows in place by existing ID. EC2/staging bootstrap updates Intake, Daily Digest, and Weekly Review in place; Error Handler is imported only when missing.

### Writer-service (SB-114+)

`writer-service` is a standalone FastAPI container (port 8001, never published to the host) that is the sole vault writer. The container runs as the host user UID when `LOCAL_UID` is set, or as UID 10003 by default, via a gosu entrypoint that creates the runtime user dynamically at start. n8n sends classified captures to `POST /internal/notes/file`; writer-service renders a deterministic Markdown note, writes it to the vault, appends an audit event to `99_log/events.ndjson`, and returns the real filesystem note path. capture-service then edits the Discord receipt to show the filed location.

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

### Brain-MCP server (SB-123+)

`brain-mcp` is a read-only MCP stdio server that exposes vault and ledger tools to AI clients (Claude Code, Claude Desktop, or any MCP-compatible client). Register it once at user scope:

```bash
claude mcp add \
  -e VAULT_PATH=/home/your-name/prj/my-vault \
  --scope user \
  second-brain \
  -- uv run --project /home/your-name/prj/second-brain brain-mcp
```

Available tools: `search_notes`, `read_note`, `list_recent_notes`, `list_open_tasks`, `get_sync_status`.

`LEDGER_PATH` is optional. When unset, all vault tools work normally and `get_sync_status` reports `ledger_exists: false`. Only set `LEDGER_PATH` if you want `get_sync_status` to include ledger state. `read_note` rejects non-markdown paths and hidden files (paths starting with `.`).

`_vault_preflight()` runs before every tool call: checks that `VAULT_PATH` is configured, exists, and has a clean git worktree. Untracked files (e.g. Obsidian's `.obsidian/` directory) do not trigger the stale-data warning — only modified or staged tracked files do.

### Vault-update proposals (SB-136–SB-140)

V3 adds a proposal-only write path. LLM clients may propose vault changes but may never write vault files directly. All approved changes go through `writer-service` under the existing `flock` Git lock.

**Proposal lifecycle:**

```text
brain-mcp-propose tool call
  → POST /internal/vault/proposals (capture-service validates + stores proposal)
  → Discord approval request message posted (approval_message_id stored)
  → User replies: approve VUP-YYYYMMDD-NNNN  or  reject VUP-YYYYMMDD-NNNN
  → On approve: writer-service apply sequence (flock → git fetch → merge → verify → mutate → audit → commit → push)
  → Proposal updated with APPLIED + git_commit_hash
  → Discord approval message edited to show outcome
```

**Allowed operations (initial set):** `mark_task_done`, `mark_task_open`, `set_task_due_date`, `set_task_priority`, `append_task`, `append_note_section`, `move_note_to_folder`, `add_project_tag`, `add_weekly_review_entry`.

**Safety invariants:**

- Path traversal (`../`, absolute paths outside vault root, hidden-file prefixes) rejected at proposal creation.
- Lifecycle guard: archived or superseded notes cannot be mutated.
- Stale anchor detection: if `target_anchor_json` is present, the operation verifies the anchor text still matches file content; stale anchor returns failure without touching the vault.
- `check_working_tree_clean` uses `--untracked-files=normal` so a crashed prior write (untracked file) blocks the next apply. `.obsidian/` is filtered to prevent Obsidian workspace noise from falsely blocking applies.
- Audit record (`VAULT_UPDATE_APPLIED`) appended to `99_log/events.ndjson` on every successful apply. Commit hash stored on the proposal row.
- Raw capture ledger is never modified by LLM tooling.

**Capture-service internal API additions:**

```text
POST /internal/vault/proposals
GET  /internal/vault/proposals/:proposal_id
GET  /internal/vault/proposals?status=PENDING
PATCH /internal/vault/proposals/:proposal_id
```

**writer-service internal API addition:**

```text
POST /internal/vault/apply-proposal
```

### Brain-MCP propose server (SB-139)

`brain-mcp-propose` is a separate MCP stdio server for LLM clients that want to propose vault changes. Register it explicitly to opt in — it is not the default:

```bash
claude mcp add \
  -e VAULT_PATH=/home/your-name/prj/my-vault \
  -e CAPTURE_SERVICE_URL=http://localhost:8000 \
  -e CAPTURE_SERVICE_INTERNAL_TOKEN=<token> \
  --scope user \
  second-brain-propose \
  -- uv run --project /home/your-name/prj/second-brain brain-mcp-propose
```

Available tools: `propose_task_completion`, `propose_due_date_change`, `propose_priority_change`, `propose_note_move`, `propose_task_append`, `propose_review_entry`, `list_pending_update_proposals`, `read_update_proposal`.

The following tools are explicitly absent from this profile: `write_note`, `delete_note`, `replace_note`, `move_note_directly`, `git_commit`, `git_push`, `shell`.

### gembrain CLI (SB-124)

`gembrain` is the host-facing wrapper around vault sync, local vault queries, and Gemini CLI:

```bash
gembrain status
gembrain recent --days 7 --limit 10
gembrain tasks --project second-brain
gembrain ask "What should I focus on next?"
```

`status` reports vault sync state without running a pull. `recent`, `tasks`, and `ask` run the `vault-pull` preflight first, so AI queries operate on a fresh, tracked-clean vault. `ask` delegates to the Gemini CLI and points it at `brain-mcp`; `gembrain` does not write to the vault.

### Encrypted off-host backups (SB-119+)

Nightly encrypted snapshots cover all durable state: SQLite ledger (via `sqlite3 .backup`, never a raw file copy), the EC2 vault clone, and the n8n data volume. Secrets are excluded or redacted from configuration backups. A weekly restore validation runs into a temporary directory only — it never touches live volumes. `secondbrain status` reports the last successful backup and restore validation timestamps.

- **`deploy/backup.sh`** — nightly snapshot script.
- **`deploy/restore-validate.sh`** — restore validation against a temporary location.

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
