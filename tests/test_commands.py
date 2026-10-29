"""Unit tests for src.commands — bridge command handlers."""
from __future__ import annotations

from src.commands import handle_callback, handle_text_command
from src.config import AgyConfig, Config, TelegramConfig
from src.state import ChatState
from src.telegram import CallbackQuery


def _cfg(model: str = "", mode: str = "code") -> Config:
    return Config(
        telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]),
        agy=AgyConfig(default_workdir="/tmp", model=model, mode=mode),
    )


def _state() -> ChatState:
    return ChatState(chat_dir="/tmp/chat")


class _FakeMsg:
    def __init__(self, text: str, chat_id: int = 42, user_id: int = 42) -> None:
        self.text = text
        self.chat_id = chat_id
        self.user_id = user_id


async def test_start_command() -> None:
    reply = await handle_text_command(_FakeMsg("/start"), _state(), _cfg())
    assert reply is not None
    assert "Привет" in reply.text


async def test_help_command() -> None:
    reply = await handle_text_command(_FakeMsg("/help"), _state(), _cfg())
    assert reply is not None
    assert "Команды" in reply.text


async def test_info_shows_default_model() -> None:
    reply = await handle_text_command(_FakeMsg("/info"), _state(), _cfg(model="gemini-3.6-flash-high"))
    assert reply is not None
    assert "gemini-3.6-flash-high" in reply.text


async def test_info_no_session() -> None:
    reply = await handle_text_command(_FakeMsg("/info"), _state(), _cfg())
    assert reply is not None
    assert "чистая" in reply.text.lower()


async def test_thinking_not_available() -> None:
    reply = await handle_text_command(_FakeMsg("/thinking on"), _state(), _cfg())
    assert reply is not None
    assert "недоступно" in reply.text.lower()


async def test_model_picker() -> None:
    reply = await handle_text_command(_FakeMsg("/model"), _state(), _cfg(model="gemini-3.6-flash-high"))
    assert reply is not None
    assert reply.keyboard is not None


async def test_model_set() -> None:
    cs = _state()
    reply = await handle_text_command(_FakeMsg("/model gemini-3.6-flash-high"), cs, _cfg())
    assert reply is not None
    assert cs.model == "gemini-3.6-flash-high"


async def test_reset_clears_session() -> None:
    cs = _state()
    cs.has_session = True
    cs.conversation_id = "old"
    reply = await handle_text_command(_FakeMsg("/reset"), cs, _cfg())
    assert reply is not None
    assert cs.has_session is False
    assert cs.conversation_id == ""


async def test_unknown_returns_none() -> None:
    reply = await handle_text_command(_FakeMsg("hello world"), _state(), _cfg())
    assert reply is None


async def test_callback_nav_settings() -> None:
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="nav:settings")
    reply = await handle_callback(cq, _state(), _cfg())
    assert reply.keyboard is not None


async def test_callback_model_choice() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="m:gemini-3.6-flash-high")
    reply = await handle_callback(cq, cs, _cfg())
    assert cs.model == "gemini-3.6-flash-high"
    assert "gemini-3.6-flash-high" in reply.toast


async def test_callback_reset() -> None:
    cs = _state()
    cs.has_session = True
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="R")
    reply = await handle_callback(cq, cs, _cfg())
    assert cs.has_session is False
    assert "сброшена" in reply.toast.lower()


async def test_callback_mode_choice() -> None:
    cs = _state()
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="M:plan")
    reply = await handle_callback(cq, cs, _cfg())
    assert cs.mode == "plan"
    assert "plan" in reply.toast.lower()


async def test_command_limits(monkeypatch) -> None:
    from src.quota import QuotaSummary, QuotaGroup, QuotaBucket

    fake_summary = QuotaSummary(
        groups=[
            QuotaGroup(
                display_name="Claude and GPT models",
                description="test",
                buckets=[
                    QuotaBucket(
                        bucket_id="3p-weekly",
                        display_name="Weekly Limit Remaining",
                        window="weekly",
                        reset_time="2026-10-30T07:58:32Z",
                        remaining_fraction=0.8,
                        description="",
                    )
                ],
            )
        ],
        user_email="user@test.com",
    )

    async def fake_fetch(agy_path=""):
        return fake_summary

    monkeypatch.setattr("src.commands.fetch_quota_summary", fake_fetch)

    reply = await handle_text_command(_FakeMsg("/limits"), _state(), _cfg())
    assert reply is not None
    assert "Лимиты моделей" in reply.text
    assert "80%" in reply.text
    assert "user@test.com" in reply.text
    assert reply.keyboard is not None

    reply_alias = await handle_text_command(_FakeMsg("/quota"), _state(), _cfg())
    assert reply_alias is not None
    assert "Лимиты моделей" in reply_alias.text

    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="nav:limits")
    cb_reply = await handle_callback(cq, _state(), _cfg())
    assert "Лимиты моделей" in cb_reply.text
    assert cb_reply.toast == "Лимиты обновлены"


async def test_command_files_and_get(tmp_path: Path) -> None:
    cs = _state()
    cs.chat_dir = str(tmp_path)

    # Empty case
    reply_empty = await handle_text_command(_FakeMsg("/files"), cs, _cfg())
    assert reply_empty is not None
    assert "пуста" in reply_empty.text

    # Write a test file
    test_file = tmp_path / "hello.py"
    test_file.write_text("print('test 123')")

    # /files list
    reply_files = await handle_text_command(_FakeMsg("/files"), cs, _cfg())
    assert reply_files is not None
    assert "hello.py" in reply_files.text
    assert reply_files.keyboard is not None
    assert any("hello.py" in btn["text"] for row in reply_files.keyboard for btn in row)

    # /get file
    reply_get = await handle_text_command(_FakeMsg("/get hello.py"), cs, _cfg())
    assert reply_get is not None
    assert reply_get.document_bytes == b"print('test 123')"
    assert reply_get.document_filename == "hello.py"

    # /get non-existent
    reply_get_none = await handle_text_command(_FakeMsg("/get missing.txt"), cs, _cfg())
    assert reply_get_none is not None
    assert "не найден" in reply_get_none.text

    # Callback getfile
    cq = CallbackQuery(update_id=1, callback_query_id="q", chat_id=42, user_id=42, message_id=1, data="getfile:hello.py")
    cb_reply = await handle_callback(cq, cs, _cfg())
    assert cb_reply.document_bytes == b"print('test 123')"
    assert "hello.py" in cb_reply.toast


async def test_command_diff(tmp_path: Path, monkeypatch) -> None:
    cs = _state()
    cs.chat_dir = str(tmp_path)

    # Non-git directory
    reply_no_git = await handle_text_command(_FakeMsg("/diff"), cs, _cfg())
    assert reply_no_git is not None
    assert "нет репозитория git" in reply_no_git.text

    # Mock git diff output (small)
    async def mock_small_diff(workdir, args=""):
        return "diff --git a/a.py b/a.py\n+new line", ""

    monkeypatch.setattr("src.commands._run_git_diff", mock_small_diff)
    reply_diff = await handle_text_command(_FakeMsg("/diff"), cs, _cfg())
    assert reply_diff is not None
    assert "+new line" in reply_diff.text
    assert reply_diff.document_bytes is None

    # Mock git diff output (large -> patch document)
    async def mock_large_diff(workdir, args=""):
        return "+line\n" * 1000, ""

    monkeypatch.setattr("src.commands._run_git_diff", mock_large_diff)
    reply_large = await handle_text_command(_FakeMsg("/diff"), cs, _cfg())
    assert reply_large is not None
    assert reply_large.document_bytes is not None
    assert reply_large.document_filename == "changes.patch"


async def test_command_pwd(tmp_path: Path) -> None:
    cs = _state()
    cs.chat_dir = str(tmp_path)
    reply = await handle_text_command(_FakeMsg("/pwd"), cs, _cfg())
    assert reply is not None
    assert "Текущая рабочая папка чата" in reply.text


async def test_command_compact_and_callback() -> None:
    cs = _state()
    # No session
    reply_empty = await handle_text_command(_FakeMsg("/compact"), cs, _cfg())
    assert reply_empty is not None
    assert "нет активной сессии" in reply_empty.text

    # With active session
    cs.has_session = True
    cs.conversation_id = "test-uuid"
    cs.turn_count = 5
    reply_active = await handle_text_command(_FakeMsg("/compact"), cs, _cfg())
    assert reply_active is not None
    assert "Сжатие контекста сессии" in reply_active.text
    assert reply_active.keyboard is not None

    # Callback do_compact
    cq = CallbackQuery(update_id=2, callback_query_id="q2", chat_id=42, user_id=42, message_id=1, data="do_compact")
    cb_reply = await handle_callback(cq, cs, _cfg())
    assert "Контекст сессии сжат" in cb_reply.text
    assert cs.has_session is False
    assert cs.conversation_id == ""
    assert cs.turn_count == 0


async def test_voice_command_and_callbacks() -> None:
    cs = _state()
    cfg = _cfg()

    # /voice menu
    reply_menu = await handle_text_command(_FakeMsg("/voice"), cs, cfg)
    assert reply_menu is not None
    assert "Настройки голосовой озвучки" in reply_menu.text
    assert reply_menu.keyboard is not None

    # /voice <name> direct set
    reply_set = await handle_text_command(_FakeMsg("/voice alena"), cs, cfg)
    assert reply_set is not None
    assert "alena" in reply_set.text
    assert cs.voice == "alena"

    # Callback nav:voice
    cq_nav = CallbackQuery(update_id=3, callback_query_id="q3", chat_id=42, user_id=42, message_id=1, data="nav:voice")
    nav_reply = await handle_callback(cq_nav, cs, cfg)
    assert "Настройки голосовой озвучки" in nav_reply.text

    # Callback v:ermil
    cq_v = CallbackQuery(update_id=4, callback_query_id="q4", chat_id=42, user_id=42, message_id=1, data="v:ermil")
    v_reply = await handle_callback(cq_v, cs, cfg)
    assert cs.voice == "ermil"
    assert "ermil" in v_reply.toast

    # Callback vs:1.3
    cq_vs = CallbackQuery(update_id=5, callback_query_id="q5", chat_id=42, user_id=42, message_id=1, data="vs:1.3")
    vs_reply = await handle_callback(cq_vs, cs, cfg)
    assert cs.voice_speed == "1.3"
    assert "1.3x" in vs_reply.toast

    # Callback v_auto:toggle
    assert cs.auto_voice is False
    cq_auto = CallbackQuery(update_id=6, callback_query_id="q6", chat_id=42, user_id=42, message_id=1, data="v_auto:toggle")
    auto_reply = await handle_callback(cq_auto, cs, cfg)
    assert cs.auto_voice is True
    assert "ВКЛ" in auto_reply.toast



