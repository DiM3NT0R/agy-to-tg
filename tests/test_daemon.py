"""Tests for src.daemon — orchestration with all I/O mocked."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.agy_runner import AgyResult
from src.config import AgyConfig, Config, TelegramConfig
from src.daemon import DaemonInfo, run
from src.state import load_state


pytestmark = pytest.mark.asyncio


@dataclass
class _FakeTelegram:
    updates_to_serve: list[list[dict[str, Any]]]
    sent_messages: list[tuple[int, str]]
    chat_actions: list[tuple[int, str]]

    async def __aenter__(self) -> "_FakeTelegram":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        if self.updates_to_serve:
            return self.updates_to_serve.pop(0)
        await asyncio.sleep(0)
        return []

    async def send_message(self, chat_id: int, text: str, *, keyboard: Any | None = None) -> int | None:
        self.sent_messages.append((chat_id, text))
        return None

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, *, keyboard: Any | None = None) -> None:
        self.sent_messages.append((chat_id, text))

    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
        return None

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.chat_actions.append((chat_id, action))


def _msg(text: str, *, update_id: int, user_id: int = 42, chat_id: int = 10) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "T"},
            "text": text,
        },
    }


def _cfg() -> Config:
    return Config(
        telegram=TelegramConfig(
            bot_token="TOKEN",
            allowed_user_ids=[42],
            allowed_chat_ids=[],
        ),
        agy=AgyConfig(default_workdir="/tmp", model="", mode="code"),
    )


async def _fake_execute_agy(
    tg: Any, chat_id: int, msg: Any, cs: Any, cfg: Any, agy_path: str,
) -> tuple[str, int]:
    return f"echo:{msg.text}", 0


async def test_run_replies_to_authorized_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "state.json"
    chats_root = tmp_path / "chats"
    tg = _FakeTelegram(
        updates_to_serve=[[_msg("hello", update_id=100)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    monkeypatch.setattr("src.daemon.execute_agy", _fake_execute_agy)

    await run(
        cfg=_cfg(),
        state_path=state_path,
        chats_root=chats_root,
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )

    assert tg.chat_actions and tg.chat_actions[0] == (10, "typing")
    assert len(tg.sent_messages) == 1
    chat_id, text = tg.sent_messages[0]
    assert chat_id == 10
    assert text == "echo:hello"

    persisted = load_state(state_path, chats_root)
    assert persisted.last_update_id == 100
    assert 10 in persisted.chats


async def test_run_drops_unauthorized_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tg = _FakeTelegram(
        updates_to_serve=[[_msg("hi", update_id=200, user_id=999)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    monkeypatch.setattr("src.daemon.execute_agy", _fake_execute_agy)

    await run(
        cfg=_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    assert tg.sent_messages == []
    assert tg.chat_actions == []


async def test_run_replies_with_error_when_agy_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_agy(*args: Any, **kwargs: Any) -> tuple[str, int]:
        return "", 2

    monkeypatch.setattr("src.daemon.execute_agy", failing_agy)

    tg = _FakeTelegram(
        updates_to_serve=[[_msg("hi", update_id=300)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    await run(
        cfg=_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    assert len(tg.sent_messages) == 1
    _, text = tg.sent_messages[0]
    assert "agy error" in text


async def test_run_uses_continue_after_first_successful_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    async def recording_agy(tg: Any, chat_id: int, msg: Any, cs: Any, cfg: Any, agy_path: str) -> tuple[str, int]:
        calls.append(cs.has_session)
        return "ok", 0

    monkeypatch.setattr("src.daemon.execute_agy", recording_agy)

    tg = _FakeTelegram(
        updates_to_serve=[
            [_msg("first", update_id=400)],
            [_msg("second", update_id=401)],
        ],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.4, stop.set)

    await run(
        cfg=_cfg(),
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=tg,
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )
    assert len(calls) == 2
    assert calls[0] is False
    assert calls[1] is True


async def test_run_logs_one_line_per_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    from src.agy_runner import AgyResult

    tg = _FakeTelegram(
        updates_to_serve=[[_msg("ping", update_id=500)]],
        sent_messages=[],
        chat_actions=[],
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    async def fake_run_agy(**kwargs: Any) -> AgyResult:
        return AgyResult(text=f"echo:{kwargs['prompt']}", exit_code=0, stderr="")

    monkeypatch.setattr("src.turn.run_agy", fake_run_agy)

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    import src.daemon as daemon_mod

    handler = _Capture()
    handler.setLevel(logging.INFO)
    daemon_mod.LOG.addHandler(handler)
    old_level = daemon_mod.LOG.level
    daemon_mod.LOG.setLevel(logging.INFO)
    print("HANDLERS:", daemon_mod.LOG.handlers, "LEVEL:", daemon_mod.LOG.level)
    try:
        await run(
            cfg=_cfg(),
            state_path=tmp_path / "state.json",
            chats_root=tmp_path / "chats",
            tg=tg,
            agy_path="/usr/bin/true",
            info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
            stop_event=stop,
        )
    finally:
        daemon_mod.LOG.setLevel(old_level)
        daemon_mod.LOG.removeHandler(handler)

    turn_lines = [r for r in captured if r.message.startswith("turn ")]
    assert len(turn_lines) == 1
    msg = turn_lines[0].message
    assert "chat=10" in msg
    assert "exit=0" in msg
    assert "ms=" in msg
    assert "reply_len=" in msg
