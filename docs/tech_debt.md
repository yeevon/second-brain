Track as later hardening debt

These are real concerns, but they should not delay SB-104.

Sanitize persisted exception messages

The worker still persists raw exception text in failure reasons:

failure_reason = (
    f"vault write failed: {type(exc).__name__}: {exc}"
)

and:

failure_reason = (
    f"worker error: {type(exc).__name__}: {exc}"
)

The classifier path already redacts the Gemini API key from exception text.

Before n8n and writer-service begin returning remote error bodies, add a shared error-sanitization helper so stored last_error values cannot accidentally contain tokens, credentials, or oversized response bodies.

Make shutdown cleanup resilient to cleanup errors

The shutdown order is now correct. Later, consider protecting cleanup steps individually so an unexpected client.close() exception cannot prevent worker cancellation or ledger closure.

That is production hardening, not an SB-103 blocker.

----------------------

