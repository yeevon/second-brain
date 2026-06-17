# TD-010: Make explicit terminal-failure callbacks idempotent

**Status:** Resolved in SB-112
**Priority:** Low — required before at-least-once n8n callback delivery is enabled

## Problem

Repeated `FILED` and `INBOX` callbacks with identical payloads are already handled as idempotent replays. However, a repeated explicit downstream terminal-failure callback currently reaches a row already marked `delivery_status = FAILED` and is rejected as an invalid-state callback (terminal failure is accepted only from active delivery states).

When n8n switches to at-least-once delivery, duplicate terminal-failure callbacks are a normal operational event, not an error. The current behavior logs them as unexpected state, which is noisy and misleading.

## Acceptance criteria

- When a terminal-failure callback arrives for a capture that is already `DELIVERY_FAILED`:
  - If the incoming `reason_category` matches the stored value: return `idempotent_replay`
  - If the incoming `reason_category` differs: return `conflicting_replay`
- Both outcomes are logged with safe metadata only (no exception bodies).
- No state transition occurs in either case — the outcome is informational only.
- New test cases cover: same-reason replay → `idempotent_replay`, different-reason → `conflicting_replay`.

## Do not

- Do not allow state to regress (DELIVERY_FAILED → retry) under any replay scenario.
- Do not implement this before at-least-once delivery is enabled — premature.
