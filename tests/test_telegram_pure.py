"""Tests for the pure helpers in src.telegram."""
from __future__ import annotations

import pytest

from src.config import TelegramConfig
from src.telegram import InboundMessage, chunk_message, is_authorized, parse_update


# --- parse_update ----------------------------------------------------

def _msg(text: str = "hi", user_id: int = 99999999, chat_id: int = 99999999) -> dict:
    return {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def test_parse_update_extracts_text() -> None:
    msg = parse_update(_msg(text="hello kimi"))
    assert msg == InboundMessage(
        update_id=100, chat_id=99999999, user_id=99999999, text="hello kimi"
    )


def test_parse_update_returns_none_when_no_message() -> None:
    assert parse_update({"update_id": 100}) is None


def test_parse_update_returns_message_for_photo() -> None:
    upd = _msg()
    del upd["message"]["text"]
    upd["message"]["photo"] = [{"file_id": "abc"}]
    result = parse_update(upd)
    assert result is not None
    assert result.photo == [{"file_id": "abc"}]


def test_parse_update_returns_none_for_edited_message() -> None:
    upd = _msg()
    upd["edited_message"] = upd.pop("message")
    assert parse_update(upd) is None


# --- is_authorized ---------------------------------------------------

def _cfg(user_ids: list[int], chat_ids: list[int] | None = None) -> TelegramConfig:
    return TelegramConfig(
        bot_token="x",
        allowed_user_ids=user_ids,
        allowed_chat_ids=chat_ids or [],
    )


def test_is_authorized_allows_listed_user() -> None:
    msg = InboundMessage(update_id=1, chat_id=10, user_id=42, text="hi")
    assert is_authorized(msg, _cfg([42])) is True


def test_is_authorized_blocks_unlisted_user() -> None:
    msg = InboundMessage(update_id=1, chat_id=10, user_id=999, text="hi")
    assert is_authorized(msg, _cfg([42])) is False


def test_is_authorized_with_chat_whitelist_blocks_other_chats() -> None:
    msg = InboundMessage(update_id=1, chat_id=20, user_id=42, text="hi")
    assert is_authorized(msg, _cfg([42], chat_ids=[10])) is False


def test_is_authorized_with_chat_whitelist_allows_listed_chat() -> None:
    msg = InboundMessage(update_id=1, chat_id=10, user_id=42, text="hi")
    assert is_authorized(msg, _cfg([42], chat_ids=[10])) is True


# --- chunk_message ---------------------------------------------------

def test_chunk_short_message_returned_as_single_chunk() -> None:
    assert chunk_message("hello", max_len=4096) == ["hello"]


def test_chunk_empty_message_returned_as_empty_list() -> None:
    assert chunk_message("", max_len=4096) == []


def test_chunk_long_message_splits_on_newlines() -> None:
    body = ("line\n" * 1000).rstrip("\n")
    chunks = chunk_message(body, max_len=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(c if c.endswith("\n") else c + "\n" for c in chunks).rstrip("\n") == body


def test_chunk_long_unbroken_text_hard_splits() -> None:
    body = "a" * 9000
    chunks = chunk_message(body, max_len=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == body


def test_chunk_default_max_is_4096() -> None:
    body = "x" * 5000
    chunks = chunk_message(body)
    assert all(len(c) <= 4096 for c in chunks)
