"""Bridge command handlers — text slash commands and inline-keyboard callbacks."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.media import clean_inbox, list_inbox
from src.state import is_valid_model
from src.telegram import InlineKeyboard

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import CallbackQuery, InboundMessage

DEFAULT_MODEL = "gemini-3.5-flash"

MODEL_CHOICES: tuple[str, ...] = ("gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.5-flash-high", "gemini-3.1-pro-high", "claude-sonnet-4-6", "claude-opus-4-6-thinking")
EFFORT_CHOICES: tuple[str, ...] = ("low", "medium", "high")
MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("code", "Code (auto)"),
    ("plan", "Plan (read-only sandbox)"),
)
_DEFAULT_TOKEN = "_DEFAULT"

WELCOME_TEXT = (
    "👋 Привет! Я твой Telegram-бот для Antigravity.\n\n"
    "Отправь мне любое сообщение, и я передам его агенту agy. "
    "Для каждого чата или темы создается своя отдельная сессия.\n\n"
    "Команды: /start · /help · /status · /settings · /model · /mode · /reset"
)

HELP_TEXT = (
    "📖 Справка по мосту Antigravity\n\n"
    "Отправьте любой текст для общения с agy.\n\n"
    "Команды:\n"
    "/status   — сводка по системе и текущему чату\n"
    "/settings — панель управления с кнопками\n"
    "/model    — выбрать модель (переопределяет настройки для чата)\n"
    "/effort   — выбрать уровень effort (low/medium/high)\n"
    "/timeout  — таймаут работы агента (напр. 30m, 1h)\n"
    "/mode     — выбрать режим (Code или Plan)\n"
    "/reset    — начать чистую сессию agy для этого чата\n"
    "/info     — то же, что и /status\n"
    "/image on|off — вкл/выкл обработку фото\n"
    "/files    — список недавних файлов\n"
    "/start, /help — показать эти сообщения\n\n"
    "Настройки для конкретного чата имеют приоритет над config.json."
)


@dataclass(frozen=True)
class BridgeReply:
    """Daemon's structured reply: text, optional inline keyboard, optional toast."""

    text: str
    keyboard: InlineKeyboard | None = None
    toast: str = ""


def _effective_model(cs: "ChatState", cfg: "Config") -> tuple[str, str]:
    if cs.model:
        return cs.model, "chat"
    if cfg.agy.model:
        return cfg.agy.model, "config"
    return DEFAULT_MODEL, "default"


def _effective_mode(cs: "ChatState", cfg: "Config") -> tuple[str, str]:
    if cs.mode:
        return cs.mode, "chat"
    return cfg.agy.mode, "config"


def render_status(cs: "ChatState", cfg: "Config") -> str:
    model, model_src = _effective_model(cs, cfg)
    mode, mode_src = _effective_mode(cs, cfg)
    session = "активна (продолжение)" if cs.has_session else "чистая (новая сессия)"
    home = os.path.expanduser("~")
    workdir = cs.chat_dir.replace(home, "~", 1)
    return (
        "🟢 Статус моста Antigravity\n"
        f"Модель:     {model}  [{model_src}]\n"
        f"Режим:      {mode}  [{mode_src}]\n" \
        f"Effort:     {cs.effort or 'default'}\n" \
        f"Таймаут:    {cs.print_timeout or '15m'}\n"
        "\n"
        "Этот чат:\n"
        f"  Сессия:   {session}\n"
        f"  Ходов:    {cs.turn_count}\n"
        f"  Папка:    {workdir}"
    )


def _settings_keyboard() -> InlineKeyboard:
    return [
        [
            {"text": "🤖 Сменить модель", "callback_data": "nav:model"},
            {"text": "🛡 Сменить режим", "callback_data": "nav:mode"},
        ],
        [
            {"text": "⚙️ Изменить Effort", "callback_data": "nav:effort"},
        ],
        [{"text": "🧹 Сбросить сессию", "callback_data": "R"}],
        [{"text": "🔄 Обновить", "callback_data": "nav:settings"}],
    ]


def _model_keyboard(current_per_chat: str) -> InlineKeyboard:
    rows: InlineKeyboard = []
    for m in MODEL_CHOICES:
        marker = "● " if m == current_per_chat else "○ "
        rows.append([{"text": marker + m, "callback_data": f"m:{m}"}])
    default_marker = "● " if not current_per_chat else "○ "
    rows.append([
        {"text": default_marker + "По умолчанию (из конфига)", "callback_data": f"m:{_DEFAULT_TOKEN}"}
    ])
    rows.append([{"text": "← Назад к настройкам", "callback_data": "nav:settings"}])
    return rows


def _mode_keyboard(current_per_chat: str) -> InlineKeyboard:
    cells: list[dict[str, object]] = []
    for value, label in MODE_CHOICES:
        marker = "● " if value == current_per_chat else "○ "
        cells.append({"text": marker + label, "callback_data": f"M:{value}"})
    default_marker = "● " if not current_per_chat else "○ "
    return [
        cells,
        [{"text": default_marker + "По умолчанию (из конфига)", "callback_data": f"M:{_DEFAULT_TOKEN}"}],
        [{"text": "← Назад к настройкам", "callback_data": "nav:settings"}],
    ]


def _render_settings(cs: "ChatState", cfg: "Config") -> BridgeReply:
    return BridgeReply(text=render_status(cs, cfg), keyboard=_settings_keyboard())



def _effort_keyboard(current_per_chat: str) -> InlineKeyboard:
    rows: InlineKeyboard = []
    for e in EFFORT_CHOICES:
        marker = "● " if e == current_per_chat else "○ "
        rows.append([{"text": marker + e, "callback_data": f"e:{e}"}])
    default_marker = "● " if not current_per_chat else "○ "
    rows.append([
        {"text": default_marker + "По умолчанию (из конфига)", "callback_data": f"e:{_DEFAULT_TOKEN}"}
    ])
    rows.append([{"text": "← Назад к настройкам", "callback_data": "nav:settings"}])
    return rows

def _render_effort_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    return BridgeReply(
        text=f"⚙️ Выберите effort для этого чата\n\nТекущий: {cs.effort or 'default'}",
        keyboard=_effort_keyboard(cs.effort),
    )

def _render_model_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur, src = _effective_model(cs, cfg)
    return BridgeReply(
        text=f"🤖 Choose a model for this chat\n\nCurrent: {cur}  [{src}]",
        keyboard=_model_keyboard(cs.model),
    )


def _render_mode_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur, src = _effective_mode(cs, cfg)
    return BridgeReply(
        text=f"🛡 Choose a mode for this chat\n\nCurrent: {cur}  [{src}]",
        keyboard=_mode_keyboard(cs.mode),
    )


async def handle_text_command(
    msg: "InboundMessage",
    cs: "ChatState",
    cfg: "Config",
) -> BridgeReply | None:
    """Return a reply for a slash command, else None (forward to agy)."""
    stripped = msg.text.strip()
    if not stripped:
        return None
    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/start":
        return BridgeReply(WELCOME_TEXT)
    if cmd == "/help":
        return BridgeReply(HELP_TEXT)
    if cmd in ("/status", "/info"):
        return BridgeReply(render_status(cs, cfg))
    if cmd == "/settings":
        return _render_settings(cs, cfg)
    if cmd == "/effort":
        if args:
            cs.effort = args
            return BridgeReply(f"⚙️ Effort установлен на {args}")
        return BridgeReply("⚙️ Используйте: /effort low | medium | high")
    if cmd == "/timeout":
        if args:
            cs.print_timeout = args
            return BridgeReply(f"⏱️ Таймаут установлен на {args}")
        return BridgeReply("⏱️ Используйте: /timeout <время> (например: 30m, 1h, 5m)")
    if cmd == "/model":
        if args:
            if is_valid_model(args):
                cs.model = args
                return BridgeReply(f"🤖 Модель изменена на {args}")
            return BridgeReply(f"⚠️ Неподдерживаемая модель: {args}")
        return _render_model_picker(cs, cfg)
    if cmd == "/mode":
        if args:
            if args in {"code", "plan"}:
                cs.mode = args
                return BridgeReply(f"🛡 Режим изменен на {args}")
            return BridgeReply(f"⚠️ Неподдерживаемый режим: {args}")
        return _render_mode_picker(cs, cfg)
    if cmd == "/reset":
        cs.has_session = False
        return BridgeReply("🧹 Сессия сброшена. Следующее сообщение начнет чистую сессию.")
    if cmd == "/thinking":
        mode = args.strip().lower()
        if mode in ("on", "true", "1"):
            return BridgeReply("💭 Отображение процесса мышления недоступно в этом режиме.")
        if mode in ("off", "false", "0"):
            return BridgeReply("💭 Процесс мышления уже скрыт.")
        return BridgeReply("💭 Процесс мышления не поддерживается.")
    if cmd == "/compact":
        return BridgeReply("🗜️ Сжатие контекста недоступно.")
    if cmd == "/image":
        mode = args.strip().lower()
        if mode in ("on", "true", "1"):
            cs.photo_enabled = True  # type: ignore[attr-defined]
            return BridgeReply("📸 Обработка фото: ВКЛ")
        if mode in ("off", "false", "0"):
            cs.photo_enabled = False  # type: ignore[attr-defined]
            return BridgeReply("📸 Обработка фото: ВЫКЛ")
        return BridgeReply("📸 Переключение обработки фото недоступно.")
    if cmd == "/files":
        wd = cfg.agy.default_workdir if hasattr(cfg.agy, "default_workdir") else ""
        files = list_inbox(wd)
        if not files:
            return BridgeReply("📂 Папка пуста.")
        return BridgeReply("📂 Недавние файлы:\n" + "\n".join(f"• {f}" for f in files))
    if cmd == "/queue":
        return BridgeReply("📋 Статус очереди доступен в логах сервера.")
    return None


def handle_callback(
    cq: "CallbackQuery",
    cs: "ChatState",
    cfg: "Config",
) -> BridgeReply:
    """Handle inline-keyboard button taps. Always returns a reply to render."""
    data = cq.data

    if data == "nav:status":
        return BridgeReply(render_status(cs, cfg))
    if data == "nav:settings":
        return _render_settings(cs, cfg)
    if data == "nav:model":
        return _render_model_picker(cs, cfg)
    if data == "nav:mode":
        return _render_mode_picker(cs, cfg)
    if data == "nav:effort":
        return _render_effort_picker(cs, cfg)
    if data == "R":
        cs.has_session = False
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Сессия сброшена")

    if data.startswith("m:"):
        choice = data[2:]
        if choice == _DEFAULT_TOKEN:
            cs.model = ""
            toast = "Настройки по умолчанию"
        elif choice in MODEL_CHOICES:
            cs.model = choice
            toast = f"Model: {choice}"
        else:
            toast = "Неизвестный выбор"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)


    if data.startswith("e:"):
        choice = data[2:]
        if choice == _DEFAULT_TOKEN:
            cs.effort = ""
            toast = "Настройки по умолчанию"
        elif choice in EFFORT_CHOICES:
            cs.effort = choice
            toast = f"Effort: {choice}"
        else:
            toast = "Неизвестный выбор"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    if data.startswith("M:"):
        choice = data[2:]
        valid_modes = {v for v, _ in MODE_CHOICES}
        if choice == _DEFAULT_TOKEN:
            cs.mode = ""
            toast = "Настройки по умолчанию"
        elif choice in valid_modes:
            cs.mode = choice
            toast = f"Mode: {choice}"
        else:
            toast = "Неизвестный выбор"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    return _render_settings(cs, cfg)
