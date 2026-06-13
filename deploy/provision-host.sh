#!/usr/bin/env bash
set -euo pipefail

DEPLOY_USER="${SUDO_USER:-ubuntu}"

sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

. /etc/os-release
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo systemctl enable --now docker
sudo usermod -aG docker "$DEPLOY_USER"

sudo mkdir -p /opt/second-brain/app /opt/second-brain/data
sudo install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /opt/second-brain/config
sudo chown -R "$DEPLOY_USER:$DEPLOY_USER" /opt/second-brain/app
sudo chown -R 10001:10001 /opt/second-brain/data

# ── Vault Git clone ───────────────────────────────────────────────────────────
# Order: clone first (vault must not exist), configure Git identity, create
# bootstrap structure, commit, push, then chown to writer UID 10003.
#
# We must NOT pre-create /opt/second-brain/vault — git clone requires the
# destination to be absent or empty.
VAULT_REMOTE="${VAULT_REMOTE:-}"
VAULT_DEPLOY_KEY_FILE="${VAULT_DEPLOY_KEY_FILE:-/opt/second-brain/config/vault-deploy-key}"
GITHUB_KNOWN_HOSTS_FILE="${GITHUB_KNOWN_HOSTS_FILE:-/opt/second-brain/config/github_known_hosts}"

if [[ -n "$VAULT_REMOTE" ]]; then
  if [[ ! -d /opt/second-brain/vault/.git ]]; then
    # Ensure the parent directory exists but the vault itself does not
    sudo mkdir -p /opt/second-brain

    # Clone using the same pinned SSH command the writer-service container uses.
    # StrictHostKeyChecking=yes requires github_known_hosts to be populated first.
    if [[ ! -f "$VAULT_DEPLOY_KEY_FILE" ]]; then
      echo "ERROR: deploy key file missing: $VAULT_DEPLOY_KEY_FILE" >&2
      echo "Generate it with: ssh-keygen -t ed25519 -f $VAULT_DEPLOY_KEY_FILE" >&2
      exit 1
    fi
    if [[ ! -f "$GITHUB_KNOWN_HOSTS_FILE" ]]; then
      echo "ERROR: known_hosts file missing: $GITHUB_KNOWN_HOSTS_FILE" >&2
      echo "Generate it with: ssh-keyscan -H github.com > $GITHUB_KNOWN_HOSTS_FILE" >&2
      exit 1
    fi

    sudo -u "$DEPLOY_USER" \
      GIT_SSH_COMMAND="ssh -i $VAULT_DEPLOY_KEY_FILE -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$GITHUB_KNOWN_HOSTS_FILE -o BatchMode=yes" \
      git clone "$VAULT_REMOTE" /opt/second-brain/vault

    sudo -u "$DEPLOY_USER" \
      git -C /opt/second-brain/vault config user.name "Second Brain Writer"
    sudo -u "$DEPLOY_USER" \
      git -C /opt/second-brain/vault config user.email "writer@second-brain.local"

    # Create bootstrap structure if not already present in the cloned repo.
    if [[ ! -f /opt/second-brain/vault/.gitignore ]]; then
      sudo -u "$DEPLOY_USER" \
        sh -c 'printf ".writer.lock\n" > /opt/second-brain/vault/.gitignore'
    elif ! grep -qxF '.writer.lock' /opt/second-brain/vault/.gitignore; then
      sudo -u "$DEPLOY_USER" \
        sh -c 'printf ".writer.lock\n" >> /opt/second-brain/vault/.gitignore'
    fi

    if [[ ! -d /opt/second-brain/vault/99_log ]]; then
      sudo -u "$DEPLOY_USER" mkdir -p /opt/second-brain/vault/99_log
      sudo -u "$DEPLOY_USER" touch /opt/second-brain/vault/99_log/.gitkeep
    fi

    # Commit bootstrap structure if there are uncommitted changes
    if [[ -n "$(git -C /opt/second-brain/vault status --porcelain 2>/dev/null)" ]]; then
      sudo -u "$DEPLOY_USER" \
        git -C /opt/second-brain/vault add .gitignore 99_log/.gitkeep
      sudo -u "$DEPLOY_USER" \
        git -C /opt/second-brain/vault commit -m "chore: bootstrap vault structure"
      sudo -u "$DEPLOY_USER" \
        GIT_SSH_COMMAND="ssh -i $VAULT_DEPLOY_KEY_FILE -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$GITHUB_KNOWN_HOSTS_FILE -o BatchMode=yes" \
        git -C /opt/second-brain/vault push origin main
    fi

    # Chown to writer UID AFTER cloning so the deploy user could write during clone
    sudo chown -R 10003:10003 /opt/second-brain/vault

  else
    echo "Vault git clone already exists, verifying remote..."
    actual_remote="$(git -C /opt/second-brain/vault remote get-url origin 2>/dev/null || true)"
    if [[ "$actual_remote" != "$VAULT_REMOTE" ]]; then
      echo "WARNING: vault remote $actual_remote does not match expected $VAULT_REMOTE" >&2
    fi
    # Ensure chown is correct even on re-runs
    sudo chown -R 10003:10003 /opt/second-brain/vault
  fi
fi

echo "Docker installed. Log out and back in for docker group membership to refresh."
echo "Mount the encrypted EBS data volume at /opt/second-brain/data before starting the service."
