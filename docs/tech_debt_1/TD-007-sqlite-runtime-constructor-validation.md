# TD-007: Add defensive validation inside SQLiteRuntime constructor

**Status:** Open
**Priority:** Low

## Problem

Production configuration validates runtime parameters:

```
SQLITE_BUSY_TIMEOUT_MS >= 0
SQLITE_BUSY_RETRY_ATTEMPTS >= 1
SQLITE_BUSY_RETRY_BASE_DELAY_MS >= 0
SQLITE_JOB_QUEUE_MAXSIZE >= 1
```

`SQLiteRuntime` can also be constructed directly by tests or future internal tooling. If an invalid value is passed directly (bypassing the config layer), the behavior is confusing: retry loops, queue deadlocks, or silent hangs rather than a clear error.

## Acceptance criteria

- `SQLiteRuntime.__init__` validates all parameters and raises `ValueError` with a clear message for each invalid value.
- Validation mirrors the production config checks (`>= 0`, `>= 1`, etc.).
- Tests that construct `SQLiteRuntime` directly with invalid parameters get a clear `ValueError` immediately.
- Existing tests and production code are unaffected (all valid callers pass valid values).

## Do not

- Do not duplicate the config-layer validation into `SQLiteRuntime` — call the same shared validation logic.
- Do not change the runtime behavior for valid inputs.
