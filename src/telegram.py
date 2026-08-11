"""Telegram glue: pure helpers + async HTTP client with media + webhook support."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config import TelegramConfig


@dataclass(frozen=True)
class InboundMessage:
    update_id: int
    chat_id: int
    user_id: int
    text: str
    message_thread_id: int | None = None
    photo: list[dict[str, Any]] | None = None
    document: dict[str, Any] | None = None


@dataclass(frozen=True)
class CallbackQuery:
    update_id: int
    callback_query_id: str
    chat_id: int
    user_id: int
    message_id: int
    data: str
    message_thread_id: int | None = None


# Telegram inline keyboards are nested lists of dicts: list[list[Button]].
InlineKeyboard = list[list[dict[str, Any]]]


def parse_update(update: dict[str, Any]) -> InboundMessage | None:
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    text = msg.get("text") or msg.get("caption") or ""
    photo = msg.get("photo")
    doc = msg.get("document")
    if not text and photo is None and doc is None:
        return None
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    cid = chat.get("id")
    uid = sender.get("id")
    uid_n = update.get("update_id")
    thread_id = msg.get("message_thread_id")
    if not isinstance(cid, int) or not isinstance(uid, int) or not isinstance(uid_n, int):
        return None
    return InboundMessage(
        update_id=uid_n,
        chat_id=cid,
        user_id=uid,
        text=text,
        message_thread_id=thread_id if isinstance(thread_id, int) else None,
        photo=list(photo) if isinstance(photo, list) else None,
        document=doc if isinstance(doc, dict) else None,
    )


def parse_callback_query(update: dict[str, Any]) -> CallbackQuery | None:
    cq = update.get("callback_query")
    if not isinstance(cq, dict):
        return None
    cq_id = cq.get("id")
    sender = cq.get("from") or {}
    user_id = sender.get("id")
    inner_msg = cq.get("message") or {}
    chat = inner_msg.get("chat") or {}
    chat_id = chat.get("id")
    thread_id = inner_msg.get("message_thread_id")
    message_id = inner_msg.get("message_id")
    data = cq.get("data")
    update_id = update.get("update_id")
    if (
        not isinstance(cq_id, str)
        or not isinstance(user_id, int)
        or not isinstance(chat_id, int)
        or not isinstance(message_id, int)
        or not isinstance(data, str)
        or not isinstance(update_id, int)
    ):
        return None
    return CallbackQuery(
        update_id=update_id,
        callback_query_id=cq_id,
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        data=data,
        message_thread_id=thread_id if isinstance(thread_id, int) else None,
    )


def is_authorized_user(user_id: int, cfg: TelegramConfig) -> bool:
    return user_id in cfg.allowed_user_ids


def is_authorized_chat(chat_id: int, cfg: TelegramConfig) -> bool:
    return not cfg.allowed_chat_ids or chat_id in cfg.allowed_chat_ids


def is_authorized(msg: InboundMessage, cfg: TelegramConfig) -> bool:
    if not is_authorized_user(msg.user_id, cfg):
        return False
    return is_authorized_chat(msg.chat_id, cfg)


def chunk_message(text: str, max_len: int = 4096) -> list[str]:
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len
        chunks.append(remaining[:cut])
        if cut < len(remaining) and remaining[cut:cut + 1] == "\n":
            remaining = remaining[cut + 1:]
        else:
            remaining = remaining[cut:]
    return chunks


import httpx


class TelegramClient:
    """Thin async wrapper around the Telegram bot HTTP API."""

    def __init__(self, bot_token: str, *, base_url: str = "https://api.telegram.org") -> None:
        self._base = f"{base_url}/bot{bot_token}"
        self._bot_token = bot_token
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TelegramClient":
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(35.0))
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._client is not None
        await self._client.aclose()
        self._client = None

    async def get_me(self) -> dict[str, Any]:
        assert self._client is not None
        r = await self._client.get(f"{self._base}/getMe")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getMe failed: {data.get('description')}")
        return dict(data.get("result") or {})

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        assert self._client is not None
        r = await self._client.get(f"{self._base}/getUpdates", params={
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": '["message","callback_query"]',
        })
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getUpdates failed: {data.get('description')}")
        return list(data.get("result") or [])

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        assert self._client is not None
        first_message_id: int | None = None
        chunks = chunk_message(text)
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id
            if i == 0 and keyboard is not None:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            r = await self._client.post(f"{self._base}/sendMessage", json=payload)
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"sendMessage failed: {data.get('description')}")
            if i == 0:
                result = data.get("result") or {}
                mid = result.get("message_id")
                if isinstance(mid, int):
                    first_message_id = mid
        return first_message_id

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        keyboard: InlineKeyboard | None = None,
    ) -> None:
        assert self._client is not None
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        r = await self._client.post(f"{self._base}/editMessageText", json=payload)
        data = r.json()
        if not data.get("ok"):
            desc = str(data.get("description", "")).lower()
            if "not modified" in desc:
                return
            raise RuntimeError(f"editMessageText failed: {data.get('description')}")

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
    ) -> None:
        assert self._client is not None
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        r = await self._client.post(f"{self._base}/answerCallbackQuery", json=payload)
        data = r.json()
        if not data.get("ok"):
            return

    async def send_chat_action(self, chat_id: int, action: str = "typing", message_thread_id: int | None = None) -> None:
        assert self._client is not None
        payload: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        r = await self._client.post(
            f"{self._base}/sendChatAction",
            json=payload,
        )
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"sendChatAction failed: {data.get('description')}")

    async def get_file(self, file_id: str) -> bytes:
        assert self._client is not None
        r = await self._client.get(f"{self._base}/getFile", params={"file_id": file_id})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getFile failed: {data.get('description')}")
        file_path = data["result"]["file_path"]
        r2 = await self._client.get(
            f"https://api.telegram.org/file/bot{self._bot_token}/{file_path}")
        return r2.content

    async def set_webhook(self, url: str, secret_token: str = "") -> dict[str, Any]:
        assert self._client is not None
        params: dict[str, Any] = {"url": url}
        if secret_token:
            params["secret_token"] = secret_token
        r = await self._client.get(f"{self._base}/setWebhook", params=params)
        return r.json()

    async def delete_webhook(self) -> dict[str, Any]:
        assert self._client is not None
        r = await self._client.get(f"{self._base}/deleteWebhook")
        return r.json()
