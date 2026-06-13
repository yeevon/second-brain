#!/usr/bin/env bash
# Regression script for writer-service failure handling (SB-116).
# Verifies that injected failures return the correct status codes,
# leave the vault clean, and never delete .git/index.lock.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WRITER_SERVICE_CONTAINER="${WRITER_SERVICE_CONTAINER:-second-brain-writer-service}"
ENV_FILE="${ROOT_DIR}/.env"

PASS=0
FAIL=0

_pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $1" >&2; FAIL=$((FAIL + 1)); }

echo "=== writer-service safe-failure regression ==="
echo ""

# ── Load token ────────────────────────────────────────────────────────────────

WRITER_TOKEN="${WRITER_SERVICE_TOKEN:-}"
if [[ -z "$WRITER_TOKEN" ]] && [[ -f "$ENV_FILE" ]]; then
  WRITER_TOKEN="$(
    grep '^WRITER_SERVICE_TOKEN=' "$ENV_FILE" 2>/dev/null \
      | cut -d= -f2- \
      || true
  )"
fi
if [[ -z "$WRITER_TOKEN" ]]; then
  WRITER_TOKEN="dev-writer-service-token-change-me"
fi

# ── Helper: fire a POST to /internal/notes/file ───────────────────────────────

_file_note() {
  local capture_id="$1"
  docker exec \
    -e WRITER_TOKEN="$WRITER_TOKEN" \
    "$WRITER_SERVICE_CONTAINER" \
    python3 -c "
import urllib.request, json, os
token = os.environ.get('WRITER_TOKEN', '')
payload = {
  'capture_id': '$capture_id',
  'source_message_id': '999888777111222333',
  'created_at': '2026-06-13T10:00:00Z',
  'delivery_attempt': 1,
  'model': 'gemini-3.5-flash',
  'prompt_version': 'classifier-v1',
  'classification': {
    'folder': 'projects',
    'project': 'safe-failure-test',
    'note_type': 'note',
    'title': 'Safe failure regression note',
    'tags': ['test'],
    'body': 'Regression test body.',
    'actions': [],
    'needs_clarification': False,
    'clarifying_question': None,
    'confidence': 0.95,
  },
  'inbox_reason': None,
}
data = json.dumps(payload).encode()
req = urllib.request.Request(
  'http://127.0.0.1:8001/internal/notes/file',
  data=data,
  headers={'Content-Type': 'application/json', 'X-Second-Brain-Writer-Token': token},
  method='POST',
)
try:
  resp = urllib.request.urlopen(req)
  print(resp.status, resp.read().decode())
except urllib.error.HTTPError as e:
  print(e.code, e.read().decode())
" 2>/dev/null || echo "0 {}"
}

# ── Helper: check vault working tree is clean ─────────────────────────────────

_vault_clean() {
  result="$(
    docker exec "$WRITER_SERVICE_CONTAINER" \
      sh -c 'git -C /opt/vault status --porcelain 2>/dev/null' \
      || true
  )"
  [[ -z "$result" ]]
}

# ── Test 1: normal filing succeeds ────────────────────────────────────────────

echo "--- Test 1: normal filing ---"

TEST_ID="SB-$(date -u +%Y%m%d)-$(date -u +%M%S)"
response="$(_file_note "$TEST_ID")"
status_code="$(echo "$response" | awk '{print $1}')"
if [[ "$status_code" == "200" ]]; then
  _pass "normal filing returns HTTP 200"
else
  _fail "normal filing returned HTTP $status_code (expected 200)"
fi

if _vault_clean; then
  _pass "vault working tree clean after normal filing"
else
  _fail "vault working tree not clean after normal filing"
fi

# ── Test 2: inject .git/index.lock → 503, lock survives ──────────────────────

echo ""
echo "--- Test 2: index lock ---"

docker exec "$WRITER_SERVICE_CONTAINER" \
  sh -c 'touch /opt/vault/.git/index.lock' 2>/dev/null || true

TEST_ID_LOCK="SB-$(date -u +%Y%m%d)-$(date -u +%H%M)"
response="$(_file_note "$TEST_ID_LOCK")"
status_code="$(echo "$response" | awk '{print $1}')"
if [[ "$status_code" == "503" ]]; then
  _pass "index lock returns HTTP 503"
else
  _fail "index lock returned HTTP $status_code (expected 503)"
fi

body="$(echo "$response" | cut -d' ' -f2-)"
error_type="$(echo "$body" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error_type',''))" 2>/dev/null || true)"
if [[ "$error_type" == "git_index_locked" ]]; then
  _pass "error_type is git_index_locked"
else
  _fail "error_type is $error_type (expected git_index_locked)"
fi

lock_still_exists="$(
  docker exec "$WRITER_SERVICE_CONTAINER" \
    sh -c 'test -f /opt/vault/.git/index.lock && echo yes || echo no' 2>/dev/null
)"
if [[ "$lock_still_exists" == "yes" ]]; then
  _pass "writer did not delete .git/index.lock"
else
  _fail "writer deleted .git/index.lock (must not auto-delete)"
fi

# Remove the lock before next test
docker exec "$WRITER_SERVICE_CONTAINER" \
  sh -c 'rm -f /opt/vault/.git/index.lock' 2>/dev/null || true

# ── Test 3: vault working tree clean after each failure ───────────────────────

echo ""
echo "--- Test 3: vault clean after index lock failure ---"

if _vault_clean; then
  _pass "vault working tree clean after index lock failure"
else
  _fail "vault working tree dirty after index lock failure"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
