# TD-012: Monitor background-task liveness independently

**Status:** Open
**Priority:** Medium — required before unattended EC2 operation

## Problem

The service runs several important background tasks:

```
capture-service heartbeat
periodic Discord reconciliation
stale-processing-lease reaper
local classifier worker (local-full mode)
downstream delivery dispatcher
```

Ordinary pass-level failures are contained. A task cancelled unexpectedly by future code may remain dead until a reconnect or service restart, with no visible indicator.

The current operational status command (`uv run secondbrain status`) does not report background task state.

## Acceptance criteria

- Each background task records a last-heartbeat timestamp on every successful pass.
- A task that has not pulsed within a configurable window is marked `degraded` in the status output.
- Status output includes for each task:
  - `task_name`
  - `running` | `completed_unexpectedly` | `degraded`
  - `last_successful_pass_at` (timestamp)
  - `last_safe_error_type` (safe slug, no message body)
- `uv run secondbrain status` exposes this per-task liveness data.
- Liveness data is safe metadata only; no exception messages in output.

## Do not

- Do not add a complex supervisor framework (supervisord, systemd within container).
- Do not restart crashed tasks automatically — log and report; let the operator restart.
