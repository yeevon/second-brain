# TD-003: Prevent SQLite contention from blocking the asyncio event loop

**Status:** Open
**Priority:** Medium — revisit after real EC2 load measurements exist

## Problem

`Ledger` exposes synchronous methods backed by the serialized SQLite worker thread. Under prolonged lock contention or queue pressure, a synchronous ledger call blocks the calling thread while waiting for:

- Queue capacity (bounded FIFO)
- SQLite busy timeout
- Bounded retry backoff
- Worker thread completion

If Discord event handling or API request handling runs on the same thread as a blocking ledger call, the entire asyncio loop stalls. This delays:

- Discord heartbeat delivery (risks gateway disconnect)
- Internal API responsiveness
- Graceful shutdown

## Acceptance criteria

Phase 1 (instrumentation — do this first):
- Add structured log events for: SQLite job queue depth, queue wait duration, job execution duration, retry count, busy exhaustion count.
- All fields are numeric metadata; no exception bodies in logs.

Phase 2 (async adapter — only if measurements justify it):
- Move blocking ledger calls to `asyncio.to_thread(...)` or a dedicated async boundary.
- Do not introduce an async database library (e.g., aiosqlite) unless phase 1 data shows a real problem.

## Do not

- Do not implement phase 2 speculatively.
- Do not change the serialized SQLite worker design without evidence of contention.
- Do not log exception messages — only error types and numeric metadata.
