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

echo "Docker installed. Log out and back in for docker group membership to refresh."
echo "Mount the encrypted EBS data volume at /opt/second-brain/data before starting the service."
