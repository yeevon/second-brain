# TD-014: Simplify the local developer workflow

**Status:** Promoted — must be addressed before the next milestone
**Priority:** High

## Problem

The local development workflow requires wrapper scripts for basic lifecycle operations. A developer cannot run:

```bash
docker compose up -d
docker compose down
docker compose logs -f
docker compose ps
```

without first exporting several shell variables (`CAPTURE_SERVICE_ENV_FILE`, `CAPTURE_DATA_SOURCE`, `N8N_IMAGE_TAG`, `N8N_ENV_FILE`, `N8N_ENCRYPTION_KEY_FILE`, `N8N_DATA_SOURCE`, `WRITER_STUB_ENV_FILE`, `COMPOSE_FILE`).

The reason: `compose.yaml` uses `:?` error guards that fail if the variables are not exported, and n8n/writer-stub services are in a separate overlay file that is not auto-loaded.

Additionally, `local-stack-up.sh` and `local-stack-down.sh` compensate by exporting these variables and setting `COMPOSE_FILE` manually. These wrappers must not be required for ordinary lifecycle operations.

## Acceptance criteria

1. `docker compose up -d` works from a configured local checkout without shell exports.
2. `docker compose down` works without shell exports.
3. `docker compose logs -f` and `docker compose ps` work without shell exports.
4. A `compose.override.yaml` is auto-loaded and provides local-safe defaults for all required Compose variables.
5. n8n and writer-stub services are included in the auto-loaded override so `docker compose up -d` starts all three services.
6. Production `deploy.sh` continues to use explicit `COMPOSE_FILE=compose.yaml` and is unaffected by the override.
7. One-time local initialization (volume creation, sentinel creation, n8n bootstrap) may remain in a wrapper script.
8. The SB-113 regression test (`deploy/test-n8n-error-workflow.sh`) requires no manual CAPTURE_ID, DELIVERY_ATTEMPT, or token input — it creates its own synthetic capture and reads TEST_HARNESS_TOKEN from `n8n-test.local.env`.

## Implementation notes

- Remove `:?` error guards from `compose.yaml`; replace with `:-` defaults.
- Create `compose.override.yaml` (auto-loaded) with local-safe defaults and the full n8n + writer-stub service definitions.
- The override entrypoint creates the sentinel file on first container start, eliminating the manual `docker run` step from `local-stack-up.sh`.
- Volumes in the override are managed by Docker Compose (not `external: true`) so they are created automatically on first `up`.

## Do not

- Do not add a production test endpoint.
- Do not build a new orchestration layer or shell framework.
- Do not require wrappers for `docker compose up/down/logs/ps`.
- Do not weaken production fail-closed behavior — `deploy.sh` always sets variables explicitly.
