#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--confirm-delete-local-n8n-data" ]]; then
  echo "Usage: $0 --confirm-delete-local-n8n-data" >&2
  echo "" >&2
  echo "This script permanently deletes the second-brain-local-n8n-data named volume" >&2
  echo "and all n8n state stored in it (workflows, credentials, owner account)." >&2
  echo "Capture-service data is not affected. This cannot be undone." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export CAPTURE_SERVICE_ENV_FILE="${CAPTURE_SERVICE_ENV_FILE:-$ROOT_DIR/.env}"
export CAPTURE_DATA_SOURCE=second-brain-local-data
export N8N_IMAGE_TAG="${N8N_IMAGE_TAG:-placeholder}"
export N8N_ENV_FILE="${N8N_ENV_FILE:-$ROOT_DIR/n8n.local.env}"
export N8N_ENCRYPTION_KEY_FILE="${N8N_ENCRYPTION_KEY_FILE:-$ROOT_DIR/n8n-encryption-key.local}"
export N8N_DATA_SOURCE=second-brain-local-n8n-data
export COMPOSE_FILE=compose.yaml:compose.local.yaml:compose.n8n.yaml

cd "$ROOT_DIR"

docker compose stop n8n || true
docker compose rm -f n8n || true

if docker volume inspect second-brain-local-n8n-data >/dev/null 2>&1; then
  docker volume rm second-brain-local-n8n-data
fi

docker volume create second-brain-local-n8n-data >/dev/null

echo "n8n data deleted. Empty n8n volume recreated."
echo "capture-service data is untouched."
echo "Run deploy/local-stack-up.sh to start the full stack."
