"""Bridge command handlers — text slash commands and inline-keyboard callbacks."""
from __future__ import annotations

import asyncio
import html
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.media import clean_inbox, list_inbox, list_workspace_files, safe_resolve_workspace_file
from src.models import FALLBACK_MODELS, get_available_models
from src.quota import fetch_quota_summary, render_limits_card
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
    "/model    — выбрать модель (переопределяет настройки для чата)\n"
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

    lines = [
        "🟢 <b>Статус моста Antigravity</b>",
        f"🖥 Хост: {' · '.join(sys_parts)}",
        f"🤖 Модель: <code>{html.escape(model)}</code> [{model_src}]",
        f"🛡 Режим: <code>{html.escape(mode)}</code> [{mode_src}]",
        f"⚙️ Effort: <code>{html.escape(cs.effort or 'default')}</code> · Таймаут: <code>{html.escape(cs.print_timeout or '15m')}</code>",
        f"🎙 Голос: <code>{html.escape(voice)}</code> ({speed}x) · Авто: <b>{av}</b>",
        "",
        "<b>Текущий чат:</b>",
        f"• Сессия: {session}",
        f"• Папка: <code>{html.escape(workdir)}</code>",
    ]
    return "\n".join(lines)


def _limits_keyboard() -> InlineKeyboard:
    return [
        [
            {"text": "🔄 Обновить квоту", "callback_data": "nav:limits"},
            {"text": "⚙️ Настройки", "callback_data": "nav:settings"},
        ]
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
            {"text": "🎙 Голос и озвучка", "callback_data": "nav:voice"},
            {"text": "📊 Лимиты и квоты", "callback_data": "nav:limits"},
        ],
        [
            {"text": "🧹 Сбросить сессию", "callback_data": "R"},
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
    models = get_available_models()
    rows: InlineKeyboard = []
    for model_id, display_name in models:
        marker = "● " if model_id == current_per_chat else "○ "
        label = f"{marker}{display_name}" if display_name and display_name != model_id else f"{marker}{model_id}"
        rows.append([{"text": label, "callback_data": f"m:{model_id}"}])
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
    if cmd in ("/limits", "/quota"):
        qs = await fetch_quota_summary()
        return BridgeReply(render_limits_card(qs), keyboard=_limits_keyboard())
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
    if cmd in ("/model", "/models"):
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
    if cmd in ("/reset", "/new", "/clear"):
        cs.has_session = False
        cs.conversation_id = ""
        return BridgeReply("🧹 Сессия сброшена. Следующее сообщение начнет чистую сессию.")
    if cmd == "/stop":
        import src.turn
        if src.turn.request_stop(
            msg.chat_id, getattr(msg, "message_thread_id", None), user_id=msg.user_id
        ):
            return BridgeReply("🛑 Остановка агента...")
        return BridgeReply("⚠️ Нет активной задачи для остановки.")
    if cmd == "/thinking":
        mode = args.strip().lower()
        if mode in ("on", "true", "1"):
            return BridgeReply("💭 Отображение процесса мышления недоступно в этом режиме.")
        if mode in ("off", "false", "0"):
            return BridgeReply("💭 Процесс мышления уже скрыт.")
        return BridgeReply("💭 Процесс мышления не поддерживается.")
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
        if sub == "clear":
            cleared = len(src.daemon._QUEUE.pending)
            for _, _, _, _, fut in src.daemon._QUEUE.pending:
                if not fut.done():
                    fut.set_result(None)
            src.daemon._QUEUE.pending.clear()
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
) -> BridgeReply:
    """Handle inline-keyboard button taps. Always returns a reply to render."""
    data = cq.data

    if data == "nav:status":
        return BridgeReply(render_status(cs, cfg))
    if data == "nav:settings":
        return _render_settings(cs, cfg)
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
    if data == "nav:limits":
        qs = await fetch_quota_summary()
        return BridgeReply(render_limits_card(qs), keyboard=_limits_keyboard(), toast="Лимиты обновлены")
    if data == "nav:model":
        return _render_model_picker(cs, cfg)
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
            toast = "Настройки по умолчанию"
        elif is_valid_model(choice):
            cs.model = choice
            toast = f"Модель: {choice}"
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
        cs.turn_count = 0
        kb: InlineKeyboard = [
            [{"text": "📁 Диалоги", "callback_data": "nav:projects"}],
            [{"text": "⚙️ Настройки", "callback_data": "nav:settings"}],
        ]
        return BridgeReply(
            "🗜️ <b>Контекст сессии сжат!</b>\n\n"
            "Предыдущая история сохранена в проекте, а текущий диалог сброшен. "
            "Следующее сообщение начнет чистый контекст с минимальным потреблением токенов квоты.",
            keyboard=kb,
            toast="Контекст сжат",
        )

    return _render_settings(cs, cfg)
