# TD-008: Centralize structured application logging

**Status:** Open
**Priority:** Low — defer until logs are shipped or queried operationally

## Problem

Structured log events are written directly to stdout via a small in-project helper. As the service grows (reconciliation, delivery leases, reapers, downstream callbacks), each subsystem adds its own log patterns. Without centralized field conventions, logs become hard to query: capture IDs appear under different field names, operation names are inconsistent, and exception handling varies.

Currently needs consolidation:

```
log levels (DEBUG vs INFO for similar events)
event naming conventions
correlation fields (capture_id, delivery_attempt)
exception sanitization (some paths log raw messages)
output destination (all should go to stdout for container log collection)
```

## Acceptance criteria

- Define a logging convention document (or inline docstring) specifying:
  - Mandatory fields for capture-related events: `capture_id`, `delivery_attempt`, `event`
  - Required log levels by event type
  - Exception handling rules: log `error_type` only, never `str(exc)`
- Refactor existing log calls to match the convention.
- All log output goes to stdout (no file logging, no stderr for structured events).

## Do not

- Do not add a logging framework (structlog, loguru, etc.) unless the team explicitly decides to ship logs to an external system.
- Do not refactor until logs are actually being queried — do this only when friction is observable.
