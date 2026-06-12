# Changelog

All notable changes to this project are documented here.

---

## Milestone 3 — Move classification into n8n

### SB-112 — At-least-once webhook delivery from capture-service to n8n

Implemented the full webhook delivery pipeline between capture-service and n8n, including a writer-stub container that breaks the n8n concurrency deadlock.

- **`src/secondbrain/n8n_delivery.py`** — `N8nWebhookDeliveryClient` posts `{capture_id, delivery_attempt}` envelopes to the n8n intake webhook with `X-Second-Brain-Intake-Token` authentication. Raises on 4xx/5xx so the dispatcher can schedule a retry.
- **`src/secondbrain/config.py`** — `DOWNSTREAM_DELIVERY_ENABLED` feature flag (default `false`). When enabled, `N8N_INTAKE_WEBHOOK_URL` (must start with `http://n8n:5678/webhook/second-brain-intake`) and `N8N_INTAKE_WEBHOOK_TOKEN` (min 32 chars) become required.
- **`src/secondbrain/app.py`** — `ensure_delivery_dispatcher_task` wires the dispatcher loop when delivery is enabled. `reconcile_once` calls it on each reconciliation pass.
- **`src/secondbrain/ledger.py`** — `_schedule_retry` now returns typed `RetryDisposition` with an `outcome` field instead of raising `ValueError`. Returns `ignored_stale_attempt`, `ignored_already_terminal`, or `ignored_retry_already_scheduled` for replay-safe cases. `_mark_delivery_failed_terminally` detects `idempotent_replay` (same attempt, same reason) and `conflicting_replay` (same attempt, different reason) when a capture is already `DELIVERY_FAILED`, and `ignored_already_terminal` for captures that already reached `COMPLETE`.
- **`src/secondbrain/capture_models.py`** — `RetryDisposition.outcome` field added. `DeliveryMutationResult.outcome` extended with `ignored_already_terminal` and `conflicting_replay` variants.
- **`src/secondbrain/delivery.py`** — Removed `try/except ValueError`; now checks `disposition.outcome.startswith("ignored_")` and skips silently.
- **`src/secondbrain/capture_api.py`** — `_acknowledge_forwarded_response()` returns HTTP 200 for all four outcomes (`changed`, `idempotent_replay`, `stale_attempt`, `invalid_state`) — the atomic winner gate prevents n8n from seeing 409 on legitimate retries. `_acknowledge_failed_response()` returns HTTP 409 only for `conflicting_replay`. Three new internal endpoints: `GET /internal/downstream/captures/{id}`, `POST /internal/security/screen`, `POST /internal/contracts/classification/validate`. All n8n-facing models inherit `StrictInternalRequest` (`extra="forbid"`).
- **`src/secondbrain/api_models.py`** — `StrictInternalRequest` base class. `DownstreamCaptureResponse`, `SecurityScreenRequest/Response`, `ClassificationValidationRequest/Response` added. Sensitive captures return `raw_text=None`.
- **`src/secondbrain/status.py`** — `captures_filed_today` and `last_successful_vault_write` queries exclude `derived_note_path LIKE 'stub://%'` so writer-stub filings do not pollute real vault metrics.
- **`src/secondbrain/receipts.py`** — `format_saved_receipt` updated: downstream-enabled variant now reads "Your note is safely captured. / Queued for downstream filing." `format_stub_filed_receipt` and `format_stub_inbox_receipt` added for writer-stub terminal paths.
- **`writer-stub/`** — Standalone FastAPI container (UID 10002, port 8001, no published host port). Receives `POST /write` and `POST /inbox` from n8n; writes `stub://<capture_id>` paths back to capture-service. Prevents the n8n concurrency deadlock (`N8N_CONCURRENCY_PRODUCTION_LIMIT=1` blocks n8n from calling itself while processing).
- **`compose.n8n.yaml`** — `second-brain-writer-stub` service added: builds from `writer-stub/`, `expose: ["8001"]`, `cap_drop: ALL`, joined to backend network, Python urllib healthcheck.
- **`n8n/workflows/second-brain-intake.json`** — `Second Brain - Intake` workflow fixture: webhook at `second-brain-intake` with `responseMode: responseNode`, immediate 202 response, screen → route → Gemini classify → validate → writer-stub, acknowledge-forwarded. `saveDataSuccessExecution: none`, `saveDataErrorExecution: none`, `active: false`.
- **`deploy/bootstrap-n8n.sh`** — Extended to bootstrap `Second Brain - Intake` alongside `Second Brain - Error Handler`. Both are idempotent by name, strip `id`/`versionId`, import inactive, and verify by name after import.
- **`deploy/writer-stub.env.example`** — Template with `WRITER_STUB_INTERNAL_TOKEN`, `CAPTURE_SERVICE_URL`, `CAPTURE_SERVICE_INTERNAL_TOKEN`.
- **`.env.example`** / **`deploy/capture-service.env.example`** — `DOWNSTREAM_DELIVERY_ENABLED`, `N8N_INTAKE_WEBHOOK_URL`, `N8N_INTAKE_WEBHOOK_TOKEN`, `DELIVERY_WEBHOOK_TIMEOUT_SECONDS` added.
- **`deploy/local-stack-up.sh`** / **`deploy/deploy.sh`** — Updated to pass `WRITER_STUB_ENV_FILE` and verify writer-stub health.
- **`tests/architecture/test_n8n_intake_workflow.py`** — 30 architecture assertions: fixture validity, webhook path and auth, all capture-service and writer-stub URLs, Gemini URL, no localhost, PLACEHOLDER credentials only, no restricted node types, execution retention settings.
- **`tests/unit/test_n8n_delivery.py`** — Config validation for `DOWNSTREAM_DELIVERY_ENABLED` and delivery client behavior (envelope shape, auth header, error propagation).
- **`tests/unit/test_retry_replay_safety.py`** — Replay-safety outcomes for `_schedule_retry` and `_mark_delivery_failed_terminally`.
- **`tests/unit/test_stub_exclusion.py`** — `stub://` exclusion in `captures_filed_today` and `last_successful_vault_write`; stub receipt formatters.

---

### SB-111 — Deploy a secured n8n foundation

Added n8n as a Compose overlay alongside capture-service. Key changes:

- **`compose.n8n.yaml`** — n8n service overlay. Image pinned to `docker.n8n.io/n8nio/n8n:1.123.55`. Port 5678 bound to `127.0.0.1` only. Encryption key injected via Docker secret. Data volume mounted at `/home/node/.n8n`.
- **`deploy/n8n.env.example`** — environment template. Execution payload retention defaults to `none` globally. `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` prevents Code nodes from reading host environment variables.
- **`deploy/local-stack-up.sh`** / **`local-stack-down.sh`** / **`local-n8n-reset.sh`** — local full-stack lifecycle scripts. Existing `deploy/local-up.sh` and capture-only scripts are unchanged.
- **`deploy/open-n8n-tunnel.sh`** — SSH tunnel helper for desktop access to the n8n editor.
- **`deploy/bootstrap-n8n.sh`** — one-time idempotent workflow importer. Strips `id` and `versionId` before import to prevent ID collision. Verifies import by name; exits non-zero on duplicate or missing post-import.
- **`deploy/test-n8n-foundation.sh`** — local runtime regression script covering health, image pin, non-root user, loopback binding, volume mount, backend network reachability, and data directory write access.
- **`n8n/workflows/second-brain-error-handler.json`** — Error Trigger workflow fixture. Normalizes safe metadata only (workflow name, IDs, error type category, timestamp). No `id` or `versionId` at the top level.
- **`deploy/deploy.sh`** — extended to export n8n variables, validate n8n data dir / env file / key file, and set `COMPOSE_FILE=compose.yaml:compose.n8n.yaml`.
- **`deploy/provision-host.sh`** — creates `/opt/second-brain/data/n8n` owned by `1000:1000`.
- **`deploy/verify.sh`** — extended with 12 n8n checks. Final output: both `capture-service deployment checks passed` and `n8n foundation deployment checks passed`.
- **`tests/architecture/test_n8n_foundation_config.py`** — 38 architecture regressions covering Compose overlay, environment template, secret exclusions, bootstrap behavior, and deployment script requirements.
- **n8n image pinned:** `1.123.55` (confirmed stable at implementation time).

---

## Milestone 2 — Harden durable intake before adding orchestration

**Commits:** `783b090` → `d2a4124` | **Branch:** `milestone_two`

The goal of Milestone 2 was to make the EC2 `capture-only` service production-grade: no data loss under load, automatic recovery from stale state, observable health, and a correctly packaged container that shuts down cleanly.

### SB-105 — SQLite runtime hardening

Replaced direct `sqlite3` calls with a dedicated background worker thread that serializes all mutations. Added WAL mode, `foreign_keys = ON`, `busy_timeout`, bounded retry logic with exponential backoff on `SQLITE_BUSY`, schema migrations applied at startup, and a SQLite version startup check. Short transactions are enforced as an architecture invariant — no write transaction is held open while calling Discord, Gemini, or any external service.

### SB-106 — Periodic Discord reconciliation

Added a periodic reconciliation loop alongside the existing startup catch-up. The loop stores a high-water mark of the last reconciled Discord message ID in the ledger, scans a bounded history window, and records a visible warning if the scan limit is reached or reconciliation fails. Metrics for recovered messages are included in each reconciliation result.

### SB-107 — Delivery leases and capped retry state

Extended the ledger with `delivery_attempts`, `processing_lease_until`, `next_attempt_at`, and `last_error`. Added explicit terminal and retryable states: `RECEIVED`, `FORWARDED`, `CLASSIFYING`, `FILED`, `INBOX`, `FAILED`. Leases prevent concurrent processing of the same capture. Retry backoff is capped — captures cannot loop forever.

### SB-108 — Stale-lease reaper

Added a single-flight watchdog loop (`reaper.py`) that runs inside the `capture-only` process. Each pass claims a bounded batch of expired leases, increments retry attempts transactionally, applies capped exponential backoff, and marks permanently stuck captures `FAILED`. A visible Discord alert is sent after the retry limit is reached. Manual retry is exposed as `secondbrain retry <capture_id>`.

### SB-109 — Operational status command

Expanded `secondbrain status` to show: captures received today, captures filed today, captures in inbox, captures failed, captures waiting for retry, stale leases, last successful reconciliation timestamp, and capture-service health (HEALTHY / STARTING / STALE / STOPPED / UNKNOWN). Health is derived from `capture_service_state` and the last heartbeat timestamp recorded by a periodic heartbeat loop.

### SB-110 — Milestone 2 exit regression runbook

Created `docs/Milestones/002/SB-110-runbook.md`: a structured E2E test procedure covering container startup, live capture, sensitive-input rejection, offline reconciliation, missing-volume recovery, clean shutdown, and EC2 deployment verification. Includes a Docker-based local validation path using `deploy/local-up.sh`.

### SB-110A — Container packaging fix

Resolved several packaging defects exposed by the SB-110 E2E procedure:

- **Named volume pinned** — `compose.local.yaml` now declares `name: second-brain-local-data` so Compose does not create a project-scoped volume that diverges from the name used by the local scripts.
- **Container environment overrides** — `compose.yaml` adds an `environment:` block that forces `CAPTURE_PROCESSING_MODE=capture-only`, `CAPTURE_API_HOST=0.0.0.0`, `CAPTURE_API_PORT=8000`, and `LEDGER_PATH=/var/lib/second-brain/ledger.sqlite3` inside the container, preventing desktop `.env` values from leaking in.
- **Required variables with `:?`** — `CAPTURE_SERVICE_ENV_FILE` and `CAPTURE_DATA_SOURCE` now use the Compose required-variable syntax; startup fails loudly if either is unset.
- **`CAPTURE_DATA_DIR` renamed to `CAPTURE_DATA_SOURCE`** — reflects that the source can be either a Docker named volume or a host bind-mount path.
- **Local scripts** — added `deploy/local-up.sh` (build → create volume → init sentinel → start → wait for healthy), `deploy/local-down.sh` (stop, preserve volume), and `deploy/local-reset.sh --confirm-delete-local-test-data` (tear down, delete volume, recreate empty volume).
- **Packaging regression test** — `deploy/test-container-packaging.sh` runs three tests: Python 3.13 runtime user, container environment overrides, and running-container invariants (health, private port, sentinel, ledger, write permission). A fourth test (SIGTERM clean shutdown) was added by SB-110B.
- **`capture-service.local.env` excluded** — added to both `.gitignore` and `.dockerignore`.
- **EC2 `deploy.sh` updated** — exports `COMPOSE_FILE=compose.yaml` to prevent `compose.local.yaml` from being included on EC2.

### SB-110B — Graceful SIGTERM shutdown

Fixed a lifecycle defect where Uvicorn could intercept SIGTERM before the outer runtime had a chance to record `capture_service_state = STOPPED` and close SQLite cleanly.

- **`EmbeddedUvicornServer`** — subclasses `uvicorn.Server` and overrides `capture_signals()` as a no-op context manager, removing Uvicorn's process-level signal interception entirely.
- **Outer signal ownership** — `run_service_runtime()` registers `loop.add_signal_handler` for SIGTERM and SIGINT, waits for the stop event alongside the API and Discord tasks, and removes the handlers at the end of the `finally` block.
- **Shutdown sequence** — SIGTERM → stop event set → `api_server.stop()` → `client.close()` → background tasks cancelled → `record_capture_service_stop()` → `capture_service.close()` → exit 0.
- **Regression tests** — unit tests verify `capture_signals()` installs no handlers, `os.kill(SIGTERM)` wakes the runtime and runs the full sequence, `record_capture_service_stop` fires once with the correct instance ID, signal handlers are removed after exit, and `test-container-packaging.sh` Test 4 proves the fix at the container boundary.

---

## Milestone 1 — Make the MVP deployable without changing behavior

**Commits:** `63a3f1a` → `3573aa8` | **Branch:** `main`

The goal of Milestone 1 was to get the working capture loop onto EC2 before introducing n8n or Git complexity. No user-facing capture behavior changed.

### SB-101 — Regression test suite

Added a comprehensive regression suite covering the proven local behavior:

- Normal message creates exactly one SQLite capture and one receipt.
- Duplicate Discord event does not create a duplicate row or receipt.
- Bot receipts are ignored.
- Secret-like input stores only a redacted rejection.
- Gemini failure routes to `00_inbox/`.
- Vault-write failure leaves the raw capture recoverable.
- Startup reconciliation recovers a missed message exactly once.
- Receipt-edit failure sends one replacement receipt.

### SB-102 — Capture-service boundary

Refactored the monolith so Discord intake, SQLite ownership, receipts, and reconciliation sit behind a clean `CaptureService` facade. `CaptureService` is now the sole production SQLite owner. `worker.py`, `app.py`, and `reconcile.py` no longer touch the ledger directly.

### SB-103 — Internal capture-service API

Added a small authenticated internal HTTP API wrapping `CaptureService`:

```text
GET  /health
GET  /internal/captures/:capture_id
POST /internal/captures/:capture_id/mark-forwarded
POST /internal/captures/:capture_id/mark-classifying
POST /internal/captures/:capture_id/mark-filed
POST /internal/captures/:capture_id/mark-inbox
POST /internal/captures/:capture_id/mark-failed
POST /internal/captures/:capture_id/retry
POST /internal/receipts/:capture_id/edit
```

Every state-changing route requires a shared-secret header. The API runs inside the capture-service process and is not published outside it.

### SB-104 — Dockerize and deploy capture-service to EC2

Added an explicit `capture-only` runtime mode alongside the existing `local-full` mode. Added Docker and EC2 deployment infrastructure.

**Application changes:**

- `CAPTURE_PROCESSING_MODE` is now required. Supported values: `local-full`, `capture-only`. Startup fails visibly on missing or unsupported values.
- `capture-only` mode does not require Gemini configuration, a vault path, or a classifier worker.
- `capture-only` receipt text is honest: "Your note is safely captured. Downstream filing is not enabled yet."
- `CaptureOnlyStartup` marks reconciliation complete only after it succeeds, so a transient failure on `on_ready` allows a retry on the next Discord ready callback.
- `CaptureService` accepts an optional `notify_capture` callback; passing `None` leaves accepted captures as `RECEIVED` without treating the absence of downstream processing as a failure.

**Container and deployment:**

- `Dockerfile` — initially Python 3.12; corrected to Python 3.13 by SB-110A. Non-root user (uid 10001), locked production dependencies, no dev dependencies, no secrets baked in.
- `.dockerignore` — excludes `.git`, `.venv`, `.env*`, `.runtime`, `vault`, `tests`, `docs`.
- `compose.yaml` — `restart: unless-stopped`, EBS bind mount, `expose: 8000` (no `ports:`), `cap_drop: ALL`, `read_only: true`, `tmpfs: /tmp`, `no-new-privileges`, JSON log rotation, internal health check. Paths initially parameterized via `CAPTURE_SERVICE_ENV_FILE` and `CAPTURE_DATA_DIR`; `CAPTURE_DATA_DIR` renamed to `CAPTURE_DATA_SOURCE` by SB-110A.
- `deploy/container-entrypoint.sh` — checks for `/var/lib/second-brain/.second-brain-ebs-volume` before starting the process. Container exits immediately if the EBS volume is not mounted, preventing SQLite writes to the root filesystem after a failed remount.
- `deploy/provision-host.sh` — installs Docker Engine and Compose plugin, creates application directories. Config directory is owned by the deploy user (not root) so Compose can read the env file without `sudo`.
- `deploy/deploy.sh` — fails before `docker compose up` if `$DATA_DIR` is not a mount point or the EBS sentinel marker is absent.
- `deploy/verify.sh` — checks container running state, restart policy, non-root user, unpublished port, mounted data path, EBS marker, bind-mount source, ledger file existence, and container health.
- `deploy/capture-service.env.example` — EC2 environment template. Does not include `GEMINI_API_KEY` or `VAULT_PATH`.
- `deploy/README.md` — EC2 provisioning guide including EBS setup, sentinel marker creation, SSH hardening verification, IMDSv2 enforcement, security-group audit, and `DeleteOnTermination=false` verification.

**Tests added:** unit, integration, and architecture tests covering `capture-only` mode behavior, container configuration semantics, and entrypoint behavior.

---

## MVP — Version 1

**Commits:** `bf44637` → `d2bb208` | **Branch:** `main`

Initial working capture loop running locally on the desktop.

- Discord Gateway listener restricted to one guild, one channel, one user.
- Pre-persistence secret screening rejects messages containing API keys, tokens, or passwords.
- SQLite ledger as the durable source of truth.
- Immediate "received" Discord receipt after successful persistence.
- Background Gemini classification worker (non-blocking).
- Markdown note rendering and filing into a numbered Obsidian vault structure.
- Final receipt edit showing filed location, inbox routing reason, or vault failure.
- Startup reconciliation replays Discord history to recover missed messages after downtime.
- Append-only audit log at `99_log/events.ndjson`.
