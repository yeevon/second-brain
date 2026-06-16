# TD-002: Make shutdown cleanup resilient to cleanup errors

**Status:** Open
**Priority:** Medium — required before long-running unattended EC2 operation

## Problem

The shutdown sequence is correctly ordered, but cleanup steps are not isolated. If any earlier step raises unexpectedly, subsequent steps are skipped. This can leave:

- SQLite runtime open (ledger rows not flushed)
- Orphaned asyncio tasks
- Discord client not closed (connection not terminated cleanly)

Current shutdown order:
```
stop API server
close Discord client
cancel periodic reconciliation
cancel local worker
cancel runtime tasks
close SQLite-backed CaptureService
```

A failure in step 2 would skip steps 3–6.

## Acceptance criteria

- Each shutdown step runs inside its own try/except so one failure cannot skip later steps.
- Failures are logged with: cleanup step name, error type (no message body), and whether cleanup continued.
- The final SQLite close always runs, even if earlier steps fail.
- No cleanup exception is silently swallowed — all exceptions are logged.
- Existing shutdown tests continue to pass.

## Do not

- Do not suppress failure details entirely — log the error type.
- Do not allow raw exception messages in the shutdown log.
- Do not introduce a complex cleanup framework; a try/except per step is sufficient.
