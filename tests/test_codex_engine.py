"""Tests for Codex runner, models and context compactor."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.codex_runner import CodexResult, _build_codex_args, _split_thoughts_and_final, check_codex_auth
from src.context_compactor import build_handoff_digest, prepare_turn_prompt
from src.models import get_available_codex_models, get_engine_for_model
from src.state import ChatState


def test_build_codex_args():
    # Fresh session
    args = _build_codex_args(
        codex_path="/usr/local/bin/codex",
        prompt="Hello Codex",
        thread_id="",
        model="gpt-6.1-sol",
        chat_dir="/tmp/test",
    )
    assert args == [
        "/usr/local/bin/codex",
        "exec",
        "Hello Codex",
        "--json",
        "--skip-git-repo-check",
        "-s",
        "danger-full-access",
        "-C",
        "/tmp/test",
        "-m",
        "gpt-6.1-sol",
    ]

    # Resumed session
    args_resume = _build_codex_args(
        codex_path="/usr/local/bin/codex",
        prompt="Next turn",
        thread_id="01a1adbc-1234",
        model="",
        chat_dir="/tmp/test",
    )
    assert args_resume == [
        "/usr/local/bin/codex",
        "exec",
        "resume",
        "01a1adbc-1234",
        "Next turn",
        "--json",
        "--skip-git-repo-check",
        "-s",
        "danger-full-access",
        "-C",
        "/tmp/test",
    ]


def test_get_engine_for_model():
    assert get_engine_for_model("gemini-4-argon") == "agy"
    assert get_engine_for_model("gemini-3.8-flash-high") == "agy"
    assert get_engine_for_model("claude-opus-5-5-high") == "agy"
    assert get_engine_for_model("gpt-oss-120b-medium") == "agy"

    assert get_engine_for_model("gpt-6.1-sol") == "codex"
    assert get_engine_for_model("gpt-6-astra") == "codex"
    assert get_engine_for_model("gpt-5.6-luna") == "codex"
    assert get_engine_for_model("codex-mini") == "codex"
    assert get_engine_for_model("o3") == "codex"


def test_get_available_codex_models():
    models = get_available_codex_models()
    assert len(models) > 0
    slugs = [slug for slug, _ in models]
    assert "gpt-6.1-sol" in slugs


def test_context_compactor_handoff():
    cs = ChatState(chat_dir="/tmp", engine="agy", conversation_id="conv-123")
    prompt = "Fix the login bug"

    # Switching from agy to codex with no active codex session
    prepared = prepare_turn_prompt(prompt, cs, target_engine="codex")
    assert "[Контекст: переключение движка с AGY на CODEX]" in prepared
    assert "Fix the login bug" in prepared

    # Same engine -> prompt unchanged
    cs.engine = "codex"
    cs.codex_thread_id = "thread-xyz"
    same = prepare_turn_prompt("Another question", cs, target_engine="codex")
    assert same == "Another question"


def test_split_thoughts_and_final():
    # Multiple messages: intermediate thoughts + final answer
    chunks = [
        "Thinking: check VPN logs",
        "Thinking: ports are open",
        "Final conclusion: VPN is working",
    ]
    thoughts, final_text = _split_thoughts_and_final(chunks)
    assert thoughts == ("Thinking: check VPN logs", "Thinking: ports are open")
    assert final_text == "Final conclusion: VPN is working"

    # Single message
    single_thoughts, single_text = _split_thoughts_and_final(["Only one answer"])
    assert single_thoughts == ()
    assert single_text == "Only one answer"

    # Empty
    empty_thoughts, empty_text = _split_thoughts_and_final([])
    assert empty_thoughts == ()
    assert empty_text == ""


@pytest.mark.asyncio
async def test_check_codex_auth_logged_in():
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"Logged in using ChatGPT\n"))
        proc.returncode = 0
        mock_exec.return_value = proc

        ok, msg = await check_codex_auth()
        assert ok is True
        assert "Logged in using ChatGPT" in msg
