# Technical Debt Backlog

These are real concerns, but they should not delay the active milestone unless explicitly promoted into a blocking ticket.

This document is for deferred hardening, cleanup, and operational improvements. It should not duplicate work that already has an assigned milestone ticket.

---

# Open Debt

## Sanitize persisted local-worker exception messages

**Status:** Partially resolved  
**Priority:** High before remote processing becomes production-facing

SB-107 now validates downstream delivery error categories inside the durable ledger layer using a restricted safe-slug format. Downstream callbacks cannot persist arbitrary exception bodies, internal URLs, tokens, or oversized response payloads through the supported service path.

The original local classifier worker still persists raw exception text for:

```text
vault write failures
unexpected worker failures
```

Examples currently include:

```python
failure_reason = (
    f"vault write failed: {type(exc).__name__}: {exc}"
)
```

and:

```python
failure_reason = (
    f"worker error: {type(exc).__name__}: {exc}"
)
```

Replace these with a shared sanitizer before allowing remote services or third-party libraries to contribute exception text to persisted `last_error` fields.

Persist only:

```text
safe error category
exception class
bounded sanitized summary when genuinely necessary
```

Do not persist:

```text
tokens
credentials
internal URLs
raw HTTP response bodies
raw note text
unbounded exception messages
```

---

## Make shutdown cleanup resilient to cleanup errors

**Status:** Open  
**Priority:** Medium before long-running EC2 operation

The shutdown order is correct, but cleanup steps are not isolated.

The runtime currently performs steps such as:

```text
stop API server
close Discord client
cancel periodic reconciliation
cancel local worker
cancel runtime tasks
close SQLite-backed CaptureService
```

If an earlier cleanup step raises unexpectedly, later cleanup may be skipped.

Wrap cleanup operations independently so one failure cannot prevent later teardown.

Log only safe metadata:

```text
cleanup step
error type
whether remaining cleanup continued
```

Do not suppress failures silently, but do not allow one cleanup exception to strand SQLite or leave an orphaned task.

---

# SQLite Runtime Follow-Up Hardening

These concerns do not block the completed Milestone 2 feature slice. Revisit them before unattended EC2 operation, automated backups, or substantially higher traffic. The current serialized SQLite runtime is appropriate for the expected capture volume, but these should be revisited before substantially increasing traffic or relying on automated backups.

## Prevent SQLite contention from blocking the asyncio event loop

**Status:** Open  
**Priority:** Medium after real EC2 measurements exist

`Ledger` intentionally exposes synchronous methods backed by the serialized SQLite worker. This is acceptable while database operations remain short.

During prolonged lock contention or SQLite queue pressure, a synchronous ledger call can still block the Discord event-loop thread while waiting for:

```text
queue capacity
SQLite busy timeout
bounded retry backoff
worker completion
```

This could delay:

```text
Discord heartbeat handling
Gateway event handling
internal API responsiveness
graceful shutdown
```

Add instrumentation for:

```text
SQLite job queue depth
queue wait duration
job execution duration
retry count
busy exhaustion count
```

If contention becomes observable in production, move blocking ledger submissions behind an async adapter such as:

```python
asyncio.to_thread(...)
```

or a dedicated async-facing service boundary.

Do not introduce an async database dependency unless measurements prove it is needed.

---

## Add bounded SQLite runtime startup and shutdown watchdogs

**Status:** Open  
**Priority:** Medium before unattended production use

`SQLiteRuntime` currently waits indefinitely for:

```text
worker startup event
bounded-queue insertion
future result delivery
worker-thread join during shutdown
```

Under normal operation this is safe because jobs are short and serialized.

If a migration, filesystem issue, or unexpected SQLite call hangs, startup or shutdown can also hang indefinitely.

Add configurable watchdog timeouts and metadata-only diagnostics for:

```text
SQLite runtime startup timeout
SQLite queue wait timeout
SQLite job completion timeout
SQLite shutdown drain timeout
worker thread unexpectedly not alive
```

A timed-out accepted capture must fail visibly.

Never silently discard queued work.

---

## Validate adopted legacy schemas before recording migration adoption

**Status:** Open  
**Priority:** Medium before schema migrations become more complex

Migration `001_initial_mvp_schema` intentionally uses:

```sql
CREATE TABLE IF NOT EXISTS
CREATE INDEX IF NOT EXISTS
```

so the pre-migration MVP database can be adopted without data loss.

That is correct for the known MVP schema. It does not prove that an existing database has the expected:

```text
columns
indexes
foreign keys
constraints
sensitive-text protections
```

A manually modified, partially restored, or corrupted database could therefore be marked as migrated while retaining schema drift.

Before future migrations become more complex, add an adoption validator using:

```text
PRAGMA table_info(...)
PRAGMA index_list(...)
PRAGMA index_info(...)
PRAGMA foreign_key_list(...)
sqlite_master schema inspection where needed
```

Fail startup clearly when the existing schema is incompatible.

Do not attempt silent schema repair.

---

## Define a WAL-aware backup and restore procedure

**Status:** Open  
**Priority:** High before backups are treated as trustworthy

SB-105 enables WAL mode.

A naive filesystem copy of only:

```text
ledger.sqlite3
```

may not include committed pages still present in:

```text
ledger.sqlite3-wal
```

Before adding automated backups, define and test a WAL-safe approach such as:

```text
SQLite backup API
controlled checkpoint followed by snapshot
EBS snapshot procedure validated against a restored instance
```

The procedure must include a restore test.

A backup that has never been restored is not trusted.

Also monitor WAL-file growth before adding manual checkpoint tuning.

Keep SQLite defaults unless measurements show a real operational problem.

---

## Add defensive validation inside SQLiteRuntime

**Status:** Open  
**Priority:** Low

Production configuration validates values such as:

```text
SQLITE_BUSY_TIMEOUT_MS >= 0
SQLITE_BUSY_RETRY_ATTEMPTS >= 1
SQLITE_BUSY_RETRY_BASE_DELAY_MS >= 0
SQLITE_JOB_QUEUE_MAXSIZE >= 1
```

`SQLiteRuntime` can also be constructed directly by tests or future internal tooling.

Add equivalent constructor-level validation so invalid direct callers fail with a clear error rather than producing confusing retry or queue behavior.

---

## Centralize structured application logging later

**Status:** Open  
**Priority:** Low until logs are shipped or queried operationally

SQLite runtime events and delivery events use metadata-only JSON logs, which is correct.

The project still writes structured events directly to stdout through a small helper. As the service grows to include reconciliation, delivery leases, reapers, and downstream callbacks, consider consolidating:

```text
log levels
event naming
correlation fields
capture ID handling
delivery-attempt fields
operation names
exception sanitization
output destination
```

Do not add a logging framework merely for abstraction.

Revisit this when logs are being shipped, indexed, or queried operationally.

---

# Delivery Callback Follow-Up Hardening

These concerns do not block the completed Milestone 2 feature slice. Revisit them before relying on n8n and writer-service as the primary production processing path. The current implementation preserves durable state correctly, but these should be revisited before relying on an external n8n workflow as the primary production processing path.

## Add durable receipt-repair tracking

**Status:** Open  
**Priority:** Medium before Discord receipts are treated as operator-grade alerts

Terminal capture state is committed before Discord receipt delivery, which is correct.

When a Discord receipt edit and its fallback replacement both fail, the system logs safe metadata and preserves the durable capture state.

A later identical `FILED` or `INBOX` callback can repair the receipt because idempotent terminal replay attempts receipt delivery again.

There is still no persisted marker or repair queue for a receipt that remains out of sync after all delivery attempts fail.

Add a durable repair mechanism later, such as:

```text
receipt_sync_status
receipt_sync_last_attempt_at
receipt_sync_last_error_type
```

or a bounded repair queue derived from capture events.

The repair path must remain best-effort.

Never roll back a successfully persisted capture transition merely because Discord is unavailable.

---

## Make explicit terminal-failure callbacks idempotent

**Status:** Open  
**Priority:** Low before at-least-once callback delivery is enabled

Repeated `FILED` and `INBOX` callbacks with identical payloads are handled as idempotent replays.

A repeated explicit downstream terminal-failure callback currently reaches a row already marked:

```text
delivery_status = FAILED
```

and is treated as an invalid-state callback because terminal failure is accepted only from active delivery states.

Before relying on at-least-once n8n callback delivery, consider storing the terminal failure reason category and treating:

```text
same capture ID
same delivery attempt
same safe reason category
```

as:

```text
idempotent_replay
```

Treat a different failure reason category as:

```text
conflicting_replay
```

The current behavior is safe because state cannot regress, but duplicate terminal-failure callbacks are noisier than necessary.

---

# Resolved Debt and Closed Findings

These were real implementation issues, but they have been resolved. Keep this section as an audit trail rather than reopening them as active debt.

## Downstream error categories are validated before persistence

**Status:** Resolved in SB-107

Downstream retry and failure categories are constrained to a safe-slug format before persistence.

Unsafe values such as free-form exception bodies, internal URLs, or token-bearing strings are rejected.

Local-worker exception sanitization remains open separately.

---

## Downstream INBOX reason categories are validated durably

**Status:** Resolved in SB-107

`Ledger.mark_inbox()` validates non-empty downstream `reason_type` values before writing them to SQLite.

This prevents future internal callers from bypassing HTTP-layer validation.

---

## Dispatcher no longer treats mutation-result objects as Boolean values

**Status:** Resolved in SB-107

The dispatcher explicitly checks:

```text
result.changed
result.outcome
```

rather than relying on object truthiness.

A stale downstream acceptance can no longer be logged incorrectly as a successful forward.

---

## Legacy HTTP retry endpoint was removed

**Status:** Resolved in SB-107

The old HTTP retry route could reset note lifecycle state without resetting delivery state, producing stranded rows.

The route was removed.

The correct manual-retry operation belongs to SB-108.

---

## Local-full startup normalizes downstream in-flight states

**Status:** Resolved in SB-107

When local-full mode starts, non-terminal downstream states are normalized to:

```text
NOT_APPLICABLE
```

and stale retry or lease metadata is cleared.

This prevents locally processed captures from remaining eligible for downstream forwarding.

---

## Stale-lease reaper ownership moved fully into SB-108

**Status:** Resolved as a scope correction

The stale-processing-lease reaper is not part of SB-107.

SB-108 owns:

```text
retry_attempts
bounded stale-lease claiming
transactional retry scheduling
manual retry
reaper receipts
reaper status fields
self-scheduling reaper loop
```

Reaper-related configuration values may be parsed early for deployment-template stability, but they remain inactive until SB-108.

---

## Add a configuration preflight command

**Status:** Open  
**Priority:** Medium before EC2 deployment

The runtime intentionally fails closed when required configuration is missing.

As the service evolved, new required values were added:

```text
CAPTURE_SERVICE_INTERNAL_TOKEN
CAPTURE_API_HOST
CAPTURE_API_PORT
```

Existing `.env` files do not automatically inherit additions from `.env.example`. This can cause a previously working local environment to fail after pulling a new milestone.

Add a command such as:

```bash
uv run secondbrain config-check
```

The command should:

```text
report missing required variables
identify variables that were renamed
distinguish local-full from capture-only requirements
validate token minimum length
validate numeric ranges
report which env template should be used
never print secret values
```

Keep startup fail-closed behavior. The preflight command is an operator convenience, not a replacement for runtime validation.

---

## Monitor background-task liveness independently

**Status:** Open  
**Priority:** Medium before unattended EC2 operation

The service now runs several important background tasks:

```text
capture-service heartbeat
periodic Discord reconciliation
stale-processing-lease reaper
local classifier worker in local-full mode
future downstream delivery dispatcher
```

Ordinary pass-level failures are contained safely. A task cancelled unexpectedly by future code may still remain dead until a reconnect or service restart.

Add lightweight task-liveness reporting later:

```text
task name
running / completed unexpectedly
last successful pass timestamp
last safe error type
```

Expose the result through the operational status command.

Do not add a complex supervisor framework unless real operational evidence justifies it.

---

## Persist dedicated last-successful-vault-write metadata

**Status:** Open  
**Priority:** Low

The operational status report currently infers the last successful vault write by selecting the most recently updated `FILED` or `INBOX` row.

A later metadata or receipt update can make an older note appear to be the most recent vault write.

Persist dedicated system-state fields during the actual successful write operation:

```text
last_successful_vault_write_at
last_successful_vault_write_path
```

The status command should read those fields directly rather than infer the value from mutable capture-row timestamps.