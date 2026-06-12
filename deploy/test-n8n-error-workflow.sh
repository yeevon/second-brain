#!/usr/bin/env bash
# Local regression script for the SB-113 n8n error workflow.
#
# Usage:
#   TEST_HARNESS_TOKEN=<token> deploy/test-n8n-error-workflow.sh <CAPTURE_ID> <DELIVERY_ATTEMPT>
#
# Prerequisites:
#   - Local stack running (deploy/local-stack-up.sh).
#   - Error Handler imported (deploy/bootstrap-n8n.sh) with Capture Service Token bound.
#   - Error Harness imported and active (deploy/bootstrap-n8n-test-fixtures.sh) with
#     Test Harness Token bound and Error Workflow set to Second Brain - Error Handler.
#   - CAPTURE_SERVICE_INTERNAL_TOKEN available in .env.
#   - Capture must be in an active delivery state (FORWARDING / FORWARDED / CLASSIFYING).
#     Create one: post a harmless Discord note while n8n is stopped, then start n8n.
#   - TEST_HARNESS_TOKEN: the value you bound to the Test Harness Token credential in n8n.
#
# API calls to capture-service run via docker exec inside the backend network.
# Webhook calls to n8n use the published 127.0.0.1:5678 port.
#
# DO NOT run in EC2/staging/production.
set -euo pipefail

CAPTURE_ID="${1:-}"
DELIVERY_ATTEMPT="${2:-}"

if [[ -z "$CAPTURE_ID" || -z "$DELIVERY_ATTEMPT" ]]; then
  echo "Usage: TEST_HARNESS_TOKEN=<token> $(basename "$0") <CAPTURE_ID> <DELIVERY_ATTEMPT>" >&2
  echo "" >&2
  echo "CAPTURE_ID      — e.g. SB-20260611-0001" >&2
  echo "DELIVERY_ATTEMPT — current delivery attempt number (check GET /internal/captures/<id>)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONTAINER="${CONTAINER:-second-brain-n8n}"
N8N_URL="${N8N_URL:-http://127.0.0.1:5678}"
ENV_FILE="${ROOT_DIR}/.env"
ERRORS=0

# ── Load tokens ───────────────────────────────────────────────────────────────

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2; exit 1
fi

CAPTURE_SERVICE_INTERNAL_TOKEN="${CAPTURE_SERVICE_INTERNAL_TOKEN:-}"
if [[ -z "$CAPTURE_SERVICE_INTERNAL_TOKEN" ]]; then
  CAPTURE_SERVICE_INTERNAL_TOKEN="$(grep '^CAPTURE_SERVICE_INTERNAL_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)"
fi
if [[ -z "$CAPTURE_SERVICE_INTERNAL_TOKEN" ]]; then
  echo "CAPTURE_SERVICE_INTERNAL_TOKEN not set and not found in $ENV_FILE" >&2; exit 1
fi

TEST_HARNESS_TOKEN="${TEST_HARNESS_TOKEN:-}"
if [[ -z "$TEST_HARNESS_TOKEN" ]]; then
  echo "TEST_HARNESS_TOKEN is not set." >&2
  echo "Export it before running: TEST_HARNESS_TOKEN=<value> $(basename "$0") ..." >&2
  exit 1
fi

echo "=== SB-113 n8n error workflow regression ==="
echo "capture_id=${CAPTURE_ID}  delivery_attempt=${DELIVERY_ATTEMPT}"
echo ""

# ── Helper: call capture-service from inside the backend network ──────────────
# Uses Node fetch() (built-in since Node 18) instead of curl — curl is not
# guaranteed to be present in the official n8n Docker image.

cs_fetch() {
  # Usage: cs_fetch <url> [method] [body_json]
  local url="$1"
  local method="${2:-GET}"
  local body="${3:-}"
  local token="$CAPTURE_SERVICE_INTERNAL_TOKEN"

  docker exec "$CONTAINER" node -e "
    const opts = {
      method: '${method}',
      headers: {
        'X-Second-Brain-Internal-Token': '${token}',
        'Content-Type': 'application/json',
      },
    };
    $( [[ -n "$body" ]] && printf 'opts.body = %s;' "'$body'" )
    fetch('${url}', opts)
      .then(r => r.text().then(t => { if (!r.ok) process.exit(1); process.stdout.write(t); }))
      .catch(() => process.exit(1));
  " 2>/dev/null
}

# ── Pre-flight: n8n reachable ─────────────────────────────────────────────────
echo "--- Pre-flight: n8n health ---"
n8n_health="$(curl -sf "$N8N_URL/healthz" 2>/dev/null || true)"
if echo "$n8n_health" | grep -qiE '"status":"ok"|"ok"'; then
  echo "  n8n: healthy"
else
  echo "  FAIL: n8n not healthy at $N8N_URL" >&2
  exit 1
fi

# ── Pre-flight: capture-service reachable via backend network ─────────────────
echo "--- Pre-flight: capture-service health ---"
cs_health="$(cs_fetch "http://capture-service:8000/health" 2>/dev/null || true)"
if echo "$cs_health" | grep -q '"ok"'; then
  echo "  capture-service: healthy"
else
  echo "  FAIL: capture-service not reachable from n8n container" >&2
  exit 1
fi

# ── Pre-flight: Error Handler and Error Harness present ───────────────────────
echo "--- Pre-flight: workflows present ---"
docker exec "$CONTAINER" \
  n8n export:workflow --all --output=/tmp/check-workflows.json >/dev/null 2>&1
docker cp "$CONTAINER:/tmp/check-workflows.json" /tmp/check-workflows.json 2>/dev/null
docker exec --user root "$CONTAINER" rm -f /tmp/check-workflows.json

for wf_name in "Second Brain - Error Handler" "Second Brain - Error Harness"; do
  if jq -r '.[].name' /tmp/check-workflows.json 2>/dev/null | grep -qxF "$wf_name"; then
    echo "  ${wf_name}: present"
  else
    echo "  FAIL: '${wf_name}' not found" >&2
    echo "  Run: deploy/bootstrap-n8n.sh (Error Handler) or deploy/bootstrap-n8n-test-fixtures.sh (Harness)" >&2
    ERRORS=$((ERRORS + 1))
  fi
done

if [[ $ERRORS -gt 0 ]]; then
  echo "Pre-flight failed. Aborting." >&2; exit 1
fi

# ── Pre-flight: capture exists and is in an active delivery state ─────────────
echo "--- Pre-flight: capture state ---"
capture_state="$(cs_fetch "http://capture-service:8000/internal/captures/$CAPTURE_ID" 2>/dev/null || true)"
if [[ -z "$capture_state" ]]; then
  echo "  FAIL: capture $CAPTURE_ID not found or capture-service unreachable" >&2
  exit 1
fi

current_ds="$(echo "$capture_state" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("delivery_status",""))' 2>/dev/null || true)"
current_attempt="$(echo "$capture_state" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("delivery_attempts",""))' 2>/dev/null || true)"

echo "  delivery_status=${current_ds}  delivery_attempts=${current_attempt}"

case "$current_ds" in
  FORWARDING|FORWARDED|CLASSIFYING)
    ;;
  *)
    echo "  FAIL: capture must be in FORWARDING, FORWARDED, or CLASSIFYING; got ${current_ds}" >&2
    echo "  Use a capture that has been claimed but not yet completed or failed." >&2
    exit 1
    ;;
esac

if [[ "$current_attempt" != "$DELIVERY_ATTEMPT" ]]; then
  echo "  WARN: provided delivery_attempt=${DELIVERY_ATTEMPT} does not match current ${current_attempt}" >&2
  echo "  Continuing with delivery_attempt=${current_attempt} from the ledger." >&2
  DELIVERY_ATTEMPT="$current_attempt"
fi

echo ""

# ── Test 1: trigger gemini_timeout via harness ────────────────────────────────
echo "--- Test 1: trigger gemini_timeout via harness ---"
curl -sf \
  --request POST \
  --header "Content-Type: application/json" \
  --header "X-Second-Brain-Webhook-Token: $TEST_HARNESS_TOKEN" \
  --data "{\"capture_id\":\"$CAPTURE_ID\",\"delivery_attempt\":$DELIVERY_ATTEMPT,\"test_case\":\"gemini_timeout\"}" \
  "$N8N_URL/webhook/second-brain-error-harness" >/dev/null 2>&1 \
  || { echo "  WARN: harness webhook returned non-200 (execution may still proceed asynchronously)"; }

echo "  harness triggered — waiting 4s for error workflow to complete..."
sleep 4

# ── Test 2: verify RETRY_WAIT ─────────────────────────────────────────────────
echo "--- Test 2: verify RETRY_WAIT state ---"
capture_state="$(cs_fetch "http://capture-service:8000/internal/captures/$CAPTURE_ID" 2>/dev/null || true)"

if echo "$capture_state" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["delivery_status"] == "RETRY_WAIT", f"expected RETRY_WAIT got {d[\"delivery_status\"]}"
assert d["retry_attempts"] >= 1, f"expected retry_attempts >= 1 got {d[\"retry_attempts\"]}"
assert d["next_attempt_at"] is not None, "expected next_attempt_at to be set"
assert d["raw_text"] is not None, "raw_text must not be null"
print(f"  delivery_status=RETRY_WAIT retry_attempts={d[\"retry_attempts\"]} PASS")
' 2>/dev/null; then
  :
else
  echo "  FAIL: capture not in RETRY_WAIT after gemini_timeout" >&2
  echo "  Current state: $capture_state" >&2
  ERRORS=$((ERRORS + 1))
fi

# ── Test 3: replay — idempotency ──────────────────────────────────────────────
echo "--- Test 3: replay gemini_timeout — idempotency ---"
report_replay="$(cs_fetch \
  "http://capture-service:8000/internal/captures/$CAPTURE_ID/delivery/report-workflow-error" \
  "POST" \
  "{\"delivery_attempt\":$DELIVERY_ATTEMPT,\"disposition\":\"retryable\",\"error_type\":\"gemini_timeout\",\"reason_type\":\"workflow_error\",\"workflow_id\":\"test_workflow\",\"workflow_name\":\"second_brain_intake\",\"stage\":\"gemini\"}" \
  2>/dev/null || true)"

if echo "$report_replay" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["outcome"] in ("ignored_retry_already_scheduled", "ignored_already_terminal", "ignored_stale_attempt"), \
    f"unexpected outcome: {d[\"outcome\"]}"
print(f"  idempotency outcome={d[\"outcome\"]} PASS")
' 2>/dev/null; then
  :
else
  echo "  FAIL: replay did not return an ignored outcome" >&2
  echo "  Response: $report_replay" >&2
  ERRORS=$((ERRORS + 1))
fi

# ── Test 4: raw_text preserved ────────────────────────────────────────────────
echo "--- Test 4: raw_text preserved ---"
raw_text="$(cs_fetch "http://capture-service:8000/internal/captures/$CAPTURE_ID" 2>/dev/null \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("raw_text",""))' 2>/dev/null || true)"
if [[ -n "$raw_text" ]]; then
  echo "  raw_text present: PASS"
else
  echo "  FAIL: raw_text missing or null" >&2
  ERRORS=$((ERRORS + 1))
fi

# ── Test 5: orphan error — no capture mutation ────────────────────────────────
echo "--- Test 5: orphan error via harness ---"
curl -sf \
  --request POST \
  --header "Content-Type: application/json" \
  --header "X-Second-Brain-Webhook-Token: $TEST_HARNESS_TOKEN" \
  --data '{"test_case":"orphan_unhandled_exception"}' \
  "$N8N_URL/webhook/second-brain-error-harness" >/dev/null 2>&1 \
  || true
echo "  orphan harness triggered — no capture mutation expected"

# ── Log leak check ────────────────────────────────────────────────────────────
echo "--- Log leak check ---"
sleep 2
if docker logs "$CONTAINER" --since=30s 2>&1 | grep -qi "password\|api_key\|raw_text\|stack\|traceback"; then
  echo "  FAIL: potential secret or raw capture text found in n8n logs" >&2
  ERRORS=$((ERRORS + 1))
else
  echo "  n8n logs clean: PASS"
fi

echo ""
if [[ $ERRORS -eq 0 ]]; then
  echo "All tests passed."
else
  echo "$ERRORS test(s) FAILED." >&2
  exit 1
fi
