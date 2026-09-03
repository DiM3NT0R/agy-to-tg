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


async def test_execute_agy_retries_on_servers_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_run_agy(**_: Any) -> AgyResult:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            return AgyResult(text="", exit_code=1, stderr="The model API is currently overloaded and servers are busy")
        return AgyResult(text="recovered reply", exit_code=0, stderr="")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "test", _FakeMsg(), cs, cfg, "/usr/bin/agy")
    assert code == 0
    assert text == "recovered reply"
    assert attempts == 4


async def test_execute_agy_does_not_retry_on_quota_error(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_run_agy(**_: Any) -> AgyResult:
        nonlocal attempts
        attempts += 1
        return AgyResult(text="", exit_code=1, stderr="Resource has been exhausted: quota exceeded")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "test", _FakeMsg(), cs, cfg, "/usr/bin/agy")
    assert code == 1
    assert attempts == 1  # No retries on quota exceeded
    assert "квота" in text.lower() or "quota" in text.lower()


async def test_execute_agy_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_run_agy(**_: Any) -> AgyResult:
        nonlocal attempts
        attempts += 1
        return AgyResult(text="", exit_code=1, stderr="server is busy")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "test", _FakeMsg(), cs, cfg, "/usr/bin/agy")
    assert code == 1
    assert attempts == 16  # 1 initial + 15 instant retries = 16 total attempts


async def test_execute_agy_resets_retries_on_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_run_agy(on_event: Any = None, **_: Any) -> AgyResult:
        nonlocal attempts
        attempts += 1
        # For the first 10 attempts, fail immediately with server busy (no progress)
        if attempts <= 10:
            return AgyResult(text="", exit_code=1, stderr="server is busy")
        # On attempt 11, make forward progress (step_update event), but still fail with transient error
        if attempts == 11:
            if on_event:
                await on_event({"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE"}})
            return AgyResult(text="", exit_code=1, stderr="server is busy")
        # Because progress was made at attempt 11, retry counter was reset,
        # so it sustains more retries beyond the initial 16!
        if attempts < 20:
            return AgyResult(text="", exit_code=1, stderr="server is busy")
        # Finally succeed at attempt 20
        return AgyResult(text="recovered after progress", exit_code=0, stderr="")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "test", _FakeMsg(), cs, cfg, "/usr/bin/agy")
    assert code == 0
    assert text == "recovered after progress"
    assert attempts == 20

