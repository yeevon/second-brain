#!/usr/bin/env bash
# Stop the local stack. Named volumes are preserved.
# Equivalent to `docker compose down` — kept as a convenience alias.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

docker compose down

echo "capture-service, n8n, and writer-stub stopped. Named volumes preserved."
