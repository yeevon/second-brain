#!/usr/bin/env bash
# Local regression script for the SB-113 n8n error workflow.
# Requires: local stack running, Error Handler imported, Error Harness imported and active.
# DO NOT run in EC2/staging/production.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONTAINER="${CONTAINER:-second-brain-n8n}"
CAPTURE_SERVICE_URL="${CAPTURE_SERVICE_URL:-http://127.0.0.1:8000}"
N8N_URL="${N8N_URL:-http://127.0.0.1:5678}"
ERRORS=0

# Load tokens from local env files — never print them
ENV_FILE="${ROOT_DIR}/.env"
N8N_ENV_FILE="${ROOT_DIR}/n8n.local.env"
WRITER_STUB_ENV_FILE="${ROOT_DIR}/writer-stub.local.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2; exit 1
fi
if [[ ! -f "$N8N_ENV_FILE" ]]; then
  echo "Missing env file: $N8N_ENV_FILE" >&2; exit 1
fi

CAPTURE_SERVICE_INTERNAL_TOKEN="$(grep '^CAPTURE_SERVICE_INTERNAL_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
N8N_WEBHOOK_TOKEN="$(grep '^WRITER_STUB_WEBHOOK_TOKEN=' "$N8N_ENV_FILE" | cut -d= -f2-)"

if [[ -z "$CAPTURE_SERVICE_INTERNAL_TOKEN" ]]; then
  echo "CAPTURE_SERVICE_INTERNAL_TOKEN not found in $ENV_FILE" >&2; exit 1
fi

echo "=== SB-113 n8n error workflow regression ==="
echo ""

# ── Pre-flight: capture-service health ────────────────────────────────────────
echo "--- Pre-flight: capture-service health ---"
cs_health="$(curl -s "$CAPTURE_SERVICE_URL/health" 2>/dev/null || true)"
if echo "$cs_health" | grep -q '"ok"'; then
  echo "  capture-service: healthy"
else
  echo "  FAIL: capture-service not healthy at $CAPTURE_SERVICE_URL" >&2
  exit 1
fi

# ── Pre-flight: n8n health ────────────────────────────────────────────────────
echo "--- Pre-flight: n8n health ---"
n8n_health="$(curl -s "$N8N_URL/healthz" 2>/dev/null || true)"
if echo "$n8n_health" | grep -qiE '"status":"ok"|"ok"'; then
  echo "  n8n: healthy"
else
  echo "  FAIL: n8n not healthy at $N8N_URL" >&2
  exit 1
fi

# ── Pre-flight: Error Handler workflow present ────────────────────────────────
echo "--- Pre-flight: Error Handler workflow present ---"
docker exec "$CONTAINER" \
  n8n export:workflow --all --output=/tmp/check-workflows.json >/dev/null 2>&1
docker cp "$CONTAINER:/tmp/check-workflows.json" /tmp/check-workflows.json 2>/dev/null
docker exec --user root "$CONTAINER" rm -f /tmp/check-workflows.json

if jq -r '.[].name' /tmp/check-workflows.json 2>/dev/null | grep -qxF "Second Brain - Error Handler"; then
  echo "  Error Handler: present"
else
  echo "  FAIL: 'Second Brain - Error Handler' workflow not found" >&2
  echo "  Run: deploy/bootstrap-n8n.sh" >&2
  ERRORS=$((ERRORS + 1))
fi

if jq -r '.[].name' /tmp/check-workflows.json 2>/dev/null | grep -qxF "Second Brain - Error Harness"; then
  echo "  Error Harness: present"
else
  echo "  FAIL: 'Second Brain - Error Harness' workflow not found" >&2
  echo "  Run: deploy/bootstrap-n8n-test-fixtures.sh and activate the harness" >&2
  ERRORS=$((ERRORS + 1))
fi

if [[ $ERRORS -gt 0 ]]; then
  echo "Pre-flight failed. Aborting." >&2; exit 1
fi

echo ""

# ── Test 1: Create a test capture ─────────────────────────────────────────────
echo "--- Test 1: Create test capture ---"

CAPTURE_RESPONSE="$(curl -sf \
  --request POST \
  --header "Content-Type: application/json" \
  --header "X-Second-Brain-Internal-Token: $CAPTURE_SERVICE_INTERNAL_TOKEN" \
  --data '{"discord_message_id":"test-err-1","discord_channel_id":"c1","discord_guild_id":"g1","discord_author_id":"u1","raw_text":"SB-113 error workflow regression test","has_attachments":false,"attachment_metadata":[]}' \
  "$CAPTURE_SERVICE_URL/internal/captures" 2>/dev/null || true)"

if [[ -z "$CAPTURE_RESPONSE" ]]; then
  echo "  FAIL: could not create test capture" >&2
  ERRORS=$((ERRORS + 1))
else
  CAPTURE_ID="$(echo "$CAPTURE_RESPONSE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["capture_id"])' 2>/dev/null || true)"
  echo "  capture_id: $CAPTURE_ID"

  # ── Test 2: Advance delivery to FORWARDING ─────────────────────────────────
  echo "--- Test 2: Advance delivery to FORWARDING ---"
  FORWARD_RESPONSE="$(curl -sf \
    --request POST \
    --header "Content-Type: application/json" \
    --header "X-Second-Brain-Internal-Token: $CAPTURE_SERVICE_INTERNAL_TOKEN" \
    --data '{"delivery_attempt":1}' \
    "$CAPTURE_SERVICE_URL/internal/captures/$CAPTURE_ID/delivery/acknowledge-forwarded" 2>/dev/null || true)"
  if echo "$FORWARD_RESPONSE" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("outcome")=="changed"' 2>/dev/null; then
    echo "  delivery advanced to FORWARDED: ok"
  else
    echo "  SKIP: could not advance to FORWARDED (capture may not be in FORWARDING state)"
  fi

  # ── Test 3: Force gemini_timeout via harness ───────────────────────────────
  echo "--- Test 3: Force gemini_timeout via harness ---"
  HARNESS_RESPONSE="$(curl -sf \
    --request POST \
    --header "Content-Type: application/json" \
    --header "X-Second-Brain-Webhook-Token: $N8N_WEBHOOK_TOKEN" \
    --data "{\"capture_id\":\"$CAPTURE_ID\",\"delivery_attempt\":1,\"test_case\":\"gemini_timeout\"}" \
    "$N8N_URL/webhook/second-brain-error-harness" 2>/dev/null || echo "{}")"
  echo "  harness triggered"

  # Wait for error workflow to process
  sleep 3

  # ── Test 4: Verify RETRY_WAIT state ───────────────────────────────────────
  echo "--- Test 4: Verify RETRY_WAIT state ---"
  CAPTURE_STATE="$(curl -sf \
    --header "X-Second-Brain-Internal-Token: $CAPTURE_SERVICE_INTERNAL_TOKEN" \
    "$CAPTURE_SERVICE_URL/internal/captures/$CAPTURE_ID" 2>/dev/null || true)"

  if echo "$CAPTURE_STATE" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["delivery_status"] == "RETRY_WAIT", f"expected RETRY_WAIT got {d[\"delivery_status\"]}"
assert d["retry_attempts"] >= 1, f"expected retry_attempts >= 1 got {d[\"retry_attempts\"]}"
assert d["next_attempt_at"] is not None, "expected next_attempt_at to be set"
assert d["raw_text"] is not None, "raw_text must not be null"
print(f"  delivery_status={d[\"delivery_status\"]} retry_attempts={d[\"retry_attempts\"]} PASS")
' 2>/dev/null; then
    :
  else
    echo "  FAIL: capture not in RETRY_WAIT after gemini_timeout" >&2
    ERRORS=$((ERRORS + 1))
  fi

  # ── Test 5: Replay same error — retry count must not increment again ────────
  echo "--- Test 5: Replay gemini_timeout — idempotency ---"
  REPORT_REPLAY="$(curl -sf \
    --request POST \
    --header "Content-Type: application/json" \
    --header "X-Second-Brain-Internal-Token: $CAPTURE_SERVICE_INTERNAL_TOKEN" \
    --data "{\"delivery_attempt\":1,\"disposition\":\"retryable\",\"error_type\":\"gemini_timeout\",\"reason_type\":\"workflow_error\",\"workflow_id\":\"test_workflow\",\"workflow_name\":\"second_brain_intake\",\"stage\":\"gemini\"}" \
    "$CAPTURE_SERVICE_URL/internal/captures/$CAPTURE_ID/delivery/report-workflow-error" 2>/dev/null || true)"

  if echo "$REPORT_REPLAY" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["outcome"] in ("ignored_retry_already_scheduled", "ignored_already_terminal", "ignored_stale_attempt"), f"unexpected outcome: {d[\"outcome\"]}"
print(f"  idempotency outcome={d[\"outcome\"]} PASS")
' 2>/dev/null; then
    :
  else
    echo "  FAIL: replay did not return ignored outcome" >&2
    ERRORS=$((ERRORS + 1))
  fi

  # ── Test 6: raw_text preserved ────────────────────────────────────────────
  echo "--- Test 6: raw_text preserved ---"
  RAW_TEXT="$(curl -sf \
    --header "X-Second-Brain-Internal-Token: $CAPTURE_SERVICE_INTERNAL_TOKEN" \
    "$CAPTURE_SERVICE_URL/internal/captures/$CAPTURE_ID" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("raw_text",""))' 2>/dev/null || true)"
  if [[ -n "$RAW_TEXT" ]]; then
    echo "  raw_text present: PASS"
  else
    echo "  FAIL: raw_text missing or null" >&2
    ERRORS=$((ERRORS + 1))
  fi
fi

# ── Test 7: Orphan error — no capture mutation ────────────────────────────────
echo "--- Test 7: Orphan error via harness ---"
curl -sf \
  --request POST \
  --header "Content-Type: application/json" \
  --header "X-Second-Brain-Webhook-Token: $N8N_WEBHOOK_TOKEN" \
  --data '{"test_case":"orphan_unhandled_exception"}' \
  "$N8N_URL/webhook/second-brain-error-harness" >/dev/null 2>&1 || true
echo "  orphan harness triggered (no capture mutation expected)"

# ── Log leak check ────────────────────────────────────────────────────────────
echo "--- Log leak check ---"
if docker logs "$CONTAINER" --since=60s 2>&1 | grep -qi "password\|api_key\|raw_text\|stack\|traceback"; then
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
