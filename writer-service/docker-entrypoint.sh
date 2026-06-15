#!/bin/sh
set -eu

RUNTIME_UID="${LOCAL_UID:-10003}"
RUNTIME_GID="${LOCAL_GID:-10003}"
RUNTIME_USER="${WRITER_RUNTIME_USER:-writerservice}"
RUNTIME_HOME="${WRITER_RUNTIME_HOME:-/home/${RUNTIME_USER}}"

if ! getent group "${RUNTIME_GID}" >/dev/null 2>&1; then
  groupadd --gid "${RUNTIME_GID}" "${RUNTIME_USER}"
fi

if ! getent passwd "${RUNTIME_UID}" >/dev/null 2>&1; then
  useradd \
    --uid "${RUNTIME_UID}" \
    --gid "${RUNTIME_GID}" \
    --home-dir "${RUNTIME_HOME}" \
    --create-home \
    --shell /usr/sbin/nologin \
    "${RUNTIME_USER}"
fi

mkdir -p "${RUNTIME_HOME}/.ssh"
chmod 700 "${RUNTIME_HOME}/.ssh"

if [ -f /run/secrets/vault_deploy_key ]; then
  cp /run/secrets/vault_deploy_key "${RUNTIME_HOME}/.ssh/id_ed25519"
  chmod 600 "${RUNTIME_HOME}/.ssh/id_ed25519"
fi

if [ -f /run/secrets/github_known_hosts ]; then
  cp /run/secrets/github_known_hosts "${RUNTIME_HOME}/.ssh/known_hosts"
  chmod 644 "${RUNTIME_HOME}/.ssh/known_hosts"
fi

chown -R "${RUNTIME_UID}:${RUNTIME_GID}" "${RUNTIME_HOME}"

export HOME="${RUNTIME_HOME}"
export GIT_SSH_COMMAND="ssh -i ${RUNTIME_HOME}/.ssh/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=${RUNTIME_HOME}/.ssh/known_hosts -o StrictHostKeyChecking=yes"

if [ "${GIT_SYNC_ENABLED:-true}" = "true" ]; then
  if [ ! -f "${RUNTIME_HOME}/.ssh/id_ed25519" ]; then
    echo "ERROR: GIT_SYNC_ENABLED=true but deploy key is missing at ${RUNTIME_HOME}/.ssh/id_ed25519" >&2
    echo "  Set VAULT_DEPLOY_KEY_FILE in .env to point at your GitHub deploy key." >&2
    exit 1
  fi
  if [ ! -f "${RUNTIME_HOME}/.ssh/known_hosts" ]; then
    echo "ERROR: GIT_SYNC_ENABLED=true but known_hosts is missing at ${RUNTIME_HOME}/.ssh/known_hosts" >&2
    echo "  Set GITHUB_KNOWN_HOSTS_FILE in .env or ensure deploy/github_known_hosts exists." >&2
    exit 1
  fi
fi

exec gosu "${RUNTIME_UID}:${RUNTIME_GID}" "$@"
