# Contributing

Patches and pull requests are welcome.

## Before opening a PR

1. Run the test suite. It should pass on Python 3.11, 3.12, and 3.13.

   ```sh
   uv venv .venv --python 3.11
   uv pip install -e ".[dev]"
   .venv/bin/pytest -v
   ```

   The full suite runs in under 2 seconds and is the gate for CI.

2. Keep new code under the existing module split. Each file in `src/` has one job:

   | File | Responsibility |
   |---|---|
   | `config.py` | Parse and validate `config.json`. |
   | `state.py` | Atomic JSON state. |
   | `telegram.py` | Telegram update parsing, authorization, chunking, async HTTP client. |
   | `agy_runner.py` | Spawn `agy` and capture its plain-text output. |
   | `daemon.py` | Orchestrate the long-poll loop. |
   | `control.py` | Plugin-tool entry point (start/stop/status/logs/setup). |

   If a change naturally crosses a module, that's fine — but please don't grow any single file past ~250 lines without splitting.

3. Add tests alongside any behaviour change. Test files mirror the source layout: `tests/test_<module>.py`.

4. Conventional-commits style for commit subjects: `feat:`, `fix:`, `chore:`, `docs:`, `ci:`, `refactor:`, `test:`. Subjects under 72 characters.

## What's in scope for v0.x

- Telegram integration (the only IM channel).
- The `agy` subprocess wrapper and plain-text capture.
- Operator-facing affordances (config, state, control tool, install script, systemd unit).
- Documentation in `docs/` and the top-level `README.md`.

## What's out of scope (for now)

- Other IM channels (Discord, Slack, Feishu, QQ). They would belong in a sibling project, not in this codebase.
- Non-text input (images, audio, files).
- Token-by-token streaming (`agy` print mode emits plain text per turn).
- Any UI for surfacing internal tool calls (the CLI does not expose them).

If you have a use case that needs any of the above, please open an issue first to discuss before submitting code.

## Reporting bugs

Open a GitHub issue with:

- The version of Python, `uv`, and `agy --version`.
- The systemd unit's status output (`systemctl --user status antigravity-telegram-bridge.service`).
- The last ~50 lines of `~/.antigravity/bridge/logs/bridge.log` (redact your bot token if it appears, though it shouldn't).

If the issue is a security vulnerability, see [`SECURITY.md`](SECURITY.md) — please do not file a public issue.

## Code review expectations

PRs are reviewed by the maintainer. Expect:

- A request for tests if any branch of new code is uncovered.
- A request to split a PR if it bundles unrelated changes.
- A note if a change drifts from the existing module decomposition.

Small, focused PRs with passing CI usually merge same-day.

## License

By submitting code you agree to release it under the MIT license (see [`LICENSE`](LICENSE)).
