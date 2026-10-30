"""Tests for the pure helpers in src.telegram."""
from __future__ import annotations

import pytest

from src.config import TelegramConfig
from src.telegram import (
    InboundMessage,
    chunk_message,
    is_authorized,
    markdown_to_telegram_html,
    parse_update,
)


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
        update_id=100, chat_id=99999999, user_id=99999999, message_id=1,
        text="hello kimi",
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
    msg = InboundMessage(update_id=1, chat_id=10, user_id=42, message_id=1, text="hi")
    assert is_authorized(msg, _cfg([42])) is True


def test_is_authorized_blocks_unlisted_user() -> None:
    msg = InboundMessage(update_id=1, chat_id=10, user_id=999, message_id=1, text="hi")
    assert is_authorized(msg, _cfg([42])) is False


def test_is_authorized_with_chat_whitelist_blocks_other_chats() -> None:
    msg = InboundMessage(update_id=1, chat_id=20, user_id=42, message_id=1, text="hi")
    assert is_authorized(msg, _cfg([42], chat_ids=[10])) is False


def test_is_authorized_with_chat_whitelist_allows_listed_chat() -> None:
    msg = InboundMessage(update_id=1, chat_id=10, user_id=42, message_id=1, text="hi")
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


def test_chunk_balances_fenced_code_blocks() -> None:
    code = "```python\n" + ("print('hello')\n" * 20) + "```"
    chunks = chunk_message(code, max_len=120)
    assert len(chunks) > 1
    for c in chunks:
        assert c.count("```") % 2 == 0


# --- markdown_to_telegram_html ---------------------------------------

def test_markdown_to_telegram_html_renders_formatting_and_links() -> None:
    raw = (
        "### Заголовок\n"
        "Текст с **жирным**, *курсивом*, `кодом` и ~~зачеркнутым~~.\n"
        "- Пункт со ссылкой [Google](https://google.com)\n"
        "- Локальный файл [daemon.py](file:///root/src/daemon.py#L10)\n"
        "> [!NOTE] Важное примечание\n"
        "```bash\n"
        "echo <test> & 'ok'\n"
        "```"
    )
    html_out = markdown_to_telegram_html(raw)
    assert "<b>Заголовок</b>" in html_out
    assert "<b>жирным</b>" in html_out
    assert "<i>курсивом</i>" in html_out
    assert "<code>кодом</code>" in html_out
    assert "<s>зачеркнутым</s>" in html_out
    assert '<a href="https://google.com">Google</a>' in html_out
    assert "<code>daemon.py</code>" in html_out
    assert "file://" not in html_out
    assert "<blockquote><b>[NOTE]</b> Важное примечание</blockquote>" in html_out
    assert '<pre><code class="language-bash">echo &lt;test&gt; &amp; &#x27;ok&#x27;</code></pre>' in html_out


def test_markdown_to_telegram_html_renders_tables_and_task_lists() -> None:
    raw = (
        "- [x] Сделано\n"
        "- [ ] В планах\n"
        "\n"
        "| Параметр | Статус |\n"
        "| :--- | :---: |\n"
        "| Модель | OK |\n"
    )
    html_out = markdown_to_telegram_html(raw)
    assert "✅ Сделано" in html_out
    assert "⬜ В планах" in html_out
    assert "▫️ <b>Модель</b> — <i>Статус:</i> OK" in html_out


def test_markdown_to_telegram_html_preserves_valid_telegram_html_tags() -> None:
    raw = (
        "📊 <b>Лимиты моделей Antigravity</b>\n"
        "👤 Аккаунт: <code>siplerviar775@gmail.com</code>\n"
        "🤖 <b>Модели Claude и GPT</b>\n"
        "<i>В группе: Claude Opus</i>\n"
        "Остаток: 🟢 <code>[██████████]</code> <b>100%</b>\n"
        "Ссылка: <a href=\"https://t.me\">Telegram</a>\n"
        "Цитата: <blockquote>Важный текст</blockquote>\n"
        "Формула с углами: if a < b and c > d:\n"
    )
    html_out = markdown_to_telegram_html(raw)
    # Valid tags must NOT be double-escaped to &lt;b&gt; or &lt;code&gt;
    assert "&lt;b&gt;" not in html_out
    assert "&lt;code&gt;" not in html_out
    assert "&lt;i&gt;" not in html_out
    assert "<b>Лимиты моделей Antigravity</b>" in html_out
    assert "<code>siplerviar775@gmail.com</code>" in html_out
    assert "<code>[██████████]</code>" in html_out
    assert '<a href="https://t.me">Telegram</a>' in html_out
    assert "<blockquote>Важный текст</blockquote>" in html_out
    # Non-tag comparisons MUST still be escaped properly
    assert "if a &lt; b and c &gt; d:" in html_out


def test_markdown_to_telegram_html_preserves_pre_code_blocks() -> None:
    raw = (
        "📝 <b>Изменения git diff:</b>\n"
        '<pre><code class="language-diff">--- a/file.py\n+++ b/file.py\n- old line\n+ new line</code></pre>'
    )
    html_out = markdown_to_telegram_html(raw)
    assert "<b>Изменения git diff:</b>" in html_out
    assert '<pre><code class="language-diff">--- a/file.py\n+++ b/file.py\n- old line\n+ new line</code></pre>' in html_out
    assert "&lt;pre&gt;" not in html_out

