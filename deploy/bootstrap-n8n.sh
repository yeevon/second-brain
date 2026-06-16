#!/usr/bin/env bash
set -euo pipefail

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

CONTAINER="${CONTAINER:-second-brain-n8n}"
ERROR_HANDLER_NAME="Second Brain - Error Handler"
INTAKE_NAME="Second Brain - Intake"
DAILY_DIGEST_NAME="Second Brain - Daily Digest"
WEEKLY_REVIEW_NAME="Second Brain - Weekly Review"
ERROR_HANDLER_FIXTURE="n8n/workflows/second-brain-error-handler.json"
INTAKE_FIXTURE="n8n/workflows/second-brain-intake.json"
DAILY_DIGEST_FIXTURE="n8n/workflows/second-brain-daily-digest.json"
WEEKLY_REVIEW_FIXTURE="n8n/workflows/second-brain-weekly-review.json"

# Resolve script root so this can be run from any working directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT_DIR"

# Verify fixtures exist before doing anything
if [[ ! -f "$ERROR_HANDLER_FIXTURE" ]]; then
  echo "workflow fixture not found: $ERROR_HANDLER_FIXTURE" >&2
  exit 1
fi
if [[ ! -f "$INTAKE_FIXTURE" ]]; then
  echo "workflow fixture not found: $INTAKE_FIXTURE" >&2
  exit 1
fi
if [[ ! -f "$DAILY_DIGEST_FIXTURE" ]]; then
  echo "workflow fixture not found: $DAILY_DIGEST_FIXTURE" >&2
  exit 1
fi
if [[ ! -f "$WEEKLY_REVIEW_FIXTURE" ]]; then
  echo "workflow fixture not found: $WEEKLY_REVIEW_FIXTURE" >&2
  exit 1
fi

# Require the container to be healthy
health="$(
  docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
    "$CONTAINER" \
    2>/dev/null || true
)"

if [[ "$health" != "healthy" ]]; then
  echo "$CONTAINER is not healthy (status: $health)" >&2
  echo "Start the stack and wait for the container to become healthy before bootstrapping." >&2
  exit 1
fi

# Export existing workflow names from the container
docker exec "$CONTAINER" \
  n8n export:workflow --all --output=/tmp/existing-workflows.json \
  >/dev/null 2>&1 || true

docker cp \
  "$CONTAINER:/tmp/existing-workflows.json" \
  "$TMP_DIR/existing-workflows.json" \
  2>/dev/null || echo "[]" > "$TMP_DIR/existing-workflows.json"

existing_names="$(
  jq -r '.[].name' "$TMP_DIR/existing-workflows.json" 2>/dev/null || true
)"

existing_count="$(echo "$existing_names" | grep -c . 2>/dev/null || echo 0)"
echo "Found $existing_count existing workflow(s)."

# ── Error Handler ────────────────────────────────────────────────────────────

if echo "$existing_names" | grep -qxF "$ERROR_HANDLER_NAME"; then
  echo "  Second Brain - Error Handler: skipped (already exists)"
else
  # Sanitize fixture: strip id and versionId, assign a fresh UUID (n8n 2.x requires id)
  jq --arg id "$(python3 -c 'import uuid; print(str(uuid.uuid4()))')" \
    'del(.id, .versionId) | .id = $id' \
    "$ERROR_HANDLER_FIXTURE" \
    > "$TMP_DIR/bootstrap-error-handler.json"

  docker cp \
    "$TMP_DIR/bootstrap-error-handler.json" \
    "$CONTAINER:/tmp/bootstrap-error-handler.json"

  docker exec "$CONTAINER" \
    n8n import:workflow --input=/tmp/bootstrap-error-handler.json

  docker exec --user root "$CONTAINER" \
    rm -f /tmp/bootstrap-error-handler.json
fi

# ── Intake Workflow ──────────────────────────────────────────────────────────

if echo "$existing_names" | grep -qxF "$INTAKE_NAME"; then
  # Upgrade path: update existing workflow in place by ID
  existing_intake_id="$(
    jq -r '.[] | select(.name == "'"$INTAKE_NAME"'") | .id' \
      "$TMP_DIR/existing-workflows.json" 2>/dev/null || true
  )"

  if [[ -n "$existing_intake_id" ]]; then
    jq --arg id "$existing_intake_id" \
      'del(.versionId) | .id = $id' \
      "$INTAKE_FIXTURE" \
      > "$TMP_DIR/upgrade-intake.json"

    docker cp \
      "$TMP_DIR/upgrade-intake.json" \
      "$CONTAINER:/tmp/upgrade-intake.json"

    docker exec "$CONTAINER" \
      n8n import:workflow --input=/tmp/upgrade-intake.json

    docker exec --user root "$CONTAINER" \
      rm -f /tmp/upgrade-intake.json

    echo "  Second Brain - Intake: updated in place (left inactive)"
    echo ""
    echo "  ACTION REQUIRED: Rebind 'Second Brain - Writer Service Header' credential"
    echo "  in the Second Brain - Intake workflow and reactivate."
  else
    echo "  Second Brain - Intake: skipped (exists but id not found)"
  fi
else
  jq --arg id "$(python3 -c 'import uuid; print(str(uuid.uuid4()))')" \
    'del(.id, .versionId) | .id = $id' \
    "$INTAKE_FIXTURE" \
    > "$TMP_DIR/bootstrap-intake.json"

  docker cp \
    "$TMP_DIR/bootstrap-intake.json" \
    "$CONTAINER:/tmp/bootstrap-intake.json"

  docker exec "$CONTAINER" \
    n8n import:workflow --input=/tmp/bootstrap-intake.json

  docker exec --user root "$CONTAINER" \
    rm -f /tmp/bootstrap-intake.json
fi

# ── Daily Digest ─────────────────────────────────────────────────────────────

if echo "$existing_names" | grep -qxF "$DAILY_DIGEST_NAME"; then
  echo "  Second Brain - Daily Digest: skipped (already exists)"
else
  jq --arg id "$(python3 -c 'import uuid; print(str(uuid.uuid4()))')" \
    'del(.id, .versionId) | .id = $id' \
    "$DAILY_DIGEST_FIXTURE" \
    > "$TMP_DIR/bootstrap-daily-digest.json"

  docker cp \
    "$TMP_DIR/bootstrap-daily-digest.json" \
    "$CONTAINER:/tmp/bootstrap-daily-digest.json"

  docker exec "$CONTAINER" \
    n8n import:workflow --input=/tmp/bootstrap-daily-digest.json

  docker exec --user root "$CONTAINER" \
    rm -f /tmp/bootstrap-daily-digest.json
fi

# ── Weekly Review ─────────────────────────────────────────────────────────────

if echo "$existing_names" | grep -qxF "$WEEKLY_REVIEW_NAME"; then
  echo "  Second Brain - Weekly Review: skipped (already exists)"
else
  jq --arg id "$(python3 -c 'import uuid; print(str(uuid.uuid4()))')" \
    'del(.id, .versionId) | .id = $id' \
    "$WEEKLY_REVIEW_FIXTURE" \
    > "$TMP_DIR/bootstrap-weekly-review.json"

  docker cp \
    "$TMP_DIR/bootstrap-weekly-review.json" \
    "$CONTAINER:/tmp/bootstrap-weekly-review.json"

  docker exec "$CONTAINER" \
    n8n import:workflow --input=/tmp/bootstrap-weekly-review.json

  docker exec --user root "$CONTAINER" \
    rm -f /tmp/bootstrap-weekly-review.json
fi

# Clean up existing-workflows temp file in container
docker exec --user root "$CONTAINER" \
  rm -f /tmp/existing-workflows.json

# ── Verify both workflows were imported ─────────────────────────────────────

docker exec "$CONTAINER" \
  n8n export:workflow --all --output=/tmp/verify-workflows.json \
  >/dev/null 2>&1

docker cp \
  "$CONTAINER:/tmp/verify-workflows.json" \
  "$TMP_DIR/verify-workflows.json"

docker exec --user root "$CONTAINER" \
  rm -f /tmp/verify-workflows.json

echo ""
for wf_name in "$ERROR_HANDLER_NAME" "$INTAKE_NAME" "$DAILY_DIGEST_NAME" "$WEEKLY_REVIEW_NAME"; do
  found="$(
    jq -r '.[].name' "$TMP_DIR/verify-workflows.json" \
      | grep -xF "$wf_name" || true
  )"
  if [[ -z "$found" ]]; then
    echo "Import appeared to succeed but workflow not found on verification: $wf_name" >&2
    exit 1
  fi
  echo "  $wf_name: imported (inactive)"
done

echo ""
echo "Open the n8n UI and complete these manual steps before activating workflows."
echo ""
echo "Step 1 — Second Brain - Error Handler"
echo "  a. Open the workflow."
echo "  b. Select the 'Report Workflow Error to Capture Service' node."
echo "  c. Bind credential: Capture Service Token"
echo "     Type: HTTP Header Auth | Header: X-Second-Brain-Internal-Token"
echo "  d. Save the workflow (leave inactive — it is triggered by n8n, not manually)."
echo ""
echo "Step 2 — Second Brain - Intake"
echo "  a. Bind these five credentials:"
echo "     - Intake Webhook Token              (HTTP Header Auth: X-Second-Brain-Intake-Token)"
echo "     - Capture Service Token             (HTTP Header Auth: X-Second-Brain-Internal-Token)"
echo "     - Gemini API Key                    (HTTP Header Auth: X-Goog-Api-Key)"
echo "     - Second Brain - Writer Service Header (HTTP Header Auth: X-Second-Brain-Writer-Token)"
echo "  b. Open Workflow Settings (... menu → Settings)."
echo "  c. Under 'Error Workflow', select 'Second Brain - Error Handler'."
echo "     NOTE: the fixture contains a placeholder — this must be set manually in the UI."
echo "  d. Save the workflow."
echo "  e. Activate the workflow."
echo ""
echo "Step 3 — Second Brain - Daily Digest"
echo "  a. Ensure DISCORD_DIGEST_WEBHOOK_URL is set in your n8n env file (n8n.local.env)"
echo "     and N8N_BLOCK_ENV_ACCESS_IN_NODE=false so the workflow can read it."
echo "     The workflow uses \$env.DISCORD_DIGEST_WEBHOOK_URL in the Send to Discord node."
echo "  b. Bind credential: Capture Service Token"
echo "     Type: HTTP Header Auth | Header: X-Second-Brain-Internal-Token"
echo "  c. Save and activate the workflow."
echo "     It will trigger daily at 07:00 UTC."
echo ""
echo "Step 4 — Second Brain - Weekly Review"
echo "  a. Ensure DISCORD_DIGEST_WEBHOOK_URL is set in your n8n env file (see Step 3a)."
echo "  b. Bind credentials:"
echo "     - Capture Service Token  (HTTP Header Auth: X-Second-Brain-Internal-Token)"
echo "     - Gemini API Key         (HTTP Header Auth: X-Goog-Api-Key)"
echo "  c. Save and activate the workflow."
echo "     It will trigger every Monday at 08:00 UTC."
