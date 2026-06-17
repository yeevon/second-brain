# TD-004: Add bounded SQLite runtime startup and shutdown watchdogs

**Status:** Open
**Priority:** Medium — required before unattended production use

## Problem

`SQLiteRuntime` currently waits indefinitely for:

- Worker startup event
- Bounded-queue insertion
- Future result delivery
- Worker-thread join during shutdown

Under normal operation this is safe. If a migration, filesystem issue, or unexpected SQLite call hangs, startup or shutdown also hangs indefinitely with no timeout or diagnostic output.

## Acceptance criteria

- Add configurable timeouts for:
  - SQLite runtime startup (`SQLITE_STARTUP_TIMEOUT_S`)
  - SQLite queue wait (`SQLITE_QUEUE_WAIT_TIMEOUT_S`)
  - SQLite job completion (`SQLITE_JOB_COMPLETION_TIMEOUT_S`)
  - SQLite shutdown drain (`SQLITE_SHUTDOWN_DRAIN_TIMEOUT_S`)
- On timeout, log structured metadata: step name, timeout value, elapsed time. No exception bodies.
- A timed-out accepted capture must fail visibly (raise, not silently discard).
- Silently discarding queued work is not allowed.
- A hung worker thread detected during shutdown is logged and the main thread exits anyway.

## Do not

- Do not change the serialized write-job design.
- Do not allow silent discard of queued work on timeout.
- Do not use default Python thread-join behavior for shutdown drain — use a timed join.
