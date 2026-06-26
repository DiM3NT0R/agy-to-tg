# kimi-to-im — Telegram bridge for Kimi CLI

**Date:** 2026-04-25
**Status:** Approved (brainstorming)

## Context and motivation

This bridge connects the [Kimi CLI](https://github.com/MoonshotAI/kimi-cli) (`kimi`, v1.39+) to Telegram so a Kimi session can be reached from a phone. The CLI already runs locally; the bridge just adds an inbound surface (a Telegram bot) and a per-chat session map so a Telegram conversation maps cleanly onto a `kimi -S <id>` session.

Design constraints:

1. **Standalone codebase** — no SDKs from other LLM vendors, no NPM dependencies, single Python runtime.
2. **Lives within Kimi's application directory** at `~/.kimi/` rather than alongside other tools' plugins.
3. **Installed as a Kimi plugin** under `~/.kimi/plugins/telegram-bridge/` so it shows up in Kimi's plugin tooling, even though the actual daemon runs as a sibling systemd service (see Architecture below).

## Constraint that shaped the design

Investigation into Kimi's extensibility surface (`~/.local/share/uv/tools/kimi-cli/lib/python3.13/site-packages/kimi_cli/`) produced a hard finding:

- **Skills** (`~/.kimi/skills/`) — markdown injected into the system prompt; non-executable.
- **Plugins** (`~/.kimi/plugins/<name>/plugin.json`) — synchronous tool wrappers, ~120s subprocess timeout, request/response only. Cannot host a long-running process.
- **Hooks** — config.toml-only lifecycle events (PreToolUse, Stop, etc.). Not plugin-writable.
- **Background tasks** — session-scoped only, die with the session.
- **Agents** — prompt + tool compositions, not daemons.

A Telegram bridge is fundamentally a long-running daemon (poll for updates, spawn Kimi per inbound message, reply). **None of Kimi's extension points host that.** Kimi itself uses an external systemd timer for token refresh — same precedent we will follow.

The design therefore splits into two layers: a Kimi-visible plugin façade and a daemon supervised by systemd.

## Architecture

### Layer 1 — Kimi plugin façade

Lives at `~/.kimi/plugins/telegram-bridge/`. Recognized by `kimi plugin list`.

`plugin.json` declares one tool, `bridge`, accepting `action: start | stop | status | logs | setup`. The tool's `command` points at a Python control CLI (`src/control.py`) that simply talks to systemd (or a pid file as fallback) to manage the daemon. The tool itself is short-lived (well under the 120s plugin timeout); it is **not** the daemon.

This satisfies *"installed as a Kimi plugin"*: from inside Kimi the user can say "start the telegram bridge" and Kimi will invoke the `bridge` tool with `{"action": "start"}`.

### Layer 2 — The daemon

A separate long-running Python process, run as `python -m src.daemon`, supervised by `systemctl --user kimi-telegram-bridge.service`. The systemd unit is installed by `install.sh`.

**Inbound message loop:**

1. Telegram `getUpdates` long-poll (timeout=30s) returns updates.
2. Filter: ignore non-text messages; reject senders whose `from.id` is not in `telegram.allowed_user_ids` (and chats not in `telegram.allowed_chat_ids` when that list is non-empty).
3. Map `chat_id → session_id` via persisted state; create a new UUID and persist if first time.
4. `sendChatAction: typing` to the chat.
5. Spawn `kimi --print --output-format stream-json -S <session_id> --quiet --work-dir <kimi.default_workdir> [--model <kimi.model>]` with the user's text piped to stdin.
6. Read stdout line-by-line. Each line is a JSON event from Kimi. Accumulate text from `assistant`-role events' `content` array (text-typed parts only).
7. On stdout EOF: `sendMessage(chat_id, accumulated_text)`. If the reply exceeds 4096 characters (Telegram's limit), split into chunks.
8. Persist new `last_update_id` and `chats` map atomically.

**Errors:** non-zero exit from kimi → reply `⚠️ kimi error: <first 500 chars of stderr>` to the chat, log full stderr at WARN. Network errors talking to Telegram → exponential backoff, never crash the loop.

**Cancellation:** SIGTERM from systemd → finish in-flight kimi run, then exit.

## File layout

```
~/.kimi/plugins/telegram-bridge/        # code + config (the "plugin")
├── plugin.json                          # Kimi plugin manifest
├── config.json                          # user-edited; bot token, allowed users
├── config.example.json                  # template, safe to commit
├── pyproject.toml                       # dep: httpx >= 0.27
├── README.md                            # install + operator guide
├── install.sh                           # uv venv + uv pip install + systemd unit install
├── systemd/
│   └── kimi-telegram-bridge.service.template
└── src/
    ├── __init__.py
    ├── control.py                       # plugin-tool entry: action=start|stop|status|logs|setup
    ├── daemon.py                        # main long-poll loop (entry: python -m src.daemon)
    ├── kimi_runner.py                   # spawn kimi, parse stream-json output to text
    ├── telegram.py                      # getUpdates, sendMessage, sendChatAction
    ├── config.py                        # load/validate config.json
    └── state.py                         # ~/.kimi/bridge/state.json reader/writer

~/.kimi/bridge/                         # runtime data (separate from code)
├── runtime/bridge.pid                   # written by daemon on start (systemd-redundant but useful for control.py fallback)
├── state.json                           # {last_update_id, chats: {chat_id: kimi_session_id}}
└── logs/bridge.log                      # daemon stdout+stderr (systemd journal also receives this)
```

Rationale for the split: `~/.kimi/plugins/telegram-bridge/` is code + configuration that should stay clean for `git` / re-install. `~/.kimi/bridge/` is mutable runtime state, akin to Kimi's own `~/.kimi/sessions/`.

## plugin.json

```json
{
  "name": "telegram-bridge",
  "version": "0.1.0",
  "description": "Bridge Telegram chats to Kimi sessions",
  "config_file": "config.json",
  "tools": [{
    "name": "bridge",
    "description": "Control the kimi telegram bridge daemon (start/stop/status/logs/setup)",
    "command": [".venv/bin/python", "-m", "src.control"],
    "parameters": {
      "type": "object",
      "properties": {
        "action": { "type": "string", "enum": ["start", "stop", "status", "logs", "setup"] },
        "lines":  { "type": "integer", "default": 50 }
      },
      "required": ["action"]
    }
  }]
}
```

`control.py` reads action from stdin (Kimi's plugin-tool calling convention: JSON params on stdin, result on stdout) and dispatches:

- `start` → `systemctl --user start kimi-telegram-bridge.service` and report status.
- `stop` → `systemctl --user stop kimi-telegram-bridge.service`.
- `status` → reports `systemctl is-active`, pid, uptime, last error.
- `logs` → tails `~/.kimi/bridge/logs/bridge.log` (last `lines`, default 50).
- `setup` → checks config.json validity, validates Telegram bot token via `getMe`, runs `kimi --version` to confirm CLI on PATH, prints next steps.

## Configuration

`~/.kimi/plugins/telegram-bridge/config.json`:

```json
{
  "telegram": {
    "bot_token": "<from BotFather>",
    "allowed_user_ids": [123456789],
    "allowed_chat_ids": []
  },
  "kimi": {
    "default_workdir": "/home/YOUR_USER",
    "model": "",
    "agent": "default"
  }
}
```

- `bot_token`: required.
- `allowed_user_ids`: at least one entry required (default-deny). Empty = block everyone.
- `allowed_chat_ids`: optional whitelist; if empty, any chat type from an allowed user is accepted.
- `kimi.model`: empty string means inherit Kimi's own default (`kimi-code/kimi-for-coding` per `~/.kimi/config.toml`).
- `kimi.agent`: passed as `--agent <agent>` to the CLI; defaults to `default`.

The config file is read once at daemon start; SIGHUP triggers a re-read.

## Dependencies

- Python 3.11+ (Kimi itself ships with 3.13 via uv, so this is satisfied).
- **httpx** (single runtime dep) for Telegram HTTP API.
- `uv` for venv creation in `install.sh`.

No Node, no npm packages, no third-party LLM SDKs.

## Install flow

`install.sh` performs:

1. `uv venv .venv` inside the plugin dir.
2. `uv pip install -e .` (or `httpx` directly via pyproject deps).
3. Render `systemd/kimi-telegram-bridge.service.template` with the actual user home + venv path → `~/.config/systemd/user/kimi-telegram-bridge.service`.
4. `systemctl --user daemon-reload && systemctl --user enable kimi-telegram-bridge.service`.
5. Print next steps: edit `config.json`, run `systemctl --user start kimi-telegram-bridge.service` (or invoke from inside Kimi via the `bridge` tool).

The systemd unit (sketch):

```ini
[Unit]
Description=Kimi Telegram Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.kimi/plugins/telegram-bridge
ExecStart=%h/.kimi/plugins/telegram-bridge/.venv/bin/python -m src.daemon
Restart=on-failure
RestartSec=5s
StandardOutput=append:%h/.kimi/bridge/logs/bridge.log
StandardError=append:%h/.kimi/bridge/logs/bridge.log

[Install]
WantedBy=default.target
```

## What's deliberately NOT in v1

- Discord, Feishu, QQ channels — Telegram only.
- Per-token streaming preview — Kimi `stream-json` emits per-turn JSON, not per-token. Buffer the turn, send once.
- Inline permission buttons — Kimi's MCP tool calls do not surface in `--output-format stream-json`. Kimi runs its tools internally with whatever yolo policy the agent has; the bridge sees only the final assistant text.
- Image / multimodal input — Kimi `--print` image handling is unverified; defer to v2.
- Setup wizard with interactive token validation — replaced by edit-config-json + `install.sh`.
- Plan/ask/code mode mapping — Kimi has its own agent system; mapping to a separate mode concept is out of scope.
- Cross-machine sync of `state.json` — single-host only.

## Verification path

End-to-end manual checks after implementation:

1. `cd ~/.kimi/plugins/telegram-bridge && bash install.sh` exits 0; venv exists; systemd unit listed by `systemctl --user list-unit-files | grep kimi-telegram-bridge`.
2. Edit `config.json` with a real bot token and the user's Telegram ID. `systemctl --user start kimi-telegram-bridge.service`.
3. `systemctl --user status kimi-telegram-bridge.service` reports active (running).
4. From the allowed Telegram account: send "hello" to the bot. Within ~10s, typing indicator appears and a Kimi reply arrives.
5. Send a follow-up message from the same chat: confirms session continuity by referencing the prior message. Verify a session directory exists at `~/.kimi/sessions/<uuid>/` matching the state.json `chats[chat_id]`.
6. From a different (non-allowed) Telegram account: send a message → daemon ignores it (verified via `bridge.log`), bot does not reply.
7. From inside Kimi: `kimi -p "use the bridge tool to check status"` → returns active/running.
8. Stop with `systemctl --user stop kimi-telegram-bridge.service`, send another Telegram message → no reply.

## Open questions deferred to implementation

- Exact JSON schema of Kimi `--output-format stream-json`'s assistant events — confirm during implementation by running `echo "say hi" | kimi --print --output-format stream-json --quiet` and inspecting. The accumulator logic is the only place this matters.
- Whether `--quiet` is needed alongside `stream-json` (the explore agent reported text mode is verbose; stream-json should be clean, but verify).
- Behavior on Telegram message edits — v1 ignores edited messages (only fresh ones with new `update_id` are processed).

## Files to create (summary)

| Path | Purpose |
|---|---|
| `~/.kimi/plugins/telegram-bridge/plugin.json` | Kimi plugin manifest |
| `~/.kimi/plugins/telegram-bridge/config.example.json` | Template config |
| `~/.kimi/plugins/telegram-bridge/pyproject.toml` | Python project / deps |
| `~/.kimi/plugins/telegram-bridge/README.md` | Install + ops guide |
| `~/.kimi/plugins/telegram-bridge/install.sh` | One-shot installer |
| `~/.kimi/plugins/telegram-bridge/systemd/kimi-telegram-bridge.service.template` | Systemd unit template |
| `~/.kimi/plugins/telegram-bridge/src/__init__.py` | Package marker |
| `~/.kimi/plugins/telegram-bridge/src/control.py` | Plugin-tool entry |
| `~/.kimi/plugins/telegram-bridge/src/daemon.py` | Long-running daemon |
| `~/.kimi/plugins/telegram-bridge/src/kimi_runner.py` | Spawn + parse Kimi |
| `~/.kimi/plugins/telegram-bridge/src/telegram.py` | Telegram HTTP client |
| `~/.kimi/plugins/telegram-bridge/src/config.py` | Config loader/validator |
| `~/.kimi/plugins/telegram-bridge/src/state.py` | State persistence |

User creates `config.json` from `config.example.json` after install. `~/.kimi/bridge/` is created at first daemon start.
