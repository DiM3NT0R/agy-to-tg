# Antigravity Telegram Bridge — Operations Runbook

## Service

```bash
systemctl --user status antigravity-telegram-bridge
systemctl --user restart antigravity-telegram-bridge
systemctl --user stop antigravity-telegram-bridge
```

## Health Checks

```bash
# Memory usage
ps -o pid,rss,comm -p $(systemctl --user show -p MainPID --value antigravity-telegram-bridge)

# Bot alive
curl -s "https://api.telegram.org/bot<TOKEN>/getMe" | jq .ok

# agy alive
agy --version

# Recent turns
tail -20 ~/.antigravity/bridge/logs/bridge.log

# Local health endpoint
curl -s http://127.0.0.1:9099/health | jq .

# Prometheus metrics
curl -s http://127.0.0.1:9099/metrics
```

## Hardening (applied)

| Directive | Value | Purpose |
|---|---|---|
| MemoryMax | 512M | Hard kill if exceeded |
| MemoryHigh | 384M | Soft throttle |
| RuntimeMaxSec | 86400 | Daily restart (clears leaks) |
| Restart | always | Survive any exit reason |
| NoNewPrivileges | true | No privilege escalation |
| UMask | 0077 | New files are group/other inaccessible |

## Troubleshooting

### Memory leak

Restart: `systemctl --user restart antigravity-telegram-bridge`.

Normal cold-start memory: ~20 MB. Expected growth over days: up to ~150 MB. If >384 MB: throttled. If >512M: killed + auto-restart.

### Bot not responding

1. Check bridge is running: `systemctl --user status antigravity-telegram-bridge`
2. Check token valid: `curl -s "https://api.telegram.org/bot<TOKEN>/getMe"`
3. Check Telegram API reachable: `curl -s "https://api.telegram.org"`
4. Check logs: `tail -50 ~/.antigravity/bridge/logs/bridge.log`
5. Run setup self-test: `echo '{"action":"setup"}' | .venv/bin/python -m src.control`

### agy CLI errors

1. Check version: `agy --version`
2. Check auth dir exists: `ls ~/.gemini/antigravity-cli/`
3. Re-authenticate if needed: run `agy` interactively and complete OAuth.
4. Restart the bridge.

### Webhook not working

1. Confirm `CTI_WEBHOOK_URL` and `CTI_WEBHOOK_PORT` are exported in the unit environment.
2. Ensure the host is reachable from Telegram's servers on the webhook port.
3. Check logs for webhook registration result.
4. Verify the secret token matches between Telegram registration and local receiver.

## Configuration

Path: `~/.gemini/extensions/antigravity-telegram-bridge/config.json`

| Key | Purpose |
|---|---|
| `telegram.bot_token` | Telegram Bot API token |
| `telegram.allowed_user_ids` | Whitelisted user IDs |
| `telegram.allowed_chat_ids` | Whitelisted chat IDs |
| `agy.chats_root` | Per-chat working directories root |
| `agy.default_workdir` | Base cwd for agy |
| `agy.model` | Override model (empty = default) |
| `agy.mode` | `code` or `plan` |

## Metrics

Tracked in bridge logs:

- Turn latency (ms)
- Exit code
- Reply length (chars)
- Chat id

Exposed via Prometheus endpoint at `http://127.0.0.1:9099/metrics`:

- `antigravity_bridge_turns_total`
- `antigravity_bridge_errors_total`
