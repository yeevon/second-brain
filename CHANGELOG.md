# Changelog

All notable changes to this project are documented here.

---

## Milestone 1 — Make the MVP deployable without changing behavior

**Commits:** `63a3f1a` → `3573aa8` | **Branch:** `main`

The goal of Milestone 1 was to get the working capture loop onto EC2 before introducing n8n or Git complexity. No user-facing capture behavior changed.

### SB-101 — Regression test suite

Added a comprehensive regression suite covering the proven local behavior:

- Normal message creates exactly one SQLite capture and one receipt.
- Duplicate Discord event does not create a duplicate row or receipt.
- Bot receipts are ignored.
- Secret-like input stores only a redacted rejection.
- Gemini failure routes to `00_inbox/`.
- Vault-write failure leaves the raw capture recoverable.
- Startup reconciliation recovers a missed message exactly once.
- Receipt-edit failure sends one replacement receipt.

### SB-102 — Capture-service boundary

Refactored the monolith so Discord intake, SQLite ownership, receipts, and reconciliation sit behind a clean `CaptureService` facade. `CaptureService` is now the sole production SQLite owner. `worker.py`, `app.py`, and `reconcile.py` no longer touch the ledger directly.

### SB-103 — Internal capture-service API

Added a small authenticated internal HTTP API wrapping `CaptureService`:

```
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

Every state-changing route requires a shared-secret header. The API runs inside the capture-service process and is not published outside it.

### SB-104 — Dockerize and deploy capture-service to EC2

Added an explicit `capture-only` runtime mode alongside the existing `local-full` mode. Added Docker and EC2 deployment infrastructure.

**Application changes:**

- `CAPTURE_PROCESSING_MODE` is now required. Supported values: `local-full`, `capture-only`. Startup fails visibly on missing or unsupported values.
- `capture-only` mode does not require Gemini configuration, a vault path, or a classifier worker.
- `capture-only` receipt text is honest: "Your note is safely captured. Downstream filing is not enabled yet."
- `CaptureOnlyStartup` marks reconciliation complete only after it succeeds, so a transient failure on `on_ready` allows a retry on the next Discord ready callback.
- `CaptureService` accepts an optional `notify_capture` callback; passing `None` leaves accepted captures as `RECEIVED` without treating the absence of downstream processing as a failure.

**Container and deployment:**

- `Dockerfile` — Python 3.12, non-root user (uid 10001), locked production dependencies, no dev dependencies, no secrets baked in.
- `.dockerignore` — excludes `.git`, `.venv`, `.env*`, `.runtime`, `vault`, `tests`, `docs`.
- `compose.yaml` — `restart: unless-stopped`, EBS bind mount, `expose: 8000` (no `ports:`), `cap_drop: ALL`, `read_only: true`, `tmpfs: /tmp`, `no-new-privileges`, JSON log rotation, internal health check. Paths parameterized via `CAPTURE_SERVICE_ENV_FILE` and `CAPTURE_DATA_DIR` for local validation.
- `deploy/container-entrypoint.sh` — checks for `/var/lib/second-brain/.second-brain-ebs-volume` before starting the process. Container exits immediately if the EBS volume is not mounted, preventing SQLite writes to the root filesystem after a failed remount.
- `deploy/provision-host.sh` — installs Docker Engine and Compose plugin, creates application directories. Config directory is owned by the deploy user (not root) so Compose can read the env file without `sudo`.
- `deploy/deploy.sh` — fails before `docker compose up` if `$DATA_DIR` is not a mount point or the EBS sentinel marker is absent.
- `deploy/verify.sh` — checks container running state, restart policy, non-root user, unpublished port, mounted data path, EBS marker, bind-mount source, ledger file existence, and container health.
- `deploy/capture-service.env.example` — EC2 environment template. Does not include `GEMINI_API_KEY` or `VAULT_PATH`.
- `deploy/README.md` — EC2 provisioning guide including EBS setup, sentinel marker creation, SSH hardening verification, IMDSv2 enforcement, security-group audit, and `DeleteOnTermination=false` verification.

**Tests added:** unit, integration, and architecture tests covering `capture-only` mode behavior, container configuration semantics, and entrypoint behavior.

---

## MVP — Version 1

**Commits:** `bf44637` → `d2bb208` | **Branch:** `main`

Initial working capture loop running locally on the desktop.

- Discord Gateway listener restricted to one guild, one channel, one user.
- Pre-persistence secret screening rejects messages containing API keys, tokens, or passwords.
- SQLite ledger as the durable source of truth.
- Immediate "received" Discord receipt after successful persistence.
- Background Gemini classification worker (non-blocking).
- Markdown note rendering and filing into a numbered Obsidian vault structure.
- Final receipt edit showing filed location, inbox routing reason, or vault failure.
- Startup reconciliation replays Discord history to recover missed messages after downtime.
- Append-only audit log at `99_log/events.ndjson`.
