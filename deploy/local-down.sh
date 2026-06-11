#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export CAPTURE_SERVICE_ENV_FILE="${CAPTURE_SERVICE_ENV_FILE:-$ROOT_DIR/.env}"
export CAPTURE_DATA_SOURCE="${CAPTURE_DATA_SOURCE:-second-brain-local-data}"

cd "$ROOT_DIR"

docker compose -f compose.yaml -f compose.local.yaml stop capture-service

echo "capture-service stopped. Named volume preserved."
