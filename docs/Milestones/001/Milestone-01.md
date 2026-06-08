# Milestone 1: Make the current MVP deployable without changing behavior

This gets the working capture loop onto EC2 before introducing n8n or Git complexity.

---

## SB-101 — Add regression tests for the proven local slice

**Branch:** ```feature/mvp-regression-suite```

Capture the current behavior as a safety net before extraction.

### Test

- Normal message creates exactly one SQLite capture and one Markdown note.
- Duplicate Discord event does not create a duplicate.
- Bot receipt is ignored.
- Secret-like input stores only a redacted rejection.
- Gemini failure routes to 00_inbox/.
- Vault-write failure leaves the raw capture recoverable.
- Startup catch-up recovers a missed message once.
- Receipt editing failure sends one replacement receipt.

**Done when:** you can refactor internals without manually retesting the entire loop after every change.

---

## SB-102 — Introduce a capture-service boundary inside the existing repo

**Branch:** ```feature/capture-service-boundary```

Do not split the repository or deploy a second process yet. Refactor the existing monolith so Discord intake, SQLite ownership, receipts, and reconciliation sit behind a clean service interface.

Move responsibility for these into the capture boundary:

- Discord Gateway listener.
- User, guild, and channel allowlists.
- Secret screening before persistence.
- SQLite mutations.
- Immediate saved receipts.
- Receipt edits.
- Startup reconciliation.
- Capture status queries.

The production architecture requires ```capture-service``` to remain the sole SQLite owner. n8n and ```writer-service``` must eventually interact with it through an authenticated API rather than touching the database file.

**Done when:** the local app still behaves identically, but downstream processing no longer reaches directly into the ledger repository.

---

## SB-103 — Add the internal capture-service API

**Branch:** ```feature/capture-service-api```

Add a small internal HTTP API around the boundary created in SB-102.

Implement the useful first subset:

```init
GET  /health
GET  /internal/captures/:capture_id
POST /internal/captures/:capture_id/mark-forwarded
POST /internal/captures/:capture_id/mark-classifying
POST /internal/captures/:capture_id/mark-filed
POST /internal/captures/:capture_id/mark-inbox
POST /internal/captures/:capture_id/mark-failed
POST /internal/captures/:capture_id/retry
POST /internal/receipts/:capture_id/edit
```

Require a shared-secret header on every state-changing route. The architecture explicitly calls for internal authenticated endpoints and private webhooks.

**Done when:** a fake downstream client can fetch a capture, transition it through processing states, and edit its receipt without direct SQLite access.

---

## SB-104 — Dockerize and deploy capture-service to EC2

**Branch:** ```feature/ec2-capture-deployment```

Deploy only the durable intake service first. Keep classification and vault writing stubbed or local during initial validation.

### Add

- Dockerfile
- docker-compose.yml
- Persistent SQLite volume
- .env.example
- Restart policy
- SSH-key-only EC2 access where practical
- Restricted security group
- Health check

Do not publish the internal service port publicly. The architecture requires private Compose-network communication without host-port exposure, while preserving outbound access for Discord and later Git operations.

**Done when:** you can shut down your desktop, post a thought from your phone, and see an immediate durable-capture receipt from the EC2 service.

That is the first meaningful post-MVP win.

---
