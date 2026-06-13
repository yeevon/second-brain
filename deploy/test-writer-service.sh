#!/usr/bin/env bash
# Local regression script for writer-service.
# Verifies the writer-service container is healthy and can accept filing requests.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WRITER_SERVICE_CONTAINER="${WRITER_SERVICE_CONTAINER:-second-brain-writer-service}"
CAPTURE_SERVICE_CONTAINER="${CAPTURE_SERVICE_CONTAINER:-second-brain-capture-service}"
ENV_FILE="${ROOT_DIR}/.env"

PASS=0
FAIL=0

_pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $1" >&2; FAIL=$((FAIL + 1)); }

echo "=== writer-service regression ==="
echo ""

# ── Container health ──────────────────────────────────────────────────────────

echo "--- Container health ---"

health="$(
  docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
    "$WRITER_SERVICE_CONTAINER" 2>/dev/null || echo "not_found"
)"
if [[ "$health" == "healthy" ]]; then
  _pass "writer-service container is healthy"
else
  _fail "writer-service container health: $health"
fi

# ── Vault volume ──────────────────────────────────────────────────────────────

echo ""
echo "--- Vault volume ---"

vault_mount="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/opt/vault"}}{{.Source}}{{end}}{{end}}' \
    "$WRITER_SERVICE_CONTAINER" 2>/dev/null || true
)"
if [[ -n "$vault_mount" ]]; then
  _pass "vault volume is mounted at /opt/vault (source: $vault_mount)"
else
  _fail "vault volume not mounted at /opt/vault"
fi

# ── Load token (without printing it) ─────────────────────────────────────────

WRITER_TOKEN="${WRITER_SERVICE_TOKEN:-}"
if [[ -z "$WRITER_TOKEN" ]] && [[ -f "$ENV_FILE" ]]; then
  WRITER_TOKEN="$(grep '^WRITER_SERVICE_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"
fi
if [[ -z "$WRITER_TOKEN" ]]; then
  WRITER_TOKEN="dev-writer-service-token-change-me"
fi

INTERNAL_TOKEN=""
if [[ -f "$ENV_FILE" ]]; then
  INTERNAL_TOKEN="$(grep '^CAPTURE_SERVICE_INTERNAL_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"
fi

# ── Health endpoint ───────────────────────────────────────────────────────────

echo ""
echo "--- Health endpoint ---"

health_resp="$(
  docker exec "$WRITER_SERVICE_CONTAINER" \
    python3 -c "
import urllib.request, json
resp = urllib.request.urlopen('http://127.0.0.1:8001/health')
print(json.loads(resp.read())['status'])
" 2>/dev/null || echo "error"
)"
if [[ "$health_resp" == "ok" ]]; then
  _pass "GET /health returns 200 with status=ok"
else
  _fail "GET /health failed: $health_resp"
fi

# ── File a test note ──────────────────────────────────────────────────────────

echo ""
echo "--- Filing test ---"

TEST_CAPTURE_ID="SB-$(date -u +%Y%m%d)-$(date -u +%M%S)"
FILING_RESULT="$(
  docker exec \
    -e WRITER_TOKEN="$WRITER_TOKEN" \
    "$WRITER_SERVICE_CONTAINER" \
    python3 -c "
import urllib.request, json, os, sys
token = os.environ.get('WRITER_TOKEN', '')
payload = {
  'capture_id': '$TEST_CAPTURE_ID',
  'source_message_id': '999888777666555444',
  'created_at': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
  'delivery_attempt': 1,
  'model': 'gemini-3.5-flash',
  'prompt_version': 'classifier-v1',
  'classification': {
    'folder': 'projects',
    'project': 'regression-test',
    'note_type': 'note',
    'title': 'Regression test note',
    'tags': ['regression'],
    'body': 'Automated regression test.',
    'actions': [],
    'needs_clarification': False,
    'clarifying_question': None,
    'confidence': 0.95
  },
  'inbox_reason': None
}
data = json.dumps(payload).encode()
req = urllib.request.Request(
  'http://127.0.0.1:8001/internal/notes/file',
  data=data,
  headers={'Content-Type': 'application/json', 'X-Second-Brain-Writer-Token': token},
  method='POST'
)
resp = urllib.request.urlopen(req)
result = json.loads(resp.read())
print(json.dumps(result))
" 2>/dev/null || echo "{}"
)"

NOTE_PATH="$(echo "$FILING_RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('note_path',''))" 2>/dev/null || true)"
RESULT_VALUE="$(echo "$FILING_RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('result',''))" 2>/dev/null || true)"

if [[ "$RESULT_VALUE" == "FILED" ]]; then
  _pass "POST /internal/notes/file returns FILED result"
else
  _fail "POST /internal/notes/file failed: $FILING_RESULT"
fi

if [[ -n "$NOTE_PATH" ]] && [[ "$NOTE_PATH" != stub://* ]]; then
  _pass "returned note_path is a real filesystem path (not stub://)"
else
  _fail "note_path is missing or is a stub:// path: $NOTE_PATH"
fi

# ── Verify note file exists in vault ─────────────────────────────────────────

if [[ -n "$NOTE_PATH" ]]; then
  FILE_EXISTS="$(
    docker exec "$WRITER_SERVICE_CONTAINER" \
      python3 -c "
import os
path = '/opt/vault/$NOTE_PATH'
print('yes' if os.path.isfile(path) else 'no')
" 2>/dev/null || echo "no"
  )"
  if [[ "$FILE_EXISTS" == "yes" ]]; then
    _pass "note file exists in vault at $NOTE_PATH"
  else
    _fail "note file not found in vault at $NOTE_PATH"
  fi
fi

# ── Audit log ─────────────────────────────────────────────────────────────────

AUDIT_HAS_EVENT="$(
  docker exec "$WRITER_SERVICE_CONTAINER" \
    python3 -c "
import json, os
log_path = '/opt/vault/99_log/events.ndjson'
if not os.path.exists(log_path):
    print('no_log')
else:
    lines = open(log_path).readlines()
    events = [json.loads(l) for l in lines if l.strip()]
    matching = [e for e in events if e.get('capture_id') == '$TEST_CAPTURE_ID']
    print('yes' if matching else 'no')
" 2>/dev/null || echo "no"
)"
if [[ "$AUDIT_HAS_EVENT" == "yes" ]]; then
  _pass "audit event appended in 99_log/events.ndjson"
else
  _fail "audit event not found for $TEST_CAPTURE_ID: $AUDIT_HAS_EVENT"
fi

# ── Idempotent replay ─────────────────────────────────────────────────────────

echo ""
echo "--- Idempotency ---"

REPLAY_RESULT="$(
  docker exec \
    -e WRITER_TOKEN="$WRITER_TOKEN" \
    "$WRITER_SERVICE_CONTAINER" \
    python3 -c "
import urllib.request, json, os
token = os.environ.get('WRITER_TOKEN', '')
payload = {
  'capture_id': '$TEST_CAPTURE_ID',
  'source_message_id': '999888777666555444',
  'created_at': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
  'delivery_attempt': 2,
  'model': 'gemini-3.5-flash',
  'prompt_version': 'classifier-v1',
  'classification': {
    'folder': 'projects',
    'project': 'regression-test',
    'note_type': 'note',
    'title': 'Regression test note',
    'tags': ['regression'],
    'body': 'Automated regression test.',
    'actions': [],
    'needs_clarification': False,
    'clarifying_question': None,
    'confidence': 0.95
  },
  'inbox_reason': None
}
data = json.dumps(payload).encode()
req = urllib.request.Request(
  'http://127.0.0.1:8001/internal/notes/file',
  data=data,
  headers={'Content-Type': 'application/json', 'X-Second-Brain-Writer-Token': token},
  method='POST'
)
resp = urllib.request.urlopen(req)
result = json.loads(resp.read())
print(json.dumps(result))
" 2>/dev/null || echo "{}"
)"

IDEMPOTENT_VALUE="$(echo "$REPLAY_RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('idempotent',''))" 2>/dev/null || true)"
if [[ "$IDEMPOTENT_VALUE" == "True" ]] || [[ "$IDEMPOTENT_VALUE" == "true" ]]; then
  _pass "idempotent replay returns idempotent=true"
else
  _fail "idempotent replay did not return idempotent=true: $REPLAY_RESULT"
fi

# ── Workflow fixture ──────────────────────────────────────────────────────────

echo ""
echo "--- Workflow fixture ---"

if grep -q "writer-service:8001/internal/notes/file" "$ROOT_DIR/n8n/workflows/second-brain-intake.json"; then
  _pass "n8n intake workflow references writer-service not writer-stub"
else
  _fail "n8n intake workflow does not reference writer-service"
fi

if ! grep -q "writer-stub" "$ROOT_DIR/n8n/workflows/second-brain-intake.json"; then
  _pass "n8n intake workflow does not reference writer-stub"
else
  _fail "n8n intake workflow still references writer-stub"
fi

# ── Compose config ────────────────────────────────────────────────────────────

echo ""
echo "--- Compose config ---"

if grep -q "writer-service" "$ROOT_DIR/compose.override.yaml" && ! grep -q "writer-stub" "$ROOT_DIR/compose.override.yaml"; then
  _pass "compose.override.yaml starts writer-service (not writer-stub)"
else
  _fail "compose.override.yaml missing writer-service or still has writer-stub"
fi

if grep -q "writer-service" "$ROOT_DIR/deploy/local-stack-up.sh" && ! grep -q "writer-stub" "$ROOT_DIR/deploy/local-stack-up.sh"; then
  _pass "deploy/local-stack-up.sh starts writer-service (not writer-stub)"
else
  _fail "local-stack-up.sh missing writer-service or still has writer-stub"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
