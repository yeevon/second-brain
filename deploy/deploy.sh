#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/second-brain/app}"
DATA_DIR="${DATA_DIR:-/opt/second-brain/data}"

if ! mountpoint -q "$DATA_DIR"; then
  echo "persistent data volume is not mounted at: $DATA_DIR" >&2
  exit 1
fi

MARKER="$DATA_DIR/.second-brain-ebs-volume"

if [[ ! -f "$MARKER" ]]; then
  echo "persistent EBS marker missing: $MARKER" >&2
  exit 1
fi

export CAPTURE_SERVICE_ENV_FILE="${CAPTURE_SERVICE_ENV_FILE:-/opt/second-brain/config/capture-service.env}"
export CAPTURE_DATA_SOURCE="${CAPTURE_DATA_SOURCE:-$DATA_DIR}"

export N8N_IMAGE_TAG="${N8N_IMAGE_TAG:?N8N_IMAGE_TAG must be set}"
export N8N_ENV_FILE="${N8N_ENV_FILE:-/opt/second-brain/config/n8n.env}"
export N8N_ENCRYPTION_KEY_FILE="${N8N_ENCRYPTION_KEY_FILE:-/opt/second-brain/config/n8n-encryption-key}"
export N8N_DATA_SOURCE="${N8N_DATA_SOURCE:-$DATA_DIR/n8n}"
export WRITER_SERVICE_ENV_FILE="${WRITER_SERVICE_ENV_FILE:-/opt/second-brain/config/writer-service.env}"
export WRITER_VAULT_SOURCE="${WRITER_VAULT_SOURCE:-/opt/second-brain/vault}"
export COMPOSE_FILE=compose.yaml:compose.n8n.yaml

N8N_DATA_DIR="$N8N_DATA_SOURCE"
if [[ ! -d "$N8N_DATA_DIR" ]]; then
  echo "n8n data directory missing: $N8N_DATA_DIR" >&2
  exit 1
fi

if [[ ! -f "$N8N_ENV_FILE" ]]; then
  echo "n8n env file missing: $N8N_ENV_FILE" >&2
  exit 1
fi

if [[ ! -f "$N8N_ENCRYPTION_KEY_FILE" ]]; then
  echo "n8n encryption key file missing: $N8N_ENCRYPTION_KEY_FILE" >&2
  exit 1
fi

if [[ ! -s "$N8N_ENCRYPTION_KEY_FILE" ]]; then
  echo "n8n encryption key file is empty: $N8N_ENCRYPTION_KEY_FILE" >&2
  exit 1
fi

if [[ ! -f "$WRITER_SERVICE_ENV_FILE" ]]; then
  echo "writer-service env file missing: $WRITER_SERVICE_ENV_FILE" >&2
  exit 1
fi

WRITER_SERVICE_TOKEN="$(grep '^WRITER_SERVICE_TOKEN=' "$WRITER_SERVICE_ENV_FILE" | cut -d= -f2-)"
if [[ -z "$WRITER_SERVICE_TOKEN" ]]; then
  echo "WRITER_SERVICE_TOKEN is not set in $WRITER_SERVICE_ENV_FILE" >&2
  exit 1
fi

if [[ ! -d "$WRITER_VAULT_SOURCE" ]]; then
  echo "vault directory missing: $WRITER_VAULT_SOURCE" >&2
  exit 1
fi

if [[ ! -w "$WRITER_VAULT_SOURCE" ]]; then
  echo "vault directory not writable: $WRITER_VAULT_SOURCE" >&2
  exit 1
fi

cd "$APP_DIR"

docker compose config >/dev/null
docker compose build
docker compose up -d
docker compose ps
