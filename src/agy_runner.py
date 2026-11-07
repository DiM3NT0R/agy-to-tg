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
import re
import signal
import subprocess
from dataclasses import dataclass

# Safety cap on stdout capture — prevents unbounded memory growth from
# runaway agy output. 1MB is generous (typical replies are 1–5KB).
_STDOUT_CAP_BYTES = 524_288  # 512 KiB

DEFAULT_OVERALL_TIMEOUT_S = 1800.0  # 30 minutes
DEFAULT_INACTIVITY_TIMEOUT_S = 600.0  # 10 minutes without model/system response
DEFAULT_PRINT_TIMEOUT = "30m"

# Regex patterns to sanitize internal system messages / wrapper XML tags injected by agy
_SYSTEM_PATTERNS = [
    re.compile(
        r"(?:The following is a <SYSTEM_MESSAGE> not actually sent by the user[^\n]*\n*)*"
        r"<SYSTEM_MESSAGE>.*?(?:</SYSTEM_MESSAGE>|\Z)\s*",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(r"The following is a <SYSTEM_MESSAGE>.*?(?:</SYSTEM_MESSAGE>|\Z)\s*", re.DOTALL | re.IGNORECASE),
    re.compile(r"<SYSTEM_MESSAGE>.*?(?:</SYSTEM_MESSAGE>|\Z)\s*", re.DOTALL | re.IGNORECASE),
    re.compile(r"<CONTEXT_SUMMARY>.*?(?:</CONTEXT_SUMMARY>|\Z)\s*", re.DOTALL | re.IGNORECASE),
    re.compile(r"<ADDITIONAL_METADATA>.*?(?:</ADDITIONAL_METADATA>|\Z)\s*", re.DOTALL | re.IGNORECASE),
    re.compile(r"</?(?:SYSTEM_MESSAGE|USER_REQUEST|CONTEXT_SUMMARY|ADDITIONAL_METADATA)>", re.IGNORECASE),
]


def clean_system_messages(text: str) -> str:
    """Strip Antigravity internal system messages, notice blocks, and XML wrapper tags."""
    if not text:
        return ""
    cleaned = text
    for pat in _SYSTEM_PATTERNS:
        cleaned = pat.sub("", cleaned)
    return cleaned.strip()

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
    conversation_id: str = "",
) -> list[str]:
    agy_abs = os.path.abspath(agy_path)
    agy_parent = os.path.dirname(agy_abs)

    args = [agy_path, "-p", prompt]

    if has_session:
        if conversation_id:
            args.extend(["--conversation", conversation_id])
        else:
            args.append("--continue")
    else:
        args.append("--new-project")
    if model:
        from src.models import extract_model_and_effort
        clean_model, eff = extract_model_and_effort(model, effort)
        args.extend(["--model", clean_model])
        if eff:
            args.extend(["--effort", eff])
    elif effort:
        args.extend(["--effort", effort])
    if mode == "plan":
        args.extend(["--mode", "plan"])
    elif mode == "code":
        args.extend(["--mode", "accept-edits"])
    args.append("--dangerously-skip-permissions")
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
    inactivity_timeout: float | None = DEFAULT_INACTIVITY_TIMEOUT_S,
    print_timeout: str = DEFAULT_PRINT_TIMEOUT,
    effort: str = "",
    conversation_id: str = "",
    on_event: "Callable[[dict], Awaitable[None]] | None" = None,
    stop_event: asyncio.Event | None = None,
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
        conversation_id=conversation_id,
    )
    if on_event is not None:
        args.extend(["--output-format", "stream-json"])

    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=chat_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2 * 1024 * 1024,  # 2MB
            start_new_session=True, # Ensure agy runs in its own process group
        )
    except Exception as e:
        return AgyResult(text="", exit_code=-1, stderr=str(e))

    stdout_chunks = []
    stderr_chunks = []
    final_text = ""
    partial_text = ""
    final_error = ""  # API-level error from stream-json result event

    async def _terminate_process_group(sig_first: int = signal.SIGTERM, grace_s: float = 1.5) -> None:
        """Gracefully terminate process group so SQLite WAL and locks flush cleanly."""
        try:
            pgid = os.getpgid(process.pid)
        except OSError:
            return
        try:
            os.killpg(pgid, sig_first)
        except OSError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_s)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass

    last_activity_time = asyncio.get_event_loop().time()
    tool_active = False

    def record_activity():
        nonlocal last_activity_time
        last_activity_time = asyncio.get_event_loop().time()

    async def read_stdout():
        nonlocal final_text, partial_text, final_error, tool_active
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            record_activity()
            if on_event is not None:
                try:
                    data = json.loads(line.decode("utf-8", errors="replace"))
                    await on_event(data)
                    event_type = data.get("event")
                    if event_type == "step_update":
                        su = data.get("step_update", {})
                        stype = su.get("step_type")
                        state = su.get("state")
                        if stype == "tool":
                            if state == "ACTIVE":
                                tool_active = True
                            else:
                                tool_active = False
                        elif stype in ("agent_response", "user_input"):
                            tool_active = False
                            if stype == "agent_response":
                                resp_text = su.get("text") or su.get("content") or ""
                                if isinstance(resp_text, str) and resp_text.strip():
                                    partial_text = resp_text
                    elif event_type in ("result", "done"):
                        tool_active = False
                        result_obj = data.get("result", {})
                        final_text = result_obj.get("response", "")
                        final_error = result_obj.get("error", "")
                except Exception:
                    pass
            else:
                stdout_chunks.append(line)

    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            record_activity()
            stderr_chunks.append(line)

    activity_event = asyncio.Event()

    async def inactivity_watchdog():
        if inactivity_timeout is None or inactivity_timeout <= 0:
            return
        while True:
            await asyncio.sleep(0.5)
            if tool_active:
                # An action/tool (run_command, subagent, script, build, etc.)
                # is actively executing. Do not count execution duration
                # against the inactivity watchdog; let it run up to the overall turn limit.
                continue
            now = asyncio.get_event_loop().time()
            elapsed = now - last_activity_time
            remaining = inactivity_timeout - elapsed
            if remaining <= 0:
                activity_event.set()
                break
            await asyncio.sleep(min(remaining, 0.5))

    inactivity_task = asyncio.create_task(activity_event.wait())
    inactivity_watchdog_task = asyncio.create_task(inactivity_watchdog())
    stop_task = asyncio.create_task(stop_event.wait()) if stop_event else None

    async def run_all():
        await asyncio.gather(read_stdout(), read_stderr(), process.wait())

    main_task = asyncio.create_task(run_all())

    wait_tasks = [main_task, inactivity_task]
    if stop_task:
        wait_tasks.append(stop_task)

    try:
        done, pending = await asyncio.wait(
            wait_tasks,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=timeout,
        )

        if stop_task and stop_task in done:
            await _terminate_process_group(signal.SIGTERM, grace_s=1.5)
            main_task.cancel()
            await process.wait()
            if on_event is None:
                raw_text = b"".join(stdout_chunks).decode("utf-8", errors="replace").strip()
            else:
                raw_text = (final_text or partial_text).strip()
            text = clean_system_messages(raw_text)
            if text:
                text += "\n\n🛑 [Остановлено пользователем]"
            stderr_out = b"".join(stderr_chunks).decode("utf-8", errors="replace") + "\n[stopped by user]"
            return AgyResult(text=text, exit_code=130, stderr=stderr_out)

        elif inactivity_task in done:
            await _terminate_process_group(signal.SIGTERM, grace_s=1.0)
            main_task.cancel()
            await process.wait()
            msg = f"agy timed out: no status update for {int(inactivity_timeout)}s"
            raw_err = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            stderr_out = f"{raw_err}\n{msg}" if raw_err else msg
            return AgyResult(text=clean_system_messages(final_text or partial_text), exit_code=124, stderr=stderr_out)

        elif not done:  # Total timeout expired
            await _terminate_process_group(signal.SIGTERM, grace_s=1.0)
            main_task.cancel()
            await process.wait()
            msg = f"agy timed out after {timeout}s"
            raw_err = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            stderr_out = f"{raw_err}\n{msg}" if raw_err else msg
            return AgyResult(text=clean_system_messages(final_text or partial_text), exit_code=124, stderr=stderr_out)

        else:
            if main_task.exception() is not None:
                raise main_task.exception()

    except (asyncio.CancelledError, BaseException):
        if process.returncode is None:
            await _terminate_process_group(signal.SIGKILL, grace_s=0.5)
            try:
                await process.wait()
            except Exception:
                pass
        raise
    finally:
        inactivity_watchdog_task.cancel()
        inactivity_task.cancel()
        if stop_task:
            stop_task.cancel()
        if process.returncode is None:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
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
        # Merge API-level error into stderr so callers can log it.
        if final_error:
            prefix = stderr_out + "\n" if stderr_out else ""
            stderr_out = prefix + final_error

    return AgyResult(
        text=clean_system_messages(text),
        exit_code=process.returncode,
        stderr=stderr_out,
    )
