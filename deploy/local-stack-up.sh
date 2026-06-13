#!/usr/bin/env bash
# One-time local stack startup.
# After the first successful run, plain `docker compose up -d` works directly.
#
# This script:
#   1. Validates required local env files are present.
#   2. Builds the capture-service image.
#   3. Starts all services (compose.override.yaml is auto-loaded).
#   4. Waits for all containers to become healthy.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

N8N_ENV_FILE="${N8N_ENV_FILE:-$ROOT_DIR/n8n.local.env}"
N8N_KEY_FILE="${N8N_ENCRYPTION_KEY_FILE:-$ROOT_DIR/n8n-encryption-key.local}"
export WRITER_VAULT_SOURCE="${WRITER_VAULT_SOURCE:-second-brain-local-vault}"
ENV_FILE="${ROOT_DIR}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "env file missing: $ENV_FILE" >&2
  echo "Copy deploy/capture-service.env.example to .env and fill in the required values." >&2
  exit 1
fi

if [[ ! -f "$N8N_ENV_FILE" ]]; then
  echo "n8n env file missing: $N8N_ENV_FILE" >&2
  echo "Copy deploy/n8n.env.example and fill in any required values." >&2
  exit 1
fi

if [[ ! -f "$N8N_KEY_FILE" ]]; then
  echo "n8n encryption key file missing: $N8N_KEY_FILE" >&2
  echo "Generate with: printf '%s' \"\$(openssl rand -hex 32)\" > n8n-encryption-key.local" >&2
  exit 1
fi

cd "$ROOT_DIR"

docker compose build capture-service writer-service

docker compose up -d capture-service n8n writer-service

echo "Waiting for containers to become healthy..."

for container in second-brain-capture-service second-brain-n8n second-brain-writer-service; do
  health=""
  for _ in $(seq 1 60); do
    health="$(
      docker inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
        "$container" \
        2>/dev/null || true
    )"
    if [[ "$health" = "healthy" ]]; then
      break
    fi
    sleep 2
  done
  if [[ "$health" != "healthy" ]]; then
    echo "$container health: $health" >&2
    exit 1
  fi
done

echo "capture-service local container is healthy"
echo "n8n local container is healthy"
echo "writer-service local container is healthy"
echo ""
echo "Open the n8n editor at http://127.0.0.1:5678"
echo "Run deploy/bootstrap-n8n.sh after creating the owner account."
