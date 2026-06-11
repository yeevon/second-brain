#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export CAPTURE_SERVICE_ENV_FILE="${CAPTURE_SERVICE_ENV_FILE:-$ROOT_DIR/.env}"
export CAPTURE_DATA_SOURCE=second-brain-local-data
export N8N_IMAGE_TAG="${N8N_IMAGE_TAG:-placeholder}"
export N8N_ENV_FILE="${N8N_ENV_FILE:-$ROOT_DIR/n8n.local.env}"
export N8N_ENCRYPTION_KEY_FILE="${N8N_ENCRYPTION_KEY_FILE:-$ROOT_DIR/n8n-encryption-key.local}"
export N8N_DATA_SOURCE=second-brain-local-n8n-data
export COMPOSE_FILE=compose.yaml:compose.local.yaml:compose.n8n.yaml

cd "$ROOT_DIR"

docker compose stop capture-service n8n

echo "capture-service and n8n stopped. Named volumes preserved."
