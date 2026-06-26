# Deployment guide

A streamlined install-only guide for getting the bridge running on a fresh host. The longer reference (with troubleshooting, upgrade, uninstall) is `operations.md`.

---

## Time estimate

About **10 minutes** the first time, including creating the Telegram bot. Subsequent installs on additional hosts are about 3 minutes.

## Prerequisites

| Requirement | Verify |
|---|---|
| Linux with `systemd --user` | `systemctl --user status` returns without error |
| Python ≥ 3.11 | `python3 --version` |
| `uv` ≥ 0.4 | `uv --version` (install: `curl -LsSf https://astral.sh/uv/install.sh \| sh`) |
| Kimi CLI v1.39+ on `PATH` | `kimi --version` (install: `uv tool install kimi-cli && kimi login`) |
| Active Kimi Coding OAuth | `~/.kimi/credentials/kimi-code.json` exists and is non-empty |
| Outbound HTTPS to `api.telegram.org` | `curl -sSI https://api.telegram.org/` returns `HTTP/2 200` |

The host does not need an inbound port. The bridge uses long-poll outbound only.

## Step 1 — Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`. Follow the prompts to pick a display name and a username ending in `bot`.
3. Copy the token BotFather replies with. The format is `<digits>:<base64-ish>`.
4. While you're there, also send `/setprivacy` and pick `Disable` for stricter group-chat behaviour, and `/setjoingroups` → `Disable` to prevent the bot being added to random groups.

## Step 2 — Find your Telegram user ID

Message [@userinfobot](https://t.me/userinfobot). It replies with your numeric user ID. You'll need it in the next step.

## Step 3 — Clone

```sh
git clone https://github.com/hah23255/kimi-to-im.git ~/.kimi/plugins/telegram-bridge
cd ~/.kimi/plugins/telegram-bridge
```

The clone target — `~/.kimi/plugins/telegram-bridge/` — is significant. The `~/.kimi/plugins/` location is where Kimi looks for plugins, so after the install Kimi can directly invoke the bridge's `bridge` tool.

## Step 4 — Configure

```sh
cp config.example.json config.json
chmod 600 config.json
$EDITOR config.json
```

Set the three required fields:

```json
{
  "telegram": {
    "bot_token": "PASTE_THE_TOKEN_FROM_BOTFATHER",
    "allowed_user_ids": [PASTE_YOUR_USER_ID_FROM_USERINFOBOT],
    "allowed_chat_ids": [PASTE_YOUR_USER_ID_AGAIN_TO_LOCK_TO_DM]
  },
  "kimi": {
    "default_workdir": "/home/YOUR_USER",
    "model": "",
    "agent": "default"
  }
}
```

Why pin `allowed_chat_ids` to your user id: in a Telegram private chat (DM), the chat id equals your user id. Putting it here means even if someone adds the bot to a group, the bot won't respond inside the group.

## Step 5 — Install

```sh
bash install.sh
```

The installer:

1. Creates `~/.kimi/bridge/{logs,runtime}/` for runtime data.
2. Builds a Python venv at `.venv/` (skipped if already present).
3. Installs the project in editable mode via `uv pip install -e .`.
4. Renders the systemd user unit from the template, substituting your `$HOME` and the venv's Python path.
5. Reloads the user systemd manager and enables the unit.

It does **not** start the service.

## Step 6 — Pre-flight check

Before starting the daemon, run the built-in setup self-test. This validates that your config parses, the `kimi` CLI is reachable, and your bot token is valid (it does a one-shot `getMe` against Telegram).

```sh
echo '{"action":"setup"}' | .venv/bin/python -m src.control | python3 -m json.tool
```

Expected:

```json
{
  "ok": true,
  "output": "config path: ~/.kimi/plugins/telegram-bridge/config.json\n✓ config.json loaded\n✓ kimi CLI found: kimi, version 1.39.0\n✓ telegram token valid (bot @your_bot_username)"
}
```

If any line begins with `✗`, fix it before continuing. Common cases:

| Line | Cause | Fix |
|---|---|---|
| `✗ config error: ...` | JSON syntax error or schema violation | re-check the file |
| `✗ kimi --version failed` | kimi binary not on `PATH` for the daemon | `which kimi`; if it's outside `~/.local/bin`, edit the rendered unit's `Environment=PATH` |
| `✗ telegram token rejected: Unauthorized` | token typo or revoked | re-paste from BotFather; if recently rotated, the old token is dead |

## Step 7 — Start

```sh
systemctl --user start kimi-telegram-bridge.service
systemctl --user is-active kimi-telegram-bridge.service
```

Expected: `active`.

## Step 8 — End-to-end smoke test

From your phone, open the bot in Telegram and send `hello`. Within ~10 seconds you should see the typing indicator, then a Kimi reply.

In a separate terminal, confirm the turn was logged:

```sh
tail -n 5 ~/.kimi/bridge/logs/bridge.log
```

You're looking for a line of the shape:

```
2026-04-25 17:00:00,123 INFO kimi_telegram_bridge: turn chat=<id> session=<8 hex> exit=0 ms=2417 reply_len=42
```

That confirms the full chain — Telegram → bridge → kimi → Telegram — works.

Send a follow-up like `what did I just say?`. The reply should reference the previous message — that's session continuity working.

## Step 9 — (Optional) headless boot

If the host is headless and you want the bridge to come up at boot without you logging in:

```sh
sudo loginctl enable-linger $USER
```

Without lingering, the user systemd manager only runs while you have an active login session.

---

## Done

The bridge is now operational. Day-to-day operations (logs, status, restart, upgrade, uninstall) are documented in [`operations.md`](operations.md). The architecture rationale lives in [`design.md`](design.md). Security policy and findings are in [`SECURITY.md`](../SECURITY.md) and [`security-scan.md`](security-scan.md).

## What to do if something goes wrong

If step 7 reports `failed`:

```sh
systemctl --user status kimi-telegram-bridge.service --no-pager
journalctl --user -u kimi-telegram-bridge.service -n 50 --no-pager
```

If the bot doesn't reply in step 8:

```sh
tail -n 20 ~/.kimi/bridge/logs/bridge.log    # daemon's own log
```

The most common failure modes — auth expired, kimi missing, token invalid — are all surfaced by the setup self-test in step 6. If step 6 was green and the bot still isn't replying, the issue is almost always the daemon being unable to reach the network or kimi having lost its OAuth token (run `kimi login` to refresh).

For systematic troubleshooting see [`operations.md`](operations.md#troubleshooting).
