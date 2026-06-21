#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${CONTAINER:-second-brain-capture-service}"
DATA_DIR="${DATA_DIR:-/opt/second-brain/data}"

docker inspect "$CONTAINER" >/dev/null

running="$(docker inspect --format '{{.State.Running}}' "$CONTAINER")"
if [[ "$running" != "true" ]]; then
  echo "container is not running" >&2
  exit 1
fi

restart_policy="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER")"
if [[ "$restart_policy" != "unless-stopped" ]]; then
  echo "unexpected restart policy: $restart_policy" >&2
  exit 1
fi

user="$(docker inspect --format '{{.Config.User}}' "$CONTAINER")"
if [[ "$user" == "" || "$user" == "0" || "$user" == "root" ]]; then
  echo "container is not configured with a non-root user" >&2
  exit 1
fi

port_bindings="$(
  docker inspect \
    --format '{{json .HostConfig.PortBindings}}' \
    "$CONTAINER"
)"

if [[ "$port_bindings" != "{}" && "$port_bindings" != "null" ]]; then
  echo "host ports appear to be published: $port_bindings" >&2
  exit 1
fi

if ! mountpoint -q "$DATA_DIR"; then
  echo "persistent data volume is not mounted at: $DATA_DIR" >&2
  exit 1
fi

MARKER="$DATA_DIR/.second-brain-ebs-volume"

if [[ ! -f "$MARKER" ]]; then
  echo "persistent EBS marker missing: $MARKER" >&2
  exit 1
fi

mount_source="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/var/lib/second-brain"}}{{.Source}}{{end}}{{end}}' \
    "$CONTAINER"
)"
if [[ "$mount_source" != "$DATA_DIR" ]]; then
  echo "unexpected ledger bind mount source: $mount_source" >&2
  exit 1
fi

if [[ ! -f "$DATA_DIR/ledger.sqlite3" ]]; then
  echo "ledger file missing from persistent data volume" >&2
  exit 1
fi

health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$CONTAINER")"
if [[ "$health" != "healthy" ]]; then
  echo "container health is not healthy: $health" >&2
  exit 1
fi

echo "capture-service deployment checks passed"

# ── n8n checks ────────────────────────────────────────────────────────────────

N8N_CONTAINER="${N8N_CONTAINER:-second-brain-n8n}"
N8N_DATA_DIR="${N8N_DATA_DIR:-/opt/second-brain/data/n8n}"
N8N_ENCRYPTION_KEY_FILE="${N8N_ENCRYPTION_KEY_FILE:-/opt/second-brain/config/n8n-encryption-key}"

docker inspect "$N8N_CONTAINER" >/dev/null

n8n_running="$(docker inspect --format '{{.State.Running}}' "$N8N_CONTAINER")"
if [[ "$n8n_running" != "true" ]]; then
  echo "n8n container is not running" >&2
  exit 1
fi

n8n_restart="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$N8N_CONTAINER")"
if [[ "$n8n_restart" != "unless-stopped" ]]; then
  echo "n8n unexpected restart policy: $n8n_restart" >&2
  exit 1
fi

n8n_user="$(docker inspect --format '{{.Config.User}}' "$N8N_CONTAINER")"
if [[ "$n8n_user" == "0" || "$n8n_user" == "root" ]]; then
  echo "n8n container is running as root" >&2
  exit 1
fi

n8n_image="$(docker inspect --format '{{.Config.Image}}' "$N8N_CONTAINER")"
if echo "$n8n_image" | grep -qE ':(latest|next)$' || ! echo "$n8n_image" | grep -q ':'; then
  echo "n8n image tag is not pinned: $n8n_image" >&2
  exit 1
fi

n8n_ports="$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$N8N_CONTAINER")"
if ! echo "$n8n_ports" | grep -q '"HostIp":"127.0.0.1"'; then
  echo "n8n is not bound to loopback: $n8n_ports" >&2
  exit 1
fi
if echo "$n8n_ports" | grep -q '"HostIp":"0.0.0.0"'; then
  echo "n8n is publicly bound on 0.0.0.0:5678" >&2
  exit 1
fi

n8n_mount_source="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}' \
    "$N8N_CONTAINER"
)"
if [[ "$n8n_mount_source" != "$N8N_DATA_DIR" ]]; then
  echo "unexpected n8n data mount source: $n8n_mount_source (expected $N8N_DATA_DIR)" >&2
  exit 1
fi

if [[ ! -d "$N8N_DATA_DIR" ]]; then
  echo "n8n data directory missing: $N8N_DATA_DIR" >&2
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

key_perms="$(stat -c '%a' "$N8N_ENCRYPTION_KEY_FILE")"
if [[ "$key_perms" != "600" ]]; then
  echo "n8n encryption key file permissions are $key_perms (expected 600)" >&2
  exit 1
fi

n8n_http="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:5678/ || true)"
if [[ "$n8n_http" -lt 100 || "$n8n_http" -ge 500 ]]; then
  echo "n8n not responding on loopback (HTTP $n8n_http)" >&2
  exit 1
fi

n8n_cs_reachable="$(
  docker exec "$N8N_CONTAINER" \
    node -e "fetch('http://capture-service:8000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
    2>/dev/null; echo $?
)"
if [[ "$n8n_cs_reachable" != "0" ]]; then
  echo "n8n cannot reach capture-service /health over backend network" >&2
  exit 1
fi

echo "n8n foundation deployment checks passed"

# ── writer-service Git-sync checks ───────────────────────────────────────────

WRITER_CONTAINER="${WRITER_CONTAINER:-second-brain-writer-service}"
VAULT_DIR="${VAULT_DIR:-/opt/second-brain/vault}"
DEPLOY_KEY_FILE="${VAULT_DEPLOY_KEY_FILE:-/opt/second-brain/config/vault-deploy-key}"
KNOWN_HOSTS_FILE="${GITHUB_KNOWN_HOSTS_FILE:-/opt/second-brain/config/github_known_hosts}"
EXPECTED_VAULT_REMOTE="${VAULT_REMOTE:-}"

docker inspect "$WRITER_CONTAINER" >/dev/null

writer_running="$(docker inspect --format '{{.State.Running}}' "$WRITER_CONTAINER")"
if [[ "$writer_running" != "true" ]]; then
  echo "writer-service container is not running" >&2
  exit 1
fi

writer_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$WRITER_CONTAINER")"
if [[ "$writer_health" != "healthy" ]]; then
  echo "writer-service container health: $writer_health" >&2
  exit 1
fi

writer_port_bindings="$(
  docker inspect \
    --format '{{json .HostConfig.PortBindings}}' \
    "$WRITER_CONTAINER"
)"
if [[ "$writer_port_bindings" != "{}" && "$writer_port_bindings" != "null" ]]; then
  echo "writer-service host ports appear to be published: $writer_port_bindings" >&2
  exit 1
fi

if [[ ! -d "$VAULT_DIR" ]]; then
  echo "vault directory missing: $VAULT_DIR" >&2
  exit 1
fi

if [[ ! -d "$VAULT_DIR/.git" ]]; then
  echo "vault is not a Git repository: $VAULT_DIR/.git missing" >&2
  exit 1
fi

vault_branch="$(docker exec --user 10003:10003 "$WRITER_CONTAINER" sh -lc 'export HOME=/home/writerservice; git -C /opt/vault rev-parse --abbrev-ref HEAD' 2>/dev/null || true)"
if [[ "$vault_branch" != "main" ]]; then
  echo "vault is not on branch main (got: $vault_branch)" >&2
  exit 1
fi

if [[ -n "$EXPECTED_VAULT_REMOTE" ]]; then
  actual_remote="$(docker exec --user 10003:10003 "$WRITER_CONTAINER" sh -lc 'export HOME=/home/writerservice; git -C /opt/vault remote get-url origin' 2>/dev/null || true)"
  if [[ "$actual_remote" != "$EXPECTED_VAULT_REMOTE" ]]; then
    echo "vault remote mismatch: expected $EXPECTED_VAULT_REMOTE, got $actual_remote" >&2
    exit 1
  fi
fi

vault_dirty="$(docker exec --user 10003:10003 "$WRITER_CONTAINER" sh -lc 'export HOME=/home/writerservice; git -C /opt/vault status --porcelain' 2>/dev/null || true)"
if [[ -n "$vault_dirty" ]]; then
  echo "vault working tree is not clean (uncommitted changes detected)" >&2
  exit 1
fi

if [[ ! -f "$DEPLOY_KEY_FILE" ]]; then
  echo "deploy key file missing: $DEPLOY_KEY_FILE" >&2
  exit 1
fi

deploy_key_perms="$(stat -c '%a' "$DEPLOY_KEY_FILE")"
if [[ "$deploy_key_perms" != "600" ]]; then
  echo "deploy key file permissions are $deploy_key_perms (expected 600)" >&2
  exit 1
fi

if [[ ! -f "$KNOWN_HOSTS_FILE" ]]; then
  echo "github_known_hosts file missing: $KNOWN_HOSTS_FILE" >&2
  exit 1
fi

writer_git_ssh="$(docker inspect --format '{{range .Config.Env}}{{.}} {{end}}' "$WRITER_CONTAINER" | tr ' ' '\n' | grep '^GIT_SSH_COMMAND=' || true)"
if [[ -z "$writer_git_ssh" ]]; then
  echo "GIT_SSH_COMMAND is not set in writer-service container" >&2
  exit 1
fi

ls_remote_result="$(
  docker exec --user 10003:10003 "$WRITER_CONTAINER" \
    sh -lc 'export HOME=/home/writerservice; export GIT_SSH_COMMAND="ssh -i /home/writerservice/.ssh/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=/home/writerservice/.ssh/known_hosts -o StrictHostKeyChecking=yes"; git -C /opt/vault ls-remote origin HEAD 2>/dev/null' \
    || true
)"
if ! echo "$ls_remote_result" | grep -qE '^[0-9a-f]{40}'; then
  echo "writer-service cannot reach GitHub via Git (git ls-remote origin HEAD failed)" >&2
  exit 1
fi

if ! grep -qxF '.writer.lock' "$VAULT_DIR/.gitignore" 2>/dev/null; then
  echo "vault .gitignore does not contain '.writer.lock' — writer lock file would be tracked by Git" >&2
  exit 1
fi

echo "writer-service Git-sync checks passed"
