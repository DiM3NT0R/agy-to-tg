"""Universal OpenAI-compatible API runner with streaming, thinking, and tool execution.

Supports any provider implementing the standard /v1/chat/completions endpoint
(ceapai.io, OpenRouter, DeepSeek, Groq, Ollama, vLLM, LM Studio, etc.).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from src.openai_providers import OpenAIProvider

LOG = logging.getLogger("antigravity_telegram_bridge")

DEFAULT_SYSTEM_PROMPT = (
    "You are an expert AI software engineer assistant embedded in Telegram. "
    "Be direct, concise, and helpful. You have access to local workspace tools "
    "to inspect files, write changes, and run shell commands in the project directory when needed."
)

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a bash shell command in the project root workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bash command line to execute.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents from the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace directory.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace directory.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete contents to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
]


@dataclass(frozen=True)
class OpenAICompatResult:
    text: str
    exit_code: int
    stderr: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    thoughts: tuple[str, ...] = ()


def _safe_resolve_path(workspace: str, rel_path: str) -> Path | None:
    try:
        base = Path(workspace).resolve()
        target = (base / rel_path).resolve()
        if base == target or base in target.parents:
            return target
    except Exception:
        pass
    return None


async def _execute_local_tool(name: str, args: dict[str, Any], workspace: str) -> str:
    """Execute a local tool call safely within the chat workspace."""
    if name == "run_command":
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            return "Error: Empty command."
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=45.0)
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            output = ""
            if stdout:
                output += stdout
            if stderr:
                output += ("\n" if output else "") + f"[stderr]\n{stderr}"
            if proc.returncode != 0:
                output += f"\n(exit code {proc.returncode})"
            # Cap output to 8 KiB
            if len(output) > 8192:
                output = output[:4096] + "\n...[truncated]...\n" + output[-4096:]
            return output or "(empty output, exit code 0)"
        except asyncio.TimeoutError:
            return "Error: Command timed out after 45 seconds."
        except Exception as exc:
            return f"Error executing command: {exc}"

    elif name == "read_file":
        path_str = str(args.get("path") or "").strip()
        target = _safe_resolve_path(workspace, path_str)
        if not target or not target.exists() or not target.is_file():
            return f"Error: File not found: {path_str}"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            if len(content) > 16384:
                content = content[:8192] + "\n...[truncated]...\n" + content[-8192:]
            return content
        except Exception as exc:
            return f"Error reading file: {exc}"

    elif name == "write_file":
        path_str = str(args.get("path") or "").strip()
        content = str(args.get("content") or "")
        target = _safe_resolve_path(workspace, path_str)
        if not target:
            return f"Error: Invalid or outside path: {path_str}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {path_str}"
        except Exception as exc:
            return f"Error writing file: {exc}"

    return f"Error: Unknown tool: {name}"


async def run_openai_compat(
    prompt: str,
    *,
    chat_dir: str,
    provider: OpenAIProvider,
    model: str = "",
    system_prompt: str = "",
    timeout: float = 600.0,
    on_thought: Callable[[str], Awaitable[None]] | None = None,
    on_tool: Callable[[str], Awaitable[None]] | None = None,
    on_status: Callable[[str], Awaitable[None]] | None = None,
    stop_event: asyncio.Event | None = None,
    max_tool_iterations: int = 8,
) -> OpenAICompatResult:
    """Run an agentic conversational turn against an OpenAI-compatible endpoint."""
    target_model = model or provider.default_model or "gpt-4o"
    base_url = provider.base_url.rstrip("/")
    endpoint = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
        "User-Agent": "agent-tg-bridge/0.2.0",
    }

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    total_in_tokens = 0
    total_out_tokens = 0
    total_reasoning_tokens = 0
    all_thoughts: list[str] = []

    use_tools = True
    final_text = ""

    for iteration in range(max_tool_iterations):
        if stop_event and stop_event.is_set():
            return OpenAICompatResult(
                text="🛑 Выполнение остановлено пользователем.",
                exit_code=130,
                stderr="User stopped execution.",
            )

        payload: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "stream": True,
        }
        if use_tools:
            payload["tools"] = TOOL_DEFINITIONS
            payload["tool_choice"] = "auto"

        curr_text_chunks: list[str] = []
        curr_reasoning_chunks: list[str] = []
        tool_calls_accumulator: dict[int, dict[str, Any]] = {}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
                    if resp.status_code == 400 and use_tools:
                        # Some providers do not support tools parameter — retry without tools
                        resp_body = await resp.aread()
                        if b"tool" in resp_body.lower() or b"function" in resp_body.lower():
                            LOG.info("Provider %s does not support tools, disabling for turn.", provider.id)
                            use_tools = False
                            continue
                        return OpenAICompatResult(
                            text="",
                            exit_code=1,
                            stderr=f"HTTP 400: {resp_body.decode('utf-8', errors='replace')}",
                        )
                    if resp.status_code != 200:
                        body = await resp.aread()
                        err_msg = body.decode("utf-8", errors="replace")
                        return OpenAICompatResult(
                            text="",
                            exit_code=1,
                            stderr=f"HTTP {resp.status_code}: {err_msg}",
                        )

                    async for raw_line in resp.aiter_lines():
                        if stop_event and stop_event.is_set():
                            break
                        line = raw_line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except Exception:
                            continue

                        # Extract token usage if available
                        usage = data.get("usage")
                        if usage and isinstance(usage, dict):
                            total_in_tokens += usage.get("prompt_tokens") or 0
                            total_out_tokens += usage.get("completion_tokens") or 0
                            details = usage.get("completion_tokens_details") or {}
                            total_reasoning_tokens += details.get("reasoning_tokens") or 0

                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}

                        # 1. Reasoning / Thinking delta (DeepSeek R1 / o1 / o3)
                        r_delta = delta.get("reasoning_content") or delta.get("reasoning")
                        if r_delta:
                            curr_reasoning_chunks.append(r_delta)
                            if on_thought:
                                await on_thought("".join(curr_reasoning_chunks))

                        # 2. Text delta
                        c_delta = delta.get("content")
                        if c_delta:
                            curr_text_chunks.append(c_delta)

                        # 3. Tool calls delta
                        tc_delta_list = delta.get("tool_calls")
                        if tc_delta_list and isinstance(tc_delta_list, list):
                            for tc in tc_delta_list:
                                idx = tc.get("index", 0)
                                entry = tool_calls_accumulator.setdefault(
                                    idx, {"id": "", "name": "", "arguments": ""}
                                )
                                if tc.get("id"):
                                    entry["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    entry["name"] = fn["name"]
                                if fn.get("arguments"):
                                    entry["arguments"] += fn["arguments"]

        except Exception as exc:
            LOG.error("OpenAI-compatible request to %s failed: %s", provider.id, exc)
            return OpenAICompatResult(text="", exit_code=1, stderr=str(exc))

        raw_assistant_text = "".join(curr_text_chunks)
        reasoning_text = "".join(curr_reasoning_chunks)

        # Parse <think>...</think> if emitted inside content
        if "<think>" in raw_assistant_text and "</think>" in raw_assistant_text:
            th_part = raw_assistant_text.split("</think>")[0].split("<think>")[1].strip()
            raw_assistant_text = raw_assistant_text.split("</think>")[1].strip()
            if th_part:
                all_thoughts.append(th_part)
                if on_thought:
                    await on_thought(th_part)

        if reasoning_text.strip():
            all_thoughts.append(reasoning_text.strip())

        # If there are tool calls to execute
        if tool_calls_accumulator:
            tool_calls_list = []
            for idx in sorted(tool_calls_accumulator.keys()):
                item = tool_calls_accumulator[idx]
                tool_calls_list.append({
                    "id": item["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": item["arguments"],
                    },
                })

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": raw_assistant_text or None,
                "tool_calls": tool_calls_list,
            }
            messages.append(assistant_msg)

            # Execute tool calls
            for tc in tool_calls_list:
                fn_name = tc["function"]["name"]
                raw_args = tc["function"]["arguments"]
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:
                    parsed_args = {"raw": raw_args}

                if on_tool:
                    if fn_name == "run_command":
                        cmd_preview = str(parsed_args.get("command") or "").split("\n")[0][:45]
                        await on_tool(f"💻 <b>Команда:</b> <code>{html.escape(cmd_preview)}</code>")
                    elif fn_name in ("read_file", "write_file"):
                        await on_tool(f"📄 <b>Файл:</b> <code>{html.escape(str(parsed_args.get('path')))}</code>")
                    else:
                        await on_tool(f"⚙️ <b>Инструмент:</b> <code>{html.escape(fn_name)}</code>")

                tool_res = await _execute_local_tool(fn_name, parsed_args, chat_dir)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_res,
                })

            # Next iteration with tool results
            continue

        # No tool calls: final response reached!
        final_text = raw_assistant_text
        break

    # If usage tokens weren't reported by provider, estimate roughly
    if total_in_tokens == 0:
        total_in_tokens = len(prompt) // 4
    if total_out_tokens == 0:
        total_out_tokens = len(final_text) // 4

    return OpenAICompatResult(
        text=final_text,
        exit_code=0,
        stderr="",
        input_tokens=total_in_tokens,
        output_tokens=total_out_tokens,
        reasoning_tokens=total_reasoning_tokens,
        thoughts=tuple(all_thoughts),
    )
