# Milestone 2: Harden durable intake before adding orchestration

Your local MVP already has startup recovery. The EC2 version needs stronger long-running failure handling.

---

## SB-105 — Upgrade the SQLite runtime for service operation

**Branch:** ```feature/sqlite-service-hardening```

### Add

- WAL mode.
- foreign_keys = ON.
- busy_timeout.
- One dedicated database worker or serialized mutation queue.
- Bounded retries for transient SQLITE_BUSY.
- Schema migrations.
- SQLite version startup check.

Keep transactions short. Never hold a write transaction open while calling Discord, n8n, Gemini, or another service. The architecture treats this as an invariant.

**Done when:** rapid inserts and concurrent status transitions do not drop accepted notes or claim that an uncommitted note was saved.

---

## SB-106 — Add periodic Discord reconciliation

**Branch:** ```feature/periodic-reconciliation```

Your MVP performs startup catch-up. Add a bounded periodic scan so a rare dropped Gateway event is recovered without requiring a restart.

### Implement

- Stored high-water mark.
- Bounded history scan.
- Existing message-ID uniqueness constraints.
- Visible warning if reconciliation fails or exceeds its scan limit.
- Metrics for recovered messages.

The architecture requires both startup catch-up and periodic reconciliation as a safety net.

**Done when:** intentionally skipping a Gateway event still results in exactly one stored capture after reconciliation runs.

---

## SB-107 — Add delivery attempts, leases, and capped retry state

**Branch:** ```feature/delivery-leases```

Extend the ledger with:

```bash
delivery_attempts
processing_lease_until
next_attempt_at
last_error
```

Add terminal and retryable states so ```capture-service``` can distinguish:

- Accepted but not forwarded.
- Forwarded but awaiting processing.
- Classifying.
- Filed.
- Inbox.
- Failed after capped retries.

The production design requires leases because webhook acceptance alone does not prove the downstream workflow completed.

**Done when:** a simulated downstream crash after webhook acceptance leaves the note retryable rather than stuck forever.

---

## SB-108 — Add the single-flight stale-lease reaper

**Branch:** ```feature/stale-lease-reaper```

Implement one self-scheduling watchdog loop inside ```capture-service```.

### Rules

- Never overlap reaper passes.
- Claim only a bounded batch.
- Increment retry attempts transactionally.
- Calculate capped exponential backoff.
- Mark permanently stuck items FAILED.
- Send a visible Discord alert after the retry limit.
- Perform network calls only after commit.

The architecture explicitly rejects an overlapping timer and infinite retries.

**Done when:** killing a fake downstream process repeatedly eventually produces a visible failed state and manual retry option rather than an endless loop.

---

## SB-109 — Expand the operational status command

**Branch:** ```feature/ops-status```

Expand your existing status command to show:

```init
captures received today
captures filed today
captures in inbox
captures failed
captures waiting for retry
stale leases
last successful reconciliation
capture-service health
```

Later, add Git push, backup, digest, n8n, and writer-service health fields as those features exist. The canonical design already defines the full target status view.

**Done when:** you can diagnose backlog health without opening SQLite manually.

---
