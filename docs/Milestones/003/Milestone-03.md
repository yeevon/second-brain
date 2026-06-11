# Milestone 3: Move classification into n8n

At this point, EC2 capture should remain safe even if n8n is completely offline.

---

## SB-111 — Deploy a secured n8n instance

**Branch:** ```feature/n8n-foundation```

Add n8n to Compose with:

- Persistent data volume.
- N8N_ENCRYPTION_KEY.
- UI access through a deliberate private-access layer or HTTPS.
- No direct public exposure of port 5678.
- Private backend network connection to capture-service.
- Development-only concurrency limit of one while the workflows are simple.

The security requirements specifically call out persistent n8n storage, secure cookie handling, reverse-proxy settings where relevant, and an Error Trigger workflow.

**Done when:** restarting the stack does not destroy n8n credentials or workflows.

---

## SB-112 — Add the n8n intake workflow with a stub writer

**Branch:** ```feature/n8n-intake-workflow```

Build this workflow:

```init
authenticated private webhook
    ↓
fetch capture from capture-service
    ↓
defense-in-depth secret scan
    ↓
mark CLASSIFYING
    ↓
call Gemini
    ↓
validate schema
    ↓
confidence gate
    ↓
submit normalized filing request to stub writer
    ↓
mark FILED or INBOX
    ↓
edit Discord receipt
```

The canonical architecture deliberately places n8n after the durable capture boundary. n8n coordinates processing but does not own intake durability or SQLite.

**Done when:** posting while n8n is stopped still produces an immediate saved receipt, and starting n8n later processes the queued item through at-least-once delivery while producing one observable terminal result.

---

## SB-113 — Add the n8n error workflow

**Branch:** ```feature/n8n-error-workflow```

Use the n8n Error Trigger to:

- Record workflow and execution identifiers.
- Include the related capture_id.
- Mark the capture failed or retryable through capture-service.
- Send a visible receipt update.
- Preserve the raw capture.

**Done when:** a forced Gemini timeout or invalid payload produces a visible failure or retry receipt instead of silent disappearance when safe correlation exists; when correlation is unavailable the system fails closed and relies on eventual stale-lease recovery without guessing.
