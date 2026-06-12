#!/usr/bin/env bash
# Import local-only test fixtures into n8n.
# DO NOT run this script in EC2/staging/production deployments.
# The Error Harness is a local validation tool only.
set -euo pipefail

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

CONTAINER="${CONTAINER:-second-brain-n8n}"
HARNESS_NAME="Second Brain - Error Harness"
HARNESS_FIXTURE="n8n/workflows/test/second-brain-error-harness.json"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT_DIR"

if [[ ! -f "$HARNESS_FIXTURE" ]]; then
  echo "harness fixture not found: $HARNESS_FIXTURE" >&2
  exit 1
fi

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

# Export existing workflow names
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

if echo "$existing_names" | grep -qxF "$HARNESS_NAME"; then
  echo "  Second Brain - Error Harness: skipped (already exists)"
else
  jq 'del(.id, .versionId)' \
    "$HARNESS_FIXTURE" \
    > "$TMP_DIR/bootstrap-harness.json"

  docker cp \
    "$TMP_DIR/bootstrap-harness.json" \
    "$CONTAINER:/tmp/bootstrap-harness.json"

  docker exec "$CONTAINER" \
    n8n import:workflow --input=/tmp/bootstrap-harness.json

  docker exec --user root "$CONTAINER" \
    rm -f /tmp/bootstrap-harness.json

  echo "  Second Brain - Error Harness: imported (inactive)"
fi

docker exec --user root "$CONTAINER" \
  rm -f /tmp/existing-workflows.json

echo ""
echo "Bind the required credential before activating the harness:"
echo "  - Test Harness Token  (HTTP Header Auth: X-Second-Brain-Webhook-Token)"
echo ""
echo "DO NOT activate this workflow outside local validation."
