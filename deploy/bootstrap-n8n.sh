#!/usr/bin/env bash
set -euo pipefail

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

CONTAINER="${CONTAINER:-second-brain-n8n}"
ERROR_HANDLER_NAME="Second Brain - Error Handler"
INTAKE_NAME="Second Brain - Intake"
ERROR_HANDLER_FIXTURE="n8n/workflows/second-brain-error-handler.json"
INTAKE_FIXTURE="n8n/workflows/second-brain-intake.json"

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
  # Sanitize fixture: strip id and versionId to prevent ID-based overwrite
  jq 'del(.id, .versionId)' \
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
  echo "  Second Brain - Intake: skipped (already exists)"
else
  jq 'del(.id, .versionId)' \
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
for wf_name in "$ERROR_HANDLER_NAME" "$INTAKE_NAME"; do
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
echo "Open the n8n UI, bind the required credentials, then activate the workflows manually."
echo ""
echo "Required credentials to bind before activating Second Brain - Intake:"
echo "  - Capture Service Token  (HTTP Header Auth: X-Second-Brain-Internal-Token)"
echo "  - Gemini API Key         (Google AI / HTTP credentials)"
echo "  - Writer Stub Token      (HTTP Header Auth: X-Writer-Stub-Token)"
echo "  - Intake Webhook Token   (HTTP Header Auth on the Webhook node)"
