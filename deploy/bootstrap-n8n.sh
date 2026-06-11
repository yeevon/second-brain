#!/usr/bin/env bash
set -euo pipefail

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

CONTAINER="${CONTAINER:-second-brain-n8n}"
ERROR_HANDLER_NAME="Second Brain - Error Handler"
FIXTURE_PATH="n8n/workflows/second-brain-error-handler.json"

# Resolve script root so this can be run from any working directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT_DIR"

# Verify the fixture exists before doing anything
if [[ ! -f "$FIXTURE_PATH" ]]; then
  echo "workflow fixture not found: $FIXTURE_PATH" >&2
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

# Fail if duplicate name detected
if echo "$existing_names" | grep -qxF "$ERROR_HANDLER_NAME"; then
  echo "Workflow already exists: $ERROR_HANDLER_NAME" >&2
  echo "Bootstrap is idempotent — skipping import." >&2
  echo ""
  echo "  Second Brain - Error Handler: skipped (already exists)"
  exit 0
fi

# Sanitize fixture: strip id and versionId to prevent ID-based overwrite
jq 'del(.id, .versionId)' \
  "$FIXTURE_PATH" \
  > "$TMP_DIR/bootstrap-error-handler.json"

# Copy sanitized fixture into the container
docker cp \
  "$TMP_DIR/bootstrap-error-handler.json" \
  "$CONTAINER:/tmp/bootstrap-error-handler.json"

# Import using the n8n CLI inside the container
docker exec "$CONTAINER" \
  n8n import:workflow --input=/tmp/bootstrap-error-handler.json

# Clean up temporary files inside the container
docker exec --user root "$CONTAINER" \
  rm -f \
    /tmp/bootstrap-error-handler.json \
    /tmp/existing-workflows.json

# Verify the workflow was imported successfully
docker exec "$CONTAINER" \
  n8n export:workflow --all --output=/tmp/verify-workflows.json \
  >/dev/null 2>&1

docker cp \
  "$CONTAINER:/tmp/verify-workflows.json" \
  "$TMP_DIR/verify-workflows.json"

docker exec --user root "$CONTAINER" \
  rm -f /tmp/verify-workflows.json

imported_name="$(
  jq -r '.[].name' "$TMP_DIR/verify-workflows.json" \
    | grep -xF "$ERROR_HANDLER_NAME" || true
)"

if [[ -z "$imported_name" ]]; then
  echo "Import appeared to succeed but workflow not found on verification." >&2
  exit 1
fi

echo ""
echo "  Second Brain - Error Handler: imported (inactive)"
echo ""
echo "Open the n8n UI, bind the required credentials, then activate the workflow manually."
