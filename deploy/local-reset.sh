#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--confirm-delete-local-test-data" ]]; then
  echo "Usage: $0 --confirm-delete-local-test-data" >&2
  echo "" >&2
  echo "This script permanently deletes the second-brain-local-data named volume" >&2
  echo "and all local test captures stored in it. This cannot be undone." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export CAPTURE_SERVICE_ENV_FILE="${CAPTURE_SERVICE_ENV_FILE:-$ROOT_DIR/.env}"
export CAPTURE_DATA_SOURCE=second-brain-local-data

cd "$ROOT_DIR"

docker compose \
  -f compose.yaml \
  -f compose.local.yaml \
  down \
  --remove-orphans

if docker volume inspect \
  second-brain-local-data \
  >/dev/null 2>&1; then

  docker volume rm \
    second-brain-local-data
fi

docker volume create \
  second-brain-local-data \
  >/dev/null

echo "Local test data deleted. Empty local volume recreated."
echo "Run deploy/local-up.sh to start fresh."
