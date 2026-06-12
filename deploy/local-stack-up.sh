#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

N8N_ENV_FILE_DEFAULT="$ROOT_DIR/n8n.local.env"
N8N_KEY_FILE_DEFAULT="$ROOT_DIR/n8n-encryption-key.local"
WRITER_STUB_ENV_FILE_DEFAULT="$ROOT_DIR/writer-stub.local.env"

if [[ ! -f "${N8N_ENV_FILE:-$N8N_ENV_FILE_DEFAULT}" ]]; then
  echo "n8n env file missing: ${N8N_ENV_FILE:-$N8N_ENV_FILE_DEFAULT}" >&2
  echo "Copy deploy/n8n.env.example and fill in any required values." >&2
  exit 1
fi

if [[ ! -f "${N8N_ENCRYPTION_KEY_FILE:-$N8N_KEY_FILE_DEFAULT}" ]]; then
  echo "n8n encryption key file missing: ${N8N_ENCRYPTION_KEY_FILE:-$N8N_KEY_FILE_DEFAULT}" >&2
  echo "Generate with: openssl rand -hex 32 > n8n-encryption-key.local" >&2
  exit 1
fi

if [[ ! -f "${WRITER_STUB_ENV_FILE:-$WRITER_STUB_ENV_FILE_DEFAULT}" ]]; then
  echo "writer-stub env file missing: ${WRITER_STUB_ENV_FILE:-$WRITER_STUB_ENV_FILE_DEFAULT}" >&2
  echo "Copy deploy/writer-stub.env.example and fill in any required values." >&2
  exit 1
fi

export CAPTURE_SERVICE_ENV_FILE="${CAPTURE_SERVICE_ENV_FILE:-$ROOT_DIR/.env}"
export CAPTURE_DATA_SOURCE=second-brain-local-data
export N8N_IMAGE_TAG="${N8N_IMAGE_TAG:?N8N_IMAGE_TAG must be set}"
export N8N_ENV_FILE="${N8N_ENV_FILE:-$N8N_ENV_FILE_DEFAULT}"
export N8N_ENCRYPTION_KEY_FILE="${N8N_ENCRYPTION_KEY_FILE:-$N8N_KEY_FILE_DEFAULT}"
export N8N_DATA_SOURCE=second-brain-local-n8n-data
export WRITER_STUB_ENV_FILE="${WRITER_STUB_ENV_FILE:-$WRITER_STUB_ENV_FILE_DEFAULT}"
export COMPOSE_FILE=compose.yaml:compose.local.yaml:compose.n8n.yaml

cd "$ROOT_DIR"

docker compose -f compose.yaml -f compose.local.yaml build capture-service

docker volume create second-brain-local-data >/dev/null

docker run \
  --rm \
  --user 0:0 \
  --entrypoint /bin/sh \
  -v second-brain-local-data:/var/lib/second-brain \
  second-brain-capture-service:local \
  -lc '
    touch /var/lib/second-brain/.second-brain-ebs-volume
    chown -R 10001:10001 /var/lib/second-brain
    chmod 755 /var/lib/second-brain
  '

docker volume create second-brain-local-n8n-data >/dev/null

docker compose up -d --build capture-service n8n writer-stub

echo "Waiting for containers to become healthy..."

for container in second-brain-capture-service second-brain-n8n second-brain-writer-stub; do
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
echo "writer-stub local container is healthy"
echo ""
echo "Open the n8n editor through an SSH tunnel or at http://127.0.0.1:5678 locally."
echo "Run deploy/bootstrap-n8n.sh after creating the owner account."
