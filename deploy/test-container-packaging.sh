#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ERRORS=0

echo "=== Container packaging tests ==="
echo ""

# ── Test 1: Runtime user can execute Python 3.13 ─────────────────────────────
echo "--- Test 1: image runtime user can execute Python 3.13 ---"

python_version="$(
  docker run \
    --rm \
    --user 10001:10001 \
    --entrypoint /app/.venv/bin/python \
    second-brain-capture-service:local \
    --version 2>&1
)"
echo "  $python_version"

if echo "$python_version" | grep -qE "^Python 3\.13\."; then
  echo "  PASS"
else
  echo "  FAIL: expected Python 3.13.x" >&2
  ERRORS=$((ERRORS + 1))
fi

echo ""

# ── Test 2: Container environment overrides desktop env-file values ───────────
echo "--- Test 2: container environment overrides desktop env-file values ---"

TEMP_ENV="$(mktemp)"
trap 'rm -f "$TEMP_ENV"' EXIT

cat > "$TEMP_ENV" << 'ENVEOF'
DISCORD_BOT_TOKEN=fake
DISCORD_GUILD_ID=1
DISCORD_CAPTURE_CHANNEL_ID=2
DISCORD_ALLOWED_USER_ID=3
CAPTURE_SERVICE_INTERNAL_TOKEN=00000000000000000000000000000000
CAPTURE_PROCESSING_MODE=local-full
LEDGER_PATH=.runtime/ledger.sqlite3
CAPTURE_API_HOST=127.0.0.1
CAPTURE_API_PORT=9999
ENVEOF

config="$(
  CAPTURE_SERVICE_ENV_FILE="$TEMP_ENV" \
  CAPTURE_DATA_SOURCE=second-brain-local-data \
  docker compose \
    -f "$ROOT_DIR/compose.yaml" \
    -f "$ROOT_DIR/compose.local.yaml" \
    config
)"

check() {
  local key="$1" expected="$2"
  if echo "$config" | grep -qF "${key}: ${expected}" || \
     echo "$config" | grep -qF "${key}: \"${expected}\""; then
    echo "  PASS: ${key} = ${expected}"
  else
    local actual
    actual="$(echo "$config" | grep "${key}:" | head -1 || echo '(not found)')"
    echo "  FAIL: expected ${key}: ${expected}" >&2
    echo "        got: $actual" >&2
    ERRORS=$((ERRORS + 1))
  fi
}

check "CAPTURE_PROCESSING_MODE" "capture-only"
check "CAPTURE_API_HOST"        "0.0.0.0"
check "CAPTURE_API_PORT"        "8000"
check "LEDGER_PATH"             "/var/lib/second-brain/ledger.sqlite3"

echo ""

# ── Test 3: Running local container invariants ────────────────────────────────
echo "--- Test 3: running local container invariants ---"

export CAPTURE_SERVICE_ENV_FILE="${CAPTURE_SERVICE_ENV_FILE:-$ROOT_DIR/.env}"
export CAPTURE_DATA_SOURCE=second-brain-local-data
export COMPOSE_FILE=compose.yaml:compose.local.yaml

health="$(
  docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
    second-brain-capture-service
)"

if [[ "$health" == "healthy" ]]; then
  echo "  PASS: container health = healthy"
else
  echo "  FAIL: container health = $health" >&2
  ERRORS=$((ERRORS + 1))
fi

port_bindings="$(
  docker inspect \
    --format '{{json .HostConfig.PortBindings}}' \
    second-brain-capture-service
)"

if [[ "$port_bindings" == "{}" || "$port_bindings" == "null" ]]; then
  echo "  PASS: no host ports are published"
else
  echo "  FAIL: host ports appear published: $port_bindings" >&2
  ERRORS=$((ERRORS + 1))
fi

if docker compose exec -T capture-service \
  test -f /var/lib/second-brain/.second-brain-ebs-volume; then
  echo "  PASS: sentinel exists"
else
  echo "  FAIL: sentinel missing" >&2
  ERRORS=$((ERRORS + 1))
fi

if docker compose exec -T capture-service \
  test -f /var/lib/second-brain/ledger.sqlite3; then
  echo "  PASS: ledger exists"
else
  echo "  FAIL: ledger missing" >&2
  ERRORS=$((ERRORS + 1))
fi

if docker compose exec -T capture-service \
  /bin/sh -lc '
    touch /var/lib/second-brain/.write-test
    rm /var/lib/second-brain/.write-test
  '; then
  echo "  PASS: runtime user can write"
else
  echo "  FAIL: runtime user cannot write" >&2
  ERRORS=$((ERRORS + 1))
fi

echo ""

# ── Test 4: Graceful SIGTERM shutdown (docker compose stop) ──────────────────
echo "--- Test 4: graceful SIGTERM shutdown (docker compose stop) ---"

container_state="$(
  docker inspect \
    --format '{{.State.Status}}' \
    second-brain-capture-service 2>/dev/null || echo missing
)"

if [[ "$container_state" != "running" ]]; then
  echo "  SKIP: container not running (state=$container_state); run deploy/local-up.sh first" >&2
  ERRORS=$((ERRORS + 1))
else
  docker compose \
    -f "$ROOT_DIR/compose.yaml" \
    -f "$ROOT_DIR/compose.local.yaml" \
    stop --timeout 15 capture-service

  exit_code="$(
    docker inspect \
      --format '{{.State.ExitCode}}' \
      second-brain-capture-service
  )"

  if [[ "$exit_code" == "0" ]]; then
    echo "  PASS: exit code = 0"
  else
    echo "  FAIL: exit code = $exit_code (expected 0)" >&2
    ERRORS=$((ERRORS + 1))
  fi

  db_state="$(
    docker run \
      --rm \
      -v second-brain-local-data:/var/lib/second-brain \
      second-brain-capture-service:local \
      /app/.venv/bin/python -c "
import sqlite3
con = sqlite3.connect('/var/lib/second-brain/ledger.sqlite3')
rows = con.execute(
    \"SELECT key, value FROM system_state WHERE key IN ('capture_service_state','capture_service_stopped_at')\"
).fetchall()
con.close()
for k, v in rows:
    print(f'{k}={v}')
"
  )"

  if echo "$db_state" | grep -qF "capture_service_state=STOPPED"; then
    echo "  PASS: capture_service_state = STOPPED"
  else
    echo "  FAIL: capture_service_state not STOPPED: $db_state" >&2
    ERRORS=$((ERRORS + 1))
  fi

  if echo "$db_state" | grep -qE "capture_service_stopped_at=20[0-9]{2}-"; then
    echo "  PASS: capture_service_stopped_at populated"
  else
    echo "  FAIL: capture_service_stopped_at not populated: $db_state" >&2
    ERRORS=$((ERRORS + 1))
  fi

  container_logs="$(docker logs second-brain-capture-service 2>&1)"

  if echo "$container_logs" | grep -qF '"event":"capture_service_stopped"'; then
    echo "  PASS: capture_service_stopped log emitted"
  else
    echo "  FAIL: capture_service_stopped log not found" >&2
    ERRORS=$((ERRORS + 1))
  fi

  if echo "$container_logs" | grep -qF '"event":"sqlite_runtime_stopped"'; then
    echo "  PASS: sqlite_runtime_stopped log emitted"
  else
    echo "  FAIL: sqlite_runtime_stopped log not found" >&2
    ERRORS=$((ERRORS + 1))
  fi
fi

echo ""

# ── Result ────────────────────────────────────────────────────────────────────
if [[ $ERRORS -eq 0 ]]; then
  echo "All container packaging tests passed."
else
  echo "$ERRORS test(s) failed." >&2
  exit 1
fi
