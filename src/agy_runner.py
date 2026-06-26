"""Spawn the Antigravity CLI (`agy`) and capture its plain-text output.

agy print mode uses Go-style flags:
  agy -p "<prompt>" [--model <id>] [--continue | --new-project]
      [--dangerously-skip-permissions] [--sandbox]
      [--print-timeout <duration>]

Output is plain text/markdown on stdout; there is no stream-json mode.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class AgyResult:
    text: str
    exit_code: int
    stderr: str


def _build_args(
    *,
    agy_path: str,
    prompt: str,
    has_session: bool,
    model: str,
    mode: str,
    print_timeout: str,
) -> list[str]:
    args: list[str] = [agy_path, "-p", prompt]
    if has_session:
        args.append("--continue")
    else:
        args.append("--new-project")
    if model:
        args.extend(["--model", model])
    args.append("--dangerously-skip-permissions")
    if mode == "plan":
        args.append("--sandbox")
    args.extend(["--print-timeout", print_timeout])
    return args


def _run_sync(
    args: list[str],
    cwd: str,
    timeout: float | None = None,
) -> AgyResult:
    """Synchronous worker for subprocess invocation. Called via asyncio.to_thread."""
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            env=os.environ.copy(),
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial_stderr = ""
        if exc.stderr is not None:
            partial_stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, (bytes, bytearray))
                else str(exc.stderr)
            )
        message = f"agy timed out after {timeout}s"
        stderr = f"{partial_stderr}\n{message}".strip() if partial_stderr else message
        return AgyResult(text="", exit_code=124, stderr=stderr)
    return AgyResult(
        text=completed.stdout.decode("utf-8", errors="replace"),
        exit_code=completed.returncode,
        stderr=completed.stderr.decode("utf-8", errors="replace"),
    )


async def run_agy(
    prompt: str,
    *,
    chat_dir: str,
    has_session: bool,
    model: str,
    mode: str,
    agy_path: str,
    timeout: float | None = None,
    print_timeout: str = "15m",
) -> AgyResult:
    """Run agy in print mode. Returns when the turn ends."""
    args = _build_args(
        agy_path=agy_path,
        prompt=prompt,
        has_session=has_session,
        model=model,
        mode=mode,
        print_timeout=print_timeout,
    )
    return await asyncio.to_thread(_run_sync, args, chat_dir, timeout)
