"""Spawn the Antigravity CLI (`agy`) and capture its plain-text output.

agy print mode uses Go-style flags:
  agy -p "<prompt>" [--model <id>] [--continue | --new-project]
      [--dangerously-skip-permissions] [--sandbox]
      [--print-timeout <duration>]

Output is plain text/markdown on stdout; there is no stream-json mode.
"""
from __future__ import annotations

import asyncio
import gc
import os
import subprocess
from dataclasses import dataclass

# Safety cap on stdout capture — prevents unbounded memory growth from
# runaway agy output. 1MB is generous (typical replies are 1–5KB).
_STDOUT_CAP_BYTES = 524_288  # 512 KiB

# Reap any defunct child processes left by prior subprocess invocations
# (bwrap sandbox can leave orphaned grand-children).
def _reap_zombies() -> None:
    try:
        while True:
            pid, _status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except (ChildProcessError, OSError):
        pass


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
    chat_dir: str = "",
) -> list[str]:
    agy_abs = os.path.abspath(agy_path)
    agy_parent = os.path.dirname(agy_abs)

    if mode == "plan" and chat_dir and "PYTEST_CURRENT_TEST" not in os.environ:
        chat_path = os.path.abspath(chat_dir)
        args: list[str] = [
            "bwrap",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/sbin", "/sbin",
            "--ro-bind", "/etc/alternatives", "/etc/alternatives",
            "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
            "--ro-bind", "/etc/ssl", "/etc/ssl",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", chat_path, chat_path,
            "--chdir", chat_path,
            "--ro-bind", agy_parent, agy_parent,
            "--unshare-net",
            agy_abs,
            "-p", prompt,
        ]
    else:
        args = [agy_path, "-p", prompt]

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
    _reap_zombies()
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
        gc.collect()
        return AgyResult(text="", exit_code=124, stderr=stderr)

    stdout = completed.stdout
    # Cap at safety limit; mark truncation if hit
    truncated = False
    if len(stdout) > _STDOUT_CAP_BYTES:
        stdout = stdout[:_STDOUT_CAP_BYTES]
        truncated = True

    text = stdout.decode("utf-8", errors="replace")
    if truncated:
        text += "\n\n[output truncated at 1MiB]"

    stderr_out = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
    gc.collect()
    return AgyResult(
        text=text,
        exit_code=completed.returncode,
        stderr=stderr_out,
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
        chat_dir=chat_dir,
    )
    return await asyncio.to_thread(_run_sync, args, chat_dir, timeout)
