"""Tests for the async TelegramClient HTTP wrapper."""
from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from src.telegram import TelegramClient


pytestmark = pytest.mark.asyncio


async def test_get_updates_long_polls_with_offset_and_timeout(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botTOKEN/getUpdates?offset=42&timeout=30&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D",
        method="GET",
        json={"ok": True, "result": [{"update_id": 50}]},
    )
    async with TelegramClient("TOKEN") as tg:
        updates = await tg.get_updates(offset=42, timeout=30)
    assert updates == [{"update_id": 50}]


async def test_get_updates_raises_on_telegram_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botTOKEN/getUpdates?offset=1&timeout=30&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D",
        method="GET",
        json={"ok": False, "description": "Unauthorized"},
        status_code=401,
    )
    async with TelegramClient("TOKEN") as tg:
        with pytest.raises(RuntimeError, match="Unauthorized"):
            await tg.get_updates(offset=1)


async def test_send_message_splits_long_text(httpx_mock: HTTPXMock) -> None:
    body = "x" * 9000
    for _ in range(3):
        httpx_mock.add_response(
            url="https://api.telegram.org/botTOKEN/sendMessage",
            method="POST",
            json={"ok": True, "result": {}},
        )
    async with TelegramClient("TOKEN") as tg:
        await tg.send_message(chat_id=10, text=body)
    requests = httpx_mock.get_requests()
    assert len(requests) == 3
    for r in requests:
        assert r.method == "POST"
        assert r.url.path == "/botTOKEN/sendMessage"


async def test_send_message_skips_empty_text(httpx_mock: HTTPXMock) -> None:
    async with TelegramClient("TOKEN") as tg:
        await tg.send_message(chat_id=10, text="")
    assert httpx_mock.get_requests() == []


async def test_send_chat_action_posts_typing(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botTOKEN/sendChatAction",
        method="POST",
        json={"ok": True, "result": True},
    )
    async with TelegramClient("TOKEN") as tg:
        await tg.send_chat_action(chat_id=10, action="typing")
    req = httpx_mock.get_requests()[0]
    assert req.method == "POST"
    body = req.read().decode()
    assert "typing" in body
    assert "10" in body
