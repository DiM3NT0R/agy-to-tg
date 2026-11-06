"""Spawn the OpenAI Codex CLI (`codex`) and stream its JSON output.

Supports:
  - codex exec [PROMPT] --json ...
  - codex exec resume <thread_id> [PROMPT] --json ...
  - codex login --device-auth
  - codex login status
"""
from __future__ import annotations

import asyncio
import gc
import html
import json
import logging
import os
import re
import signal
from dataclasses import dataclass
from typing import Awaitable, Callable

LOG = logging.getLogger("antigravity_telegram_bridge")

_STDOUT_CAP_BYTES = 524_288  # 512 KiB
DEFAULT_OVERALL_TIMEOUT_S = 1800.0  # 30 minutes
DEFAULT_INACTIVITY_TIMEOUT_S = 600.0  # 10 minutes without model/system response


@dataclass(frozen=True)
class CodexResult:
    text: str
    exit_code: int
    stderr: str
    thread_id: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    thoughts: tuple[str, ...] = ()


def _split_thoughts_and_final(chunks: list[str]) -> tuple[tuple[str, ...], str]:
    if len(chunks) > 1:
        return tuple(chunks[:-1]), chunks[-1].strip()
    elif chunks:
        return (), chunks[0].strip()
    return (), ""


def _reap_zombies() -> None:
    try:
        while True:
            pid, _status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except (ChildProcessError, OSError):
        pass


def _build_codex_args(
    *,
    codex_path: str,
    prompt: str,
    thread_id: str,
    model: str,
    chat_dir: str,
    sandbox: str = "danger-full-access",
) -> list[str]:
    args = [codex_path]
    if sandbox:
        args.extend(["-s", sandbox])
    if chat_dir:
        args.extend(["-C", chat_dir])

    if thread_id:
        args.extend(["exec", "resume", "--json", "--skip-git-repo-check"])
        if model:
            args.extend(["-m", model])
        args.extend([thread_id, prompt])
    else:
        args.extend(["exec", "--json", "--skip-git-repo-check"])
        if model:
            args.extend(["-m", model])
        args.append(prompt)

    return args


async def run_codex(
    prompt: str,
    *,
    chat_dir: str,
    thread_id: str = "",
    model: str = "",
    codex_path: str = "/usr/local/bin/codex",
    timeout: float | None = None,
    inactivity_timeout: float | None = DEFAULT_INACTIVITY_TIMEOUT_S,
    on_event: "Callable[[dict], Awaitable[None]] | None" = None,
    stop_event: asyncio.Event | None = None,
) -> CodexResult:
    """Run codex exec (or resume) with JSON streaming output."""
    _reap_zombies()
    args = _build_codex_args(
        codex_path=codex_path,
        prompt=prompt,
        thread_id=thread_id,
        model=model,
        chat_dir=chat_dir,
    )

    env = os.environ.copy()
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=chat_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2 * 1024 * 1024,  # 2MB buffer
            start_new_session=True,  # Ensure codex runs in its own process group
        )
    except Exception as e:
        return CodexResult(text="", exit_code=-1, stderr=str(e))

    observed_thread_id = thread_id
    message_chunks: list[str] = []
    stderr_chunks: list[bytes] = []
    codex_error_message = ""
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    reasoning_output_tokens = 0

    tool_active = False
    last_activity_time = asyncio.get_event_loop().time()

    def record_activity():
        nonlocal last_activity_time
        last_activity_time = asyncio.get_event_loop().time()

    async def _terminate_process_group(sig_first: int = signal.SIGTERM, grace_s: float = 1.5) -> None:
        if process.returncode is not None:
            return
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, sig_first)
        except OSError:
            pass

        try:
            await asyncio.wait_for(process.wait(), timeout=grace_s)
        except asyncio.TimeoutError:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    async def read_stdout():
        nonlocal observed_thread_id, tool_active, input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, codex_error_message
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            record_activity()
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            ev_type = data.get("type", "")

            if ev_type == "thread.started":
                tid = data.get("thread_id")
                if tid:
                    observed_thread_id = tid
                    if on_event:
                        await on_event({"event": "init", "conversation_id": tid})

            elif ev_type == "error":
                err_msg = str(data.get("message") or "").strip()
                if err_msg:
                    codex_error_message = err_msg

            elif ev_type == "turn.failed":
                err_obj = data.get("error", {})
                err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                if err_msg:
                    codex_error_message = str(err_msg).strip()

            elif ev_type == "item.started":
                item = data.get("item", {})
                itype = item.get("type")
                if itype == "command_execution":
                    tool_active = True
                    cmd = item.get("command", "")
                    if on_event:
                        await on_event({
                            "event": "step_update",
                            "step_update": {
                                "step_type": "tool",
                                "state": "ACTIVE",
                                "tool_name": "run_command",
                                "tool_info": {"parameters": {"CommandLine": cmd, "toolSummary": cmd[:50]}},
                            },
                        })

            elif ev_type == "item.completed":
                item = data.get("item", {})
                itype = item.get("type")
                if itype == "command_execution":
                    tool_active = False
                    record_activity()
                elif itype == "agent_message":
                    text = item.get("text", "")
                    if text:
                        message_chunks.append(text)
                        if on_event:
                            await on_event({
                                "event": "step_update",
                                "step_update": {
                                    "step_type": "thought",
                                    "text": text,
                                    "state": "ACTIVE",
                                },
                            })
                elif itype == "error":
                    err_msg = str(item.get("message") or "").strip()
                    if err_msg:
                        codex_error_message = err_msg

            elif ev_type == "turn.completed":
                tool_active = False
                record_activity()
                usage = data.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                cached_input_tokens = usage.get("cached_input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                reasoning_output_tokens = usage.get("reasoning_output_tokens", 0)

    async def read_stderr():
        assert process.stderr is not None
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
            thoughts, text = _split_thoughts_and_final(message_chunks)
            if text:
                text += "\n\n🛑 [Остановлено пользователем]"
            stderr_out = b"".join(stderr_chunks).decode("utf-8", errors="replace") + "\n[stopped by user]"
            return CodexResult(
                text=text,
                exit_code=130,
                stderr=stderr_out,
                thread_id=observed_thread_id,
                thoughts=thoughts,
            )

        elif inactivity_task in done:
            await _terminate_process_group(signal.SIGTERM, grace_s=1.0)
            main_task.cancel()
            await process.wait()
            thoughts, text = _split_thoughts_and_final(message_chunks)
            msg = f"codex timed out: no status update for {int(inactivity_timeout)}s"
            raw_err = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            stderr_out = f"{raw_err}\n{msg}" if raw_err else msg
            return CodexResult(
                text=text,
                exit_code=124,
                stderr=stderr_out,
                thread_id=observed_thread_id,
                thoughts=thoughts,
            )

        elif not done:  # Total timeout expired
            await _terminate_process_group(signal.SIGTERM, grace_s=1.0)
            main_task.cancel()
            await process.wait()
            thoughts, text = _split_thoughts_and_final(message_chunks)
            msg = f"codex timed out after {timeout}s"
            raw_err = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            stderr_out = f"{raw_err}\n{msg}" if raw_err else msg
            return CodexResult(
                text=text,
                exit_code=124,
                stderr=stderr_out,
                thread_id=observed_thread_id,
                thoughts=thoughts,
            )

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
    thoughts, final_text = _split_thoughts_and_final(message_chunks)

    err_text = final_text
    if not err_text and (process.returncode or 0) != 0:
        if codex_error_message:
            low_msg = codex_error_message.lower()
            if "usage limit" in low_msg or "quota" in low_msg or "rate limit" in low_msg:
                err_text = (
                    f"⚠️ <b>Исчерпан лимит использования OpenAI (Usage limit reached)</b>\n\n"
                    f"{html.escape(codex_error_message)}\n\n"
                    "💡 <i>Переключитесь на другую модель (например, Gemini Flash) через /model или смените движок на AGY через /engine agy.</i>"
                )
            else:
                err_text = f"⚠️ Ошибка Codex:\n<code>{html.escape(codex_error_message)}</code>"
        elif stderr_out.strip():
            clean_err = re.sub(r"\x1b\[[0-9;]*[mK]", "", stderr_out).strip()
            err_text = f"⚠️ Ошибка Codex (код {process.returncode}):\n<code>{html.escape(clean_err[:1000])}</code>"

    return CodexResult(
        text=err_text,
        exit_code=process.returncode or 0,
        stderr=stderr_out,
        thread_id=observed_thread_id,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        thoughts=thoughts,
    )


# --- Authentication & Device Auth flow ---

_DEVICE_URL_RE = re.compile(r"https://auth\.openai\.com/[^\s]+")
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,5})\b")


async def check_codex_auth(codex_path: str = "/usr/local/bin/codex") -> tuple[bool, str]:
    """Check if codex is authenticated."""
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_path, "login", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = (stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")).strip()
        if proc.returncode == 0 and "Logged in" in out:
            # Extract just the "Logged in..." line
            for line in out.splitlines():
                if "logged in" in line.lower():
                    return True, line.strip()
            return True, out
        return False, out
    except Exception as e:
        return False, str(e)


async def run_codex_device_auth(
    on_code: Callable[[str, str], Awaitable[None]],
    codex_path: str = "/usr/local/bin/codex",
    cancel_event: asyncio.Event | None = None,
    timeout_s: float = 900.0,  # 15 minutes
) -> tuple[bool, str]:
    """Execute codex login --device-auth and invoke on_code(url, code).

    Waits for the user to authenticate in their browser.
    """
    _reap_zombies()
    try:
        # We redirect stderr to stdout so all auth prompts are read in order
        proc = await asyncio.create_subprocess_exec(
            codex_path, "login", "--device-auth",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        return False, f"Не удалось запустить codex login: {e}"

    captured_url = ""
    captured_code = ""
    code_reported = False

    async def read_lines():
        nonlocal captured_url, captured_code, code_reported
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            url_match = _DEVICE_URL_RE.search(text)
            if url_match and not captured_url:
                captured_url = url_match.group(0)

            code_match = _DEVICE_CODE_RE.search(text)
            if code_match and not captured_code:
                captured_code = code_match.group(1)

            if captured_url and captured_code and not code_reported:
                code_reported = True
                try:
                    await on_code(captured_url, captured_code)
                except Exception as e:
                    LOG.warning("on_code callback failed: %s", e)

    read_task = asyncio.create_task(read_lines())
    wait_proc = asyncio.create_task(proc.wait())
    cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event else None

    wait_list = [read_task, wait_proc]
    if cancel_task:
        wait_list.append(cancel_task)

    try:
        done, _ = await asyncio.wait(wait_list, return_when=asyncio.FIRST_COMPLETED, timeout=timeout_s)

        if cancel_task and cancel_task in done:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
            return False, "Авторизация отменена пользователем"

        if wait_proc in done:
            rc = proc.returncode
            if rc == 0:
                return True, "Авторизация успешно завершена!"
            return False, f"Ошибка авторизации (код {rc})"

        # Timeout
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        return False, "Время ожидания авторизации истекло (15 минут)"
    finally:
        read_task.cancel()
        wait_proc.cancel()
        if cancel_task:
            cancel_task.cancel()


async def queue_codex_message(
    thread_id: str,
    message: str,
    codex_path: str = "/usr/local/bin/codex",
) -> tuple[bool, str]:
    """Queue a message to an active or existing codex thread."""
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_path, "queue", "--thread", thread_id, "--message", message,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, stdout.decode().strip()
        return False, stderr.decode().strip()
    except Exception as e:
        return False, str(e)
