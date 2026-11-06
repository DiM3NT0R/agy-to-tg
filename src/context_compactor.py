"""Context compaction and cross-engine handoff layer.

Optimizes token usage when switching between engines (agy <-> codex) or
when the user explicitly compacts a chat session (/compact).
Instead of passing bloated raw transcript dumps (50k-100k+ tokens),
this layer synthesizes a high-density structured summary (~400-800 tokens).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.state import ChatState

LOG = logging.getLogger("antigravity_telegram_bridge")


def get_git_changed_files(chat_dir: str) -> list[str]:
    """Return list of modified or untracked files in the workspace, if git repo."""
    try:
        res = subprocess.run(
            ["git", "status", "--short"],
            cwd=chat_dir,
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout:
            lines = res.stdout.strip().splitlines()
            return [line.strip() for line in lines[:10]]
    except Exception:
        pass
    return []


def get_agy_session_summary(conversation_id: str) -> str:
    """Fetch high-level preview/title from agy's conversation_summaries.db."""
    if not conversation_id:
        return ""
    db_path = os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")
    if not os.path.exists(db_path):
        return ""
    try:
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            c = conn.cursor()
            c.execute(
                "SELECT title, preview FROM conversation_summaries WHERE conversation_id = ? LIMIT 1",
                (conversation_id,),
            )
            row = c.fetchone()
            if row:
                title, preview = row
                parts = []
                if title:
                    parts.append(title.strip())
                if preview and preview != title:
                    parts.append(preview.strip())
                return " — ".join(parts)
    except Exception as e:
        LOG.debug("Failed to query agy conversation_summaries: %s", e)
    return ""


def get_codex_session_summary(thread_id: str) -> str:
    """Fetch recent agent summary/items from codex thread_history_1.sqlite."""
    if not thread_id:
        return ""
    db_path = os.path.expanduser("~/.codex/thread_history_1.sqlite")
    if not os.path.exists(db_path):
        return ""
    try:
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            c = conn.cursor()
            c.execute(
                "SELECT item_json FROM thread_items WHERE thread_id = ? AND item_type = 'agent_message' ORDER BY rowid DESC LIMIT 2",
                (thread_id,),
            )
            rows = c.fetchall()
            summaries = []
            for (raw_json,) in rows:
                try:
                    data = json.loads(raw_json)
                    text = data.get("text", "").strip()
                    if text:
                        # Extract first 200 chars
                        snip = text[:200] + "..." if len(text) > 200 else text
                        summaries.append(snip)
                except Exception:
                    continue
            if summaries:
                return " | ".join(reversed(summaries))
    except Exception as e:
        LOG.debug("Failed to query codex thread_history: %s", e)
    return ""


def build_handoff_digest(
    chat_dir: str,
    from_engine: str,
    to_engine: str,
    conversation_id: str = "",
    codex_thread_id: str = "",
) -> str:
    """Build a compact (~400-600 tokens) cross-engine handoff memorandum."""
    lines = [
        f"[Контекст: переключение движка с {from_engine.upper()} на {to_engine.upper()}]",
        "Для оптимизации токенов сырая история очищена. Ключевое состояние проекта:",
    ]

    # 1. Summary from previous engine
    prev_summary = ""
    if from_engine == "agy":
        prev_summary = get_agy_session_summary(conversation_id)
    elif from_engine == "codex":
        prev_summary = get_codex_session_summary(codex_thread_id)
    elif from_engine in ("openai", "openai_compat"):
        prev_summary = "OpenAI-compatible turn"

    if prev_summary:
        lines.append(f"• Текущий контекст/задача: {prev_summary}")

    # 2. Changed files in workspace
    changed_files = get_git_changed_files(chat_dir)
    if changed_files:
        lines.append("• Измененные/созданные файлы в репозитории:")
        for cf in changed_files[:6]:
            lines.append(f"  {cf}")

    lines.append("Продолжай выполнение задачи с учетом вышеизложенного контекста.")
    return "\n".join(lines)


def prepare_turn_prompt(
    user_prompt: str,
    cs: "ChatState",
    target_engine: str,
) -> str:
    """Prefix user prompt with handoff digest if switching engines for the first time."""
    current_engine = getattr(cs, "engine", "") or "agy"

    # If engine changed, build handoff digest
    if current_engine != target_engine:
        # Check if target engine already has an active session
        has_target_session = False
        if target_engine == "codex" and getattr(cs, "codex_thread_id", ""):
            has_target_session = True
        elif target_engine == "agy" and getattr(cs, "conversation_id", ""):
            has_target_session = True

        # If target engine has NO active session, inject handoff digest to boot it
        if not has_target_session:
            digest = build_handoff_digest(
                chat_dir=cs.chat_dir,
                from_engine=current_engine,
                to_engine=target_engine,
                conversation_id=getattr(cs, "conversation_id", ""),
                codex_thread_id=getattr(cs, "codex_thread_id", ""),
            )
            return f"{digest}\n\n[Запрос пользователя]:\n{user_prompt}"

    return user_prompt
