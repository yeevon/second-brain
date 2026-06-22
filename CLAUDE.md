# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                        # install dependencies
uv run pytest                                  # run all tests (both packages)
uv run pytest tests/unit/test_ledger.py        # run one test file
uv run pytest tests/unit/test_ledger.py::test_insert_capture  # run one test
uv run pytest tests/unit/ tests/integration/   # run a subset of test groups
uv run python -m secondbrain run               # start the service (requires .env)
uv run python -m secondbrain status            # read-only operational status
uv run python -m secondbrain retry SB-YYYYMMDD-NNNN
docker compose up -d                           # full local stack (capture-service + n8n + writer-service)
docker compose down                            # stop; named volumes preserved
docker compose down -v                         # destructive reset — deletes all volume data
deploy/local-stack-up.sh                       # build + start + wait-for-healthy wrapper
deploy/test-writer-service.sh                  # writer-service regression (health, filing, idempotency)
deploy/test-writer-safe-failure.sh             # Git failure injection regression
```

There is no separate lint command. `pyproject.toml` has no ruff or flake8 configuration.

## Architecture

### Two runtime modes

`CAPTURE_PROCESSING_MODE` must be set explicitly — startup fails if it is missing.

| Mode | Where | What it does |
|------|-------|--------------|
| `local-full` | Desktop | Capture → classify (Gemini in-process) → file (local vault) → receipt |
| `capture-only` | EC2 / local Docker | Capture → receipt only; filing delegated to n8n + writer-service |

`app.py` branches on mode early in `run_service()` and assembles two different startup graphs (`LocalWorkerStartup` vs `CaptureOnlyStartup`). Both modes share the same `CaptureService`, internal API, and Discord listener.

### Ownership boundaries (hard invariants)

- **`CaptureService`** is the sole owner of SQLite. External code (n8n, writer-service, tests) reaches it through the internal HTTP API (`capture_api.py`) or the `CaptureService` methods directly in tests. Never import `Ledger` from outside `capture_service.py` in production paths.
- **`writer-service`** is the sole vault writer when running in Docker/EC2. It owns the Git credential and the `flock` lock. `capture-service` never writes to the vault itself in `capture-only` mode.
- **No network I/O inside SQLite transactions.** `SQLiteRuntime.write()` runs its callback inside `BEGIN IMMEDIATE ... COMMIT`. Discord calls, HTTP calls, Gemini calls must happen outside this boundary.

### SQLite layer

`SQLiteRuntime` (`sqlite_runtime.py`) owns a single dedicated worker thread and one SQLite connection. All reads and writes are submitted as callables via `runtime.read(fn)` / `runtime.write(fn)` and execute synchronously on the worker thread. The calling thread blocks on `Future.result()`.

`Ledger` wraps `SQLiteRuntime` and is constructed by `CaptureService.open()`. Schema migrations run inside the worker thread at startup via `migrations.py`. To add a migration, append a new `Migration` to `_MIGRATIONS` with the next version number — migrations are applied in ascending version order and are idempotent.

### Capture lifecycle

```
Discord MESSAGE_CREATE
  → secret_screen.py (pre-persistence)
  → ledger: INSERT capture (status=RECEIVED, delivery_status=PENDING_FORWARD)
  → Discord: immediate receipt ⏳
  → delivery dispatcher (capture-only) or queue.enqueue (local-full)
  → n8n / Gemini classify
  → writer-service / vault_writer.py: write Markdown note
  → capture-service callback: update status=FILED/INBOX, derived_note_path, git_commit_hash
  → Discord: edit receipt ✅
```

Capture IDs use the format `SB-YYYYMMDD-NNNN`. The Discord message snowflake is the idempotency key — `UNIQUE` constraint on `discord_message_id` prevents duplicates at all layers.

### Background tasks (capture-only mode)

All tasks start before `on_ready` so they survive Discord connection delays:

- **Delivery dispatcher** (`delivery.py`) — polls `PENDING_FORWARD` rows and POSTs `capture_id` to the n8n webhook.
- **Stale-lease reaper** (`reaper.py`) — single-flight loop; claims expired leases, increments `retry_attempts`, requeues or marks `FAILED`.
- **Periodic reconciliation** (`reconcile.py`) — bounded Discord history scan that recovers missed messages.
- **Heartbeat** (`heartbeat.py`) — writes `capture_service_state` to `system_state` on an interval.

`app.py` contains `ensure_*_task()` helpers that restart each task if it has died (dead-task guard). These are called from `on_ready` and from the `ensure_*` pattern.

### Internal API

FastAPI app created in `capture_api.py`, embedded via `EmbeddedUvicornServer` in `api_server.py`. Authentication uses `X-Second-Brain-Internal-Token` header checked with `secrets.compare_digest`. All routes (except `/health`) require the token.

writer-service exposes a separate FastAPI app on port 8001 with `X-Second-Brain-Writer-Token`.

### writer-service package

Lives in `writer-service/` as a separate Python package (`writerservice`). Both `src/` and `writer-service/src/` are on `pythonpath` in `pyproject.toml`, so `uv run pytest` covers both packages in one run. `writer-service/tests/conftest.py` sets up a temporary vault directory for isolation.

Key files: `writer.py` (Markdown generation + idempotency by `capture_id`), `git_ops.py` (fetch/merge/commit/push under `flock`), `flock.py` (OS advisory lock via `fcntl.LOCK_EX`), `vault.py` (folder mapping + path guards).

### Test structure

| Directory | Purpose |
|-----------|---------|
| `tests/unit/` | Fast, no network, no Docker. Uses `FakeDiscordClient`, `FakeClassifier`, `FakeVaultWriter`. |
| `tests/integration/` | Multi-component flows with fakes. Uses `make_app()` + `drain_worker()` from `tests/support.py`. |
| `tests/architecture/` | Structural enforcement. Uses AST analysis to guard import boundaries, n8n workflow shape, Compose config, and container invariants. These tests have no runtime side effects. |

`tests/conftest.py` provides `test_settings` (a `SimpleNamespace` with `tmp_path`-based paths), `ledger`, `queue`, `vault_writer`, `fake_discord`, `capture_service`, and `capture_handler` fixtures.

`tests/support.py` provides:

- `make_app()` — assembles a `SimpleNamespace` test subject with all components wired together.
- `drain_worker()` — runs the capture worker in a task and waits for the queue to empty.
- `note_files()`, `audit_events()`, `ledger_rows()`, `event_types()` — test inspection helpers.

### Observability

`observability.py` exports `log_metadata(event_name, **fields)` which writes structured JSON to stdout. All operational events use this — no bare `print()` for service-level events. The `log_metadata` call is the only logging mechanism; there is no `logging` module configuration in capture-service.

### n8n workflows

JSON fixtures live in `n8n/workflows/`. `deploy/local-n8n-init.py` seeds them on `docker compose up` and updates them in place by existing workflow ID (no wipe required). Architecture tests in `tests/architecture/test_n8n_*.py` enforce the fixture shape, credential placeholder names, and in-place update behavior.

## Key invariants to preserve

1. `SQLiteRuntime.write()` callbacks must never call network APIs, async functions, or block on I/O.
2. `capture-service` is the sole SQLite owner — do not open `ledger.sqlite3` from writer-service or n8n.
3. `writer-service` is the sole vault writer and Git pusher when `GIT_SYNC_ENABLED=true`.
4. The OS advisory `flock` in `writer-service/src/writerservice/flock.py` is the only Git serialization mechanism — do not replace it with a file-existence sentinel.
5. Raw message text and credentials must never appear in `capture_events.event_payload_json` or `captures.last_error` — only sanitized error types and numeric metadata.
6. Discord message IDs are idempotency keys — `UNIQUE` constraints in the schema are the final guard.
