"""Tests for src.voice — STT, TTS, and reply text caching."""
from __future__ import annotations

from typing import Any

import pytest

from src.config import AgyConfig, Config, TelegramConfig
from src.daemon import DaemonInfo, run
from src.telegram import parse_callback_query, parse_update
from src.voice import (
    _split_tts_chunks,
    cache_reply_text,
    get_cached_reply_text,
    strip_markdown_for_speech,
)


def test_parse_update_voice_only_message() -> None:
    upd = {
        "update_id": 10,
        "message": {
            "message_id": 99,
            "date": 0,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 42, "is_bot": False, "first_name": "D"},
            "message_thread_id": 175,
            "voice": {"file_id": "v123", "duration": 5, "mime_type": "audio/ogg"},
        },
    }
    msg = parse_update(upd)
    assert msg is not None
    assert msg.text == ""
    assert msg.voice is not None
    assert msg.voice["file_id"] == "v123"
    assert msg.message_thread_id == 175


def test_parse_callback_query_captures_message_text() -> None:
    upd = {
        "update_id": 11,
        "callback_query": {
            "id": "cq1",
            "from": {"id": 42},
            "message": {
                "message_id": 100,
                "chat": {"id": -100},
                "message_thread_id": 175,
                "text": "Ответ бота для озвучки",
            },
            "data": "tts",
        },
    }
    cq = parse_callback_query(upd)
    assert cq is not None
    assert cq.data == "tts"
    assert cq.message_text == "Ответ бота для озвучки"


def test_strip_markdown_for_speech_and_chunking() -> None:
    raw = (
        "### Заголовок\n"
        "Проверка **жирного** текста и [ссылки](https://example.com).\n"
        "```python\nprint('hello')\n```\n"
        "- Первый пункт\n"
    )
    cleaned = strip_markdown_for_speech(raw)
    assert "Заголовок" in cleaned
    assert "жирного" in cleaned
    assert "ссылки" in cleaned
    assert "https://example.com" not in cleaned
    assert "[блок кода]" in cleaned
    chunks = _split_tts_chunks(raw, max_chars=50)
    assert len(chunks) >= 2
    assert all(len(c) <= 50 for c in chunks)


def test_reply_text_cache_roundtrip() -> None:
    cache_reply_text(-100, 555, "Тестовый ответ")
    assert get_cached_reply_text(-100, 555) == "Тестовый ответ"


@pytest.mark.asyncio
async def test_voice_message_transcribes_and_triggers_turn(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sent_messages: list[tuple[int, str, Any]] = []
    edited_messages: list[tuple[int, int, str]] = []
    seen_prompts: list[str] = []

    class FakeTG:
        def __init__(self) -> None:
            self.updates = [
                [
                    {
                        "update_id": 700,
                        "message": {
                            "message_id": 50,
                            "date": 0,
                            "chat": {"id": 10, "type": "private"},
                            "from": {"id": 42, "is_bot": False, "first_name": "T"},
                            "voice": {"file_id": "voice_fid_1"},
                        },
                    }
                ]
            ]

        async def __aenter__(self) -> "FakeTG":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
            if self.updates:
                return self.updates.pop(0)
            import asyncio
            await asyncio.sleep(0)
            return []

        async def get_file(self, file_id: str) -> bytes:
            assert file_id == "voice_fid_1"
            return b"OggS-fake-voice"

        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
            sent_messages.append((chat_id, text, kwargs.get("keyboard")))
            return len(sent_messages)

        async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs: Any) -> None:
            edited_messages.append((chat_id, message_id, text))

        async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
            return None

        async def send_chat_action(self, chat_id: int, action: str = "typing", **kwargs: Any) -> None:
            return None

        async def delete_message(self, chat_id: int, message_id: int) -> None:
            return None

    async def fake_transcribe(audio_bytes: bytes, api_key: str, **kwargs: Any) -> str:
        assert audio_bytes == b"OggS-fake-voice"
        assert api_key == "gsk_test"
        return "Проверь статус сервера"

    async def fake_execute_agy(tg: Any, chat_id: int, prompt: str, msg: Any, cs: Any, cfg: Any, agy_path: str) -> tuple[str, int]:
        seen_prompts.append(prompt)
        return "Сервер в порядке", 0

    monkeypatch.setattr("src.daemon.transcribe_voice_groq", fake_transcribe)
    monkeypatch.setattr("src.daemon.execute_agy", fake_execute_agy)

    import asyncio
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    cfg = Config(
        telegram=TelegramConfig(bot_token="T", allowed_user_ids=[42], groq_api_key="gsk_test"),
        agy=AgyConfig(default_workdir="/tmp", model="", mode="code"),
    )
    await run(
        cfg=cfg,
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=FakeTG(),
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )

    assert len(seen_prompts) == 1
    assert "Проверь статус сервера" in seen_prompts[0]
    assert any("Сервер в порядке" in m[1] for m in sent_messages)
    # Check that the reply has the "🔊 Озвучить" inline button
    final_msg = [m for m in sent_messages if "Сервер в порядке" in m[1]][-1]
    assert final_msg[2] == [[{"text": "🔊 Озвучить", "callback_data": "tts"}]]


@pytest.mark.asyncio
async def test_tts_callback_synthesizes_and_sends_voice(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sent_voices: list[tuple[int, bytes, str, int | None]] = []

    class FakeTG:
        def __init__(self) -> None:
            self.updates = [
                [
                    {
                        "update_id": 800,
                        "callback_query": {
                            "id": "cq_tts_1",
                            "from": {"id": 42},
                            "message": {
                                "message_id": 77,
                                "chat": {"id": 10},
                                "text": "Привет, это текстовый ответ бота",
                            },
                            "data": "tts",
                        },
                    }
                ]
            ]

        async def __aenter__(self) -> "FakeTG":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
            if self.updates:
                return self.updates.pop(0)
            import asyncio
            await asyncio.sleep(0)
            return []

        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
            return 1

        async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs: Any) -> None:
            return None

        async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
            return None

        async def send_chat_action(self, chat_id: int, action: str = "typing", **kwargs: Any) -> None:
            return None

        async def delete_message(self, chat_id: int, message_id: int) -> None:
            return None

        async def send_voice(
            self,
            chat_id: int,
            voice_bytes: bytes,
            *,
            mime_type: str = "audio/ogg",
            caption: str = "",
            message_thread_id: int | None = None,
            reply_to_message_id: int | None = None,
        ) -> int | None:
            sent_voices.append((chat_id, voice_bytes, mime_type, reply_to_message_id))
            return 88

    async def fake_synthesize(text: str, **kwargs: Any) -> tuple[bytes, str]:
        assert "Привет, это текстовый ответ бота" in text
        return b"FAKE_MP3_AUDIO", "audio/mpeg"

    monkeypatch.setattr("src.daemon.synthesize_speech", fake_synthesize)

    import asyncio
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    cfg = Config(
        telegram=TelegramConfig(bot_token="T", allowed_user_ids=[42]),
        agy=AgyConfig(default_workdir="/tmp", model="", mode="code"),
    )
    await run(
        cfg=cfg,
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=FakeTG(),
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )

    assert len(sent_voices) == 1
    assert sent_voices[0] == (10, b"FAKE_MP3_AUDIO", "audio/mpeg", 77)


@pytest.mark.asyncio
async def test_auto_voice_synthesizes_automatically_on_turn(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sent_voices: list[tuple[int, bytes, str, int | None]] = []
    seen_tts_kwargs: list[dict[str, Any]] = []

    class FakeTG:
        def __init__(self) -> None:
            self.updates = [
                [
                    {
                        "update_id": 900,
                        "message": {
                            "message_id": 50,
                            "date": 0,
                            "chat": {"id": 10, "type": "private"},
                            "from": {"id": 42, "is_bot": False, "first_name": "T"},
                            "text": "Скажи привет",
                        },
                    }
                ]
            ]

        async def __aenter__(self) -> "FakeTG":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
            if self.updates:
                return self.updates.pop(0)
            import asyncio
            await asyncio.sleep(0)
            return []

        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
            return 101

        async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs: Any) -> None:
            return None

        async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
            return None

        async def send_chat_action(self, chat_id: int, action: str = "typing", **kwargs: Any) -> None:
            return None

        async def delete_message(self, chat_id: int, message_id: int) -> None:
            return None

        async def send_voice(
            self,
            chat_id: int,
            voice_bytes: bytes,
            *,
            mime_type: str = "audio/ogg",
            caption: str = "",
            message_thread_id: int | None = None,
            reply_to_message_id: int | None = None,
        ) -> int | None:
            sent_voices.append((chat_id, voice_bytes, mime_type, reply_to_message_id))
            return 102

    async def fake_synthesize(text: str, **kwargs: Any) -> tuple[bytes, str]:
        seen_tts_kwargs.append(kwargs)
        return b"AUTO_VOICE_BYTES", "audio/ogg"

    async def fake_execute_agy(tg: Any, chat_id: int, prompt: str, msg: Any, cs: Any, cfg: Any, agy_path: str) -> tuple[str, int]:
        cs.auto_voice = True
        cs.voice = "zahar"
        cs.voice_speed = "1.3"
        return "Привет, человек!", 0

    monkeypatch.setattr("src.daemon.synthesize_speech", fake_synthesize)
    monkeypatch.setattr("src.daemon.execute_agy", fake_execute_agy)

    import asyncio
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)

    cfg = Config(
        telegram=TelegramConfig(bot_token="T", allowed_user_ids=[42]),
        agy=AgyConfig(default_workdir="/tmp", model="", mode="code"),
    )
    await run(
        cfg=cfg,
        state_path=tmp_path / "state.json",
        chats_root=tmp_path / "chats",
        tg=FakeTG(),
        agy_path="/usr/bin/true",
        info=DaemonInfo(bot_username="@bot", started_at=0.0, agy_version="1.0"),
        stop_event=stop,
    )

    assert len(sent_voices) == 1
    assert sent_voices[0] == (10, b"AUTO_VOICE_BYTES", "audio/ogg", 101)
    assert len(seen_tts_kwargs) == 1
    assert seen_tts_kwargs[0]["voice"] == "zahar"
    assert seen_tts_kwargs[0]["speed"] == "1.3"

