# Track as later hardening debt

These are real concerns, but they should not delay **SB-104**.

Sanitize persisted exception messages

The worker still persists raw exception text in failure reasons:

```init
failure_reason = (
    f"vault write failed: {type(exc).__name__}: {exc}"
)
```

and:

```init
failure_reason = (
    f"worker error: {type(exc).__name__}: {exc}"
)
```

The classifier path already redacts the Gemini API key from exception text.

Before **n8n** and ```writer-service``` begin returning remote error bodies, add a shared ```error-sanitization``` helper so stored last_error values cannot accidentally contain tokens, credentials, or oversized response bodies.

Make shutdown cleanup resilient to cleanup errors

The shutdown order is now correct. Later, consider protecting cleanup steps individually so an unexpected ```client.close()``` exception cannot prevent worker cancellation or ledger closure.

That is production hardening, not an **SB-103** blocker.

---

## SQLite runtime follow-up hardening

These concerns do not block SB-105. The current runtime is appropriate for the expected capture volume, but these should be revisited before substantially increasing traffic or relying on automated backups.

### Prevent SQLite contention from blocking the asyncio event loop

`Ledger` intentionally exposes synchronous methods backed by the serialized SQLite worker. That is acceptable while database operations remain short.

During prolonged lock contention or SQLite queue pressure, a synchronous ledger call can still block the Discord event-loop thread while waiting for:

```text
queue capacity
SQLite busy timeout
bounded retry backoff
worker completion
```

This could delay:

Discord heartbeat handling
Gateway event handling
internal API responsiveness
graceful shutdown

Add instrumentation for queue depth and wait duration. If contention becomes observable in production, move blocking ledger submissions behind an async adapter such as asyncio.to_thread() or a dedicated async-facing service boundary.

Do not introduce async database dependencies unless measurement proves they are needed.

Add bounded SQLite runtime startup and shutdown watchdogs

SQLiteRuntime currently waits indefinitely for:

worker startup event
worker thread join during shutdown
queue insertion when the bounded queue is full

Under normal operation this is safe because jobs are short and serialized. If a migration, filesystem issue, or unexpected SQLite call hangs, startup or shutdown could also hang indefinitely.

Add configurable watchdog timeouts and metadata-only diagnostics for:

SQLite runtime startup timeout
SQLite queue wait timeout
SQLite shutdown drain timeout
worker thread unexpectedly not alive

A timed-out accepted capture must fail visibly. Never silently discard queued work.

Validate adopted legacy schemas before recording migration adoption

Migration 001_initial_mvp_schema intentionally uses:

CREATE TABLE IF NOT EXISTS
CREATE INDEX IF NOT EXISTS

so the pre-migration MVP database can be adopted without data loss.

That is correct for the known MVP schema. It does not prove that an existing database has the expected columns, indexes, foreign keys, or sensitive-text constraint before version 1 is recorded.

A manually modified, partially restored, or corrupted database could therefore be marked as migrated while retaining schema drift.

Before future schema migrations become more complex, add an adoption validator using:

PRAGMA table_info(...)
PRAGMA index_list(...)
PRAGMA index_info(...)
PRAGMA foreign_key_list(...)
sqlite_master schema inspection where needed

Fail startup clearly when the existing schema is incompatible. Do not attempt silent repair.

Define a WAL-aware backup and restore procedure

SB-105 enables WAL mode.

A naive filesystem copy of only:

ledger.sqlite3

may not include committed pages still present in:

ledger.sqlite3-wal

Before adding automated backups, define and test a WAL-safe approach such as:

SQLite backup API
or
controlled checkpoint followed by snapshot
or
EBS snapshot procedure validated against a restored instance

The backup procedure must include a restore test. A backup that has never been restored is not trusted.

Also monitor WAL-file growth before adding manual checkpoint tuning. Keep SQLite defaults unless measurements show a real operational problem.

Add defensive validation inside SQLiteRuntime

Production configuration validates:

SQLITE_BUSY_TIMEOUT_MS >= 0
SQLITE_BUSY_RETRY_ATTEMPTS >= 1
SQLITE_BUSY_RETRY_BASE_DELAY_MS >= 0
SQLITE_JOB_QUEUE_MAXSIZE >= 1

SQLiteRuntime can also be constructed directly by tests or future internal tooling.

Add equivalent constructor-level validation so invalid direct callers fail with a clear error rather than producing confusing retry or queue behavior.

Centralize structured application logging later

SQLite runtime events now use metadata-only JSON logs, which is correct.

The project still writes structured events directly to stdout through a small helper. As the service grows to include reconciliation, delivery leases, reapers, and downstream callbacks, consider consolidating:

log levels
event naming
correlation fields
capture_id handling
operation names
exception sanitization
output destination

Do not add a logging framework merely for abstraction. Revisit this when logs are being shipped or queried operationally.


## Priority order

The first four are the useful additions:

1. WAL-aware backups and restore validation.
2. Event-loop blocking and SQLite queue-pressure instrumentation.
3. Startup and shutdown watchdogs.
4. Legacy-schema adoption validation.

The constructor validation and logging consolidation are lower priority cleanup.

Your existing file should also be updated to remove the stale wording:

```text
These are real concerns, but they should not delay SB-104.
```

Replace it with:

These are real concerns, but they should not delay the active milestone unless explicitly promoted into a blocking ticket.

The two existing entries—exception sanitization and cleanup resilience—still belong in the file.
