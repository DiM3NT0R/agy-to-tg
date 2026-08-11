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
    effort: str = "",
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
    if effort:
        args.extend(["--effort", effort])
    args.append("--dangerously-skip-permissions")
    if mode == "plan":
        args.append("--sandbox")
    args.extend(["--print-timeout", print_timeout])
    return args


import json

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
    effort: str = "",
    on_event: "Callable[[dict], Awaitable[None]] | None" = None,
) -> AgyResult:
    """Run agy in print mode. Optionally stream JSON events."""
    _reap_zombies()
    args = _build_args(
        agy_path=agy_path,
        prompt=prompt,
        has_session=has_session,
        model=model,
        mode=mode,
        print_timeout=print_timeout,
        effort=effort,
        chat_dir=chat_dir,
    )
    if on_event is not None:
        args.extend(["--output-format", "stream-json"])

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=chat_dir,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2 * 1024 * 1024,  # 2MB
        )
    except Exception as e:
        return AgyResult(text="", exit_code=-1, stderr=str(e))

    stdout_chunks = []
    stderr_chunks = []
    final_text = ""

    async def read_stdout():
        nonlocal final_text
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            if on_event is not None:
                try:
                    data = json.loads(line.decode("utf-8", errors="replace"))
                    await on_event(data)
                    if data.get("event") == "result":
                        final_text = data.get("result", {}).get("response", "")
                except Exception:
                    pass
            else:
                stdout_chunks.append(line)

    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            stderr_chunks.append(line)

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr(), process.wait()),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        msg = f"agy timed out after {timeout}s"
        return AgyResult(text=final_text, exit_code=124, stderr=msg)
    finally:
        gc.collect()

    stderr_out = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    
    if on_event is None:
        stdout_bytes = b"".join(stdout_chunks)
        truncated = False
        if len(stdout_bytes) > _STDOUT_CAP_BYTES:
            stdout_bytes = stdout_bytes[:_STDOUT_CAP_BYTES]
            truncated = True
        
        text = stdout_bytes.decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n[output truncated at 1MiB]"
    else:
        text = final_text

    return AgyResult(
        text=text,
        exit_code=process.returncode,
        stderr=stderr_out,
    )
