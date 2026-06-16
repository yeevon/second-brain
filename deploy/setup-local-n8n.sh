#!/usr/bin/env bash
# One-time local n8n setup for SB-113 error workflow testing.
#
# Run after deploy/local-stack-up.sh and after creating the n8n owner account.
# Idempotent — safe to re-run if something was reset.
#
# What this script does automatically:
#   1. Generates TEST_HARNESS_TOKEN and stores it in n8n-test.local.env.
#   2. Imports Second Brain - Error Handler (via bootstrap-n8n.sh).
#   3. Imports Second Brain - Error Harness with errorWorkflow wired to the
#      real Error Handler workflow ID (not the placeholder).
#   4. Patches the Intake workflow's errorWorkflow to the real Error Handler ID
#      if Intake is already imported.
#
# What still requires a one-time manual step in the n8n UI:
#   - Binding credentials to workflow nodes (tokens are printed below).
#   - Activating Second Brain - Error Harness.
#
# After this script succeeds, you can run deploy/test-n8n-error-workflow.sh
# any number of times without further manual steps (once credentials are bound).
set -euo pipefail

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

N8N_CONTAINER="${N8N_CONTAINER:-second-brain-n8n}"
ERROR_HANDLER_NAME="Second Brain - Error Handler"
ERROR_HARNESS_NAME="Second Brain - Error Harness"
INTAKE_NAME="Second Brain - Intake"
ERROR_HANDLER_FIXTURE="$ROOT_DIR/n8n/workflows/second-brain-error-handler.json"
ERROR_HARNESS_FIXTURE="$ROOT_DIR/n8n/workflows/test/second-brain-error-harness.json"
INTAKE_FIXTURE="$ROOT_DIR/n8n/workflows/second-brain-intake.json"
ENV_FILE="$ROOT_DIR/.env"
TEST_ENV_FILE="$ROOT_DIR/n8n-test.local.env"

# ── Pre-flight ────────────────────────────────────────────────────────────────

for fixture in "$ERROR_HANDLER_FIXTURE" "$ERROR_HARNESS_FIXTURE" "$INTAKE_FIXTURE"; do
  if [[ ! -f "$fixture" ]]; then
    echo "fixture not found: $fixture" >&2; exit 1
  fi
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2; exit 1
fi

health="$(
  docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
    "$N8N_CONTAINER" 2>/dev/null || true
)"
if [[ "$health" != "healthy" ]]; then
  echo "$N8N_CONTAINER is not healthy (status: $health)" >&2
  echo "Run deploy/local-stack-up.sh and wait for all containers to become healthy." >&2
  exit 1
fi

# ── Step 1: Generate / read TEST_HARNESS_TOKEN ────────────────────────────────

if [[ -f "$TEST_ENV_FILE" ]] && grep -q '^TEST_HARNESS_TOKEN=' "$TEST_ENV_FILE" 2>/dev/null; then
  TEST_HARNESS_TOKEN="$(grep '^TEST_HARNESS_TOKEN=' "$TEST_ENV_FILE" | cut -d= -f2-)"
  if [[ -z "$TEST_HARNESS_TOKEN" ]]; then
    TEST_HARNESS_TOKEN="$(openssl rand -hex 32)"
    # Replace the empty value
    sed -i "s|^TEST_HARNESS_TOKEN=.*|TEST_HARNESS_TOKEN=${TEST_HARNESS_TOKEN}|" "$TEST_ENV_FILE"
    echo "  TEST_HARNESS_TOKEN: regenerated (was empty)"
  else
    echo "  TEST_HARNESS_TOKEN: read from $TEST_ENV_FILE"
  fi
else
  TEST_HARNESS_TOKEN="$(openssl rand -hex 32)"
  echo "TEST_HARNESS_TOKEN=${TEST_HARNESS_TOKEN}" >> "$TEST_ENV_FILE"
  echo "  TEST_HARNESS_TOKEN: generated and saved to $TEST_ENV_FILE"
fi

CAPTURE_SERVICE_INTERNAL_TOKEN="$(grep '^CAPTURE_SERVICE_INTERNAL_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)"
if [[ -z "$CAPTURE_SERVICE_INTERNAL_TOKEN" ]]; then
  echo "CAPTURE_SERVICE_INTERNAL_TOKEN not found in $ENV_FILE" >&2; exit 1
fi

# ── Step 2: Export current n8n workflow state ─────────────────────────────────

docker exec "$N8N_CONTAINER" \
  n8n export:workflow --all --output=/tmp/sb-setup-workflows.json >/dev/null 2>&1 || true

docker cp \
  "$N8N_CONTAINER:/tmp/sb-setup-workflows.json" \
  "$TMP_DIR/existing-workflows.json" \
  2>/dev/null || echo "[]" > "$TMP_DIR/existing-workflows.json"

docker exec --user root "$N8N_CONTAINER" rm -f /tmp/sb-setup-workflows.json

existing_names="$(jq -r '.[].name' "$TMP_DIR/existing-workflows.json" 2>/dev/null || true)"

# ── Step 3: Import Error Handler ──────────────────────────────────────────────

echo "--- Importing Error Handler ---"
if echo "$existing_names" | grep -qxF "$ERROR_HANDLER_NAME"; then
  echo "  $ERROR_HANDLER_NAME: already imported — skipped"
else
  jq --arg id "$(python3 -c 'import uuid; print(str(uuid.uuid4()))')" \
    'del(.id, .versionId) | .id = $id' "$ERROR_HANDLER_FIXTURE" > "$TMP_DIR/import-handler.json"
  docker cp "$TMP_DIR/import-handler.json" "$N8N_CONTAINER:/tmp/import-handler.json"
  docker exec "$N8N_CONTAINER" n8n import:workflow --input=/tmp/import-handler.json
  docker exec --user root "$N8N_CONTAINER" rm -f /tmp/import-handler.json
  echo "  $ERROR_HANDLER_NAME: imported (inactive)"
fi

# Refresh workflow list to get the real Error Handler ID
docker exec "$N8N_CONTAINER" \
  n8n export:workflow --all --output=/tmp/sb-setup-workflows2.json >/dev/null 2>&1
docker cp \
  "$N8N_CONTAINER:/tmp/sb-setup-workflows2.json" \
  "$TMP_DIR/workflows-after-handler.json" 2>/dev/null
docker exec --user root "$N8N_CONTAINER" rm -f /tmp/sb-setup-workflows2.json

HANDLER_ID="$(
  jq -r '.[] | select(.name=='"\"$ERROR_HANDLER_NAME\""') | .id // ""' \
    "$TMP_DIR/workflows-after-handler.json"
)"
if [[ -z "$HANDLER_ID" ]]; then
  echo "Could not read Error Handler workflow ID — import may have failed." >&2; exit 1
fi
echo "  Error Handler workflow ID: $HANDLER_ID"

# ── Step 4: Import Error Harness with patched errorWorkflow ───────────────────

echo "--- Importing Error Harness ---"
if echo "$existing_names" | grep -qxF "$ERROR_HARNESS_NAME"; then
  # Already imported — check if errorWorkflow is correctly wired
  HARNESS_EW="$(
    jq -r '.[] | select(.name=='"\"$ERROR_HARNESS_NAME\""') | .settings.errorWorkflow // ""' \
      "$TMP_DIR/workflows-after-handler.json"
  )"
  if [[ "$HARNESS_EW" == "$HANDLER_ID" ]]; then
    echo "  $ERROR_HARNESS_NAME: already imported with correct errorWorkflow — skipped"
  else
    echo "  $ERROR_HARNESS_NAME: exists but errorWorkflow is '${HARNESS_EW:-unset}'"
    HARNESS_ID="$(
      jq -r '.[] | select(.name=='"\"$ERROR_HARNESS_NAME\""') | .id // ""' \
        "$TMP_DIR/workflows-after-handler.json"
    )"
    # Patch the exported harness and re-import with the same ID (in-place update)
    jq \
      --arg id "$HARNESS_ID" \
      --arg ew "$HANDLER_ID" \
      'first(.[] | select(.name=='"\"$ERROR_HARNESS_NAME\""')) | .id = $id | .settings.errorWorkflow = $ew' \
      "$TMP_DIR/workflows-after-handler.json" > "$TMP_DIR/patched-harness.json"
    docker cp "$TMP_DIR/patched-harness.json" "$N8N_CONTAINER:/tmp/patched-harness.json"
    docker exec "$N8N_CONTAINER" n8n import:workflow --input=/tmp/patched-harness.json
    docker exec --user root "$N8N_CONTAINER" rm -f /tmp/patched-harness.json
    echo "  $ERROR_HARNESS_NAME: errorWorkflow patched to $HANDLER_ID"
  fi
else
  jq \
    --arg id "$(python3 -c 'import uuid; print(str(uuid.uuid4()))')" \
    --arg ew "$HANDLER_ID" \
    'del(.id, .versionId) | .id = $id | .settings.errorWorkflow = $ew' \
    "$ERROR_HARNESS_FIXTURE" > "$TMP_DIR/import-harness.json"
  docker cp "$TMP_DIR/import-harness.json" "$N8N_CONTAINER:/tmp/import-harness.json"
  docker exec "$N8N_CONTAINER" n8n import:workflow --input=/tmp/import-harness.json
  docker exec --user root "$N8N_CONTAINER" rm -f /tmp/import-harness.json
  echo "  $ERROR_HARNESS_NAME: imported with errorWorkflow = $HANDLER_ID"
fi

# ── Step 5: Patch Intake errorWorkflow (if Intake is already imported) ────────

echo "--- Patching Intake errorWorkflow ---"
INTAKE_ID="$(
  jq -r '.[] | select(.name=='"\"$INTAKE_NAME\""') | .id // ""' \
    "$TMP_DIR/workflows-after-handler.json"
)"
if [[ -z "$INTAKE_ID" ]]; then
  echo "  $INTAKE_NAME: not yet imported — run deploy/bootstrap-n8n.sh first"
else
  INTAKE_EW="$(
    jq -r '.[] | select(.name=='"\"$INTAKE_NAME\""') | .settings.errorWorkflow // ""' \
      "$TMP_DIR/workflows-after-handler.json"
  )"
  if [[ "$INTAKE_EW" == "$HANDLER_ID" ]]; then
    echo "  $INTAKE_NAME: errorWorkflow already correctly set — skipped"
  else
    jq \
      --arg id "$INTAKE_ID" \
      --arg ew "$HANDLER_ID" \
      'first(.[] | select(.name=='"\"$INTAKE_NAME\""')) | .id = $id | .settings.errorWorkflow = $ew' \
      "$TMP_DIR/workflows-after-handler.json" > "$TMP_DIR/patched-intake.json"
    docker cp "$TMP_DIR/patched-intake.json" "$N8N_CONTAINER:/tmp/patched-intake.json"
    docker exec "$N8N_CONTAINER" n8n import:workflow --input=/tmp/patched-intake.json
    docker exec --user root "$N8N_CONTAINER" rm -f /tmp/patched-intake.json
    echo "  $INTAKE_NAME: errorWorkflow patched to $HANDLER_ID"
  fi
fi

# ── Summary: manual steps that remain ────────────────────────────────────────

echo ""
echo "================================================================"
echo "  Automated setup complete."
echo "================================================================"
echo ""
echo "One-time manual steps remaining in the n8n UI (http://127.0.0.1:5678):"
echo ""
echo "  1. Open '$ERROR_HANDLER_NAME'"
echo "     Select node: 'Report Workflow Error to Capture Service'"
echo "     Bind credential: Capture Service Token"
echo "       Type: HTTP Header Auth"
echo "       Header name: X-Second-Brain-Internal-Token"
echo "       Header value: (from $ENV_FILE)"
echo "     Save the workflow."
echo ""
echo "  2. Open '$ERROR_HARNESS_NAME'"
echo "     Select node: 'Harness Webhook'"
echo "     Bind credential: Test Harness Token"
echo "       Type: HTTP Header Auth"
echo "       Header name: X-Second-Brain-Webhook-Token"
echo "       Header value: $TEST_HARNESS_TOKEN"
echo "     Open Workflow Settings → Error Workflow → should already be set"
echo "       (verify it shows: $ERROR_HANDLER_NAME)"
echo "     Save, then activate the workflow."
echo ""
echo "Token file: $TEST_ENV_FILE"
echo "  TEST_HARNESS_TOKEN=$TEST_HARNESS_TOKEN"
echo ""
echo "After completing the steps above, run:"
echo "  deploy/test-n8n-error-workflow.sh"
echo ""
