# 🤖 antigravity-telegram-bridge

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-112%20passed-brightgreen.svg)](./tests)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](./LICENSE)
[![systemd](https://img.shields.io/badge/supervisor-systemd--user-lightgrey.svg)](https://systemd.io/)
[![CLI](https://img.shields.io/badge/backend-Antigravity%20%28agy%29-ff69b4.svg)](https://antigravity.google)

Chat with the [Antigravity CLI](https://antigravity.google) (`agy`) from Telegram.

Forked from [`hah23255/kimi-to-im`](https://github.com/hah23255/kimi-to-im) and adapted to drive Google's `agy` CLI instead of the discontinued `gemini` CLI. Each Telegram chat gets its own `agy` project/working directory so sessions persist across messages while staying isolated from one another.

**Self-hosted · single-user · Python · systemd-supervised · webhook-ready**

---

## What this is

`agy` is a local, agentic coding assistant. This bridge extends it to Telegram so you can:

- Continue a coding conversation from your phone while away from your desk.
- Send text, photos, and documents to `agy` through a Telegram bot.
- Switch models, toggle `code`/`plan` mode, and reset sessions with inline buttons.
- Run the bridge as a `systemd --user` service with health/metrics endpoints.

**Text-first, self-hosted, default-deny.** The bot replies only to Telegram user IDs you explicitly whitelist. There is no cloud component beyond Telegram message transport.

---

## Features

| Feature | Status |
|---|---|
| Telegram ↔ `agy` messaging | ✅ |
| Per-chat project isolation (`--new-project` / `--continue`) | ✅ |
| Inline keyboard control panel (`/settings`, `/model`, `/mode`) | ✅ |
| Photo + document upload support | ✅ |
| Multi-user FIFO turn queue | ✅ |
| Webhook mode with HMAC verification | ✅ |
| Health endpoint (`/health`) and Prometheus metrics (`/metrics`) | ✅ |
| `systemd --user` service with hardening | ✅ |
| Plugin tool for `agy` (`bridge start/stop/status/logs/setup`) | ✅ |
| 112 tests, ~100 % pass | ✅ |

---

## Architecture

```mermaid
flowchart LR
    User([📱 You on Telegram])
    TG[Telegram Bot API]
    Bridge[bridge daemon<br/>Python / systemd --user]
    Agy[agy CLI subprocess<br/>per turn]
    State[(state.json<br/>chat → project dir)]
    Health[health + metrics<br/>:9100]

    User -->|message| TG
    TG -->|long-poll / webhook| Bridge
    Bridge -->|agy -p prompt --continue/--new-project| Agy
    Agy -->|plain text reply| Bridge
    Bridge -->|sendMessage| TG
    TG -->|reply| User
    Bridge <-->|read/write| State
    Bridge -->|/health /metrics| Health
```

---

## Quickstart

You need: Linux with `systemd --user`, Python ≥3.11, [`uv`](https://docs.astral.sh/uv/), a working `agy` CLI on `PATH`, and an authenticated `agy` session.

1. **Get a Telegram bot token** from [@BotFather](https://t.me/BotFather).
2. **Get your Telegram user ID** from [@userinfobot](https://t.me/userinfobot).
3. **Authenticate `agy` once** in a terminal:
   ```bash
   agy
   # complete the browser OAuth flow
   ```
4. **Install the bridge.**
   ```bash
   git clone https://github.com/hah23255/agy-to-im.git \
     ~/.gemini/extensions/antigravity-telegram-bridge
   cd ~/.gemini/extensions/antigravity-telegram-bridge
   ./install.sh
   cp config.example.json config.json
   chmod 600 config.json
   $EDITOR config.json   # fill bot_token + allowed_user_ids
   systemctl --user start antigravity-telegram-bridge.service
   ```

For the full step-by-step guide see [`docs/deployment.md`](docs/deployment.md). Day-to-day operations are in [`docs/operations.md`](docs/operations.md).

---

## Configuration

```json
{
  "telegram": {
    "bot_token": "1234567890:...",
    "allowed_user_ids": [123456789],
    "allowed_chat_ids": []
  },
  "agy": {
    "chats_root": "",
    "default_workdir": "",
    "model": "",
    "mode": "code"
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `telegram.bot_token` | yes | From BotFather |
| `telegram.allowed_user_ids` | yes | Default-deny whitelist |
| `telegram.allowed_chat_ids` | recommended | Restrict to your DM |
| `agy.chats_root` | no | Per-chat dirs; defaults to `~/.antigravity/bridge/chats` |
| `agy.default_workdir` | no | Base cwd for `agy` |
| `agy.model` | no | Empty = `agy` default |
| `agy.mode` | no | `code` (auto) or `plan` (read-only sandbox) |

---

## Commands

### Text commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | Usage help |
| `/status` / `/info` | Session summary |
| `/settings` | Inline control panel |
| `/model` | Pick a model (or `/model <id>` to set directly) |
| `/mode` | Pick `code` or `plan` (or `/mode <code/plan>`) |
| `/reset` | Start a fresh `agy` project for this chat |
| `/image on\|off` | Toggle photo processing |
| `/files` | List recent uploads |
| `/queue` | Queue status |

### Inline keyboard callbacks

The `/settings` panel exposes buttons for:

- **Change model** — radio buttons with config-default fallback
- **Change mode** — `code` vs `plan`
- **Reset session** — equivalent to `/reset`
- **Refresh** — re-render the status panel

---

## Operation

```bash
# Status
systemctl --user status antigravity-telegram-bridge.service

# Logs (journal)
journalctl --user -u antigravity-telegram-bridge.service -n 100 --no-pager

# Logs (file)
tail -f ~/.antigravity/bridge/logs/bridge.log

# Health (port configurable via AGY_BRIDGE_HEALTH_PORT; default 9100)
curl http://127.0.0.1:9100/health

# Metrics
curl http://127.0.0.1:9100/metrics

# Restart
systemctl --user restart antigravity-telegram-bridge.service

# Self-test (config + agy + Telegram token)
echo '{"action":"setup"}' | .venv/bin/python -m src.control | python3 -m json.tool
```

---

## Webhook mode

By default the bridge uses Telegram long-polling. To switch to webhook delivery:

```bash
export CTI_WEBHOOK_URL=https://your.host.example/webhook
export CTI_WEBHOOK_PORT=8080
systemctl --user restart antigravity-telegram-bridge.service
```

The daemon registers the URL with Telegram and starts a local HTTP receiver. Requests are verified with an HMAC-SHA256 secret derived from your bot token.

---

## Development

```bash
uv venv .venv --python 3.11
uv pip install -e ".[dev]"
.venv/bin/pytest -v
```

Run the test suite with coverage (install `pytest-cov` first):

```bash
.venv/bin/pytest --cov=src --cov-report=term-missing
```

---

## Security

- `config.json` is gitignored and should be mode `0600`.
- The bot is default-deny: only whitelisted `allowed_user_ids` are served.
- Per-chat working directories are validated to stay under `chats_root`.
- Model identifiers are regex-validated to prevent argv injection.
- Webhook callbacks are HMAC-verified.
- The systemd unit runs with `NoNewPrivileges=true`, memory limits, and restricted privileges.

See [`SECURITY.md`](SECURITY.md) and [`docs/security-scan.md`](docs/security-scan.md) for audit details.

---

## Project tags / topics

`telegram`, `bot`, `antigravity`, `agy`, `google`, `cli`, `agent`, `coding-assistant`, `self-hosted`, `systemd`, `python`, `asyncio`

---

## License

MIT — see [LICENSE](LICENSE).
