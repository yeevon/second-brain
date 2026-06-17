# TD-009: Add durable receipt-repair tracking

**Status:** Open
**Priority:** Medium — required before Discord receipts are treated as operator-grade alerts

## Problem

Terminal capture state is committed before Discord receipt delivery (correct). When a Discord receipt edit and its fallback replacement both fail, the system logs safe metadata and preserves the durable capture state — but there is no persisted marker indicating the receipt is out of sync.

A later idempotent terminal replay attempts receipt delivery again, which can repair the receipt. However, if all retries fail and the capture reaches `DELIVERY_FAILED` or `FILED` permanently, the receipt may remain visibly broken in Discord with no way for the operator to know.

## Acceptance criteria

- Add a durable repair marker to the `captures` table (or a companion `receipt_repairs` table), tracking:
  - `receipt_sync_status` (in_sync | sync_failed | repair_pending)
  - `receipt_sync_last_attempt_at`
  - `receipt_sync_last_error_type`
- Populate the marker when a Discord receipt delivery fails after all fallbacks are exhausted.
- Include `receipt_sync_status` in the operational status output so broken receipts are visible to the operator.
- The repair path remains best-effort — a broken receipt never causes rollback of a committed capture transition.

## Do not

- Do not roll back a successfully committed capture transition because Discord is unavailable.
- Do not build an automatic repair daemon — best-effort manual repair is sufficient for now.
