#!/usr/bin/env bash
# One-time local stack startup.
# After the first successful run, plain `docker compose up -d` works directly.
#
# This script:
#   1. Validates required local env files are present.
#   2. Builds service images.
#   3. Starts all services (compose.override.yaml is auto-loaded).
#   4. Waits for long-running containers to become healthy.
#   5. Waits for one-shot init containers (local-vault-init, local-n8n-init) to exit 0.
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

docker compose up -d

echo "Waiting for long-running containers to become healthy..."

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

echo "Verifying local writer-service Git vault..."
docker exec second-brain-writer-service sh -lc '
set -e
test "$(printenv GIT_SYNC_ENABLED)" = "true"
git -C /opt/vault rev-parse --is-inside-work-tree >/dev/null
git -C /opt/vault remote get-url origin >/dev/null
grep -qxF ".writer.lock" /opt/vault/.gitignore
git -C /opt/vault status --porcelain >/dev/null
'
echo "writer-service local Git vault is ready"

echo "Waiting for init containers to exit..."

for container in second-brain-local-vault-init second-brain-local-n8n-init; do
  state=""
  for _ in $(seq 1 90); do
    state="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)"
    if [[ "$state" = "exited" ]]; then
      break
    fi
    sleep 2
  done
  if [[ "$state" != "exited" ]]; then
    echo "$container did not finish (state=$state)" >&2
    docker logs "$container" >&2 || true
    exit 1
  fi
  exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$container" 2>/dev/null || true)"
  if [[ "$exit_code" != "0" ]]; then
    echo "$container exited with code $exit_code" >&2
    docker logs "$container" >&2 || true
    exit 1
  fi
  echo "$container exited 0"
done

echo ""
echo "Stack is ready. Open the n8n editor at http://127.0.0.1:5678"
echo "  Admin login: \${N8N_LOCAL_EMAIL:-admin@second-brain.local}"
echo "  Intake webhook: POST http://127.0.0.1:5678/webhook/second-brain-intake"
