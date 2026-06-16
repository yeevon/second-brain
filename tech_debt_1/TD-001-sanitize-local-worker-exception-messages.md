# TD-001: Sanitize persisted local-worker exception messages

**Status:** Partially resolved (downstream path resolved in SB-107; local-worker path open)
**Priority:** High — required before remote services or third-party libraries contribute exception text

## Problem

The local classifier worker still persists raw exception text in two locations:

```python
failure_reason = f"vault write failed: {type(exc).__name__}: {exc}"
failure_reason = f"worker error: {type(exc).__name__}: {exc}"
```

These persist arbitrary exception messages — including potential tokens, internal URLs, raw HTTP response bodies, or unbounded stack content — into the `last_error` field in SQLite.

SB-107 validated downstream delivery error categories via a safe-slug format, but the local worker was explicitly excluded from that fix.

## Acceptance criteria

- Introduce a shared sanitizer function used by all local-worker failure paths.
- Sanitizer emits only: safe error category, exception class name, and an optional bounded summary (max ~200 chars) with no credential-bearing content.
- Sanitizer never persists: tokens, credentials, internal URLs, raw HTTP response bodies, raw note text, or unbounded exception messages.
- All existing local-worker failure paths use the sanitizer; no raw `str(exc)` in persisted fields.
- Sanitizer has unit tests covering vault failures, unexpected errors, and inputs that contain URLs or long strings.

## Do not

- Do not change the downstream delivery error validation added in SB-107.
- Do not build a complex sanitization framework; a single bounded function is enough.
