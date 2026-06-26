# Kimi-to-IM Bridge — Operations Runbook

## Service

```bash
systemctl --user status kimi-telegram-bridge
systemctl --user restart kimi-telegram-bridge
systemctl --user stop kimi-telegram-bridge
```

## Health Checks

```bash
# Memory usage
ps -o pid,rss,comm -p $(systemctl --user show -p MainPID --value kimi-telegram-bridge)

# Bot alive
curl -s "https://api.telegram.org/bot<TOKEN>/getMe" | jq .ok

# Kimi CLI alive
kimi --print --quiet -p "reply: OK"

# Recent turns
tail -20 ~/.kimi/bridge/logs/bridge.log
```

## Hardening (applied 2026-06-25)

| Directive | Value | Purpose |
|---|---|---|
| MemoryMax | 512M | Hard kill if exceeded |
| MemoryHigh | 384M | Soft throttle |
| RuntimeMaxSec | 86400 | Daily restart (clears leaks) |
| Restart | always | Survive any exit reason |
| NoNewPrivileges | true | No privilege escalation |

## Troubleshooting

### Memory leak
Restart: `systemctl --user restart kimi-telegram-bridge`
Normal after restart: ~35MB. Expected growth over days: up to 150MB.
If > 384MB: MemoryHigh throttles. If > 512M: MemoryMax kills + auto-restart.

### Bot not responding
1. Check bridge is running: `systemctl --user status kimi-telegram-bridge`
2. Check token valid: `curl -s "https://api.telegram.org/bot<TOKEN>/getMe"`
3. Check Telegram API reachable: `curl -s "https://api.telegram.org"`
4. Check logs: `tail -50 ~/.kimi/bridge/logs/bridge.log`

### Kimi CLI errors
1. Check kimi version: `kimi --version`
2. Test: `kimi --print --quiet -p "reply: OK"`
3. Check token: `kimi login` (refreshes via kimi-token-refresh.timer)

## Configuration

Path: `~/.kimi/plugins/telegram-bridge/config.json`

| Key | Purpose |
|---|---|
| telegram.bot_token | Telegram Bot API token |
| telegram.allowed_user_ids | Whitelisted user IDs |
| telegram.allowed_chat_ids | Whitelisted chat IDs |
| kimi.default_workdir | Kimi working directory |
| kimi.model | Override model (empty = default) |
| kimi.agent | Agent spec (default: "default") |

## Metrics

Tracked in bridge logs:
- Turn latency (ms)
- Exit code
- Reply length (chars)
- Session ID prefix

No Prometheus endpoint. Partial visibility via log parsing.
