# Changelog

All notable changes to this project are documented here.

---

## v1.0.5 — Milestone 5: End-to-end note lifecycle

**Branch:** `milestone_5`

Completed the full note lifecycle: ambiguous captures ask a clarification question and wait for a reply before filing; filed notes can be corrected by replying to the receipt or supplying an explicit capture ID; the entire corpus — SQLite ledger, vault, and n8n data volume — is backed up nightly to an encrypted off-host snapshot with a weekly restore validation. Several pipeline reliability bugs were also fixed during milestone delivery.

### SB-117 — Clarification handling

When Gemini returns `needs_clarification: true`, the note is filed into `00_inbox/` and a follow-up question is sent to the Discord receipt via the receipt/edit API. The capture transitions to `NEEDS_CLARIFICATION` and remains there until a user reply resolves it.

- **`POST /internal/clarifications/:capture_id`** — records the clarification question and transitions the capture to `NEEDS_CLARIFICATION`. Idempotent: a duplicate request for the same capture returns the existing record.
- **`src/secondbrain/ledger.py`** — `record_clarification()` write job. Stores the clarification question text, receipt message ID, and timestamp. Enforces that only `INBOX`-status captures can transition to `NEEDS_CLARIFICATION`.
- **`src/secondbrain/capture_service.py`** — `record_clarification()` facade. Correction commands (`fix:`, `fix SB-…:`) detected before persistence and skipped by `_capture_if_allowed` so they are never saved as notes.
- **`n8n/workflows/second-brain-intake.json`** — `Needs Clarification?` IF node branches after inbox acknowledgement. True branch calls `POST /internal/clarifications/:capture_id` via `Record Clarification` HTTP node.
- **`secondbrain status`** — unresolved clarifications appear as a separate count; captures in `NEEDS_CLARIFICATION` are excluded from the inbox count.
- Timeout does not delete or reclassify the note. It remains in `00_inbox/` until a reply resolves it.

### SB-118 — Corrections

Supports targeted note corrections via Discord reply or explicit capture ID. Bare unthreaded `fix:` messages are rejected — the system never guesses the most-recent capture.

- **Reply-to-receipt form** — replying to a filing receipt with `fix: <new folder or reason>` moves the note and updates the receipt.
- **Explicit form** — `fix SB-YYYYMMDD-NNNN: <reason>` works without an open thread.
- **Bare-message rejection** — an unthreaded `fix:` with no capture ID in context is rejected with a clear error reply. Bare fix messages are detected in the gateway path before any persistence attempt.
- **`writer-service`** — `POST /internal/notes/move` performs the `git mv`, commits, pushes, and returns `old_note_path`, `new_note_path`, and `git_commit_hash`.
- **`capture-service`** — resolves the correction target from the reply thread or explicit ID. Records `old_note_path`, `new_note_path`, and `git_commit_hash` on the correction event. Correction history is append-only; no prior record is mutated.
- A second correction after a previous move targets the current note path and never creates a duplicate.

### SB-119 — Encrypted off-host backups and restore validation

Added nightly encrypted snapshots of all durable state and a weekly restore validation that never touches live volumes.

- **SQLite backup** — uses `sqlite3 .backup` (WAL-safe) rather than a raw `cp` of the live database file.
- **Vault and n8n data** — EC2 vault clone and n8n data volume included in each snapshot.
- **Encryption** — backup output encrypted before leaving the host. Secrets excluded or redacted from configuration backups.
- **Restore validation** — validates a backup into a temporary directory or container only. Never mounts or touches live volumes.
- **`deploy/backup.sh`** / **`deploy/restore-validate.sh`** — nightly and weekly scripts; both are idempotent and exit non-zero on failure.
- **`secondbrain status`** — reports last successful backup timestamp and last successful restore validation timestamp.

### Pipeline reliability fixes

Several correctness and reliability issues identified during milestone testing were fixed before merge.

- **Receipt condition** — `downstream_processing_enabled` in the Discord receipt now reads `settings.downstream_delivery_enabled` (the env var) instead of `_notify_capture is not None`. In `capture-only` + n8n mode the receipt correctly says "Queued for downstream filing" rather than "Downstream filing is not enabled yet." The `capture_deferred` log only fires when delivery is genuinely disabled.
- **Correction commands excluded from capture** — `fix:` and `fix SB-…:` messages are detected in `_capture_if_allowed` before secret screening and persistence, preventing them from being saved as notes while downstream routing handles them.
- **Classifier retry credentials** — `Schedule Retry (classifier)` node was missing the `PLACEHOLDER_CAPTURE_SERVICE_TOKEN` credential block; added.
- **Credential consistency test hardened** — `test_every_capture_service_node_uses_same_credential` now hard-fails on any capture-service HTTP node that lacks a credential, rather than silently skipping it.
- **Gemini parse robustness** — `Parse Gemini Response` now joins all `parts[].text` entries before parsing, handling split-output responses. Surfaces `parse_error`, `finish_reason`, and `raw_preview` for debugging.
- **Gemini error branch** — `Classify with Gemini` is wrapped in a `Gemini OK?` IF node. HTTP 429/403/5xx and timeouts route to `Schedule Retry (Gemini error)` with `error_type: gemini_http_error` instead of leaking a stale lease.
- **Invalid classification routing** — `Valid Classification?` IF node inserted between `Validate Classification` and `File or Inbox?`. Empty route or `valid: false` routes to `Schedule Retry (classifier)` with `error_type: invalid_classifier_output` rather than silently dropping the execution.
- **Gemini classifier config** — `temperature: 0` (fully deterministic), `maxOutputTokens: 2048`. Prompt hardened: "Return compact JSON only. No explanation. No markdown. Body under 80 words. At most one action."
- **Startup order enforced** — n8n healthcheck now probes `POST /rest/login` (HTTP 400 = REST route exists) instead of `GET /` (only proves static server is up). `capture-service` depends on `local-n8n-init: service_completed_successfully` and `writer-service: service_healthy` so `docker compose up -d` cannot start Discord intake before the n8n webhook is registered. `setup_owner()` in `local-n8n-init.py` now hard-fails on HTTP 404 ("REST not ready") and only accepts 200 (created) or 400 (already configured).
- **Test environment leakage** — `DOWNSTREAM_DELIVERY_ENABLED=false` added to `BASE_ENV` in capture-only unit tests; `GIT_SYNC_ENABLED=false` added to the autouse fixture in writer-service tests. Both prevent leaked environment variables from producing false failures.

---

## v1.0.4 — Milestone 4: Git-backed writer service

**Branch:** `milestone_4`

Replaced the writer-stub placeholder with a full `writer-service` that renders deterministic Markdown notes, writes them to a local vault filesystem (SB-114), adds a kernel-managed advisory `flock` and Git-backed vault sync (SB-115), and adds explicit safe-failure handling for every Git error the write sequence can encounter (SB-116). Also added two one-shot Compose services so `docker compose up -d` works out of the box with no manual setup: `local-vault-init` seeds the Git-backed local vault and fake remote, and `local-n8n-init` creates the n8n owner account, all four credentials, imports both workflows, activates them, and verifies the intake webhook.

### SB-114 — Create writer-service with a local-only vault

Extracted the deterministic Markdown generation logic from the MVP into a standalone `writer-service` FastAPI container that replaces the `writer-stub`. All vault file writes now go through this service; no other container mounts or mutates the vault path.

- **`writer-service/`** — new standalone FastAPI service (UID 10003, port 8001, not published to the host). Contains `main.py`, `config.py`, `api_models.py`, `writer.py`, `vault.py`, and `audit.py`. Preserves all MVP Markdown generation behavior: folder allowlist, title and project-slug sanitization, path traversal protection, deterministic filenames (`YYYY-MM-DD--<capture_id>--<title-slug>.md`), deterministic frontmatter field order, idempotency by `capture_id` frontmatter field, and append-only audit trail at `99_log/events.ndjson`.
- **`GET /health`** — returns HTTP 200 when vault path is writable, HTTP 503 when not. No authentication required.
- **`POST /internal/notes/file`** — requires `X-Second-Brain-Writer-Token`. Validates folder enum, action status, confidence range, `capture_id` format (`^SB-\d{8}-\d{4}$`), `delivery_attempt >= 1`, and path traversal on all derived components. Returns `{result, note_path, git_commit_hash, idempotent}`. When `inbox_reason` is non-null, writes to `00_inbox/` regardless of `classification.folder`.
- **`compose.n8n.yaml`** — `writer-service` service added (replaces `writer-stub`). Backend network only, `cap_drop: ALL`, `no-new-privileges`, vault volume mount.
- **`compose.override.yaml`** — `writer-stub` removed; `writer-service` added with local-safe environment defaults (`GIT_SYNC_ENABLED=false` for SB-114, `second-brain-local-vault` named volume).
- **`deploy/writer-service.env.example`** — environment template with `WRITER_SERVICE_TOKEN`, `VAULT_PATH`, `AUDIT_LOG_PATH`, `LOG_LEVEL`.
- **`n8n/workflows/second-brain-intake.json`** — updated `Second Brain - Intake` workflow. Both file and inbox branches now call `POST http://writer-service:8001/internal/notes/file` then the appropriate `acknowledge-filed` or `acknowledge-inbox` endpoint. Classification routing Code node extended to produce a typed `inbox_reason` slug (`classifier_selected_inbox`, `needs_clarification`, `low_confidence`). `stub://` path references removed.
- **`src/secondbrain/api_models.py`** — `DownstreamCaptureResponse` extended with `source_message_id` and `created_at` fields so n8n can pass them in the filing request.
- **`src/secondbrain/capture_api.py`** — `GET /internal/downstream/captures/:capture_id` response populates the new fields from existing `discord_message_id` and `received_at` columns. No migration required.
- **`deploy/bootstrap-n8n.sh`** — upgrade path: if `Second Brain - Intake` already exists, the script exports its ID, sanitizes the updated fixture, injects the ID, and imports in-place so the workflow is updated without losing the existing credential binding.
- **`deploy/local-stack-up.sh`** — all `writer-stub` references replaced with `writer-service`; post-health vault verification via `docker exec`.
- **`deploy/deploy.sh`** / **`deploy/provision-host.sh`** — extended with `WRITER_SERVICE_ENV_FILE`, `WRITER_VAULT_SOURCE`, vault directory creation, and pre-start validation.
- **`deploy/test-writer-service.sh`** — local regression script: container health, vault mount, `/health` 200, `POST /internal/notes/file` with real payload, note file present in vault, audit event appended, idempotent replay, `acknowledge-filed` count increment, n8n workflow references writer-service.
- **`tests/architecture/test_writer_service_config.py`** — architecture assertions: pinned image, backend-only network, no published ports, vault volume, `cap_drop ALL`, `no-new-privileges`, `writer-stub` absent from `compose.n8n.yaml`.
- **`tests/architecture/test_n8n_intake_workflow.py`** — updated: writer-service URL present, writer-stub URL absent, no `stub://` references, both branches call acknowledge endpoints after writer-service.
- **`writer-service/tests/unit/`** — unit tests for token auth, classification input validation, path traversal rejection, Markdown generation (filename format, frontmatter field order, golden fixture), idempotency, audit log, and health endpoint.
- **`writer-service/tests/integration/test_writer_service_filing.py`** — integration tests for normal filing, idempotent replay, and inbox routing using a temporary vault directory.

### SB-115 — Add GitHub vault sync and serialized Git writes

Added a Git-backed vault to `writer-service`. Every successful note write is now committed and pushed to a private GitHub repository. An OS-level advisory `flock` serializes all writes so concurrent requests never corrupt the repository.

- **`writer-service/src/writerservice/flock.py`** — `WriterLock` context manager using `fcntl.flock(fd, fcntl.LOCK_EX)` on `/opt/vault/.writer.lock`. The kernel releases the lock automatically if the process terminates — no stale application lock possible.
- **`writer-service/src/writerservice/git_ops.py`** — `GitVaultOps` class implementing the full atomic write sequence: acquire flock → `git fetch origin` → `git merge --ff-only origin/main` → idempotency check → write note file → append audit event → `git add` → `git commit` → `git push` → release flock → return `note_path` and `git_commit_hash`.
- **`writer-service/src/writerservice/config.py`** — `GIT_SYNC_ENABLED` feature flag. When `false` (local default), notes are written to the local filesystem only with no Git operations. When `true`, the full Git sequence runs.
- **`writer-service/src/writerservice/writer.py`** — updated to dispatch through `GitVaultOps` when `GIT_SYNC_ENABLED=true`; falls back to direct filesystem write when disabled.
- **`deploy/provision-host.sh`** — extended with vault clone at `/opt/second-brain/vault`, SSH deploy key setup, and Git identity configuration for the writer-service user.
- **`deploy/writer-service.env.example`** — `GIT_SYNC_ENABLED` and `VAULT_REMOTE` added. Branch is always `main`; no separate branch variable.
- **`writer-service/tests/unit/test_flock.py`** — 81 assertions covering lock acquisition, re-entrancy, and automatic kernel release.
- **`writer-service/tests/unit/test_git_ops.py`** — 299 assertions covering the full write sequence, commit message format, push behavior, and flock hold timing.
- **`writer-service/tests/integration/test_git_vault_sync.py`** — end-to-end integration tests: two near-simultaneous notes each produce exactly one committed file; killing writer-service while holding the lock does not leave an immortal application lock.

### SB-116 — Add Git conflict and stale-lock failure handling

Added explicit, typed failure detection for every bad state the Git-backed write sequence can reach. The guiding rule: fail visibly, preserve the raw SQLite capture, never blindly delete Git-internal lock files, never auto-resolve conflicts.

- **`writer-service/src/writerservice/git_errors.py`** — typed error classes for all Git-layer failures: `GitMergeConflictError`, `GitPushRejectedError`, `GitIndexLockedError`, `GitCaptureIdDuplicateError`, `GitPathEscapeError`.
- **`writer-service/src/writerservice/git_ops.py`** — updated to detect and raise typed errors:
  - `git_merge_conflict` — `git merge --ff-only` exits non-zero. Returns HTTP 409. n8n routes to terminal (`acknowledge-failed`); operator must resolve the diverged local clone manually before retrying through capture-service.
  - `git_push_rejected` — `git push` exits non-zero. Local filesystem write rolled back before flock release. Returns HTTP 409. n8n schedules automatic retry; the retry fetches the intervening commit and pushes cleanly.
  - `git_index_locked` — `.git/index.lock` exists at write-sequence start. Returns HTTP 503. Never deleted automatically; operator must verify no live Git process holds it.
  - `capture_id_duplicate` — idempotency scan finds `capture_id` in more than one vault file. Returns HTTP 409.
  - `path_traversal_attempt` — derived path component contains `..`, `/`, absolute segment, null byte, or leading `.`. Returns HTTP 422.
- **`n8n/workflows/second-brain-error-handler.json`** — updated to route `git_merge_conflict`, `git_index_locked`, and `capture_id_duplicate` as terminal; `git_push_rejected` as retryable.
- **`src/secondbrain/downstream_errors.py`** — `RETRYABLE_DOWNSTREAM_ERRORS` and `TERMINAL_DOWNSTREAM_ERRORS` extended with the new Git error types.
- **`deploy/test-writer-safe-failure.sh`** — regression script: injects each Git failure type, verifies the correct HTTP status and `error_type`, confirms raw capture remains in SQLite, confirms vault working tree is in a known state.
- **`deploy/verify.sh`** — extended with writer-service deployment checks: container running, vault mounted, health endpoint, Git sync configuration present.
- **`writer-service/tests/integration/test_git_failure_handling.py`** — 284 assertions covering injected failures for all five error types, verifying HTTP response codes, error body structure, vault state after failure, and SQLite capture preservation.

### Local dev: self-contained vault initialization

Added a `local-vault-init` one-shot service to `compose.override.yaml` so `docker compose up -d` works with Git sync enabled out of the box — no manual `git init`, remote setup, or SSH key configuration required.

- **`compose.override.yaml`** — `local-vault-init` one-shot service (`alpine/git:latest`) initializes both the vault working tree and a bare fake remote in separate named volumes before writer-service starts. `writer-service` now has `depends_on: local-vault-init: condition: service_completed_successfully`. `GIT_SYNC_ENABLED` defaults to `true` locally. `second-brain-local-vault-remote` named volume added so the fake remote persists across container recreates.
- **`tests/architecture/test_writer_service_config.py`** — six architecture assertions added: `local-vault-init` service present, remote volume present, `depends_on` condition, `GIT_SYNC_ENABLED` default, remote mount, and init script content.

### Local dev: self-contained n8n initialization

Added a `local-n8n-init` one-shot service to `compose.override.yaml` so `docker compose up -d` seeds all local n8n state without manual bootstrap steps or any UI interaction.

- **`deploy/local-n8n-init.py`** — Python script that runs once after n8n is healthy and performs seven steps, all idempotent:
  1. Creates the local n8n owner account.
  2. Logs in and stores the session cookie.
  3. Creates four HTTP-header-auth credentials: `Capture Service Token`, `Second Brain - Writer Service Header`, `Intake Webhook Token`, and `Gemini API Key`.
  4. Imports `Second Brain - Error Handler` with credential IDs patched in.
  5. Activates `Second Brain - Error Handler`.
  6. Imports `Second Brain - Intake` with credential IDs and the Error Handler workflow ID patched in.
  7. Activates `Second Brain - Intake` and verifies `POST /webhook/second-brain-intake` responds non-404.
- **`compose.override.yaml`** — `local-n8n-init` service (`python:3.13-alpine`) mounts `n8n/workflows/` read-only and `deploy/local-n8n-init.py`; depends on `n8n: condition: service_healthy`. Requires `CAPTURE_SERVICE_INTERNAL_TOKEN`, `WRITER_SERVICE_TOKEN`, `N8N_INTAKE_WEBHOOK_TOKEN`, and `GEMINI_API_KEY` (hard-required; startup fails if missing). `N8N_LOCAL_EMAIL` and `N8N_LOCAL_PASSWORD` default to local dev values.

---

## v1.0.3 — Local developer workflow and n8n operational hardening

**Branch:** `tech_debt_one`

Resolved tech debt items TD-014 and TD-015 (local Docker lifecycle), fixed three bugs in the SB-113 regression script, and applied several n8n operational hardening improvements.

### TD-014 / TD-015 — Plain Docker lifecycle commands now work without shell exports

- **`compose.yaml`** — replaced `:?` error guards on `CAPTURE_SERVICE_ENV_FILE` and `CAPTURE_DATA_SOURCE` with `:-` defaults (`.env` and `./second-brain-local-data` respectively). Plain `docker compose up/down/logs/ps` now parse without exporting any variables.
- **`compose.override.yaml`** (new) — auto-loaded by Docker Compose when `COMPOSE_FILE` is not set. Provides local-safe defaults for all required variables, includes the full n8n and writer-stub service definitions, and wraps the capture-service entrypoint to create the EBS sentinel marker automatically on first start. Eliminates the manual `docker run` init step.
- **`deploy/local-stack-up.sh`** / **`deploy/local-stack-down.sh`** — simplified to plain `docker compose` calls; no longer export `COMPOSE_FILE` or manage volumes manually.
- **`deploy/deploy.sh`** — unchanged. Continues to set `COMPOSE_FILE=compose.yaml:compose.n8n.yaml` explicitly, which prevents `compose.override.yaml` from being auto-loaded in production.
- **`tests/architecture/test_container_config.py`** — updated assertions to match `:-` defaults.
- TD-015 is fully resolved as a subset of TD-014. Both files marked **Status: Resolved**.

### n8n workflow import fix

- **`deploy/bootstrap-n8n.sh`** / **`deploy/setup-local-n8n.sh`** — n8n 2.25.7 requires a UUID `id` field on import. Both scripts now generate a fresh UUID via `python3 -c 'import uuid; ...'` and inject it after `del(.id, .versionId)`. The fixture files remain id-free; the architecture test still passes because `del(.id, .versionId)` is still present.

### `deploy/test-n8n-error-workflow.sh` bug fixes

Three bugs fixed in the SB-113 local regression script:

- **Missing `-i` flag** — `docker exec` without `-i` does not pass the shell's stdin (the heredoc) into the container. Added to both `docker exec` calls.
- **Cleanup race** — The exit trap previously called the production `report-workflow-error` API with a `terminal` disposition after a prior `retryable` report, which the ledger correctly rejects as `ignored_conflicting_replay`. Replaced with a direct SQL `UPDATE` in the exit trap.
- **Dispatcher race** — The original helper used a two-step insert + claim that the running dispatcher could race. Replaced with a single atomic SQLite transaction that inserts the capture directly in `FORWARDING` state.

Additional script improvements:

- **Timing** — replaced the fixed 5-second sleep with a 30-second poll loop that waits for `RETRY_WAIT` state before proceeding to the idempotency step.
- **Python f-string syntax** — `d[\"key\"]` inside f-string expressions is a compile-time `SyntaxError` on Python ≤ 3.12. All inline assertion messages rewritten to avoid backslashes in f-string expressions.
- **Step 6 robustness** — orphan test now compares `retry_attempts` before and after instead of asserting a specific `delivery_status`, which is more robust against dispatcher reclaims during the test window.

### n8n config cleanup

- **`N8N_RUNNERS_ENABLED`** removed from `deploy/n8n.env.example` (deprecated; n8n warns to remove it). Architecture test `test_env_runners_enabled` renamed to `test_env_runners_not_present` and assertion inverted.
- **Encryption key newline** — all documentation and error messages updated to use `printf '%s' "$(openssl rand -hex 32)"` which writes the key without a trailing newline (n8n rejects keys with leading/trailing whitespace).
- **Tini double-init** — removed `init: true` from the n8n service in `compose.override.yaml` and `compose.n8n.yaml`. The n8n image already runs through Tini; Docker's `init: true` inserted a second PID-1 init layer, causing Tini to log a warning on every startup. `init: true` remains in place for capture-service and writer-stub.

### TD-010 — Idempotent terminal-failure callbacks (documented, already implemented)

TD-010 was already implemented in SB-112 via `_report_workflow_error` in `ledger.py`: same disposition for the same delivery attempt returns `ignored_retry_already_scheduled` / `ignored_already_terminal`; conflicting disposition returns `ignored_conflicting_replay`. TD-010 file updated to **Status: Resolved in SB-112**.

---

## Milestone 3 — Move classification into n8n

### SB-113 — n8n error workflow

Implemented the durable error-reporting path from n8n back to capture-service, closing the retry/failure loop for downstream delivery.

- **`src/secondbrain/downstream_errors.py`** — error taxonomy: `RETRYABLE_DOWNSTREAM_ERRORS`, `TERMINAL_DOWNSTREAM_ERRORS`, `ALLOWED_STAGES`. All values validated as safe slugs — no raw exception text can enter the ledger through this path.
- **`src/secondbrain/capture_models.py`** — `WorkflowErrorOutcome` dataclass with `capture_id`, `delivery_attempt`, `delivery_status`, `retry_attempts`, and `outcome`.
- **`src/secondbrain/api_models.py`** — `ReportWorkflowErrorRequest` (`StrictInternalRequest`, `extra="forbid"`) with cross-field validation: `disposition=retryable` requires a retryable `error_type`; `disposition=terminal` requires a terminal `error_type`. `WorkflowErrorResponse` surfaces the `outcome` string.
- **`src/secondbrain/ledger.py`** — `report_workflow_error()` write job. Idempotency is enforced via a `N8N_WORKFLOW_ERROR_REPORTED` audit event per delivery attempt: a duplicate report returns `ignored_retry_already_scheduled` or `ignored_already_terminal`; a conflicting disposition for the same attempt returns `ignored_conflicting_replay`; a stale attempt returns `ignored_stale_attempt`. Retryable reports call `_schedule_retry` (existing capped backoff); terminal reports call `_mark_delivery_failed_terminally`.
- **`src/secondbrain/capture_service.py`** — `report_workflow_error()` facade method.
- **`src/secondbrain/capture_api.py`** — `POST /internal/captures/{id}/delivery/report-workflow-error`. Returns HTTP 200 for all outcomes including idempotent and ignored cases; n8n never sees a 4xx for a legitimate retry of a valid prior report.
- **`n8n/workflows/second-brain-error-handler.json`** — `Second Brain - Error Handler` updated with the full implementation: HTTP Request node posts to `report-workflow-error` with `X-Second-Brain-Internal-Token` auth, structured payload including `execution_id`, `workflow_id`, `workflow_name`, `stage`, `error_type`, and `reason_type`. Normalizes safe metadata only — no raw error messages, stack traces, or capture text.
- **`n8n/workflows/second-brain-intake.json`** — `errorWorkflow` setting wired to the Error Handler workflow (placeholder resolved at import time via `deploy/setup-local-n8n.sh`).
- **`n8n/workflows/test/second-brain-error-harness.json`** — local-only `Second Brain - Error Harness` workflow. Accepts a test webhook with `test_case` routing, synthetically triggers the Error Handler, and validates the full error path without a real n8n execution failure.
- **`deploy/setup-local-n8n.sh`** — one-time local setup: generates `TEST_HARNESS_TOKEN` into `n8n-test.local.env`, imports the Error Handler, resolves the real workflow ID, patches the Error Harness and Intake `errorWorkflow` field in-place via `n8n export:workflow` + jq + `n8n import:workflow`. Prints exact credential-binding steps for the n8n UI.
- **`deploy/test-n8n-error-workflow.sh`** — self-contained local regression script. Creates a synthetic capture directly in `FORWARDING` state via a single atomic SQLite transaction (no dispatcher race), triggers the Error Harness, verifies RETRY_WAIT state, idempotent replay, raw-text preservation, and orphan behavior. Cleans up via direct SQL on exit. No arguments, no log scraping, no manual token input required.
- **`deploy/bootstrap-n8n-test-fixtures.sh`** — imports the Error Harness fixture into the local n8n instance (idempotent by name).
- **`tests/architecture/test_n8n_error_workflow.py`** — architecture assertions: error handler fixture validity, no execution retention, no localhost URLs, PLACEHOLDER credentials only, correct endpoint path, safe-slug payload fields.
- **`tests/integration/test_n8n_error_workflow.py`** — 37 integration tests covering: auth (missing/wrong/correct token), request validation (disposition × error_type cross-check, unknown stage, extra fields, unsafe slugs), retryable transitions (FORWARDING/CLASSIFYING → RETRY_WAIT, retry count, lease cleared, raw_text preserved, audit event), terminal transitions (DELIVERY_FAILED, lease cleared), retry exhaustion, idempotency (duplicate/stale/already-terminal/conflicting replays), and concurrent calls (at most one retry increment and one audit event per attempt).

---

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
