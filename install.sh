#!/usr/bin/env bash
# Install the Antigravity Telegram bridge as a systemd --user service.
# Idempotent: safe to re-run.
set -euo pipefail

PLUGIN_DIR="${HOME}/.gemini/extensions/antigravity-telegram-bridge"
RUNTIME_DIR="${HOME}/.antigravity/bridge"
LOG_DIR="${RUNTIME_DIR}/logs"
CHATS_DIR="${RUNTIME_DIR}/chats"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
SERVICE_TEMPLATE="${PLUGIN_DIR}/systemd/antigravity-telegram-bridge.service.template"
SERVICE_TARGET="${SYSTEMD_USER_DIR}/antigravity-telegram-bridge.service"

if [[ "$(realpath "${PWD}")" != "$(realpath "${PLUGIN_DIR}")" ]]; then
    echo "Run this from ${PLUGIN_DIR}" >&2
    exit 2
fi

echo "==> Creating runtime directories"
mkdir -p "${LOG_DIR}" "${RUNTIME_DIR}/runtime" "${CHATS_DIR}" "${SYSTEMD_USER_DIR}"

echo "==> Building Python venv"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required (https://github.com/astral-sh/uv)" >&2
    exit 3
fi
if [[ ! -d .venv ]]; then
    uv venv .venv --python 3.11
fi
uv pip install --quiet -e .

VENV_PYTHON="${PLUGIN_DIR}/.venv/bin/python"

echo "==> Verifying agy CLI is available"
if ! command -v agy >/dev/null 2>&1; then
    echo "WARN: agy CLI not found on PATH — install it before starting the bridge" >&2
fi

echo "==> Verifying agy authentication"
if [[ ! -d "${HOME}/.gemini/antigravity-cli" ]]; then
    echo "WARN: ${HOME}/.gemini/antigravity-cli missing — run \`agy\` interactively once to log in" >&2
fi

echo "==> Rendering systemd unit -> ${SERVICE_TARGET}"
sed \
    -e "s|__HOME__|${HOME}|g" \
    -e "s|__VENV_PYTHON__|${VENV_PYTHON}|g" \
    "${SERVICE_TEMPLATE}" > "${SERVICE_TARGET}"

echo "==> Reloading systemd --user and enabling unit"
systemctl --user daemon-reload
systemctl --user enable antigravity-telegram-bridge.service

echo "==> Installing logrotate config for bridge.log"
if [[ -w /etc/logrotate.d ]]; then
    cp "${PLUGIN_DIR}/systemd/antigravity-telegram-bridge.logrotate" \
        /etc/logrotate.d/antigravity-telegram-bridge
else
    echo "WARN: no write access to /etc/logrotate.d — copy" \
        "${PLUGIN_DIR}/systemd/antigravity-telegram-bridge.logrotate" \
        "there manually (as root) to enable log rotation" >&2
fi

if [[ ! -f "${PLUGIN_DIR}/config.json" ]]; then
    cat <<EOF

==> Next steps:
  1. Copy and edit the config:
       cp ${PLUGIN_DIR}/config.example.json ${PLUGIN_DIR}/config.json
       chmod 600 ${PLUGIN_DIR}/config.json
       \$EDITOR ${PLUGIN_DIR}/config.json

  2. Start the bridge:
       systemctl --user start antigravity-telegram-bridge.service

  3. Or, from inside agy:
       agy -p "use the bridge tool to start"

EOF
else
    echo "==> Existing config.json detected. Start with:"
    echo "    systemctl --user start antigravity-telegram-bridge.service"
fi
