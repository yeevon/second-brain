# TD-014: Simplify the local developer workflow

**Status:** Resolved
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

---

1. Blocker: synthetic-capture helper is missing docker exec -i

The new validator correctly tries to seed its own capture inside the capture-service container:

docker exec -e "DISCORD_MSG_ID=$DISCORD_MSG_ID" "$CS_CONTAINER" python3 - <<'PYEOF'

But python3 - reads the script from standard input. docker exec needs -i to pass the heredoc into the container.

Fix
docker exec -i \
  -e "DISCORD_MSG_ID=$DISCORD_MSG_ID" \
  "$CS_CONTAINER" \
  python3 - <<'PYEOF'

Without that flag, the synthetic-capture creation step can return no capture ID.

2. Bug: cleanup does not actually clean up the synthetic capture

The exit handler posts a terminal workflow error:

{
  "disposition": "terminal",
  "error_type": "contract_violation",
  "reason_type": "test_cleanup"
}

But the test previously reported a retryable gemini_timeout for the same capture and delivery attempt.

The ledger intentionally rejects a later report with a different disposition as:

ignored_conflicting_replay

So the cleanup callback does not mark the capture terminal. It leaves a synthetic capture in RETRY_WAIT.

Minimal fix

Clean up the synthetic record through a local-only Python snippet in the test script after validation. Do not route cleanup through the production workflow-error API.

A direct local test cleanup is acceptable because the record was created directly by the local test helper. Do not add a production cleanup endpoint.

3. Determinism issue: dispatcher can race the synthetic helper

The test helper currently:

inserts the capture;
calls claim_due_deliveries() afterward.

Those are separate ledger operations while the real capture-service dispatcher may already be running. The dispatcher could claim the newly inserted capture before the helper does.

That would make the supposedly deterministic test fail intermittently.

Minimal fix

Seed the synthetic capture and move it into FORWARDING within one local-only transaction or one local-only helper operation.

Do not add another service, endpoint, or orchestration layer.

Tech-debt plan review
TD-014 is the correct plan

TD-014 stays focused on the real problem:

standard Docker commands should work;
wrappers should not be required for routine operations;
local validation should seed its own capture;
production behavior should remain fail-closed;
no new framework or production test endpoint should be introduced.

Keep this ticket.

TD-015 is redundant and should be closed after validation

TD-015 tracks the same Compose-lifecycle issue as a subset of TD-014.

That was useful while implementing, but once plain Compose commands pass, mark it resolved. Do not maintain two active tickets for the same problem.

Do not add more cleanup scope

There is no reason to redesign the Docker layout again.

Fix the three test-script issues, then verify locally:

docker compose down
docker compose up -d
docker compose ps
deploy/test-n8n-error-workflow.sh
docker compose down

I reviewed the pushed files statically through GitHub. I could not execute Docker in this environment. After those commands pass on your machine, close TD-014 and TD-015 and move to the next milestone.