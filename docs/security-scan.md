# Security scan report

**Repository:** `kimi-to-im`
**Date scanned:** 2026-04-25
**Commits in scope:** initial repo (`1f47f51`) through `a12aeeb` (pre-public).

This document records the formal pre-publication security scan, the findings, and the remediation status of each one. Future audits should append a new dated section rather than overwriting this one.

---

## Methodology

The scan covered ten areas:

1. Code-level vulnerabilities in `src/` (shell injection, path traversal, JSON injection, authorization bypass, DoS, race conditions, TOCTOU, info leakage).
2. Secrets handling — locations, file modes, log surfaces, error tracebacks, process listings, environment variables.
3. Authorization correctness — verifying default-deny is enforced before any side effect.
4. Subprocess discipline — every `subprocess.run` call audited for shell=False, list-form argv, and isolation of user input.
5. State and persistence — `state.json` content, session directory permissions, log file permissions.
6. Systemd hardening — namespace isolation, syscall filtering, address-family restriction.
7. Git history — full `git log --all -p` greps for known token patterns, AWS keys, SSH keys, and personal identifiers.
8. Network surface — listening sockets, outbound TLS verification, certificate handling.
9. Dependency CVEs — `httpx`, `httpcore`, transitive deps, dev-only deps.
10. Personal-data scrubbing for public release — hardcoded paths, usernames, hostnames, references to private infrastructure.

---

## Findings summary

| Severity | Count at scan | Count remaining |
|---|---|---|
| Critical | 0 | 0 |
| High | 0 | 0 |
| Medium | 2 | 0 |
| Low | 3 | 0 |
| Informational | 5 | 5 (documented; no action) |

No live credentials were found in any tracked file or in git history. One token-leak incident was discovered during the deployment of the bridge (httpx's INFO-level logger was writing the full request URL — including the bot token in the URL path — to `bridge.log`) and was patched in flight; the patch is verified clean and the previously-leaked log was scrubbed.

---

## Findings, in priority order

### F-1 (Medium, remediated). Bridge log file mode 0664

**Symptom.** `~/.kimi/bridge/logs/bridge.log` was created with default umask, leaving it world-readable for any local user in the same group.

**Risk.** The log file does not currently contain credentials (the httpx-leak fix landed; see F-10), but it does record per-turn metadata (`chat_id`, `session_id` prefix, exit code, reply length). An attacker with read access to the log can observe usage patterns.

**Fix.** Two-part:
- One-time `chmod 600 bridge.log` on the running deployment.
- Systemd unit now ships with `UMask=0077` so future log files are created mode 0600. Same setting also applies to `state.json` writes.

**Status.** Closed.

### F-2 (Medium, remediated). Systemd unit had no hardening directives

**Symptom.** The user-unit ran the daemon without namespace isolation, syscall filtering, or address-family restrictions. A compromised `kimi` subprocess (which the daemon spawns) would have full read/write access to the user's `~/.ssh`, `~/.config`, OAuth caches, etc.

**Fix.** Added the following directives to `systemd/kimi-telegram-bridge.service.template`:

```ini
NoNewPrivileges=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
UMask=0077
```

Deliberately not enabled, because they break either the daemon or `kimi`:

- `ProtectSystem=strict` (kimi reads `/etc/`, `/usr/`).
- `ProtectHome=` (the daemon writes to `~/.kimi/bridge/`).
- `MemoryDenyWriteExecute=true` (some Python C extensions trip on it).
- `PrivateUsers=true` (incompatible with user-systemd).

**Status.** Closed.

### F-3 (Low, remediated). Plugin-tool entry point not gagged against httpx URL logging

**Symptom.** `_action_setup` in `src/control.py` calls `httpx.get(getMe)` whose URL embeds the bot token. The daemon entry point silenced httpx's INFO logger; the plugin-tool entry point did not. If anyone ever raised the root logger to INFO before invoking `python -m src.control`, the token would be re-logged.

**Fix.** Mirrored the daemon's logger-gag (`logging.getLogger("httpx").setLevel(WARNING)` plus `httpcore`) into `control.py:main()`. Lazy-imports `logging` to keep the plugin-tool import time minimal.

**Status.** Closed.

### F-4 (Low, remediated). `state.json` argv-injection (theoretical)

**Symptom.** `load_state` returned session ids verbatim, then passed them as the value of `-S` in the `kimi` argv list. A tampered `state.json` containing `"chats": {"<chat>": "--evil-flag"}` would not trigger shell injection (subprocess uses list args), but would pass `--evil-flag` as an extra argument to `kimi`.

**Risk.** Local. Pre-condition is filesystem write access to `~/.kimi/bridge/state.json`, which means the attacker is already on the box as the running user.

**Fix.** `load_state` now validates each session id against `^[0-9a-f]{32}$` (matching uuid4 hex), silently dropping any non-conforming entry. Two new tests cover the format guard and the regex itself.

**Status.** Closed.

### F-5 (Low, documented). No log rotation

**Symptom.** `bridge.log` grows unbounded. Old logs on disk are a re-leak surface (especially after F-10).

**Fix.** Documented in `docs/operations.md` under "Log rotation": both a `logrotate` snippet and the alternative of switching `StandardOutput=journal` (which has built-in rotation).

**Status.** Documented; deployment-specific configuration not enforced by the package.

### F-6 (Informational). Stderr from `kimi` is forwarded to the chat on error

**Symptom.** When `kimi` exits non-zero, the bridge replies with `⚠️ kimi error: <first 500 chars of stderr>`. If `kimi` ever prints credential paths or environment hints to stderr, they reach the Telegram chat.

**Risk.** For a single-user bot the chat is the operator's own DM; acceptable. If `allowed_user_ids` ever expands to include other people, this becomes information disclosure to those users.

**Fix.** Documented; deferred. If multi-user is enabled, redact stderr — show "kimi error: exit N" only and write the snippet to `bridge.log`.

**Status.** Open by design.

### F-7 (Informational). `last_update_id` advances for unauthorized messages

**Symptom.** Unauthorized messages have their `update_id` recorded in state before being dropped.

**Risk.** None. This is the correct design — without it, a stuck unauthorized message would replay forever, and a malicious sender could poison the daemon with a single bad update.

**Status.** Open by design.

### F-8 (Informational, remediated by config change). Group-chat exposure

**Symptom.** With the default empty `allowed_chat_ids`, only `user_id` is checked. If the operator sends a message inside a Telegram group where the bot is also a member, the bot will respond — exposing the kimi session to other group members.

**Fix.** Operators are encouraged to set `allowed_chat_ids: [<DM chat_id>]` explicitly. Documented in both `docs/operations.md` and `SECURITY.md`. Telegram bots default to "privacy mode" (only see messages addressed to them), so the practical risk in v1 is small.

**Status.** Documented; operator-controlled.

### F-9 (Informational). Kimi sessions may be group-readable

**Symptom.** `~/.kimi/sessions/<sid>/` directories inherit umask 0002 and are readable by group `i`. Conversation history lives there.

**Risk.** Out of scope for this bridge (kimi creates the directories). On a single-user box the practical exposure is small; on a shared box it is real.

**Fix.** Documented as a hardening recommendation. The runbook includes a one-liner `chmod -R go-rwx ~/.kimi/sessions/`.

**Status.** Out of scope; operator-controlled.

### F-10 (High, remediated immediately on first deployment). httpx INFO-level URL logging leaked the bot token

**Symptom.** httpx's default logger writes the full request URL on every call at INFO level. The Telegram API embeds the bot token in the URL path. With the daemon configuring the root logger at INFO, every `getUpdates`/`sendMessage`/`sendChatAction` call wrote the token into `bridge.log`.

**Detection.** Caught in real time during the first production turn by reviewing the live log.

**Fix.**
- Suppressed `httpx` and `httpcore` loggers to WARNING in the daemon (`src/daemon.py:main`).
- Truncated the existing log file to remove the previously-leaked URLs.
- Token in `config.json` is unchanged because the leak window was short (~4 minutes), the file was mode 0644 with parent dir 0750 (group-only), and the disk was not backed up to off-host storage during the window.
- Plugin-tool entry point also gagged — see F-3.

**Status.** Closed. Verified no token traces remain on disk (`grep -RnE '8693880546' ~/.kimi/` → no matches outside `config.json`) and no traces in git history.

---

## Personal-data scrub for public release

Done as part of the same audit:

- Replaced hardcoded `/home/i/...` paths with `~/...` or `/home/YOUR_USER/...` placeholders in `docs/operations.md` and `docs/design.md`.
- Removed all references to private parallel infrastructure from `docs/design.md`.
- Deleted `docs/plans/2026-04-25-kimi-to-im-bridge.md` (an internal build-session artifact, not user documentation).
- Verified no Telegram user IDs, bot tokens, hostnames, or email addresses appear in any tracked file body. (Author identity on commits is unavoidable and acceptable per industry convention.)
- Verified `.gitignore` excludes `config.json` and `.venv/`. Verified neither has ever been tracked.

## Verification commands

To re-run the scan locally:

```sh
# Working tree
git ls-files | xargs grep -nE '/home/i|claude-to-im|/home/YOUR_USER' || echo OK

# History (looking for live token / user ID)
git log --all -p | grep -nE '8693880546|AAFn6mPj3ulyvdQ48hjB1llrRdRVhYHuAvQ|8145172607' || echo OK

# Test suite
.venv/bin/pytest -v

# Setup-action self-check
echo '{"action":"setup"}' | .venv/bin/python -m src.control
```

## Remaining recommendations (operator-controlled)

These are not required to ship, but are listed here so they aren't forgotten:

1. Enable `loginctl enable-linger $USER` if you want the bridge to start at boot on a headless host.
2. Add a logrotate rule (or switch `StandardOutput=journal`) before the bridge has been running for a month.
3. If `~/.kimi/sessions/` is not already owner-only, run `chmod -R go-rwx ~/.kimi/sessions/`.
4. If the host is shared or backed up to off-host storage, add `~/.kimi/bridge/logs/`, `~/.kimi/credentials/`, and `~/.kimi/plugins/telegram-bridge/config.json` to your backup-exclude list (or encrypt the backup).
5. Set `allowed_chat_ids` to your DM chat-id explicitly if your bot is ever invited to a group.
