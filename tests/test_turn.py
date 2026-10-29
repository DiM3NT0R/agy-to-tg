"""Tests for src.turn — agy turn execution."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from src.agy_runner import AgyResult
from src.config import AgyConfig, Config, TelegramConfig
from src.state import ChatState
from src.turn import ACTIVE_STOPS, ActiveRun, execute_agy, request_stop, session_key


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


def test_stale_stop_callback_cannot_cancel_current_run() -> None:
    event = asyncio.Event()
    key = session_key(-100, 155)
    ACTIVE_STOPS[key] = ActiveRun(event, "current", 42, 99)
    try:
        assert request_stop(-100, 155, user_id=42, nonce="old", status_message_id=99) is False
        assert event.is_set() is False
        assert request_stop(-100, 155, user_id=42, nonce="current", status_message_id=98) is False
        assert request_stop(-100, 6, user_id=42, nonce="current", status_message_id=99) is False
        assert request_stop(-100, 155, user_id=42, nonce="current", status_message_id=99) is True
        assert event.is_set() is True
    finally:
        ACTIVE_STOPS.pop(key, None)


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


def test_is_quota_error_ignores_successful_turns() -> None:
    from src.turn import _is_quota_error
    from src.agy_runner import AgyResult

    # Exit code 0 with text mentioning quota should NOT be treated as error
    succ = AgyResult(text="Мы проверили лимит квоты: quota exceeded не обнаружено", exit_code=0, stderr="")
    assert _is_quota_error(succ) is False

    # Exit code 1 with stderr mentioning quota SHOULD be treated as error
    err = AgyResult(text="", exit_code=1, stderr="Error: quota exceeded")
    assert _is_quota_error(err) is True


def test_transient_rate_limit_is_retryable() -> None:
    from src.turn import _is_retryable_failure, _is_hard_quota_error
    from src.agy_runner import AgyResult

    # Transient 429 burst rate limit should be retryable
    transient = AgyResult(text="", exit_code=1, stderr="rpc error: code = ResourceExhausted desc = Rate limit exceeded")
    assert _is_hard_quota_error(transient) is False
    assert _is_retryable_failure(transient) is True

    # Hard weekly exhaustion should NOT be retryable
    hard = AgyResult(text="", exit_code=1, stderr="You have exceeded your quota limit")
    assert _is_hard_quota_error(hard) is True
    assert _is_retryable_failure(hard) is False


async def test_execute_agy_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.turn._RETRY_DELAY_S", 0.0)
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
    monkeypatch.setattr("src.turn._RETRY_DELAY_S", 0.0)
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


async def test_execute_agy_preserves_session_after_stop_when_conv_exists(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv_id = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr("src.turn._conversation_exists_on_disk", lambda cid: cid == conv_id)

    async def fake_run_agy(on_event: Any = None, **_: Any) -> AgyResult:
        if on_event:
            await on_event({"event": "init", "conversation_id": conv_id})
        return AgyResult(text="", exit_code=130, stderr="[stopped by user]")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir=str(tmp_path), has_session=False, conversation_id="")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "start task", _FakeMsg(), cs, cfg, "/usr/bin/agy")

    assert code == 130
    assert cs.has_session is True
    assert cs.conversation_id == conv_id


async def test_execute_agy_restores_prev_session_when_stopped_before_disk_persist(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    prev_cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    unpersisted_cid = "99999999-9999-9999-9999-999999999999"
    monkeypatch.setattr("src.turn._conversation_exists_on_disk", lambda cid: cid == prev_cid)

    async def fake_run_agy(on_event: Any = None, **_: Any) -> AgyResult:
        if on_event:
            await on_event({"event": "init", "conversation_id": unpersisted_cid})
        return AgyResult(text="", exit_code=130, stderr="[stopped by user]")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    tg = _FakeTG()
    cs = ChatState(chat_dir=str(tmp_path), has_session=True, conversation_id=prev_cid)
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "continue task", _FakeMsg(), cs, cfg, "/usr/bin/agy")

    assert code == 130
    assert cs.has_session is True
    assert cs.conversation_id == prev_cid


async def test_execute_agy_does_not_retry_unsupported_location(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_run_agy(**_: Any) -> AgyResult:
        nonlocal attempts
        attempts += 1
        return AgyResult(text="", exit_code=1, stderr="User location is not supported for the API use")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)
    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "hello", _FakeMsg(), cs, cfg, "/usr/bin/agy")
    assert code == 1
    assert attempts == 1
    assert "регион" in text.lower()


async def test_execute_agy_does_not_retry_invalid_model(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_run_agy(**_: Any) -> AgyResult:
        nonlocal attempts
        attempts += 1
        return AgyResult(
            text="",
            exit_code=1,
            stderr='error: invalid model selection (--model "claude-sonnet-4-6"): model claude-sonnet-4-6 is not recognized',
        )

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)
    tg = _FakeTG()
    cs = ChatState(chat_dir="/tmp/chat")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    text, code = await execute_agy(tg, 42, "hello", _FakeMsg(), cs, cfg, "/usr/bin/agy")
    assert code == 1
    assert attempts == 1
    assert "/model" in text

