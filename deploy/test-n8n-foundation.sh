#!/usr/bin/env bash
set -euo pipefail

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1" >&2; FAIL=$((FAIL + 1)); }

# ── capture-service health ────────────────────────────────────────────────────

cs_health="$(
  docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
    second-brain-capture-service \
    2>/dev/null || true
)"
if [[ "$cs_health" = "healthy" ]]; then
  pass "capture-service container is healthy"
else
  fail "capture-service container is healthy (status: $cs_health)"
fi

# ── n8n health ────────────────────────────────────────────────────────────────

n8n_health="$(
  docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
    second-brain-n8n \
    2>/dev/null || true
)"
if [[ "$n8n_health" = "healthy" ]]; then
  pass "n8n container is healthy"
else
  fail "n8n container is healthy (status: $n8n_health)"
fi

# ── n8n image tag is pinned ───────────────────────────────────────────────────

n8n_image="$(
  docker inspect \
    --format '{{.Config.Image}}' \
    second-brain-n8n \
    2>/dev/null || true
)"
if echo "$n8n_image" | grep -qE ':1\.[0-9]+\.[0-9]+$' && \
   ! echo "$n8n_image" | grep -qE ':(latest|next)$'; then
  pass "n8n image tag is pinned ($n8n_image)"
else
  fail "n8n image tag is pinned (got: $n8n_image)"
fi

# ── n8n is not running as root ────────────────────────────────────────────────

n8n_user="$(
  docker exec second-brain-n8n id -u 2>/dev/null || echo "unknown"
)"
if [[ "$n8n_user" != "0" && "$n8n_user" != "unknown" ]]; then
  pass "n8n is not running as root (uid=$n8n_user)"
else
  fail "n8n is not running as root (uid=$n8n_user)"
fi

# ── n8n host binding is loopback-only ────────────────────────────────────────

port_bindings="$(
  docker inspect \
    --format '{{json .HostConfig.PortBindings}}' \
    second-brain-n8n \
    2>/dev/null || echo "{}"
)"
if echo "$port_bindings" | grep -q '"HostIp":"127.0.0.1"' && \
   ! echo "$port_bindings" | grep -q '"HostIp":"0.0.0.0"'; then
  pass "n8n host binding is loopback-only"
else
  fail "n8n host binding is loopback-only (bindings: $port_bindings)"
fi

# ── n8n data volume mounted at /home/node/.n8n ────────────────────────────────

n8n_mount="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}' \
    second-brain-n8n \
    2>/dev/null || true
)"
if [[ -n "$n8n_mount" ]]; then
  pass "n8n data volume is mounted at /home/node/.n8n ($n8n_mount)"
else
  fail "n8n data volume is mounted at /home/node/.n8n (no mount found)"
fi

# ── n8n can reach capture-service /health over backend network ───────────────

n8n_reach="$(
  docker exec second-brain-n8n \
    node -e "fetch('http://capture-service:8000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
    2>/dev/null; echo $?
)"
if [[ "$n8n_reach" = "0" ]]; then
  pass "n8n can reach capture-service /health through backend DNS"
else
  fail "n8n can reach capture-service /health through backend DNS"
fi

# ── capture-service has no published ports ────────────────────────────────────

cs_ports="$(
  docker inspect \
    --format '{{json .HostConfig.PortBindings}}' \
    second-brain-capture-service \
    2>/dev/null || echo "{}"
)"
if [[ "$cs_ports" = "{}" || "$cs_ports" = "null" ]]; then
  pass "capture-service has no published ports"
else
  fail "capture-service has no published ports (got: $cs_ports)"
fi

# ── n8n data directory is writable by n8n runtime user ───────────────────────

if docker exec second-brain-n8n \
    sh -c 'touch /home/node/.n8n/.write_test && rm /home/node/.n8n/.write_test' \
    2>/dev/null; then
  pass "n8n data directory is writable by n8n runtime user"
else
  fail "n8n data directory is writable by n8n runtime user"
fi

# ── summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi

echo "n8n foundation local regression passed"
