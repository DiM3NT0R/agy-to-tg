"""Telegram glue: pure helpers + async HTTP client with media + webhook support."""
from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Any

from src.config import TelegramConfig


@dataclass(frozen=True)
class InboundMessage:
    update_id: int
    chat_id: int
    user_id: int
    message_id: int
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
    mid = msg.get("message_id")
    thread_id = msg.get("message_thread_id")
    if not isinstance(cid, int) or not isinstance(uid, int) or not isinstance(uid_n, int) or not isinstance(mid, int):
        return None
    return InboundMessage(
        update_id=uid_n,
        chat_id=cid,
        user_id=uid,
        message_id=mid,
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
        chunk = remaining[:cut]
        if cut < len(remaining) and remaining[cut:cut + 1] == "\n":
            remaining = remaining[cut + 1:]
        else:
            remaining = remaining[cut:]

        # Ensure fenced code blocks (```) are balanced across chunks
        fence_matches = list(re.finditer(r"(?m)^[ \t]*```([a-zA-Z0-9_+-]*)", chunk))
        if len(fence_matches) % 2 == 1:
            lang = fence_matches[-1].group(1) or ""
            chunk = chunk + "\n```"
            remaining = f"```{lang}\n" + remaining

        chunks.append(chunk)
    return chunks


def _format_inline_markdown(text: str) -> str:
    """Convert inline Markdown elements inside an already HTML-escaped string."""
    placeholders: list[str] = []

    def _stash(html_fragment: str) -> str:
        idx = len(placeholders)
        placeholders.append(html_fragment)
        return f"\x00PH{idx}\x00"

    # 1. Inline code inside markdown links: [`code`](url) or [label](url)
    # First extract inline code spans `...`
    def _replace_inline_code(m: re.Match[str]) -> str:
        code_body = m.group(1)
        return _stash(f"<code>{code_body}</code>")

    text = re.sub(r"`([^`\n]+)`", _replace_inline_code, text)

    # 2. Markdown links: [label](url)
    def _replace_link(m: re.Match[str]) -> str:
        label = m.group(1)
        url = html.unescape(m.group(2).strip())
        # Restore any inline code placeholders inside label before wrapping
        for i, frag in enumerate(placeholders):
            label = label.replace(f"\x00PH{i}\x00", frag)
        if re.match(r"^(?:https?://|tg://|mailto:)", url, re.IGNORECASE):
            safe_url = html.escape(url, quote=True)
            return _stash(f'<a href="{safe_url}">{label}</a>')
        # Non-web links (e.g. file:///..., conversation://...) are rejected by Telegram <a href>.
        # Render the label cleanly (wrapped in <code> if not already containing tags).
        if "<code>" in label:
            return _stash(label)
        return _stash(f"<code>{label}</code>")

    text = re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", _replace_link, text)

    # 3. Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text)

    # 4. Strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 5. Italic: *text* or _text_
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)

    # Restore stashed fragments
    for i, frag in enumerate(placeholders):
        text = text.replace(f"\x00PH{i}\x00", frag)

    return text


def markdown_to_telegram_html(text: str) -> str:
    """Convert GitHub-Flavored Markdown to Telegram-compatible HTML."""
    if not text:
        return ""

    code_blocks: list[str] = []

    def _replace_fenced_block(m: re.Match[str]) -> str:
        lang = (m.group(1) or "").strip()
        code = html.escape(m.group(2))
        if code.endswith("\n"):
            code = code[:-1]
        if lang:
            safe_lang = html.escape(lang, quote=True)
            block_html = f'<pre><code class="language-{safe_lang}">{code}</code></pre>'
        else:
            block_html = f"<pre><code>{code}</code></pre>"
        idx = len(code_blocks)
        code_blocks.append(block_html)
        return f"\x00CB{idx}\x00"

    # Extract fenced code blocks ```lang ... ```
    processed = re.sub(
        r"```([a-zA-Z0-9_+-]*)[ \t]*\n(.*?)```",
        _replace_fenced_block,
        text,
        flags=re.DOTALL,
    )

    lines = processed.split("\n")
    out_lines: list[str] = []
    quote_buffer: list[str] = []

    def _flush_quote() -> None:
        if quote_buffer:
            out_lines.append("<blockquote>" + "\n".join(quote_buffer) + "</blockquote>")
            quote_buffer.clear()

    for line in lines:
        # Check if line is just a code block placeholder
        if re.match(r"^\x00CB\d+\x00$", line.strip()):
            _flush_quote()
            out_lines.append(line)
            continue

        # Blockquotes: > text
        q_match = re.match(r"^[ \t]*>[ \t]?(.*)$", line)
        if q_match:
            q_content = html.escape(q_match.group(1))
            # Strip GitHub alert markers like [!NOTE], [!WARNING], etc. into bold labels
            q_content = re.sub(
                r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*",
                r"<b>[\1]</b> ",
                q_content,
                flags=re.IGNORECASE,
            )
            quote_buffer.append(_format_inline_markdown(q_content))
            continue
        else:
            _flush_quote()

        # Headings: # Heading -> <b>Heading</b>
        h_match = re.match(r"^[ \t]*(#{1,6})[ \t]+(.+?)[ \t]*$", line)
        if h_match:
            h_content = _format_inline_markdown(html.escape(h_match.group(2)))
            out_lines.append(f"<b>{h_content}</b>")
            continue

        # Horizontal rule: --- or ***
        if re.match(r"^[ \t]*(?:-\s*){3,}$|^[ \t]*(?:\*\s*){3,}$", line):
            out_lines.append("───────────────")
            continue

        # Bullet list items: - item or * item at start of line
        b_match = re.match(r"^([ \t]*)[-*][ \t]+(.+)$", line)
        if b_match:
            indent = b_match.group(1)
            item_content = _format_inline_markdown(html.escape(b_match.group(2)))
            out_lines.append(f"{indent}• {item_content}")
            continue

        escaped_line = html.escape(line)
        out_lines.append(_format_inline_markdown(escaped_line))

    _flush_quote()
    result = "\n".join(out_lines)

    for i, block_html in enumerate(code_blocks):
        result = result.replace(f"\x00CB{i}\x00", block_html)

    return result


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

    async def _post_with_retry(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to Telegram Bot API with automatic retry on 429 Too Many Requests."""
        assert self._client is not None
        url = f"{self._base}/{method}"
        for attempt in range(3):
            r = await self._client.post(url, json=payload)
            data = r.json()
            if data.get("ok"):
                return data
            error_code = data.get("error_code") or r.status_code
            if error_code == 429 and attempt < 2:
                retry_after = (data.get("parameters") or {}).get("retry_after", 2)
                await asyncio.sleep(min(float(retry_after), 30.0))
                continue
            return data
        return data

    async def get_me(self) -> dict[str, Any]:
        assert self._client is not None
        r = await self._client.get(f"{self._base}/getMe")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getMe failed: {data.get('description')}")
        return dict(data.get("result") or {})

    async def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        assert self._client is not None
        payload = {"commands": commands}
        data = await self._post_with_retry("setMyCommands", payload)
        if not data.get("ok"):
            raise RuntimeError(f"setMyCommands failed: {data.get('description')}")
        return True

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
        reply_to_message_id: int | None = None,
    ) -> int | None:
        assert self._client is not None
        first_message_id: int | None = None
        # Use 3500 max_len for raw markdown so HTML tags stay well under Telegram's 4096 char limit
        chunks = chunk_message(text, max_len=3500 if len(text) > 4096 else 4096)
        for i, chunk in enumerate(chunks):
            html_chunk = markdown_to_telegram_html(chunk)
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": html_chunk,
                "parse_mode": "HTML",
            }
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id
            if reply_to_message_id is not None and i == 0:
                payload["reply_to_message_id"] = reply_to_message_id
            if i == 0 and keyboard is not None:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            data = await self._post_with_retry("sendMessage", payload)
            if not data.get("ok"):
                desc = str(data.get("description", "")).lower()
                if "can't parse entities" in desc or "parse entities" in desc or "find end of the entity" in desc:
                    # Fallback to plain text without parse_mode
                    fallback_payload = dict(payload)
                    fallback_payload.pop("parse_mode", None)
                    fallback_payload["text"] = chunk
                    data = await self._post_with_retry("sendMessage", fallback_payload)
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
        html_text = markdown_to_telegram_html(text)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": html_text,
            "parse_mode": "HTML",
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        data = await self._post_with_retry("editMessageText", payload)
        if not data.get("ok"):
            desc = str(data.get("description", "")).lower()
            if "not modified" in desc:
                return
            if "can't parse entities" in desc or "parse entities" in desc or "find end of the entity" in desc:
                fallback_payload = dict(payload)
                fallback_payload.pop("parse_mode", None)
                fallback_payload["text"] = text
                data = await self._post_with_retry("editMessageText", fallback_payload)
                if data.get("ok") or "not modified" in str(data.get("description", "")).lower():
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

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a message."""
        assert self._client is not None
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        r = await self._client.post(f"{self._base}/deleteMessage", json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"deleteMessage failed: {data.get('description')}")

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
