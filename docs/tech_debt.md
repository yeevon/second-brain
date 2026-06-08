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
