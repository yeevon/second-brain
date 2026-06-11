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
export COMPOSE_FILE=compose.yaml

cd "$APP_DIR"

docker compose config >/dev/null
docker compose build
docker compose up -d
docker compose ps
