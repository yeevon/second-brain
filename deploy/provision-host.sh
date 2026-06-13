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

sudo mkdir -p /opt/second-brain/vault/99_log
sudo chown -R 10003:10003 /opt/second-brain/vault

# ── Vault Git clone ───────────────────────────────────────────────────────────
# Only clone if the vault directory does not already contain a .git folder.
VAULT_REMOTE="${VAULT_REMOTE:-}"
if [[ -n "$VAULT_REMOTE" ]]; then
  if [[ ! -d /opt/second-brain/vault/.git ]]; then
    sudo -u "$DEPLOY_USER" git clone "$VAULT_REMOTE" /opt/second-brain/vault
    git -C /opt/second-brain/vault config user.name "Second Brain Writer"
    git -C /opt/second-brain/vault config user.email "writer@second-brain.local"
  else
    echo "Vault git clone already exists, verifying remote..."
    actual_remote="$(git -C /opt/second-brain/vault remote get-url origin 2>/dev/null || true)"
    if [[ "$actual_remote" != "$VAULT_REMOTE" ]]; then
      echo "WARNING: vault remote $actual_remote does not match expected $VAULT_REMOTE" >&2
    fi
  fi
  sudo chown -R 10003:10003 /opt/second-brain/vault
fi

echo "Docker installed. Log out and back in for docker group membership to refresh."
echo "Mount the encrypted EBS data volume at /opt/second-brain/data before starting the service."
