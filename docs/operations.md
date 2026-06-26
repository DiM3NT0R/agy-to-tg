# Operations runbook — kimi-to-im

Step-by-step guide for installing, operating, and troubleshooting the Telegram→Kimi bridge. The README has the quick install snippet; this document is the long-form reference.

---

## Contents

1. [Prerequisites](#prerequisites)
2. [Get a Telegram bot token](#get-a-telegram-bot-token)
3. [Find your Telegram user ID](#find-your-telegram-user-id)
4. [Configure the bridge](#configure-the-bridge)
5. [Install](#install)
6. [Start the bridge](#start-the-bridge)
7. [Verify end-to-end](#verify-end-to-end)
8. [Daily ops](#daily-ops)
9. [Troubleshooting](#troubleshooting)
10. [Upgrading](#upgrading)
11. [Uninstall](#uninstall)
12. [Pushing to GitHub](#pushing-to-github)
13. [Architecture refresher](#architecture-refresher)

---

## Prerequisites

| Requirement | Why | How to verify |
|---|---|---|
| Linux with systemd `--user` | Daemon supervisor | `systemctl --user status` returns without error |
| Python ≥ 3.11 | Runtime | `python3 --version` |
| `uv` ≥ 0.4 | Venv + dep management | `uv --version` |
| Kimi CLI v1.39+ on `PATH` | The thing the bridge talks to | `kimi --version` |
| Active Kimi Coding OAuth | For the CLI to actually work | `~/.kimi/credentials/kimi-code.json` exists and is fresh |
| Outbound HTTPS to `api.telegram.org` and `api.kimi.com` | Bot polling + Kimi API | `curl -sSI https://api.telegram.org/` returns 200 |

If `kimi` is not yet installed:

```sh
uv tool install kimi-cli
kimi login   # OAuth device flow
```

If `uv` is not yet installed:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Get a Telegram bot token

1. Open Telegram and start a chat with [`@BotFather`](https://t.me/BotFather).
2. Send `/newbot`.
3. Pick a display name (e.g. *My Kimi*).
4. Pick a username ending in `bot` (e.g. `my_kimi_personal_bot`). Must be unique on Telegram.
5. BotFather replies with a token like `1234567890:ABCDEFghijklmnopqrstuvwxyz_-1234567890`. **Copy this token now**; you'll paste it into `config.json` in the next step. Keep it secret — anyone with the token can post messages as the bot.

Recommended hardening (still inside the BotFather chat):

- `/setprivacy` → choose your bot → `Disable` (lets the bot read all messages in any group it joins; for a personal 1:1 bot this matters less, but the bridge currently only handles 1:1 chats anyway).
- `/setjoingroups` → choose your bot → `Disable` (so the bot can't be dragged into random groups).

---

## Find your Telegram user ID

The bridge is **default-deny**: only user IDs you list in `allowed_user_ids` can talk to the bot. Get yours one of these ways:

**Option A — ask another bot:** message `@userinfobot` on Telegram. It replies with your ID.

**Option B — use this bot, briefly opened:** start the bot once, send any message from your account, and read the daemon log:

```sh
tail -f ~/.kimi/bridge/logs/bridge.log
# Send a message from your phone — the log will say:
# "dropping unauthorized message from user_id=123456789"
```

Add `123456789` to `allowed_user_ids`, restart the bridge, and you're in.

The user ID is a positive integer between 6 and 12 digits.

---

## Configure the bridge

```sh
cd ~/.kimi/plugins/telegram-bridge
cp config.example.json config.json
chmod 600 config.json   # protect the bot token
$EDITOR config.json
```

Fill in:

```json
{
  "telegram": {
    "bot_token": "1234567890:ABCDEFghijklmnopqrstuvwxyz_-1234567890",
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

Field-by-field:

| Field | Required? | Notes |
|---|---|---|
| `telegram.bot_token` | yes | The string from BotFather. Format: `<int>:<base64-ish>`. |
| `telegram.allowed_user_ids` | yes | Non-empty list of int Telegram user IDs. Empty = the bridge refuses to start (default-deny). |
| `telegram.allowed_chat_ids` | optional | If non-empty, restricts which chats accept messages even from allowed users. Useful if your account is in groups you don't want the bot active in. Empty = any private/group chat from an allowed user is OK. |
| `kimi.default_workdir` | optional | The cwd Kimi runs in. Defaults to empty string (Kimi's own default — usually `$HOME`). Set this if you want Kimi to read files from a specific repo. |
| `kimi.model` | optional | Empty = Kimi's default (`kimi-code/kimi-for-coding` per `~/.kimi/config.toml`). Override if you want to pin to a specific model. |
| `kimi.agent` | optional | The Kimi agent profile. `default` is the standard agent; `okabe` is the alternative bundled agent. |

**Never commit `config.json`.** It is gitignored already, but double-check after editing: `git status` should show no changes from the config file.

---

## Install

```sh
cd ~/.kimi/plugins/telegram-bridge   # cd is mandatory
bash install.sh
```

What `install.sh` does (idempotent — safe to re-run):

1. Creates `~/.kimi/bridge/{logs,runtime}/` for runtime data.
2. Creates `~/.config/systemd/user/` if missing.
3. Builds the Python venv at `.venv/` (skipped if already exists, so locally-installed dev tools survive re-runs).
4. Installs the project in editable mode: `pip install -e .`
5. Renders `systemd/kimi-telegram-bridge.service.template` with your `$HOME` and venv-python path, writes the result to `~/.config/systemd/user/kimi-telegram-bridge.service`.
6. Runs `systemctl --user daemon-reload` and `systemctl --user enable kimi-telegram-bridge.service`.

The installer **does not start** the service. That's a separate command (next section).

If `install.sh` exits with code 2: you ran it from outside the plugin directory. `cd` first.
If it exits with code 3: `uv` is not on `PATH`. Install it (link in [Prerequisites](#prerequisites)).

### Optional: enable lingering for headless boot

If this machine is headless and you want the bridge to start on boot without your login, enable lingering:

```sh
sudo loginctl enable-linger $USER
```

Without lingering, the user systemd manager only runs while you have an active login session.

---

## Start the bridge

Three equivalent ways:

```sh
# 1. systemd directly
systemctl --user start kimi-telegram-bridge.service

# 2. Through the Kimi plugin tool, from inside Kimi
kimi -p "use the bridge tool with action=start"

# 3. From any shell, via the same plugin entry point
echo '{"action":"start"}' | ~/.kimi/plugins/telegram-bridge/.venv/bin/python -m src.control
```

Confirm it's up:

```sh
systemctl --user is-active kimi-telegram-bridge.service
# expect: active
```

For the live status with recent log entries:

```sh
systemctl --user status kimi-telegram-bridge.service --no-pager
```

---

## Verify end-to-end

1. From your phone, open the bot in Telegram (search for the username you picked from BotFather).
2. Send `/start` or just `hello`.
3. Within ~10s expect:
   - The "typing…" indicator appears in the chat.
   - A reply from Kimi appears.
4. In a separate terminal:

```sh
tail -n 20 ~/.kimi/bridge/logs/bridge.log
```

You should see one INFO line per turn, e.g.:

```
2026-04-25 17:00:00,123 INFO kimi_telegram_bridge: turn chat=123456789 session=a7b3c8d2 exit=0 ms=2417 reply_len=42
```

5. Send a follow-up like `what did I just say?`. The reply should reference the prior message — proves session continuity. Verify:

```sh
cat ~/.kimi/bridge/state.json
ls ~/.kimi/sessions/
```

The `state.json` will have your chat_id mapped to a session UUID; the same UUID should exist as a directory under `~/.kimi/sessions/`.

6. From a different (non-allowed) Telegram account, send a message to the bot. Expect: no reply. Log shows:

```
INFO kimi_telegram_bridge: dropping unauthorized message from user_id=999999999
```

If all six pass, you're operating cleanly.

---

## Daily ops

### Cheat-sheet

| Action | Command |
|---|---|
| Start | `systemctl --user start kimi-telegram-bridge.service` |
| Stop | `systemctl --user stop kimi-telegram-bridge.service` |
| Restart | `systemctl --user restart kimi-telegram-bridge.service` |
| Status | `systemctl --user status kimi-telegram-bridge.service` |
| Live log | `tail -f ~/.kimi/bridge/logs/bridge.log` |
| Recent journal | `journalctl --user -u kimi-telegram-bridge.service -n 100 --no-pager` |
| Validate config | `echo '{"action":"setup"}' \| .venv/bin/python -m src.control` |

### From inside Kimi

The plugin manifest exposes a `bridge` tool to Kimi sessions, so any of the above can be triggered conversationally:

```sh
kimi -p "use the bridge tool with action=status"
kimi -p "use the bridge tool with action=logs and lines=200"
kimi -p "use the bridge tool with action=setup"
```

The tool returns a JSON envelope `{"ok": bool, "output": str}`; Kimi paraphrases this into prose.

### Reload after config edits

```sh
systemctl --user restart kimi-telegram-bridge.service
```

The daemon re-reads `config.json` only at startup. There is no `SIGHUP` handler in v0.1.

### Log rotation

`bridge.log` grows unbounded. For long-running deployments add a logrotate rule at `/etc/logrotate.d/kimi-telegram-bridge`:

```
/home/YOUR_USER/.kimi/bridge/logs/bridge.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
```

Or rely on `journalctl` (which has built-in rotation) and edit the systemd unit to drop `StandardOutput=append:` / `StandardError=append:`.

---

## Troubleshooting

### "Bot not responding" — check the chain

```sh
# 1. Is the daemon running?
systemctl --user is-active kimi-telegram-bridge.service

# 2. Recent log
journalctl --user -u kimi-telegram-bridge.service -n 50 --no-pager

# 3. Validate config + tools
cd ~/.kimi/plugins/telegram-bridge
echo '{"action":"setup"}' | .venv/bin/python -m src.control
```

The `setup` output gives a per-check report:

```
config path: /home/YOUR_USER/.kimi/plugins/telegram-bridge/config.json
✓ config.json loaded
✓ kimi CLI found: kimi, version 1.39.0
✓ telegram token valid (bot @your_bot_username)
```

A `✗` line tells you which subsystem is broken.

### Common failure modes

#### "telegram getUpdates failed: Unauthorized"

The bot token is wrong. Re-check `config.json`. If you've recently regenerated the token in BotFather, the old one is dead.

#### "kimi CLI not found on PATH"

Either `kimi` is not installed, or systemd's PATH doesn't include `~/.local/bin`. The systemd unit explicitly exports `PATH=$HOME/.local/bin:...`, so this should work — but if `kimi` is somewhere unusual (`/opt/kimi/bin` or similar), edit `~/.config/systemd/user/kimi-telegram-bridge.service` to extend the PATH and `systemctl --user daemon-reload`.

#### "kimi error: auth expired" reply in Telegram

Your Kimi OAuth token expired and the auto-refresh isn't working. Run:

```sh
kimi login   # interactive OAuth re-link
```

Then `systemctl --user restart kimi-telegram-bridge.service`. The Kimi side has a systemd timer (`kimi-token-refresh.timer`) that should normally keep this fresh.

#### "kimi error: kimi timed out after 300.0s"

A single Kimi turn ran for 5 minutes and was killed. This is the bridge's safety net. Either the prompt was unusually heavy, or Kimi is stuck. Check `~/.kimi/logs/` for kimi-side issues. Bump `KIMI_TIMEOUT_S` in `src/daemon.py` if your workload genuinely needs longer turns.

#### Bridge replies with `(empty reply)`

Kimi exited 0 but emitted no `assistant` text. Either:
- The prompt was filtered/refused by the model — check the actual `~/.kimi/sessions/<uuid>/` for the recorded turn.
- Kimi changed its `stream-json` envelope shape (parser drift). Compare current output with the parser's expectations:

```sh
echo "ping" | kimi --print --output-format stream-json --no-thinking | head -5
```

If the envelope no longer uses `role: "assistant"` / `content: <str|list>`, update `parse_stream_json` in `src/kimi_runner.py`.

#### Telegram replies are getting truncated

Single message exceeds 4096 chars. The bridge auto-splits at newline boundaries via `chunk_message`. If a single chunk is *itself* over 4096 chars (one giant unbroken paragraph), it hard-splits — visually ugly but never lost. No fix needed.

#### Stale `config.json` with a leaked token in git history

If you accidentally committed `config.json` and pushed: rotate the bot token (`/revoke` in BotFather, then `/token`), update `config.json` locally, force-push to scrub history (`git filter-repo --path config.json --invert-paths` then `git push --force-with-lease`). The leaked token is dead the moment you `/revoke`.

### "How do I see exactly what Kimi was sent?"

Set the daemon log level to DEBUG:

```sh
systemctl --user edit kimi-telegram-bridge.service
```

Add:

```
[Service]
Environment=PYTHONLOGLEVEL=DEBUG
```

Save, then `systemctl --user restart kimi-telegram-bridge.service`. (Currently the daemon does not log prompt bodies even at DEBUG — sensitive content. If you really need that, edit `src/daemon.py` to add a `LOG.debug("prompt: %s", msg.text)` line. Don't ship that to production.)

---

## Upgrading

To pull updates from the GitHub remote (when one exists):

```sh
cd ~/.kimi/plugins/telegram-bridge
git pull
bash install.sh   # idempotent — refreshes deps and re-renders the systemd unit
systemctl --user restart kimi-telegram-bridge.service
```

If `pyproject.toml` changed dependencies, `install.sh`'s `pip install -e .` step picks up the new requirements.

If `systemd/kimi-telegram-bridge.service.template` changed, `install.sh` re-renders the unit file. `systemctl --user restart` picks up the new unit.

---

## Uninstall

Complete removal:

```sh
systemctl --user disable --now kimi-telegram-bridge.service
rm -f ~/.config/systemd/user/kimi-telegram-bridge.service
systemctl --user daemon-reload
rm -rf ~/.kimi/plugins/telegram-bridge ~/.kimi/bridge
```

The first three commands stop and unregister the service. The last removes the plugin code, your config, and runtime state (sessions, state.json, logs).

To keep the code but stop running the bridge:

```sh
systemctl --user disable --now kimi-telegram-bridge.service
```

Re-enable later with `systemctl --user enable --now kimi-telegram-bridge.service`. Config and state are preserved.

---

## Pushing to GitHub

The repo lives at `~/.kimi/plugins/telegram-bridge/`. To publish:

1. Create the empty repository on GitHub (web UI, `gh repo create`, etc.). Do not initialise it with a README or LICENSE — those already exist locally and would conflict.

2. Add the remote and push:

```sh
cd ~/.kimi/plugins/telegram-bridge
git remote add origin git@github.com:YOUR_USER/kimi-to-im.git
git push -u origin main
```

3. The first push triggers GitHub Actions (`.github/workflows/test.yml`). Confirm green:

```sh
gh run list --workflow=tests --limit 5
```

4. (Optional) Tag the v0.1.0 release:

```sh
git tag v0.1.0
git push origin v0.1.0
```

### Branch protection (recommended)

In the GitHub repo settings under **Branches**, add a rule for `main`:

- Require pull-request review before merging.
- Require status checks: `pytest (3.11)`, `pytest (3.12)`, `pytest (3.13)`.

This prevents direct pushes that fail tests.

### Pre-push checklist

Before any push, confirm no secret made it into a tracked file:

```sh
git ls-files | xargs grep -l 'bot_token\|api_key\|password' 2>/dev/null
# Should match only the example, README, and design doc — never config.json with a real token.
git ls-files | grep -E '^config\.json$'
# Should be empty (config.json is gitignored).
```

---

## Hardening notes (audit-derived)

### File permissions

After install, verify the runtime data dir is locked down:

```sh
chmod 700 ~/.kimi/bridge ~/.kimi/bridge/logs ~/.kimi/bridge/runtime
chmod 600 ~/.kimi/bridge/state.json ~/.kimi/bridge/logs/bridge.log 2>/dev/null
chmod -R go-rwx ~/.kimi/sessions/   # kimi conversation history, not bridge state
```

The systemd unit installs with `UMask=0077` so newly-created files inherit 0600.

### Lock the bot to a single chat

`config.json` accepts `allowed_chat_ids`. Setting it to your DM's chat_id (equal to your user_id for private chats) prevents the bot from responding inside any group it's added to. Recommended even with Telegram's default privacy mode.

```json
{
  "telegram": {
    "allowed_user_ids": [123456789],
    "allowed_chat_ids": [123456789]
  }
}
```

### Token hygiene

The Telegram bot token lives in `config.json` (mode 0600, gitignored). It is also held in memory by the running daemon. To rotate:

```sh
# In Telegram, message @BotFather → /token → choose your bot → /revoke (kills old token)
# BotFather replies with a new token; paste into config.json
$EDITOR ~/.kimi/plugins/telegram-bridge/config.json
systemctl --user restart kimi-telegram-bridge.service
```

If the previous token leaked (e.g., into a backup), `/revoke` makes it inert immediately.

### Backups

If you back up `~/.kimi/`, your `bridge/logs/bridge.log` and `config.json` go with it. Either exclude them from the backup, or encrypt the backup at rest.

---

## Architecture refresher

Two layers:

```
                               ┌─────────────────────────────┐
                               │  ~/.kimi/plugins/            │
                               │      telegram-bridge/        │
                               │   plugin.json                │   ← Kimi sees this as a plugin
   Kimi CLI session ──────────▶│   src/control.py "bridge"    │     and can call its `bridge` tool
                               │     tool (start/stop/...)    │     to control the daemon
                               └────────────┬────────────────┘
                                            │ systemctl --user
                                            ▼
                               ┌─────────────────────────────┐
                               │  systemd-supervised daemon   │
                               │  python -m src.daemon        │
                               │                              │
   Telegram phone ──────────▶  │  • polls getUpdates          │
                               │  • parse_update              │
                               │  • is_authorized             │
                               │  • run_kimi (subprocess)     │── kimi --print --output-format
                               │  • parse_stream_json         │           stream-json -S <uuid>
                               │  • sendMessage               │
                               │                              │
                               │  state: ~/.kimi/bridge/      │
                               │    state.json (chat→session) │
                               └─────────────────────────────┘
```

Code lives at `~/.kimi/plugins/telegram-bridge/`. Mutable runtime state lives at `~/.kimi/bridge/`. The two paths are deliberately separated so the plugin dir stays clean for `git pull` and the runtime data stays out of the repo.

For full architectural detail see [`design.md`](design.md).
For the original implementation plan see [`plans/2026-04-25-kimi-to-im-bridge.md`](plans/2026-04-25-kimi-to-im-bridge.md).

---

## Index of source files

| Path | Responsibility |
|---|---|
| `src/config.py` | Parse + validate `config.json` → frozen `Config` dataclass |
| `src/state.py` | Atomic JSON state at `~/.kimi/bridge/state.json` |
| `src/telegram.py` | Pure helpers (`parse_update`, `is_authorized`, `chunk_message`) + async `TelegramClient` |
| `src/kimi_runner.py` | Stream-JSON parser + `run_kimi` subprocess wrapper with timeout |
| `src/daemon.py` | Orchestrating async `run()` + production `main()` entry point |
| `src/control.py` | Plugin-tool dispatcher (`start`/`stop`/`status`/`logs`/`setup`) |
| `plugin.json` | Kimi plugin manifest |
| `install.sh` | Idempotent installer |
| `systemd/kimi-telegram-bridge.service.template` | Systemd user unit template |
| `pyproject.toml` | Project metadata + deps |

Test coverage:

| Test file | Asserts |
|---|---|
| `tests/test_config.py` | Config validation (7 tests) |
| `tests/test_state.py` | Atomic state persistence (6 tests) |
| `tests/test_telegram_pure.py` | parse_update / is_authorized / chunk_message (13 tests) |
| `tests/test_telegram_http.py` | Async TelegramClient with mocked httpx (5 tests) |
| `tests/test_kimi_runner.py` | Stream-JSON parser + subprocess plumbing + timeout (13 tests) |
| `tests/test_daemon.py` | Orchestration with injected fakes (5 tests) |
| `tests/test_control.py` | Plugin-tool dispatcher with mocked subprocess (7 tests) |

Total: **57 tests**, runs in under 2 seconds.
