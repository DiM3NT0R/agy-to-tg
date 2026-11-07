"""Bridge command handlers — text slash commands and inline-keyboard callbacks."""
from __future__ import annotations

import asyncio
import html
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.media import clean_inbox, list_inbox, list_workspace_files, safe_resolve_workspace_file
from src.models import (
    FALLBACK_MODELS,
    get_available_models,
    get_available_codex_models,
    get_available_openai_models,
    get_engine_for_model,
)
from src.quota import (
    fetch_quota_summary,
    render_limits_card,
    fetch_codex_limits,
    render_codex_limits_card,
    render_openai_provider_limits_card,
)
from src.state import is_valid_model
from src.telegram import InlineKeyboard

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import CallbackQuery, InboundMessage

DEFAULT_MODEL = "gemini-3.8-flash-high"

EFFORT_CHOICES: tuple[str, ...] = ("low", "medium", "high")
MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("code", "Code (auto)"),
    ("plan", "Plan (no auto-execute)"),
)
_DEFAULT_TOKEN = "_DEFAULT"

WELCOME_TEXT = (
    "👋 Привет! Я твой Telegram-бот для Antigravity.\n\n"
    "Отправь мне любое сообщение, и я передам его агенту agy. "
    "Для каждого чата или темы создается своя отдельная сессия.\n\n"
    "Команды: /start · /help · /limits · /diff · /files · /voice · /compact · /pwd · /settings · /model · /mode · /reset"
)

HELP_TEXT = (
    "📖 Справка по мосту Antigravity\n\n"
    "Отправьте любой текст для общения с agy.\n\n"
    "Команды:\n"
    "/limits   — просмотр лимитов моделей и таймеров сброса\n"
    "/diff     — просмотр изменений git в рабочей папке\n"
    "/files    — список файлов проекта и инбокса\n"
    "/get      — скачать файл из рабочей папки (/get имя_файла)\n"
    "/compact  — сжатие длинного контекста сессии для экономии квоты\n"
    "/voice    — настройки голоса, скорости и автоозвучки\n"
    "/pwd      — показать рабочую папку чата\n"
    "/status   — сводка по системе и текущему чату\n"
    "/settings — панель управления с кнопками\n"
    "/account  — управление и смена аккаунтов OpenAI Codex (/auth)\n"
    "/codex_key — подключить OpenAI по API ключу\n"
    "/model    — выбрать модель (переопределяет настройки для чата)\n"
    "/models_config — настройка списка и видимости моделей (AGY, Codex, API)\n"
    "/model_add — добавить свою модель в список (/model_add prov id [name])\n"
    "/model_del — удалить добавленную модель (/model_del id)\n"
    "/effort   — выбрать уровень effort (low/medium/high)\n"
    "/timeout  — таймаут работы агента (напр. 30m, 1h)\n"
    "/mode     — выбрать режим (Code или Plan)\n"
    "/reset    — начать чистую сессию agy для этого чата\n"
    "/info     — то же, что и /status\n"
    "/image on|off — вкл/выкл обработку фото\n"
    "/update   — проверить обновления из GitHub\n"
    "/start, /help — показать эти сообщения\n\n"
    "Настройки для конкретного чата имеют приоритет над config.json."
)


@dataclass(frozen=True)
class BridgeReply:
    """Daemon's structured reply: text, optional inline keyboard, optional toast, optional document."""

    text: str = ""
    keyboard: InlineKeyboard | None = None
    toast: str = ""
    document_bytes: bytes | None = None
    document_filename: str = ""


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
    import platform
    import shutil

    model, model_src = _effective_model(cs, cfg)
    mode, mode_src = _effective_mode(cs, cfg)
    session = f"активна ({cs.turn_count} ходов)" if cs.has_session else "чистая (новая сессия)"
    home = os.path.expanduser("~")
    workdir = cs.chat_dir.replace(home, "~", 1)

    node = platform.node() or "unknown"
    host_type = "VPS" if ("dm" in node.lower() or "vps" in node.lower()) else "Home Server"
    disk_free = ""
    try:
        du = shutil.disk_usage(cs.chat_dir)
        disk_free = f"{du.free / (1024**3):.1f}GB"
    except Exception:
        pass

    voice = cs.voice or "filipp"
    speed = cs.voice_speed or "1.15"
    av = "ВКЛ" if cs.auto_voice else "ВЫКЛ"

    sys_parts = [f"<code>{html.escape(node)}</code> ({host_type})"]
    if disk_free:
        sys_parts.append(f"диск: <b>{disk_free}</b> свободно")

    engine = getattr(cs, "engine", "") or get_engine_for_model(model)
    lines = [
        "🟢 <b>Статус моста AI Agent Bridge</b>",
        f"🖥 Хост: {' · '.join(sys_parts)}",
        f"⚡️ Движок: <b>{engine.upper()}</b>",
        f"🤖 Модель: <code>{html.escape(model)}</code> [{model_src}]",
        f"🛡 Режим: <code>{html.escape(mode)}</code> [{mode_src}]",
        f"⚙️ Effort: <code>{html.escape(cs.effort or 'default')}</code> · Таймаут: <code>{html.escape(cs.print_timeout or '30m')}</code>",
        f"🎙 Голос: <code>{html.escape(voice)}</code> ({speed}x) · Авто: <b>{av}</b>",
        "",
        "<b>Текущий чат:</b>",
        f"• Сессия: {session}",
        f"• Папка: <code>{html.escape(workdir)}</code>",
    ]
    return "\n".join(lines)


def _limits_keyboard(current_tab: str = "agy") -> InlineKeyboard:
    from src.openai_providers import get_active_provider
    has_prov = bool(get_active_provider())

    tabs: list[dict[str, Any]] = [
        {"text": ("● " if current_tab == "agy" else "○ ") + "🌐 Antigravity", "callback_data": "limits:agy"},
        {"text": ("● " if current_tab == "codex" else "○ ") + "⚡️ Codex", "callback_data": "limits:codex"},
    ]
    if has_prov:
        tabs.append({"text": ("● " if current_tab == "openai" else "○ ") + "🌐 API", "callback_data": "limits:openai"})

    return [
        tabs,
        [
            {"text": "🔄 Обновить", "callback_data": f"limits:{current_tab}"},
            {"text": "🤖 Модели", "callback_data": "nav:model"},
        ],
        [
            {"text": "← Назад к настройкам", "callback_data": "nav:settings"},
        ],
    ]


def _settings_keyboard() -> InlineKeyboard:
    return [
        [
            {"text": "🤖 Сменить модель", "callback_data": "nav:model"},
            {"text": "🛡 Сменить режим", "callback_data": "nav:mode"},
        ],
        [
            {"text": "⚙️ Изменить Effort", "callback_data": "nav:effort"},
            {"text": "📁 Диалоги", "callback_data": "nav:projects"},
        ],
        [
            {"text": "🌐 API Провайдеры", "callback_data": "nav:providers"},
            {"text": "🔐 Аккаунты Codex", "callback_data": "nav:auth"},
        ],
        [
            {"text": "🎙 Голос и озвучка", "callback_data": "nav:voice"},
            {"text": "📊 Лимиты и квоты", "callback_data": "nav:limits"},
        ],
        [
            {"text": "🗜️ Сжать контекст", "callback_data": "do_compact"},
            {"text": "🧹 Сбросить сессию", "callback_data": "R"},
        ],
        [
            {"text": "🔄 Обновить", "callback_data": "nav:settings"},
        ],
    ]


VOICE_CHOICES = [
    ("filipp", "👨 Филипп (нейтральный)"),
    ("ermil", "👨 Ермил (дружелюбный)"),
    ("zahar", "👨 Захар (деловой)"),
    ("alena", "👩 Алёна (мягкий)"),
    ("dasha", "👩 Даша (энергичный)"),
    ("jane", "👩 Джейн (яркий)"),
]

SPEED_CHOICES = [
    ("1.0", "1.0x"),
    ("1.15", "1.15x"),
    ("1.3", "1.3x"),
]


def _voice_keyboard(current_voice: str, current_speed: str, auto_voice: bool) -> InlineKeyboard:
    rows: InlineKeyboard = []
    # Voice buttons
    for vid, vtitle in VOICE_CHOICES:
        cur = current_voice or "filipp"
        marker = "● " if vid == cur else "○ "
        rows.append([{"text": f"{marker}{vtitle}", "callback_data": f"v:{vid}"}])

    # Speeds
    speed_row = []
    cur_speed = current_speed or "1.15"
    for spd, stitle in SPEED_CHOICES:
        marker = "● " if (spd == cur_speed or (cur_speed == "1.1" and spd == "1.15")) else "○ "
        speed_row.append({"text": f"{marker}{stitle}", "callback_data": f"vs:{spd}"})
    rows.append(speed_row)

    # Auto-voice toggle
    av_text = "🔊 Автоозвучка: ВКЛ" if auto_voice else "🔇 Автоозвучка: ВЫКЛ"
    rows.append([{"text": av_text, "callback_data": "v_auto:toggle"}])

    rows.append([{"text": "← Назад к настройкам", "callback_data": "nav:settings"}])
    return rows


def _render_voice_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur_voice = cs.voice or "filipp"
    cur_speed = cs.voice_speed or "1.15"
    av_status = "ВКЛ (каждый ответ озвучивается)" if cs.auto_voice else "ВЫКЛ (только по кнопке «🔊 Озвучить»)"
    text = (
        "🎙 <b>Настройки голосовой озвучки (TTS)</b>\n\n"
        f"Текущий голос: <code>{cur_voice}</code>\n"
        f"Скорость речи: <b>{cur_speed}x</b>\n"
        f"Автоматическая озвучка: <b>{av_status}</b>\n\n"
        "Выберите голос и параметры ниже:"
    )
    return BridgeReply(text=text, keyboard=_voice_keyboard(cs.voice, cs.voice_speed, cs.auto_voice))


def _model_keyboard(current_per_chat: str) -> InlineKeyboard:
    from src.models import strip_model_effort
    from src.models_config import get_active_models_for_picker

    agy_models = get_active_models_for_picker("agy")
    codex_models = get_active_models_for_picker("codex")
    openai_models = get_active_models_for_picker("openai")
    rows: InlineKeyboard = []

    current_base = strip_model_effort(current_per_chat)[0] if current_per_chat else ""

    # Section 1: Google Antigravity / Gemini
    if agy_models:
        rows.append([{"text": "── 🌐 Antigravity / Gemini ──", "callback_data": "noop"}])
        for model_id, display_name in agy_models:
            marker = "● " if (model_id == current_per_chat or model_id == current_base) else "○ "
            label = f"{marker}{display_name}" if display_name and display_name != model_id else f"{marker}{model_id}"
            rows.append([{"text": label, "callback_data": f"m:{model_id}"}])

    # Section 2: OpenAI Codex
    if codex_models:
        rows.append([{"text": "── ⚡️ OpenAI Codex ──", "callback_data": "noop"}])
        for model_id, display_name in codex_models:
            marker = "● " if (model_id == current_per_chat or model_id == current_base) else "○ "
            label = f"{marker}{display_name}" if display_name and display_name != model_id else f"{marker}{model_id}"
            rows.append([{"text": label, "callback_data": f"m:{model_id}"}])

    # Section 3: OpenAI-Compatible / Custom Provider (CheapAI, OpenRouter, DeepSeek, etc.)
    if openai_models:
        rows.append([{"text": "── 🌐 OpenAI-Compatible API ──", "callback_data": "noop"}])
        for model_id, display_name in openai_models[:12]:
            marker = "● " if (model_id == current_per_chat or model_id == current_base) else "○ "
            label = f"{marker}{display_name}" if display_name and display_name != model_id else f"{marker}{model_id}"
            rows.append([{"text": label, "callback_data": f"m:{model_id}"}])

    default_marker = "● " if not current_per_chat else "○ "
    rows.append([
        {"text": default_marker + "По умолчанию (из конфига)", "callback_data": f"m:{_DEFAULT_TOKEN}"}
    ])
    rows.append([
        {"text": "⚙️ Настроить список моделей", "callback_data": "mcfg:view:agy:0"}
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


def _effort_keyboard(current_per_chat: str, model_id: str = "") -> InlineKeyboard:
    rows: InlineKeyboard = []
    labels = {
        "low": "⚡️ Low (Быстрый)",
        "medium": "⚖️ Medium (Сбалансированный)",
        "high": "🧠 High (Глубокое мышление)",
    }
    for e in EFFORT_CHOICES:
        marker = "● " if e == current_per_chat else "○ "
        lbl = labels.get(e, e.capitalize())
        rows.append([{"text": f"{marker}{lbl}", "callback_data": f"e:{e}"}])
    default_marker = "● " if not current_per_chat else "○ "
    rows.append([
        {"text": default_marker + "По умолчанию (из конфига)", "callback_data": f"e:{_DEFAULT_TOKEN}"}
    ])
    back_row: list[dict[str, Any]] = []
    if model_id:
        back_row.append({"text": "← К выбору моделей", "callback_data": "nav:model"})
    back_row.append({"text": "← Назад к настройкам", "callback_data": "nav:settings"})
    rows.append(back_row)
    return rows


def _render_effort_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur = cs.model or cfg.agy.model
    engine = getattr(cs, "engine", "") or get_engine_for_model(cur)
    eng_name = "Codex" if engine == "codex" else ("OpenAI" if engine == "openai" else "Gemini / Antigravity")
    m_info = f" для модели <code>{html.escape(cur)}</code> ({eng_name})" if cur else ""
    return BridgeReply(
        text=f"⚙️ <b>Выберите уровень Reasoning Effort</b>{m_info}\n\nТекущий уровень: <code>{html.escape(cs.effort or 'default')}</code>",
        keyboard=_effort_keyboard(cs.effort, model_id=cs.model),
    )


def _render_model_effort_picker(model_id: str, cs: "ChatState", cfg: "Config") -> BridgeReply:
    engine = get_engine_for_model(model_id)
    eng_name = "Codex" if engine == "codex" else ("OpenAI" if engine == "openai" else "Gemini / Antigravity")
    text = (
        f"🤖 <b>Выбрана модель:</b> <code>{html.escape(model_id)}</code> ({eng_name})\n\n"
        f"⚙️ <b>Теперь выберите уровень Reasoning Effort (мышления) для этой модели:</b>\n"
        f"Текущий уровень: <code>{html.escape(cs.effort or 'default')}</code>"
    )
    return BridgeReply(
        text=text,
        keyboard=_effort_keyboard(cs.effort, model_id=model_id),
        toast=f"Модель: {model_id}. Выберите effort",
    )

import sqlite3
import os

def _render_projects_picker(cs: "ChatState", offset: int = 0) -> BridgeReply:
    rows: InlineKeyboard = []
    
    db_path = os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")
    projects = []
    seen_ids = set()
    has_more = False
    
    # 1. Fetch global from SQLite
    sqlite_projects = []
    if os.path.exists(db_path):
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT conversation_id, preview, title, workspace_uris, last_modified_time FROM conversation_summaries "
                    f"ORDER BY last_modified_time DESC LIMIT 11 OFFSET {offset}"
                )
                results = cursor.fetchall()
                if len(results) > 10:
                    has_more = True
                    results = results[:10]
                for row in results:
                    cid, preview, title, wsuris, mod_time = row
                    date_str = ""
                    if mod_time and len(mod_time) >= 16:
                        date_str = f"{mod_time[8:10]}.{mod_time[5:7]} {mod_time[11:16]}"
                    
                    if title and preview and title != preview:
                        name_core = f"{title}: {preview}"
                    elif title:
                        name_core = title
                    elif preview:
                        name_core = preview
                    else:
                        name_core = cid
                    
                    name = f"[{date_str}] {name_core}" if date_str else name_core
                    sqlite_projects.append({"id": cid, "snippet": name})
        except Exception:
            pass

    # 2. Inject local projects at the top if we are on the first page
    if offset == 0:
        for p in cs.projects:
            pid = p.get("id")
            if pid and pid not in seen_ids:
                projects.append({"id": pid, "snippet": f"[Текущий чат] {p.get('snippet')}"})
                seen_ids.add(pid)
                
    # 3. Add SQLite projects
    for p in sqlite_projects:
        pid = p.get("id")
        if pid not in seen_ids:
            projects.append(p)
            seen_ids.add(pid)
            
    # Limit to 10 items for display on this page
    if len(projects) > 10:
        has_more = True
        projects = projects[:10]

    if not projects:
        rows.append([{"text": "Нет сохраненных диалогов", "callback_data": "nav:settings"}])
    for p in projects:
        marker = "● " if p.get("id") == cs.conversation_id else "○ "
        text = marker + p.get("snippet", "")
        if len(text) > 40:
            text = text[:37] + "..."
        rows.append([{"text": text, "callback_data": f"P:{p.get('id')}"}])
        
    nav_row = [{"text": "⬅️ Назад", "callback_data": "nav:settings"}]
    if has_more:
        nav_row.append({"text": "Ещё 10 ➡️", "callback_data": f"nav:projects:{offset + 10}"})
    if offset > 0:
        prev_offset = max(0, offset - 10)
        nav_row.insert(1, {"text": "⬅️ Пред.", "callback_data": f"nav:projects:{prev_offset}"})
        
    rows.append(nav_row)
    return BridgeReply(
        text="📁 **Выбор сессии:**\n\nВыберите диалог со всего сервера:",
        keyboard=rows,
    )

def _render_model_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur, src = _effective_model(cs, cfg)
    engine = getattr(cs, "engine", "") or get_engine_for_model(cur)
    return BridgeReply(
        text=f"🤖 <b>Выбор модели</b>\n\nТекущая: <code>{html.escape(cur)}</code> [{src}]\nДвижок: <b>{engine.upper()}</b>",
        keyboard=_model_keyboard(cs.model),
    )


def _render_mode_picker(cs: "ChatState", cfg: "Config") -> BridgeReply:
    cur, src = _effective_mode(cs, cfg)
    return BridgeReply(
        text=f"🛡 Choose a mode for this chat\n\nCurrent: {cur}  [{src}]",
        keyboard=_mode_keyboard(cs.mode),
    )


async def _run_git_diff(workdir: str, args: str = "") -> tuple[str, str]:
    """Run git diff in workdir safely."""
    try:
        check_proc = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if check_proc.returncode != 0:
            return "", "В рабочей папке чата нет репозитория git."

        cmd = ["git", "diff"]
        if args.strip():
            clean_arg = args.strip().split()[0]
            if re.match(r"^[a-zA-Z0-9_\-\.~^]+$", clean_arg):
                cmd.append(clean_arg)

        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if proc.returncode != 0:
            return "", proc.stderr.strip() or "Ошибка выполнения git diff"
        return proc.stdout, ""
    except Exception as exc:
        return "", f"Не удалось выполнить git diff: {exc}"


_ACTIVE_AUTH_CANCELS: dict[int, asyncio.Event] = {}


async def _render_auth_card() -> BridgeReply:
    from src.codex_runner import check_codex_auth
    from src.codex_accounts import (
        load_saved_accounts,
        read_active_auth_data,
        parse_auth_data,
    )

    codex_ok, codex_status = await check_codex_auth()
    saved_accs = load_saved_accounts()

    agy_status = "Авторизован (OAuth Google)"
    codex_status_text = f"Авторизован ({codex_status})" if codex_ok else "Не авторизован"
    codex_icon = "🟢" if codex_ok else "⚪️"

    lines = [
        "🔐 <b>Авторизация и аккаунты CLI-агентов</b>\n",
        f"🟢 <b>Google Antigravity:</b> {agy_status}",
        f"{codex_icon} <b>OpenAI Codex:</b> {codex_status_text}",
    ]

    active_acc = next((a for a in saved_accs if a.is_active), None)
    if active_acc and codex_ok:
        email_display = f"<code>{html.escape(active_acc.email or active_acc.label)}</code>"
        name_str = f" ({html.escape(active_acc.name)})" if active_acc.name else ""
        mode_str = "ChatGPT Subscription (OAuth)" if active_acc.auth_mode == "chatgpt" else "OpenAI API Key"
        lines.append(f"   👤 <b>Текущий:</b> {email_display}{name_str}")
        lines.append(f"   🔑 <b>Тип:</b> {mode_str}")
    elif not codex_ok:
        lines.append("   ⚠️ <i>Вход в Codex не выполнен. Подключите аккаунт ниже:</i>")

    keyboard: InlineKeyboard = []

    # If there are multiple saved accounts, show fast switcher buttons
    if len(saved_accs) > 1:
        lines.append(f"\n📁 <b>Сохраненные аккаунты Codex ({len(saved_accs)}):</b>")
        for acc in saved_accs:
            marker = "● " if acc.is_active else "○ "
            state = " — <i>активен</i>" if acc.is_active else ""
            lines.append(f"• {marker}<b>{html.escape(acc.label)}</b>{state}")

        # Account switch buttons row (2 buttons per row)
        row: list[dict[str, Any]] = []
        for acc in saved_accs:
            marker = "● " if acc.is_active else "○ "
            lbl = acc.label
            if len(lbl) > 18:
                lbl = lbl[:16] + ".."
            row.append({"text": f"{marker}{lbl}", "callback_data": f"acc:switch:{acc.id}"})
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

    # Actions keyboard
    action_row: list[dict[str, Any]] = []
    if codex_ok:
        action_row.append({"text": "➕ Добавить аккаунт", "callback_data": "auth:codex"})
    else:
        action_row.append({"text": "🔑 Войти в ChatGPT (OAuth)", "callback_data": "auth:codex"})
    action_row.append({"text": "🔑 API ключ", "callback_data": "acc:add_key"})
    keyboard.append(action_row)

    manage_row: list[dict[str, Any]] = []
    if len(saved_accs) > 0:
        manage_row.append({"text": "🔄 Продлить сессии", "callback_data": "acc:refresh_all"})
    if len(saved_accs) > 1:
        manage_row.append({"text": "🗑 Удалить", "callback_data": "acc:del_menu"})
    if codex_ok:
        manage_row.append({"text": "🚪 Выйти", "callback_data": "acc:logout"})
    if manage_row:
        keyboard.append(manage_row)

    keyboard.append([{"text": "← Назад к настройкам", "callback_data": "nav:settings"}])
    return BridgeReply(text="\n".join(lines), keyboard=keyboard)


def _render_delete_accounts_menu(saved_accs: list[Any]) -> BridgeReply:
    lines = [
        "🗑 <b>Удаление сохраненного аккаунта Codex</b>\n",
        "Выберите аккаунт для удаления из моста:\n",
    ]
    keyboard: InlineKeyboard = []
    for acc in saved_accs:
        marker = " [активен]" if acc.is_active else ""
        lines.append(f"• <b>{html.escape(acc.label)}</b>{marker}")
        lbl = acc.label
        if len(lbl) > 22:
            lbl = lbl[:20] + ".."
        keyboard.append([{"text": f"🗑 Удалить {lbl}", "callback_data": f"acc:delete:{acc.id}"}])
    keyboard.append([{"text": "← Назад к аккаунтам", "callback_data": "nav:auth"}])
    return BridgeReply(text="\n".join(lines), keyboard=keyboard)


async def _render_providers_card() -> BridgeReply:
    from src.openai_providers import load_providers, get_active_provider

    provs = load_providers()
    active_prov = get_active_provider()

    lines = [
        "🌐 <b>OpenAI-совместимые API провайдеры</b>\n",
        "Универсальное подключение любого стороннего API или локального сервера (OpenRouter, DeepSeek, Groq, Ollama).\n",
    ]

    if active_prov:
        lines.append(f"🟢 <b>Активный:</b> <b>{html.escape(active_prov.name)}</b>")
        lines.append(f"   🌐 <b>Base URL:</b> <code>{html.escape(active_prov.base_url)}</code>")
        lines.append(f"   🔑 <b>Ключ:</b> <code>{html.escape(active_prov.masked_key)}</code>")
        lines.append(f"   🤖 <b>Модель:</b> <code>{html.escape(active_prov.default_model or 'default')}</code>")
        if active_prov.models:
            lines.append(f"   📚 <b>Обнаружено моделей:</b> {len(active_prov.models)}")
    else:
        lines.append("⚪️ <i>Провайдеры не настроены. Добавьте первый URL или эндпоинт ниже:</i>")

    keyboard: InlineKeyboard = []

    # Fast switcher between saved providers
    if len(provs) > 1:
        lines.append(f"\n📁 <b>Сохраненные провайдеры ({len(provs)}):</b>")
        for p in provs:
            marker = "● " if p.is_active else "○ "
            state = " — <i>активен</i>" if p.is_active else ""
            lines.append(f"• {marker}<b>{html.escape(p.name)}</b> ({html.escape(p.base_url)}){state}")

        row: list[dict[str, Any]] = []
        for p in provs:
            marker = "● " if p.is_active else "○ "
            lbl = p.name
            if len(lbl) > 16:
                lbl = lbl[:14] + ".."
            row.append({"text": f"{marker}{lbl}", "callback_data": f"prov:switch:{p.id}"})
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

    # Action row
    keyboard.append([
        {"text": "🧙‍♂️ Настроить провайдер (по шагам)", "callback_data": "prov:wiz:start"},
    ])

    edit_row: list[dict[str, Any]] = []
    if active_prov:
        edit_row.append({"text": "🌐 Сменить URL", "callback_data": "prov:edit_url"})
        edit_row.append({"text": "🔑 Сменить ключ", "callback_data": "prov:edit_key"})
    if edit_row:
        keyboard.append(edit_row)

    tools_row: list[dict[str, Any]] = []
    if active_prov:
        tools_row.append({"text": "🤖 Модели", "callback_data": "prov:models"})
        tools_row.append({"text": "🧪 Тест", "callback_data": "prov:test"})
    if provs:
        tools_row.append({"text": "🗑 Удалить", "callback_data": "prov:del_menu"})
    if tools_row:
        keyboard.append(tools_row)

    keyboard.append([{"text": "← Назад к настройкам", "callback_data": "nav:settings"}])
    return BridgeReply(text="\n".join(lines), keyboard=keyboard)


def _render_add_provider_help() -> BridgeReply:
    lines = [
        "🌐 <b>Подключение OpenAI-совместимого API (любой URL)</b>\n",
        "Вы можете подключить <b>абсолютно любой OpenAI-совместимый URL</b> — список ничем не ограничен!\n",
        "<b>1. Универсальная команда с любым URL:</b>",
        "<code>/provider add &lt;URL&gt; &lt;api_key&gt; [модель]</code>\n",
        "<b>Примеры для крупных провайдеров:</b>",
        "• <b>OpenRouter:</b>",
        "<code>/provider add https://openrouter.ai/api/v1 sk-...</code>",
        "<i>(или кратко: <code>/provider add openrouter sk-...</code>)</i>\n",
        "• <b>DeepSeek:</b>",
        "<code>/provider add https://api.deepseek.com/v1 sk-... deepseek-chat</code>\n",
        "• <b>Локальный или сторонний сервер:</b>",
        "<code>/provider add http://127.0.0.1:11434/v1 ollama qwen2.5-coder:32b</code>\n",
        "<b>2. Быстрое изменение активного провайдера:</b>",
        "• <code>/provider url https://...</code> — изменить адрес",
        "• <code>/provider key sk-...</code> — обновить API ключ",
        "• <code>/provider model gpt-4o</code> — сменить модель\n",
        "<b>3. Готовые пресеты крупных провайдеров:</b>",
        "<i>(Сообщение с секретным API ключом сразу удаляется ботом из чата)</i>",
    ]
    keyboard = [
        [
            {"text": "⚡️ OpenRouter", "callback_data": "prov:preset:openrouter"},
            {"text": "⚡️ DeepSeek", "callback_data": "prov:preset:deepseek"},
        ],
        [
            {"text": "⚡️ Groq Cloud", "callback_data": "prov:preset:groq"},
            {"text": "⚡️ Ollama", "callback_data": "prov:preset:ollama"},
        ],
        [{"text": "← К провайдерам", "callback_data": "nav:providers"}],
    ]
    return BridgeReply(text="\n".join(lines), keyboard=keyboard)


def _render_delete_providers_menu(provs: list[Any]) -> BridgeReply:
    lines = [
        "🗑 <b>Удаление API провайдера</b>\n",
        "Выберите провайдер для удаления:\n",
    ]
    keyboard: InlineKeyboard = []
    for p in provs:
        marker = " [активен]" if p.is_active else ""
        lines.append(f"• <b>{html.escape(p.name)}</b> ({html.escape(p.base_url)}){marker}")
        lbl = p.name
        if len(lbl) > 22:
            lbl = lbl[:20] + ".."
        keyboard.append([{"text": f"🗑 Удалить {lbl}", "callback_data": f"prov:delete:{p.id}"}])
    keyboard.append([{"text": "← Назад к провайдерам", "callback_data": "nav:providers"}])
    return BridgeReply(text="\n".join(lines), keyboard=keyboard)


# ---------------------------------------------------------------------------
# Step-by-Step Provider Setup Wizard
# ---------------------------------------------------------------------------
_PROVIDER_WIZARDS: dict[tuple[int, int | None] | int, dict[str, Any]] = {}


def get_provider_wizard(chat_id: int, thread_id: int | None = None) -> dict[str, Any] | None:
    key = (chat_id, thread_id) if thread_id is not None else chat_id
    wiz = _PROVIDER_WIZARDS.get(key)
    if wiz is None and thread_id is not None:
        wiz = _PROVIDER_WIZARDS.get(chat_id)
    if wiz is not None:
        if time.monotonic() - wiz.get("started_at", 0) > 600:
            clear_provider_wizard(chat_id, thread_id)
            return None
    return wiz


def set_provider_wizard(chat_id: int, thread_id: int | None, data: dict[str, Any]) -> None:
    data["started_at"] = time.monotonic()
    key = (chat_id, thread_id) if thread_id is not None else chat_id
    _PROVIDER_WIZARDS[key] = data


def clear_provider_wizard(chat_id: int, thread_id: int | None = None) -> None:
    if thread_id is not None:
        _PROVIDER_WIZARDS.pop((chat_id, thread_id), None)
    _PROVIDER_WIZARDS.pop(chat_id, None)


def _render_wizard_step_url(wiz: dict[str, Any]) -> BridgeReply:
    lines = [
        "🧙‍♂️ <b>Мастер настройки API (Шаг 1 из 3: Base URL)</b>\n",
        "Укажите сетевой адрес OpenAI-совместимого эндпоинта.\n",
        "👇 <b>Отправьте URL сообщением в чат</b> (любой адрес без ограничений):",
        "• <code>https://openrouter.ai/api/v1</code>",
        "• <code>https://api.deepseek.com/v1</code>",
        "• <code>https://api.groq.com/openai/v1</code>",
        "• <code>http://127.0.0.1:11434/v1</code> (локальная Ollama)\n",
        "Или выберите готовый пресет крупного провайдера кнопкой ниже:",
    ]
    keyboard = [
        [
            {"text": "⚡️ OpenRouter", "callback_data": "prov:wiz:preset:openrouter"},
            {"text": "⚡️ DeepSeek", "callback_data": "prov:wiz:preset:deepseek"},
        ],
        [
            {"text": "⚡️ Groq Cloud", "callback_data": "prov:wiz:preset:groq"},
            {"text": "⚡️ Ollama (Local)", "callback_data": "prov:wiz:preset:ollama"},
        ],
        [
            {"text": "❌ Отмена", "callback_data": "prov:wiz:cancel"},
            {"text": "📝 Ввести одной строкой", "callback_data": "prov:help_cmd"},
        ],
    ]
    return BridgeReply(text="\n".join(lines), keyboard=keyboard)


def _render_wizard_step_key(wiz: dict[str, Any]) -> BridgeReply:
    url = wiz.get("url", "")
    name = wiz.get("name", "Custom")
    lines = [
        "🧙‍♂️ <b>Мастер настройки API (Шаг 2 из 3: Ключ)</b>\n",
        f"🌐 <b>Base URL:</b> <code>{html.escape(url)}</code>",
        f"🏷 <b>Сервис:</b> <b>{html.escape(name)}</b>\n",
        "🔑 <b>Отправьте API ключ (Bearer token) ответным сообщением в чат.</b>\n",
        "<i>🔒 Безопасность: сообщение с ключом будет немедленно удалено ботом из чата.</i>",
        "<i>ℹ️ Если ключ не нужен (например, для локальной Ollama/vLLM), нажмите кнопку «Пропустить».</i>",
    ]
    keyboard = [
        [{"text": "⏭ Пропустить (без ключа)", "callback_data": "prov:wiz:skip_key"}],
        [
            {"text": "⬅️ Назад (к URL)", "callback_data": "prov:wiz:back:url"},
            {"text": "❌ Отмена", "callback_data": "prov:wiz:cancel"},
        ],
    ]
    return BridgeReply(text="\n".join(lines), keyboard=keyboard)


def _render_wizard_step_model(wiz: dict[str, Any], page: int = 0) -> BridgeReply:
    url = wiz.get("url", "")
    name = wiz.get("name", "Custom")
    masked_key = wiz.get("masked_key", "(нет ключа)")
    models: list[str] = wiz.get("models", [])
    default_sugg = wiz.get("default_model", "")

    lines = [
        "🧙‍♂️ <b>Мастер настройки API (Шаг 3 из 3: Модель)</b>\n",
        f"🌐 <b>Base URL:</b> <code>{html.escape(url)}</code>",
        f"🏷 <b>Сервис:</b> <b>{html.escape(name)}</b>",
        f"🔑 <b>Ключ:</b> <code>{html.escape(masked_key)}</code>\n",
    ]

    if models:
        lines.append(f"✅ <b>Связь установлена!</b> Обнаружено моделей: <b>{len(models)}</b>.\n")
        lines.append("Выберите модель кнопкой или <b>отправьте её точное название сообщением</b>:")
    else:
        lines.append("ℹ️ <i>Автообнаружение через /models не вернуло список (или эндпоинт его не отдает).</i>\n")
        lines.append("<b>Отправьте название модели сообщением в чат</b> (например: <code>gpt-4o</code>) или выберите вариант:")

    keyboard: InlineKeyboard = []

    # If preset has a suggested model, show it prominently at top
    if default_sugg and (not models or default_sugg not in models[:6]):
        keyboard.append([{"text": f"⭐️ {default_sugg} (рекомендуется)", "callback_data": f"prov:wiz:model:{default_sugg}"}])

    page_size = 6
    if models:
        total_pages = (len(models) + page_size - 1) // page_size
        page = max(0, min(page, total_pages - 1))
        start_idx = page * page_size
        page_models = models[start_idx : start_idx + page_size]

        row: list[dict[str, Any]] = []
        for i, m in enumerate(page_models):
            global_idx = start_idx + i
            lbl = m
            if len(lbl) > 20:
                lbl = lbl[:18] + ".."
            row.append({"text": f"🤖 {lbl}", "callback_data": f"prov:wiz:m_idx:{global_idx}"})
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        nav_row: list[dict[str, Any]] = []
        if page > 0:
            nav_row.append({"text": "⬅️ Назад", "callback_data": f"prov:wiz:page:{page - 1}"})
        if total_pages > 1:
            nav_row.append({"text": f"📄 {page + 1}/{total_pages}", "callback_data": "noop"})
        if page < total_pages - 1:
            nav_row.append({"text": "Вперед ➡️", "callback_data": f"prov:wiz:page:{page + 1}"})
        if nav_row:
            keyboard.append(nav_row)
    else:
        keyboard.append([
            {"text": "⚡️ gpt-4o", "callback_data": "prov:wiz:model:gpt-4o"},
            {"text": "⚡️ default", "callback_data": "prov:wiz:model:default"},
        ])

    keyboard.append([
        {"text": "⬅️ Назад (к ключу)", "callback_data": "prov:wiz:back:key"},
        {"text": "❌ Отмена", "callback_data": "prov:wiz:cancel"},
    ])
    return BridgeReply(text="\n".join(lines), keyboard=keyboard)


def _render_wizard_success(provider: Any) -> BridgeReply:
    lines = [
        "🎉 <b>Провайдер успешно настроен и активирован!</b>\n",
        f"🏷 <b>Сервис:</b> <b>{html.escape(provider.name)}</b>",
        f"🌐 <b>Base URL:</b> <code>{html.escape(provider.base_url)}</code>",
        f"🔑 <b>API-ключ:</b> <code>{html.escape(provider.masked_key)}</code>",
        f"🤖 <b>Модель по умолчанию:</b> <code>{html.escape(provider.default_model or 'default')}</code>",
    ]
    if provider.models:
        lines.append(f"📚 <b>Всего моделей:</b> {len(provider.models)}")
    lines.append("\nВсе последующие запросы к OpenAI-совместимому движку будут использовать этот сервер.")

    keyboard = [
        [
            {"text": "🧪 Проверить связь", "callback_data": "prov:test"},
            {"text": "🤖 Модели", "callback_data": "prov:models"},
        ],
        [
            {"text": "🌐 Все провайдеры", "callback_data": "nav:providers"},
            {"text": "⚙️ Настройки", "callback_data": "nav:settings"},
        ],
    ]
    return BridgeReply(text="\n".join(lines), keyboard=keyboard, toast="Провайдер активирован!")


async def _handle_wizard_text_input(
    msg: "InboundMessage",
    wiz: dict[str, Any],
    tg: Any = None,
) -> BridgeReply:
    from src.openai_providers import (
        normalize_base_url,
        derive_provider_from_url,
        is_url_like,
        fetch_provider_models,
        save_provider,
        OpenAIProvider,
    )

    step = wiz.get("step", "url")
    user_text = msg.text.strip()
    tid = getattr(msg, "message_thread_id", None)

    if step == "url":
        if not is_url_like(user_text) and not user_text.startswith(("http://", "https://")):
            return BridgeReply(
                "⚠️ <b>Это не похоже на валидный URL адрес.</b>\n\n"
                "Пожалуйста, отправьте корректный адрес эндпоинта, например:\n"
                "• <code>https://openrouter.ai/api/v1</code>\n"
                "• <code>https://api.deepseek.com/v1</code>\n"
                "• <code>http://127.0.0.1:11434/v1</code>\n\n"
                "Или нажмите кнопку с шаблоном:",
                keyboard=_render_wizard_step_url(wiz).keyboard,
            )
        norm_url = normalize_base_url(user_text)
        slug, name = derive_provider_from_url(norm_url)
        wiz["url"] = norm_url
        wiz["id"] = slug
        wiz["name"] = name
        wiz["step"] = "key"
        set_provider_wizard(msg.chat_id, tid, wiz)
        return _render_wizard_step_key(wiz)

    elif step == "key":
        if tg and getattr(msg, "message_id", None):
            try:
                asyncio.create_task(tg.delete_message(msg.chat_id, msg.message_id))
            except Exception:
                pass

        key = user_text
        wiz["key"] = key
        wiz["masked_key"] = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
        wiz["step"] = "model"

        temp_prov = OpenAIProvider(
            id=wiz.get("id", "custom"),
            name=wiz.get("name", "Custom"),
            base_url=wiz.get("url", ""),
            api_key=key,
        )
        models = await fetch_provider_models(temp_prov, timeout=5.0)
        wiz["models"] = models
        set_provider_wizard(msg.chat_id, tid, wiz)
        return _render_wizard_step_model(wiz)

    elif step == "model":
        model_name = user_text
        final_prov = OpenAIProvider(
            id=wiz.get("id", "custom"),
            name=wiz.get("name", "Custom"),
            base_url=wiz.get("url", ""),
            api_key=wiz.get("key", ""),
            default_model=model_name,
            models=wiz.get("models", []),
        )
        saved = save_provider(final_prov, make_active=True)
        clear_provider_wizard(msg.chat_id, tid)
        return _render_wizard_success(saved)

    return _render_wizard_step_url(wiz)


async def _run_device_auth_flow(tg: Any, chat_id: int, message_id: int):
    from src.codex_runner import run_codex_device_auth
    from src.codex_accounts import (
        backup_active_auth_for_login,
        restore_backup_after_aborted_login,
        clean_login_backup,
        read_active_auth_data,
        save_account_data,
    )

    backup_active_auth_for_login()

    cancel_ev = asyncio.Event()
    _ACTIVE_AUTH_CANCELS[chat_id] = cancel_ev

    async def on_code(url: str, code: str):
        try:
            await tg.edit_message_text(
                chat_id,
                message_id,
                f"🔐 <b>Авторизация в OpenAI Codex (OAuth)</b>\n\n"
                f"1️⃣ Перейдите по ссылке:\n{url}\n\n"
                f"2️⃣ Введите код:\n<code>{code}</code> <i>(нажмите для копирования)</i>\n\n"
                f"⏳ <i>Ожидание подтверждения в браузере (до 15 минут)...</i>",
                keyboard=[
                    [{"text": "🔗 Открыть auth.openai.com", "url": url}],
                    [{"text": "❌ Отмена", "callback_data": "auth:cancel"}],
                ],
            )
        except Exception as e:
            LOG.warning("Failed to update auth message: %s", e)

    try:
        ok, msg = await run_codex_device_auth(on_code, cancel_event=cancel_ev)
        if ok:
            clean_login_backup()
            new_auth = read_active_auth_data()
            details = ""
            if new_auth:
                saved_acc = save_account_data(new_auth, make_active=True)
                email_str = f"<code>{html.escape(saved_acc.email or saved_acc.label)}</code>"
                name_str = f" ({html.escape(saved_acc.name)})" if saved_acc.name else ""
                details = f"\n\n👤 <b>Подключен:</b> {email_str}{name_str}\n🔑 <b>Режим:</b> ChatGPT Subscription"

            await tg.edit_message_text(
                chat_id,
                message_id,
                f"✅ <b>Авторизация в OpenAI Codex успешно завершена!</b>{details}\n\n"
                "Аккаунт сохранен и выбран активным. Теперь вам доступны модели GPT-6.1 Sol, GPT-6 Astra, GPT-6.1 Terra и др.",
                keyboard=[
                    [{"text": "👤 Аккаунты Codex", "callback_data": "nav:auth"}],
                    [{"text": "🤖 Выбрать модель", "callback_data": "nav:model"}],
                    [{"text": "⚙️ Настройки", "callback_data": "nav:settings"}],
                ],
            )
        else:
            restore_backup_after_aborted_login()
            await tg.edit_message_text(
                chat_id,
                message_id,
                f"⚠️ <b>Авторизация не завершена</b>\n\n{msg}",
                keyboard=[
                    [{"text": "🔄 Попробовать снова", "callback_data": "auth:codex"}],
                    [{"text": "← К управлению аккаунтами", "callback_data": "nav:auth"}],
                ],
            )
    except Exception as e:
        restore_backup_after_aborted_login()
        LOG.error("Device auth flow failed: %s", e)
    finally:
        _ACTIVE_AUTH_CANCELS.pop(chat_id, None)


async def _handle_codex_key_cmd(key_arg: str, msg: "InboundMessage", tg: Any = None) -> BridgeReply:
    clean_key = key_arg.strip()
    if not clean_key:
        return BridgeReply(
            "🔑 <b>Установка API ключа OpenAI Codex</b>\n\n"
            "Отправьте команду:\n<code>/codex_key sk-...</code>\n\n"
            "<i>(Бот сразу удалит сообщение с ключом из чата для безопасности)</i>",
            keyboard=[[{"text": "← К управлению аккаунтами", "callback_data": "nav:auth"}]],
        )

    from src.codex_accounts import save_account_data
    auth_data = {
        "auth_mode": "api_key",
        "OPENAI_API_KEY": clean_key,
        "tokens": None,
    }
    acc = save_account_data(auth_data, make_active=True)

    # Attempt to delete the message containing secret key
    if tg and getattr(msg, "message_id", None):
        try:
            asyncio.create_task(tg.delete_message(msg.chat_id, msg.message_id))
        except Exception:
            pass

    return BridgeReply(
        f"✅ <b>API ключ OpenAI успешно сохранен и активирован!</b>\n\n"
        f"👤 <b>Аккаунт:</b> <code>{html.escape(acc.label)}</code>\n"
        f"🔑 <b>Режим:</b> OpenAI API Key\n\n"
        f"<i>(Сообщение с ключом удалено из чата для безопасности)</i>",
        keyboard=[
            [{"text": "👤 Аккаунты Codex", "callback_data": "nav:auth"}],
            [{"text": "🤖 Выбрать модель", "callback_data": "nav:model"}],
            [{"text": "⚙️ Настройки", "callback_data": "nav:settings"}],
        ],
        toast="API ключ сохранен!",
    )



# ---------------------------------------------------------------------------
# In-Bot Model Display & Visibility Configuration
# ---------------------------------------------------------------------------
_WAITING_FOR_MODEL_ADD: dict[tuple[int, int | None] | int, dict[str, Any]] = {}


def get_waiting_model_add(chat_id: int, thread_id: int | None = None) -> dict[str, Any] | None:
    key = (chat_id, thread_id) if thread_id is not None else chat_id
    item = _WAITING_FOR_MODEL_ADD.get(key)
    if item is None and thread_id is not None:
        item = _WAITING_FOR_MODEL_ADD.get(chat_id)
    if item is not None:
        if time.monotonic() - item.get("started_at", 0) > 300:
            clear_waiting_model_add(chat_id, thread_id)
            return None
    return item


def set_waiting_model_add(chat_id: int, thread_id: int | None, data: dict[str, Any]) -> None:
    data["started_at"] = time.monotonic()
    key = (chat_id, thread_id) if thread_id is not None else chat_id
    _WAITING_FOR_MODEL_ADD[key] = data


def clear_waiting_model_add(chat_id: int, thread_id: int | None = None) -> None:
    if thread_id is not None:
        _WAITING_FOR_MODEL_ADD.pop((chat_id, thread_id), None)
    _WAITING_FOR_MODEL_ADD.pop(chat_id, None)


def _render_models_config_card(provider: str = "agy", page: int = 0) -> BridgeReply:
    prov = provider.strip().lower()
    if prov not in ("agy", "codex", "openai"):
        prov = "agy"

    from src.models_config import (
        get_all_provider_models,
        get_provider_summary_counts,
    )

    counts = get_provider_summary_counts()
    agy_on, agy_tot = counts.get("agy", (0, 0))
    cdx_on, cdx_tot = counts.get("codex", (0, 0))
    api_on, api_tot = counts.get("openai", (0, 0))

    prov_names = {
        "agy": "Google Antigravity / Gemini",
        "codex": "OpenAI Codex",
        "openai": "OpenAI API / Провайдеры",
    }
    cur_title = prov_names.get(prov, prov.upper())

    all_models = get_all_provider_models(prov)
    total_items = len(all_models)
    page_size = 6
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    if page < 0:
        page = 0
    elif page >= total_pages:
        page = total_pages - 1

    start_idx = page * page_size
    page_items = all_models[start_idx : start_idx + page_size]
    cur_on = sum(1 for _, _, is_on, _ in all_models if is_on)

    lines = [
        "⚙️ <b>Настройка видимости моделей</b>\n",
        f"Раздел: <b>{cur_title}</b>",
        f"Активно: <b>{cur_on}</b> из <b>{total_items}</b> моделей\n",
        "Нажимайте на модель, чтобы скрыть <code>[❌]</code> или вернуть <code>[✅]</code> её в меню <code>/model</code>.",
    ]
    if any(is_custom for _, _, _, is_custom in all_models):
        lines.append("💡 Кнопка 🗑 удаляет пользовательскую добавленную модель.")

    keyboard: InlineKeyboard = []

    # 1. Provider Tabs
    keyboard.append([
        {"text": ("● " if prov == "agy" else "") + f"🌐 AGY ({agy_on}/{agy_tot})", "callback_data": "mcfg:tab:agy"},
        {"text": ("● " if prov == "codex" else "") + f"⚡️ Codex ({cdx_on}/{cdx_tot})", "callback_data": "mcfg:tab:codex"},
        {"text": ("● " if prov == "openai" else "") + f"🔌 API ({api_on}/{api_tot})", "callback_data": "mcfg:tab:openai"},
    ])

    # 2. Model Rows
    if not page_items:
        keyboard.append([{"text": "— Нет моделей в этом разделе —", "callback_data": "noop"}])
    else:
        for idx, (mid, mname, is_on, is_custom) in enumerate(page_items):
            icon = "✅ " if is_on else "❌ "
            lbl = mname if mname and mname != mid else mid
            if len(lbl) > 28:
                lbl = lbl[:26] + "…"
            btn_text = f"{icon}{lbl}"
            row = [{"text": btn_text, "callback_data": f"mcfg:tgl:{prov}:{page}:{idx}"}]
            if is_custom:
                row.append({"text": "🗑", "callback_data": f"mcfg:del:{prov}:{page}:{idx}"})
            keyboard.append(row)

    # 3. Pagination Row
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append({"text": "⬅️ Назад", "callback_data": f"mcfg:page:{prov}:{page - 1}"})
        else:
            nav_row.append({"text": "·", "callback_data": "noop"})
        nav_row.append({"text": f"{page + 1} / {total_pages}", "callback_data": "noop"})
        if page < total_pages - 1:
            nav_row.append({"text": "Вперед ➡️", "callback_data": f"mcfg:page:{prov}:{page + 1}"})
        else:
            nav_row.append({"text": "·", "callback_data": "noop"})
        keyboard.append(nav_row)

    # 4. Action Row
    short_prov = {"agy": "AGY", "codex": "Codex", "openai": "API"}.get(prov, prov.upper())
    keyboard.append([
        {"text": f"➕ Добавить в {short_prov}", "callback_data": f"mcfg:add:{prov}:{page}"},
        {"text": "🔄 Сброс раздела", "callback_data": f"mcfg:rst:{prov}"},
    ])

    # 5. Bottom Navigation
    keyboard.append([
        {"text": "← К выбору модели", "callback_data": "nav:model"},
        {"text": "← В настройки", "callback_data": "nav:settings"},
    ])

    return BridgeReply(text="\n".join(lines), keyboard=keyboard)


async def handle_text_command(
    msg: "InboundMessage",
    cs: "ChatState",
    cfg: "Config",
    tg: Any = None,
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

    tid = getattr(msg, "message_thread_id", None)
    if cmd in ("/cancel", "/stop"):
        if get_waiting_model_add(msg.chat_id, tid):
            w = get_waiting_model_add(msg.chat_id, tid)
            clear_waiting_model_add(msg.chat_id, tid)
            prov = w.get("provider", "agy") if w else "agy"
            page = w.get("page", 0) if w else 0
            return _render_models_config_card(prov, page)
        if get_provider_wizard(msg.chat_id, tid):
            clear_provider_wizard(msg.chat_id, tid)
            return BridgeReply(
                "❌ <b>Пошаговая настройка провайдера отменена.</b>",
                keyboard=[[{"text": "🌐 К провайдерам", "callback_data": "nav:providers"}]],
            )

    if not cmd.startswith("/"):
        w_add = get_waiting_model_add(msg.chat_id, tid)
        if w_add:
            clear_waiting_model_add(msg.chat_id, tid)
            prov = w_add.get("provider", "openai")
            page = w_add.get("page", 0)
            parts = stripped.split(maxsplit=1)
            mid = parts[0]
            mname = parts[1] if len(parts) > 1 else mid
            if is_valid_model(mid):
                from src.models_config import add_custom_model
                add_custom_model(prov, mid, mname)
                rep = _render_models_config_card(prov, page)
                return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=f"Модель {mid} добавлена")
            else:
                return BridgeReply(
                    f"⚠️ Недопустимый идентификатор модели: <code>{html.escape(mid)}</code>\n\n"
                    "Используйте только латинские буквы, цифры, дефис, точку или слэш.",
                    keyboard=[[{"text": "← К настройке моделей", "callback_data": f"mcfg:view:{prov}:{page}"}]],
                )

        wiz = get_provider_wizard(msg.chat_id, tid)
        if wiz:
            return await _handle_wizard_text_input(msg, wiz, tg)

    if cmd == "/start":
        return BridgeReply(WELCOME_TEXT)
    if cmd == "/help":
        return BridgeReply(HELP_TEXT)
    if cmd in ("/limits", "/quota"):
        sub = args.strip().lower()
        active_tab = "agy"
        if sub in ("codex", "openai_codex", "chatgpt"):
            active_tab = "codex"
        elif sub in ("api", "provider", "providers", "openai"):
            active_tab = "openai"
        elif not sub:
            cur_eng = getattr(cs, "engine", "") or get_engine_for_model(cs.model or cfg.agy.model)
            if cur_eng == "codex":
                active_tab = "codex"
            elif cur_eng in ("openai", "openai_compat"):
                active_tab = "openai"
            else:
                active_tab = "agy"

        if active_tab == "codex":
            cqs = await fetch_codex_limits()
            return BridgeReply(render_codex_limits_card(cqs), keyboard=_limits_keyboard("codex"))
        elif active_tab == "openai":
            from src.openai_providers import get_active_provider
            prov = get_active_provider()
            if prov:
                return BridgeReply(render_openai_provider_limits_card(prov), keyboard=_limits_keyboard("openai"))
            return BridgeReply("⚠️ Нет активного OpenAI-совместимого провайдера.", keyboard=_limits_keyboard("agy"))
        else:
            qs = await fetch_quota_summary()
            return BridgeReply(render_limits_card(qs), keyboard=_limits_keyboard("agy"))
    if cmd in ("/status", "/info"):
        return BridgeReply(render_status(cs, cfg))
    if cmd == "/settings":
        return _render_settings(cs, cfg)
    if cmd in ("/auth", "/login", "/account", "/accounts", "/codex_account"):
        return await _render_auth_card()
    if cmd == "/codex_login":
        if args:
            return await _handle_codex_key_cmd(args, msg, tg)
        return await _render_auth_card()
    if cmd in ("/codex_key", "/codex_token"):
        return await _handle_codex_key_cmd(args, msg, tg)
    if cmd in ("/provider", "/providers", "/api"):
        from src.openai_providers import (
            load_providers,
            get_active_provider,
            get_provider,
            save_provider,
            set_active_provider,
            delete_provider,
            fetch_provider_models,
            normalize_base_url,
            OpenAIProvider,
            PROVIDER_PRESETS,
        )
        sub_parts = args.strip().split()
        sub = sub_parts[0].lower() if sub_parts else ""

        if sub in ("wizard", "new"):
            wiz = {"step": "url"}
            set_provider_wizard(msg.chat_id, tid, wiz)
            return _render_wizard_step_url(wiz)

        if sub == "add":
            if tg and getattr(msg, "message_id", None):
                try:
                    asyncio.create_task(tg.delete_message(msg.chat_id, msg.message_id))
                except Exception:
                    pass

            rem_args = sub_parts[1:]
            if not rem_args:
                wiz = {"step": "url"}
                set_provider_wizard(msg.chat_id, tid, wiz)
                return _render_wizard_step_url(wiz)

            from src.openai_providers import is_url_like, derive_provider_from_url

            # Format 1: Direct URL first
            # e.g. /provider add https://api.cheapai.io/v1 sk-xxx [model]
            if is_url_like(rem_args[0]) and len(rem_args) >= 2:
                raw_url = rem_args[0]
                base_u = normalize_base_url(raw_url)
                key = rem_args[1]
                def_m = rem_args[2] if len(rem_args) > 2 else ""
                slug, display_name = derive_provider_from_url(base_u)
                new_p = OpenAIProvider(
                    id=slug,
                    name=display_name,
                    base_url=base_u,
                    api_key=key,
                    default_model=def_m,
                )
            # Format 2: Preset name first
            # e.g. /provider add cheapai sk-xxx [model]
            elif rem_args[0].lower() in PROVIDER_PRESETS and len(rem_args) >= 2:
                prov_id = rem_args[0].lower()
                preset = PROVIDER_PRESETS[prov_id]
                def_m = rem_args[2] if len(rem_args) > 2 else preset.get("default_model", "")
                new_p = OpenAIProvider(
                    id=prov_id,
                    name=preset["name"],
                    base_url=preset["base_url"],
                    api_key=rem_args[1],
                    default_model=def_m,
                )
            # Format 3: Explicit ID + URL + KEY + [MODEL]
            # e.g. /provider add myprov https://api.cheapai.io/v1 sk-xxx [model]
            elif len(rem_args) >= 3 and is_url_like(rem_args[1]):
                prov_id = rem_args[0].lower()
                base_u = normalize_base_url(rem_args[1])
                key = rem_args[2]
                def_m = rem_args[3] if len(rem_args) > 3 else ""
                name = PROVIDER_PRESETS.get(prov_id, {}).get("name", prov_id.capitalize())
                new_p = OpenAIProvider(
                    id=prov_id,
                    name=name,
                    base_url=base_u,
                    api_key=key,
                    default_model=def_m,
                )
            # Format 4: Update key of an existing saved provider
            # e.g. /provider add cheapai sk-xxx
            elif len(rem_args) == 2 and get_provider(rem_args[0].lower()):
                prov_id = rem_args[0].lower()
                existing = get_provider(prov_id)
                assert existing is not None
                existing.api_key = rem_args[1]
                new_p = existing
            else:
                return BridgeReply(
                    "⚠️ <b>Формат команды:</b>\n"
                    "<code>/provider add &lt;URL&gt; &lt;api_key&gt; [модель]</code>\n\n"
                    "Например:\n"
                    "<code>/provider add https://openrouter.ai/api/v1 sk-...</code>\n\n"
                    "Или по пресету:\n"
                    "<code>/provider add openrouter &lt;api_key&gt;</code>",
                    keyboard=[[{"text": "← К провайдерам", "callback_data": "nav:providers"}]],
                )

            saved = save_provider(new_p, make_active=True)
            asyncio.create_task(fetch_provider_models(saved))

            return BridgeReply(
                f"✅ <b>Провайдер {html.escape(saved.name)} успешно сохранен и активирован!</b>\n\n"
                f"🌐 <b>Base URL:</b> <code>{html.escape(saved.base_url)}</code>\n"
                f"🔑 <b>API Key:</b> <code>{html.escape(saved.masked_key)}</code>\n"
                f"🤖 <b>Модель:</b> <code>{html.escape(saved.default_model or 'авто')}</code>\n\n"
                f"<i>(Сообщение с ключом удалено из чата для безопасности)</i>",
                keyboard=[
                    [{"text": "🌐 Все провайдеры", "callback_data": "nav:providers"}],
                    [{"text": "🤖 Модели", "callback_data": "prov:models"}],
                    [{"text": "⚙️ Настройки", "callback_data": "nav:settings"}],
                ],
            )

        elif sub == "url":
            if len(sub_parts) < 2:
                p = get_active_provider()
                cur = f"\nТекущий: <code>{p.base_url}</code>" if p else ""
                return BridgeReply(
                    "⚠️ <b>Укажите новый URL:</b>\n"
                    f"<code>/provider url https://openrouter.ai/api/v1</code>{cur}",
                    keyboard=[[{"text": "← К провайдерам", "callback_data": "nav:providers"}]],
                )
            from src.openai_providers import update_provider_url
            new_url = sub_parts[1]
            saved = update_provider_url(new_url)
            asyncio.create_task(fetch_provider_models(saved))
            return BridgeReply(
                f"✅ <b>Base URL обновлен для {html.escape(saved.name)}:</b>\n"
                f"<code>{html.escape(saved.base_url)}</code>",
                keyboard=[
                    [{"text": "🧪 Проверить подключение", "callback_data": "prov:test"}],
                    [{"text": "🌐 Все провайдеры", "callback_data": "nav:providers"}],
                ],
            )

        elif sub in ("key", "token"):
            if tg and getattr(msg, "message_id", None):
                try:
                    asyncio.create_task(tg.delete_message(msg.chat_id, msg.message_id))
                except Exception:
                    pass
            if len(sub_parts) < 2:
                return BridgeReply(
                    "⚠️ <b>Укажите новый API ключ:</b>\n"
                    "<code>/provider key sk-...</code>\n\n"
                    "<i>(Сообщение с ключом будет удалено сразу после отправки)</i>",
                    keyboard=[[{"text": "← К провайдерам", "callback_data": "nav:providers"}]],
                )
            from src.openai_providers import update_provider_key
            new_key = sub_parts[1]
            saved = update_provider_key(new_key)
            return BridgeReply(
                f"🔑 <b>API ключ для {html.escape(saved.name)} успешно обновлен!</b>\n"
                f"<code>{html.escape(saved.masked_key)}</code>\n\n"
                f"<i>(Сообщение с ключом удалено из чата для безопасности)</i>",
                keyboard=[
                    [{"text": "🧪 Проверить подключение", "callback_data": "prov:test"}],
                    [{"text": "🌐 Все провайдеры", "callback_data": "nav:providers"}],
                ],
            )

        elif sub == "model":
            if len(sub_parts) < 2:
                p = get_active_provider()
                cur = f"\nТекущая модель: <code>{p.default_model}</code>" if (p and p.default_model) else ""
                return BridgeReply(
                    "⚠️ <b>Укажите название модели:</b>\n"
                    f"<code>/provider model gpt-4o</code>{cur}",
                    keyboard=[
                        [{"text": "🤖 Выбрать из списка", "callback_data": "prov:models"}],
                        [{"text": "← К провайдерам", "callback_data": "nav:providers"}],
                    ],
                )
            from src.openai_providers import update_provider_model
            new_model = sub_parts[1]
            saved = update_provider_model(new_model)
            if not saved:
                return BridgeReply("⚠️ Нет активного провайдера. Сначала добавьте провайдер через <code>/provider add &lt;URL&gt; &lt;key&gt;</code>.")
            return BridgeReply(
                f"🤖 <b>Модель по умолчанию для {html.escape(saved.name)} изменена:</b>\n"
                f"<code>{html.escape(saved.default_model)}</code>",
                keyboard=[
                    [{"text": "🌐 Все провайдеры", "callback_data": "nav:providers"}],
                    [{"text": "🤖 Все модели", "callback_data": "prov:models"}],
                ],
            )

        elif sub in ("switch", "set", "use"):
            if len(sub_parts) < 2:
                return BridgeReply("⚠️ Укажите ID провайдера: <code>/provider set openrouter</code>")
            target_id = sub_parts[1].lower()
            if set_active_provider(target_id):
                p = get_active_provider()
                return BridgeReply(
                    f"⚡️ Активный провайдер переключен на <b>{html.escape(p.name if p else target_id)}</b>",
                    keyboard=[[{"text": "🌐 К провайдерам", "callback_data": "nav:providers"}], [{"text": "🤖 Модели", "callback_data": "prov:models"}]],
                )
            return BridgeReply(f"⚠️ Провайдер с ID <code>{html.escape(target_id)}</code> не найден.")

        elif sub in ("del", "delete", "remove"):
            if len(sub_parts) < 2:
                return BridgeReply("⚠️ Укажите ID провайдера: <code>/provider del openrouter</code>")
            target_id = sub_parts[1].lower()
            if delete_provider(target_id):
                return BridgeReply(
                    f"🗑 Провайдер <code>{html.escape(target_id)}</code> удален.",
                    keyboard=[[{"text": "🌐 К провайдерам", "callback_data": "nav:providers"}]],
                )
            return BridgeReply(f"⚠️ Провайдер с ID <code>{html.escape(target_id)}</code> не найден.")

        elif sub in ("models", "list_models"):
            p = get_active_provider()
            if not p:
                return BridgeReply("⚠️ Нет активного провайдера.")
            models = await fetch_provider_models(p)
            if not models and p.models:
                models = p.models
            if not models:
                return BridgeReply(
                    f"⚠️ Не удалось обнаружить модели у <b>{html.escape(p.name)}</b> "
                    f"через <code>{html.escape(p.base_url)}/models</code>. "
                    f"Текущая модель: <code>{html.escape(p.default_model or 'default')}</code>."
                )
            lines = [f"🤖 <b>Доступные модели {html.escape(p.name)} ({len(models)}):</b>"]
            for m in models[:25]:
                marker = "● " if m == p.default_model else "• "
                lines.append(f"{marker}<code>{html.escape(m)}</code>")
            if len(models) > 25:
                lines.append(f"<i>...и еще {len(models) - 25} моделей</i>")
            return BridgeReply("\n".join(lines), keyboard=[[{"text": "← К провайдерам", "callback_data": "nav:providers"}]])

        return await _render_providers_card()

    if cmd == "/engine":
        if args:
            clean_eng = args.strip().lower()
            if clean_eng in ("agy", "codex", "openai", "openai_compat"):
                cs.engine = "openai" if clean_eng == "openai_compat" else clean_eng
                return BridgeReply(f"⚡️ Движок переключен на <b>{cs.engine.upper()}</b>")
            return BridgeReply("⚠️ Доступные движки: agy, codex, openai")
        cur_eng = getattr(cs, "engine", "") or get_engine_for_model(cs.model or cfg.agy.model)
        return BridgeReply(
            f"⚡️ Текущий движок: <b>{cur_eng.upper()}</b>\n"
            "Используйте: /engine agy | /engine codex | /engine openai"
        )
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
    if cmd in ("/model", "/models"):
        if args:
            if is_valid_model(args):
                cs.model = args
                cs.engine = get_engine_for_model(args)
                if cs.engine in ("agy", "codex"):
                    return _render_model_effort_picker(args, cs, cfg)
                return BridgeReply(f"🤖 Модель изменена на <b>{args}</b> (Движок: <b>{cs.engine.upper()}</b>)")
            return BridgeReply(f"⚠️ Неподдерживаемая модель: {args}")
        return _render_model_picker(cs, cfg)
    if cmd in ("/models_config", "/model_config", "/models_visibility"):
        target_prov = "agy"
        if args:
            sub = args.strip().lower()
            if sub in ("codex", "openai_codex", "chatgpt"):
                target_prov = "codex"
            elif sub in ("api", "provider", "providers", "openai", "cheapai"):
                target_prov = "openai"
        return _render_models_config_card(target_prov, 0)
    if cmd in ("/model_add", "/models_add"):
        if not args:
            return BridgeReply(
                "➕ <b>Добавление модели</b>\n\n"
                "Использование:\n"
                "• <code>/model_add &lt;провайдер&gt; &lt;model_id&gt; [название]</code>\n"
                "• <code>/model_add &lt;model_id&gt; [название]</code> (автоопределение)\n\n"
                "Провайдеры: <code>agy</code>, <code>codex</code>, <code>openai</code>\n\n"
                "Примеры:\n"
                "• <code>/model_add codex gpt-5-preview GPT-5 Preview</code>\n"
                "• <code>/model_add openai mistral-large Mistral Large</code>\n"
                "• <code>/model_add gemini-2.5-flash-custom</code>",
                keyboard=[[{"text": "⚙️ Настроить список моделей", "callback_data": "mcfg:view:agy:0"}]],
            )
        parts = args.split()
        if parts[0].lower() in ("agy", "codex", "openai"):
            prov = parts[0].lower()
            mid = parts[1] if len(parts) > 1 else ""
            mname = " ".join(parts[2:]) if len(parts) > 2 else mid
        else:
            mid = parts[0]
            mname = " ".join(parts[1:]) if len(parts) > 1 else mid
            prov = get_engine_for_model(mid)

        if not mid or not is_valid_model(mid):
            return BridgeReply(f"⚠️ Недопустимый идентификатор модели: <code>{html.escape(mid)}</code>")

        from src.models_config import add_custom_model
        add_custom_model(prov, mid, mname)
        return BridgeReply(
            f"✅ Модель <code>{html.escape(mid)}</code> добавлена в раздел <b>{prov.upper()}</b>!",
            keyboard=[
                [{"text": "🤖 К выбору модели", "callback_data": "nav:model"}],
                [{"text": "⚙️ Настроить список моделей", "callback_data": f"mcfg:view:{prov}:0"}],
            ],
        )
    if cmd in ("/model_del", "/models_del"):
        if not args:
            return BridgeReply("Использование: <code>/model_del &lt;model_id&gt;</code> или <code>/model_del &lt;провайдер&gt; &lt;model_id&gt;</code>")
        parts = args.split()
        prov = ""
        if parts[0].lower() in ("agy", "codex", "openai"):
            prov = parts[0].lower()
            mid = parts[1] if len(parts) > 1 else ""
        else:
            mid = parts[0]

        from src.models_config import remove_custom_model
        removed = False
        target_prov = prov or "openai"
        if prov:
            removed = remove_custom_model(prov, mid)
        else:
            for p in ("agy", "codex", "openai"):
                if remove_custom_model(p, mid):
                    removed = True
                    target_prov = p
                    break

        if removed:
            return BridgeReply(
                f"🗑 Пользовательская модель <code>{html.escape(mid)}</code> удалена.",
                keyboard=[[{"text": "⚙️ Настроить список моделей", "callback_data": f"mcfg:view:{target_prov}:0"}]],
            )
        return BridgeReply(f"⚠️ Модель <code>{html.escape(mid)}</code> не найдена в списке добавленных.")
    if cmd == "/mode":
        if args:
            if args in {"code", "plan"}:
                cs.mode = args
                return BridgeReply(f"🛡 Режим изменен на {args}")
            return BridgeReply(f"⚠️ Неподдерживаемый режим: {args}")
        return _render_mode_picker(cs, cfg)
    if cmd in ("/restart", "/reload"):
        marker = Path.home() / ".antigravity" / "bridge" / ".restart_pending"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return BridgeReply("🔄 Перезапускаю мост... Служба плавно перезагрузится через 1-2 секунды.")
    if cmd in ("/reset", "/new", "/clear"):
        cs.has_session = False
        cs.conversation_id = ""
        cs.codex_thread_id = ""
        return BridgeReply("🧹 Сессия сброшена для обоих движков (AGY и Codex). Следующее сообщение начнет чистую сессию.")
    if cmd == "/stop":
        import src.turn
        if src.turn.request_stop(
            msg.chat_id, getattr(msg, "message_thread_id", None), user_id=msg.user_id
        ):
            return BridgeReply("🛑 Остановка агента...")
        return BridgeReply("⚠️ Нет активной задачи для остановки.")
    if cmd == "/thinking":
        mode = args.strip().lower()
        if mode in ("live", "on", "1"):
            cs.thinking = "live"
            return BridgeReply("💭 <b>Отображение размышлений: ВЖИВУЮ</b>\n\nМодель выводит свои мысли и чекпоинты в реальном времени во время работы, не захламляя итоговый ответ.")
        if mode in ("all", "full"):
            cs.thinking = "all"
            return BridgeReply("💭 <b>Отображение размышлений: ПОЛНОЕ</b>\n\nРазмышления транслируются в реальном времени, а в конце сворачиваются в цитату перед итоговым ответом.")
        if mode in ("off", "0", "hide"):
            cs.thinking = "off"
            return BridgeReply("💭 <b>Отображение размышлений: ВЫКЛ</b>\n\nВ статусе отображаются только выполняемые команды и таймер.")
        cur_mode = getattr(cs, "thinking", "live")
        return BridgeReply(
            f"💭 <b>Режим отображения размышлений (Thinking):</b> <code>{cur_mode}</code>\n\n"
            "• <code>/thinking live</code> — выводить размышления в реальном времени в статусе (по умолчанию)\n"
            "• <code>/thinking all</code> — выводить в статусе и сворачивать в цитату в конце\n"
            "• <code>/thinking off</code> — скрыть размышления"
        )
    if cmd == "/compact":
        if not cs.has_session or not cs.conversation_id:
            return BridgeReply("ℹ️ В текущем чате нет активной сессии для сжатия.")
        return BridgeReply(
            f"🗜️ <b>Сжатие контекста сессии</b> (ходов: {cs.turn_count})\n\n"
            "Сжатие архивирует текущий диалог и начинает новую чистую сессию. "
            "Это снижает расход токенов до 90% и предотвращает лимиты квоты.\n\n"
            "Нажмите кнопку ниже для подтверждения:",
            keyboard=[
                [
                    {"text": "✅ Сжать сессию", "callback_data": "do_compact"},
                    {"text": "❌ Отмена", "callback_data": "nav:settings"},
                ]
            ],
        )
    if cmd == "/pwd":
        home = os.path.expanduser("~")
        pretty_dir = cs.chat_dir.replace(home, "~", 1)
        return BridgeReply(f"📂 <b>Текущая рабочая папка чата:</b>\n<code>{pretty_dir}</code>")
    if cmd == "/voice":
        if args:
            v_clean = args.strip().lower()
            valid_voices = {vid for vid, _ in VOICE_CHOICES}
            if v_clean in valid_voices:
                cs.voice = v_clean
                return BridgeReply(f"🎙 Голос озвучки установлен на <code>{v_clean}</code>")
            return BridgeReply(f"⚠️ Неизвестный голос: {args}\nДоступные: filipp, ermil, zahar, alena, dasha, jane")
        return _render_voice_picker(cs, cfg)
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
        ws_files = list_workspace_files(cs.chat_dir, limit=10)
        inbox_files = list_inbox(cs.chat_dir, limit=5)
        if not ws_files and not inbox_files:
            return BridgeReply("📂 Рабочая папка пуста (нет файлов).")

        lines = ["📂 <b>Файлы в рабочей папке:</b>"]
        kb_rows: InlineKeyboard = []
        if ws_files:
            lines.append("\n<b>Файлы проекта:</b>")
            for fname, sz in ws_files:
                sz_str = f"{sz}B" if sz < 1024 else f"{sz // 1024}KB"
                lines.append(f"• <code>{html.escape(fname)}</code> ({sz_str})")
                if len(kb_rows) < 6:
                    short_name = Path(fname).name
                    if len(short_name) > 20:
                        short_name = short_name[:17] + "..."
                    kb_rows.append([{"text": f"📥 Скачать {short_name}", "callback_data": f"getfile:{fname}"}])

        if inbox_files:
            lines.append("\n<b>Входящие (от пользователя):</b>")
            for f in inbox_files:
                lines.append(f"• {html.escape(f)}")

        lines.append("\n💡 <i>Для скачивания используйте кнопки выше или команду /get имя_файла</i>")
        return BridgeReply("\n".join(lines), keyboard=kb_rows if kb_rows else None)
    if cmd in ("/get", "/file", "/download"):
        if not args:
            return BridgeReply("ℹ️ Укажите имя файла: <code>/get имя_файла</code>\nСписок файлов: /files")
        resolved = safe_resolve_workspace_file(cs.chat_dir, args.strip())
        if not resolved or not resolved.exists():
            return BridgeReply(f"⚠️ Файл <code>{html.escape(args.strip())}</code> не найден в рабочей папке.")
        try:
            data = resolved.read_bytes()
            size_kb = len(data) / 1024
            return BridgeReply(
                text=f"📄 Файл: <code>{html.escape(resolved.name)}</code> ({size_kb:.1f} KB)",
                document_bytes=data,
                document_filename=resolved.name,
            )
        except Exception as exc:
            return BridgeReply(f"⚠️ Ошибка чтения файла: {exc}")
    if cmd == "/diff":
        diff_output, err = await _run_git_diff(cs.chat_dir, args)
        if err:
            return BridgeReply(f"ℹ️ {err}")
        if not diff_output.strip():
            return BridgeReply("✅ Рабочая копия чиста (нет изменений в git).")

        if len(diff_output) <= 3500:
            return BridgeReply(
                f"📝 <b>Изменения git diff:</b>\n<pre><code class=\"language-diff\">{html.escape(diff_output)}</code></pre>"
            )
        else:
            patch_bytes = diff_output.encode("utf-8")
            return BridgeReply(
                text="📝 <b>Git Diff</b> превышает лимит сообщения Telegram (отправлен файлом changes.patch):",
                document_bytes=patch_bytes,
                document_filename="changes.patch",
            )
    if cmd == "/queue":
        import src.daemon
        sub = args.strip().lower()
        if sub in ("clear", "clean"):
            cleared = src.daemon._QUEUE.clear()
            return BridgeReply(f"🧹 Очередь очищена (удалено сообщений: {cleared}).")
        return BridgeReply("📋 Статус очереди:\n" + "\n".join(src.daemon._QUEUE.status()))
    if cmd == "/update":
        from src.updater import check_and_pull_update, trigger_service_restart
        updated, old_sha, new_sha = await asyncio.to_thread(check_and_pull_update)
        if updated:
            asyncio.get_running_loop().call_later(1.5, trigger_service_restart)
            return BridgeReply(
                f"✅ Обновление скачано (`{old_sha[:7]}` → `{new_sha[:7]}`). Перезапускаю сервис..."
            )
        if new_sha:
            return BridgeReply(f"👌 Установлена актуальная версия (`{new_sha[:7]}`).")
        return BridgeReply("⚠️ Не удалось проверить обновления в GitHub.")
    return None


async def handle_callback(
    cq: "CallbackQuery",
    cs: "ChatState",
    cfg: "Config",
    tg: Any = None,
) -> BridgeReply:
    """Handle inline-keyboard button taps. Always returns a reply to render."""
    data = cq.data

    if data == "noop":
        return BridgeReply(None, toast="")
    if data == "nav:status":
        return BridgeReply(render_status(cs, cfg))
    if data == "nav:settings":
        return _render_settings(cs, cfg)
    if data == "nav:auth":
        return await _render_auth_card()
    if data == "auth:codex":
        if tg:
            asyncio.create_task(_run_device_auth_flow(tg, cq.chat_id, cq.message_id))
        return BridgeReply(
            "⏳ <b>Запуск авторизации OpenAI Codex...</b>\n\nОжидайте ссылку и одноразовый код.",
            keyboard=[[{"text": "❌ Отмена", "callback_data": "auth:cancel"}]],
            toast="Запуск авторизации...",
        )
    if data == "auth:cancel":
        cancel_ev = _ACTIVE_AUTH_CANCELS.get(cq.chat_id)
        if cancel_ev:
            cancel_ev.set()
        from src.codex_accounts import restore_backup_after_aborted_login
        restore_backup_after_aborted_login()
        rep = await _render_auth_card()
        return BridgeReply(
            text=rep.text,
            keyboard=rep.keyboard,
            toast="Авторизация отменена",
        )
    if data.startswith("acc:switch:"):
        acc_id = data.partition("acc:switch:")[2]
        from src.codex_accounts import switch_account, is_token_stale, refresh_active_codex_tokens
        ok, msg, acc = switch_account(acc_id)
        if ok and acc and acc.auth_mode == "chatgpt":
            if is_token_stale(acc.auth_data or {}):
                ref_ok, ref_msg, _ = await refresh_active_codex_tokens()
                if ref_ok:
                    msg = f"{msg} (токены обновлены)"
        rep = await _render_auth_card()
        return BridgeReply(
            text=rep.text,
            keyboard=rep.keyboard,
            toast=msg if ok else f"Ошибка: {msg}",
        )
    if data == "acc:refresh_all":
        from src.codex_accounts import refresh_all_codex_accounts
        res = await refresh_all_codex_accounts(force=True)
        ok_count = sum(1 for ok, _ in res.values() if ok)
        total = len(res)
        rep = await _render_auth_card()
        toast = f"Обновлено {ok_count} из {total} аккаунтов" if total else "Нет аккаунтов для обновления"
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)
    if data == "acc:del_menu":
        from src.codex_accounts import load_saved_accounts
        accs = load_saved_accounts()
        rep = _render_delete_accounts_menu(accs)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Выберите аккаунт для удаления")
    if data.startswith("acc:delete:"):
        acc_id = data.partition("acc:delete:")[2]
        from src.codex_accounts import remove_account
        ok, msg = remove_account(acc_id)
        rep = await _render_auth_card()
        return BridgeReply(
            text=rep.text,
            keyboard=rep.keyboard,
            toast=msg if ok else f"Ошибка: {msg}",
        )
    if data == "acc:logout":
        from src.codex_accounts import logout_active_account
        ok, msg = logout_active_account()
        rep = await _render_auth_card()
        return BridgeReply(
            text=rep.text,
            keyboard=rep.keyboard,
            toast=msg,
        )
    if data == "acc:add_key":
        return BridgeReply(
            "🔑 <b>Подключение OpenAI Codex по API ключу</b>\n\n"
            "Отправьте команду в чат:\n"
            "<code>/codex_key sk-...</code>\n\n"
            "<i>(Бот сразу сохранит ключ и удалит сообщение из чата для безопасности)</i>",
            keyboard=[[{"text": "← Назад к аккаунтам", "callback_data": "nav:auth"}]],
            toast="Отправьте /codex_key <ключ>",
        )
    if data.startswith("getfile:"):
        relpath = data.partition(":")[2]
        resolved = safe_resolve_workspace_file(cs.chat_dir, relpath)
        if not resolved or not resolved.exists():
            return BridgeReply("⚠️ Файл не найден", toast="Файл не найден")
        try:
            data_bytes = resolved.read_bytes()
            return BridgeReply(
                text=f"📄 Отправляю <code>{html.escape(resolved.name)}</code>",
                toast=f"Скачивание {resolved.name}",
                document_bytes=data_bytes,
                document_filename=resolved.name,
            )
        except Exception as exc:
            return BridgeReply(f"⚠️ Ошибка чтения: {exc}", toast="Ошибка чтения")
    if data == "nav:limits" or data.startswith("limits:"):
        tab = data.partition("limits:")[2] if data.startswith("limits:") else ""
        if not tab:
            cur_eng = getattr(cs, "engine", "") or get_engine_for_model(cs.model or cfg.agy.model)
            tab = "codex" if cur_eng == "codex" else ("openai" if cur_eng in ("openai", "openai_compat") else "agy")

        if tab == "codex":
            cqs = await fetch_codex_limits()
            return BridgeReply(render_codex_limits_card(cqs), keyboard=_limits_keyboard("codex"), toast="Лимиты Codex обновлены")
        elif tab == "openai":
            from src.openai_providers import get_active_provider
            prov = get_active_provider()
            if prov:
                return BridgeReply(render_openai_provider_limits_card(prov), keyboard=_limits_keyboard("openai"), toast="Статус API обновлен")
            return BridgeReply("⚠️ Нет активного API провайдера", keyboard=_limits_keyboard("agy"), toast="Нет провайдера")
        else:
            qs = await fetch_quota_summary()
            return BridgeReply(render_limits_card(qs), keyboard=_limits_keyboard("agy"), toast="Лимиты обновлены")
    tid = getattr(cq, "message_thread_id", None)
    if data == "nav:providers":
        clear_provider_wizard(cq.chat_id, tid)
        return await _render_providers_card()
    if data in ("prov:add", "prov:wiz:start"):
        wiz = {"step": "url"}
        set_provider_wizard(cq.chat_id, tid, wiz)
        return _render_wizard_step_url(wiz)
    if data == "prov:help_cmd":
        return _render_add_provider_help()
    if data.startswith("prov:wiz:preset:"):
        preset_name = data.partition("prov:wiz:preset:")[2]
        from src.openai_providers import PROVIDER_PRESETS
        pre = PROVIDER_PRESETS.get(preset_name, {})
        wiz = {
            "step": "key",
            "url": pre.get("base_url", ""),
            "id": preset_name,
            "name": pre.get("name", preset_name.capitalize()),
            "default_model": pre.get("default_model", ""),
        }
        set_provider_wizard(cq.chat_id, tid, wiz)
        return _render_wizard_step_key(wiz)
    if data == "prov:wiz:skip_key":
        wiz = get_provider_wizard(cq.chat_id, tid) or {}
        wiz["key"] = ""
        wiz["masked_key"] = "(нет ключа)"
        wiz["step"] = "model"
        from src.openai_providers import OpenAIProvider, fetch_provider_models
        temp_prov = OpenAIProvider(
            id=wiz.get("id", "custom"),
            name=wiz.get("name", "Custom"),
            base_url=wiz.get("url", ""),
            api_key="",
        )
        models = await fetch_provider_models(temp_prov, timeout=5.0)
        wiz["models"] = models
        set_provider_wizard(cq.chat_id, tid, wiz)
        return _render_wizard_step_model(wiz)
    if data == "prov:wiz:back:url":
        wiz = get_provider_wizard(cq.chat_id, tid) or {}
        wiz["step"] = "url"
        set_provider_wizard(cq.chat_id, tid, wiz)
        return _render_wizard_step_url(wiz)
    if data == "prov:wiz:back:key":
        wiz = get_provider_wizard(cq.chat_id, tid) or {}
        wiz["step"] = "key"
        set_provider_wizard(cq.chat_id, tid, wiz)
        return _render_wizard_step_key(wiz)
    if data.startswith("prov:wiz:page:"):
        page_num = int(data.partition("prov:wiz:page:")[2])
        wiz = get_provider_wizard(cq.chat_id, tid) or {}
        return _render_wizard_step_model(wiz, page=page_num)
    if data.startswith("prov:wiz:m_idx:"):
        idx = int(data.partition("prov:wiz:m_idx:")[2])
        wiz = get_provider_wizard(cq.chat_id, tid) or {}
        models = wiz.get("models", [])
        m_name = models[idx] if idx < len(models) else "default"
        from src.openai_providers import OpenAIProvider, save_provider
        final_prov = OpenAIProvider(
            id=wiz.get("id", "custom"),
            name=wiz.get("name", "Custom"),
            base_url=wiz.get("url", "https://openrouter.ai/api/v1"),
            api_key=wiz.get("key", ""),
            default_model=m_name,
            models=models,
        )
        saved = save_provider(final_prov, make_active=True)
        clear_provider_wizard(cq.chat_id, tid)
        return _render_wizard_success(saved)
    if data.startswith("prov:wiz:model:"):
        m_name = data.partition("prov:wiz:model:")[2]
        wiz = get_provider_wizard(cq.chat_id, tid) or {}
        from src.openai_providers import OpenAIProvider, save_provider
        final_prov = OpenAIProvider(
            id=wiz.get("id", "custom"),
            name=wiz.get("name", "Custom"),
            base_url=wiz.get("url", "https://openrouter.ai/api/v1"),
            api_key=wiz.get("key", ""),
            default_model=m_name,
            models=wiz.get("models", []),
        )
        saved = save_provider(final_prov, make_active=True)
        clear_provider_wizard(cq.chat_id, tid)
        return _render_wizard_success(saved)
    if data in ("prov:wiz:cancel", "prov:cancel"):
        clear_provider_wizard(cq.chat_id, tid)
        rep = await _render_providers_card()
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Настройка отменена")
    if data == "prov:edit_url":
        from src.openai_providers import get_active_provider
        p = get_active_provider()
        cur_url = p.base_url if p else "https://openrouter.ai/api/v1"
        return BridgeReply(
            "🌐 <b>Изменение Base URL провайдера</b>\n\n"
            f"Текущий адрес: <code>{html.escape(cur_url)}</code>\n\n"
            "Чтобы задать новый URL, отправьте в чат:\n"
            f"<code>/provider url {html.escape(cur_url)}</code>\n\n"
            "<i>(Поддерживается абсолютно любой URL OpenAI-совместимого эндпоинта)</i>",
            keyboard=[[{"text": "← К провайдерам", "callback_data": "nav:providers"}]],
            toast="Отправьте /provider url <URL>",
        )
    if data == "prov:edit_key":
        from src.openai_providers import get_active_provider
        p = get_active_provider()
        p_name = p.name if p else "провайдера"
        return BridgeReply(
            f"🔑 <b>Смена API ключа для {html.escape(p_name)}</b>\n\n"
            "Отправьте команду в чат:\n"
            "<code>/provider key sk-...</code>\n\n"
            "<i>(Сообщение с секретным ключом сразу удалится ботом из чата для безопасности)</i>",
            keyboard=[[{"text": "← К провайдерам", "callback_data": "nav:providers"}]],
            toast="Отправьте /provider key <KEY>",
        )
    if data.startswith("prov:preset:"):
        preset_name = data.partition("prov:preset:")[2]
        from src.openai_providers import PROVIDER_PRESETS
        pre = PROVIDER_PRESETS.get(preset_name, {})
        p_name = pre.get("name", preset_name.capitalize())
        b_url = pre.get("base_url", "")
        d_model = pre.get("default_model", "")
        return BridgeReply(
            f"⚡️ <b>Быстрое подключение {html.escape(p_name)}</b>\n\n"
            f"Отправьте команду в чат:\n"
            f"<code>/provider add {preset_name} sk-...</code>\n\n"
            f"<i>(Base URL <code>{b_url}</code> и модель <code>{d_model}</code> подставятся автоматически. "
            f"Сообщение с секретным ключом сразу удалится ботом из чата)</i>",
            keyboard=[
                [{"text": "➕ Другой провайдер", "callback_data": "prov:add"}],
                [{"text": "← К провайдерам", "callback_data": "nav:providers"}],
            ],
            toast=f"Пресет {p_name}",
        )
    if data.startswith("prov:switch:"):
        target_id = data.partition("prov:switch:")[2]
        from src.openai_providers import set_active_provider
        set_active_provider(target_id)
        rep = await _render_providers_card()
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Провайдер переключен")
    if data == "prov:del_menu":
        from src.openai_providers import load_providers
        return _render_delete_providers_menu(load_providers())
    if data.startswith("prov:delete:"):
        target_id = data.partition("prov:delete:")[2]
        from src.openai_providers import delete_provider
        delete_provider(target_id)
        rep = await _render_providers_card()
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Провайдер удален")
    if data == "prov:test":
        from src.openai_providers import get_active_provider, fetch_provider_models
        p = get_active_provider()
        if not p:
            return BridgeReply("⚠️ Нет активного провайдера", toast="Нет провайдера")
        models = await fetch_provider_models(p)
        toast = f"✅ Успешно! Моделей: {len(models)}" if models else "⚠️ Ошибка подключения к API"
        rep = await _render_providers_card()
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)
    if data == "prov:models":
        from src.openai_providers import get_active_provider, fetch_provider_models
        p = get_active_provider()
        if not p:
            return BridgeReply("⚠️ Нет активного провайдера", toast="Нет провайдера")
        models = await fetch_provider_models(p)
        if not models and p.models:
            models = p.models
        if not models:
            return BridgeReply(
                f"⚠️ Не удалось обнаружить модели через <code>{html.escape(p.base_url)}/models</code>.\n"
                f"Текущая модель: <code>{html.escape(p.default_model or 'default')}</code>",
                keyboard=[[{"text": "← К провайдерам", "callback_data": "nav:providers"}]],
                toast="Модели не найдены",
            )
        lines = [f"🤖 <b>Выберите модель {html.escape(p.name)}:</b>"]
        rows: InlineKeyboard = []
        for m in models[:12]:
            marker = "● " if m == cs.model else "○ "
            rows.append([{"text": f"{marker}{m}", "callback_data": f"m:{m}"}])
        rows.append([{"text": "← К провайдерам", "callback_data": "nav:providers"}])
        return BridgeReply(text="\n".join(lines), keyboard=rows)
    if data == "nav:model":
        return _render_model_picker(cs, cfg)
    if data in ("nav:models_config", "nav:models_cfg"):
        return _render_models_config_card("agy", 0)
    if data.startswith("mcfg:"):
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else "view"
        prov = parts[2] if len(parts) > 2 else "agy"
        tid = getattr(cq, "message_thread_id", None)

        if action == "view":
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            clear_waiting_model_add(cq.chat_id, tid)
            return _render_models_config_card(prov, page)

        if action == "tab":
            clear_waiting_model_add(cq.chat_id, tid)
            return _render_models_config_card(prov, 0)

        if action == "page":
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            return _render_models_config_card(prov, page)

        if action == "tgl":
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            idx = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            from src.models_config import get_all_provider_models, toggle_model_visibility
            all_m = get_all_provider_models(prov)
            page_size = 6
            start_idx = page * page_size
            page_items = all_m[start_idx : start_idx + page_size]
            toast = ""
            if 0 <= idx < len(page_items):
                target_mid = page_items[idx][0]
                is_on = toggle_model_visibility(target_mid)
                toast = f"✅ {target_mid} включена" if is_on else f"❌ {target_mid} скрыта"
            rep = _render_models_config_card(prov, page)
            return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

        if action == "del":
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            idx = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            from src.models_config import get_all_provider_models, remove_custom_model
            all_m = get_all_provider_models(prov)
            page_size = 6
            start_idx = page * page_size
            page_items = all_m[start_idx : start_idx + page_size]
            toast = ""
            if 0 <= idx < len(page_items):
                target_mid = page_items[idx][0]
                rem = remove_custom_model(prov, target_mid)
                toast = f"🗑 Модель {target_mid} удалена" if rem else "Не удалось удалить"
            rep = _render_models_config_card(prov, page)
            return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

        if action == "rst":
            from src.models_config import reset_models_config
            reset_models_config(prov)
            rep = _render_models_config_card(prov, 0)
            return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=f"Раздел {prov.upper()} сброшен")

        if action == "add":
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            set_waiting_model_add(cq.chat_id, tid, {"provider": prov, "page": page})
            short_prov = {"agy": "Google Antigravity", "codex": "OpenAI Codex", "openai": "OpenAI API"}.get(prov, prov.upper())
            lines = [
                f"➕ <b>Добавление модели ({short_prov})</b>\n",
                "Отправьте идентификатор модели сообщением в чат.",
                "• Пример ID: <code>gpt-4.5-preview</code> или <code>deepseek-reasoner</code>",
                "• Можно также указать название: <code>gpt-4.5-preview GPT-4.5 Preview</code>\n",
                "👇 <i>Отправьте сообщение или нажмите кнопку отмены:</i>",
            ]
            keyboard = [[{"text": "❌ Отмена", "callback_data": f"mcfg:view:{prov}:{page}"}]]
            return BridgeReply(text="\n".join(lines), keyboard=keyboard, toast="Жду имя модели в чат")
    if data == "nav:mode":
        return _render_mode_picker(cs, cfg)
    if data == "nav:voice":
        return _render_voice_picker(cs, cfg)
    if data == "nav:effort":
        return _render_effort_picker(cs, cfg)
    if data == "nav:projects" or data.startswith("nav:projects:"):
        parts = data.split(":")
        offset = int(parts[2]) if len(parts) > 2 else 0
        return _render_projects_picker(cs, offset)
    if data.startswith("stop_turn"):
        import src.turn
        nonce = data.partition(":")[2]
        if src.turn.request_stop(
            cq.chat_id,
            cq.message_thread_id,
            user_id=cq.user_id,
            nonce=nonce,
            status_message_id=cq.message_id,
        ):
            return BridgeReply("Останавливаю...", toast="Запрос на остановку отправлен")
        return BridgeReply("Нет активной задачи", toast="Ничего не выполняется")
    if data == "R":
        cs.has_session = False
        cs.conversation_id = ""
        cs.codex_thread_id = ""
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Сессия сброшена")
        
    if data.startswith("P:"):
        choice = data[2:]
        cs.conversation_id = choice
        cs.has_session = True
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast="Диалог выбран")

    if data.startswith("m:"):
        choice = data[2:]
        if choice == _DEFAULT_TOKEN:
            cs.model = ""
            cs.engine = ""
            toast = "Настройки по умолчанию"
            rep = _render_settings(cs, cfg)
            return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)
        elif is_valid_model(choice):
            cs.model = choice
            cs.engine = get_engine_for_model(choice)
            if cs.engine in ("agy", "codex"):
                return _render_model_effort_picker(choice, cs, cfg)
            toast = f"Модель: {choice} ({cs.engine.upper()})"
        else:
            toast = "Неизвестный выбор"
        rep = _render_settings(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    if data.startswith("e:"):
        choice = data[2:]
        if choice == _DEFAULT_TOKEN:
            cs.effort = ""
            toast = f"Модель {cs.model}: Effort по умолчанию" if cs.model else "Effort по умолчанию"
        elif choice in EFFORT_CHOICES:
            cs.effort = choice
            toast = f"✅ Модель {cs.model} · Effort: {choice}" if cs.model else f"Effort: {choice}"
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

    if data.startswith("v:"):
        choice = data[2:]
        valid_voices = {vid for vid, _ in VOICE_CHOICES}
        if choice in valid_voices:
            cs.voice = choice
            toast = f"Голос: {choice}"
        else:
            toast = "Неизвестный голос"
        rep = _render_voice_picker(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    if data.startswith("vs:"):
        choice = data[3:]
        valid_speeds = {spd for spd, _ in SPEED_CHOICES}
        if choice in valid_speeds:
            cs.voice_speed = choice
            toast = f"Скорость: {choice}x"
        else:
            toast = "Неизвестная скорость"
        rep = _render_voice_picker(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    if data == "v_auto:toggle":
        cs.auto_voice = not cs.auto_voice
        toast = "Автоозвучка ВКЛ" if cs.auto_voice else "Автоозвучка ВЫКЛ"
        rep = _render_voice_picker(cs, cfg)
        return BridgeReply(text=rep.text, keyboard=rep.keyboard, toast=toast)

    if data == "do_compact":
        cs.has_session = False
        cs.conversation_id = ""
        cs.codex_thread_id = ""
        cs.turn_count = 0
        kb: InlineKeyboard = [
            [{"text": "📁 Диалоги", "callback_data": "nav:projects"}],
            [{"text": "⚙️ Настройки", "callback_data": "nav:settings"}],
        ]
        return BridgeReply(
            "🗜️ <b>Контекст сессии сжат!</b>\n\n"
            "История сохранена в памяти проекта, сырые логи очищены. "
            "Следующее сообщение начнет чистый контекст с минимальным потреблением токенов квоты.",
            keyboard=kb,
            toast="Контекст сжат",
        )

    return _render_settings(cs, cfg)
