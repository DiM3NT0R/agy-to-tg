"""Tests for src.turn — agy turn execution."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from src.agy_runner import AgyResult
from src.config import AgyConfig, Config, TelegramConfig
from src.state import ChatState
from src.turn import execute_agy


@dataclass(frozen=True)
class _FakeMsg:
    chat_id: int = 42
    text: str = "hello"
    message_thread_id: int | None = None


class _FakeTG:
    def __init__(self) -> None:
        self.actions: list[tuple[int, str]] = []

    async def send_chat_action(self, chat_id: int, action: str = "typing", **kwargs: Any) -> None:
        self.actions.append((chat_id, action))
        
    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        return 1
        
    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs: Any) -> None:
        pass
        
    async def delete_message(self, chat_id: int, message_id: int) -> None:
        pass


async def test_execute_agy_returns_reply_and_records_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_run_agy(prompt: str, **kwargs: Any) -> AgyResult:
        calls.append({"prompt": prompt, **kwargs})
        await asyncio.sleep(0.01)
        return AgyResult(text="reply text", exit_code=0, stderr="")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "hello", _FakeMsg(text="hello"), cs, cfg, "/usr/bin/agy")

    assert code == 0
    assert text == "reply text"
    assert tg.actions
    assert calls[0]["prompt"] == "hello"
    assert calls[0]["chat_dir"] == "/tmp/chat"


async def test_execute_agy_returns_timeout_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_agy(**_: Any) -> AgyResult:
        return AgyResult(text="", exit_code=124, stderr="timeout")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "", _FakeMsg(), cs, cfg, "/usr/bin/agy")
    assert code == 124
    assert text == ""
