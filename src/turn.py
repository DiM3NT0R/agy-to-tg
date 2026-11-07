"""Turn execution — agy print-mode invocation."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.agy_runner import clean_system_messages, run_agy
from src.questions import build_options_keyboard, extract_question_options

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import InboundMessage

LOG = logging.getLogger("antigravity_telegram_bridge")
AGY_TIMEOUT_S = 1800.0  # 30 minutes max turn duration
AGY_INACTIVITY_TIMEOUT_S = 600.0  # 10 minutes without model response (outside active tools)


def parse_timeout_seconds(val: str, default: float = 1800.0) -> float:
    """Parse duration string like '30m', '1h', '600s' into float seconds."""
    if not val:
        return default
    val = val.strip().lower()
    try:
        if val.endswith("h"):
            return float(val[:-1]) * 3600.0
        elif val.endswith("m"):
            return float(val[:-1]) * 60.0
        elif val.endswith("s"):
            return float(val[:-1])
        return float(val)
    except (ValueError, TypeError):
        return default


SessionKey = tuple[int, int]


def session_key(chat_id: int, message_thread_id: int | None) -> SessionKey:
    return (chat_id, message_thread_id or 0)


@dataclass(frozen=True)
class ActiveRun:
    event: asyncio.Event
    nonce: str
    user_id: int
    status_message_id: int | None


ACTIVE_STOPS: dict[SessionKey, ActiveRun] = {}


def request_stop(
    chat_id: int,
    message_thread_id: int | None,
    *,
    user_id: int,
    nonce: str = "",
    status_message_id: int | None = None,
) -> bool:
    run = ACTIVE_STOPS.get(session_key(chat_id, message_thread_id))
    if run is None or run.user_id != user_id:
        return False
    if nonce and run.nonce != nonce:
        return False
    if status_message_id is not None and run.status_message_id != status_message_id:
        return False
    run.event.set()
    return True


class StatusUpdater:
    def __init__(
        self, tg: "_TelegramLike", chat_id: int, message_id: int, run_nonce: str = ""
    ):
        self.tg = tg
        self.chat_id = chat_id
        self.message_id = message_id
        self.run_nonce = run_nonce
        self.last_update = 0.0
        self.current_text = "Инициализация..."
        self.current_thought = ""
        self.current_tool = ""
        self.pending_task = None
        self.closed = False
        self.start_time = time.time()
        self.spinner = ["🕛", "🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚"]
        self.tick = 0
        self.ticker_task = asyncio.create_task(self._ticker())

    async def _ticker(self):
        while not self.closed:
            await asyncio.sleep(4.0)
            if self.closed:
                break
            await self._edit()

    def _build_display_text(self) -> str:
        self.tick += 1
        spin = self.spinner[self.tick % len(self.spinner)]
        elapsed = int(time.time() - self.start_time)
        m = elapsed // 60
        s = elapsed % 60

        header = f"{spin} [{m:02d}:{s:02d}]"

        lines = []
        if self.current_thought:
            lines.append(f"{header} <b>Выполняю задачу...</b>\n")
            thought_text = self.current_thought.strip()
            if len(thought_text) > 350:
                thought_text = thought_text[:347] + "..."
            lines.append(f"💭 <i>{html.escape(thought_text)}</i>")
            if self.current_tool:
                lines.append(f"\n{self.current_tool}")
        else:
            text = self.current_tool or self.current_text
            lines.append(f"{header} {text}")

        return "\n".join(lines)

    async def update(self, new_text: str):
        if self.closed:
            return
        self.current_text = new_text
        self.current_tool = new_text
        await self._schedule_edit()

    async def update_thought(self, thought_text: str):
        if self.closed or not thought_text.strip():
            return
        self.current_thought = thought_text.strip()
        self.current_tool = ""
        await self._schedule_edit()

    async def update_tool(self, tool_text: str):
        if self.closed:
            return
        self.current_tool = tool_text
        await self._schedule_edit()

    async def _schedule_edit(self):
        now = time.time()
        if now - self.last_update > 2.0:
            self.last_update = now
            await self._edit()
        else:
            if not (self.pending_task and not self.pending_task.done()):
                delay = 2.0 - (now - self.last_update)
                self.pending_task = asyncio.create_task(self._delayed_edit(delay))

    async def _delayed_edit(self, delay: float):
        await asyncio.sleep(delay)
        if not self.closed:
            self.last_update = time.time()
            await self._edit()

    async def _edit(self, text: str = ""):
        try:
            display_text = self._build_display_text()
            kb: list[list[dict[str, str]]] = []
            if self.current_thought:
                opts = extract_question_options(self.current_thought)
                if opts:
                    kb.extend(build_options_keyboard(opts, prefix="opt_live"))
            kb.append([{"text": "🛑 Остановить", "callback_data": f"stop_turn:{self.run_nonce}"}])
            await self.tg.edit_message_text(
                self.chat_id, 
                self.message_id, 
                display_text,
                keyboard=kb,
            )
        except Exception:
            pass

    async def close(self):
        self.closed = True
        if self.pending_task:
            self.pending_task.cancel()
        if hasattr(self, 'ticker_task') and self.ticker_task:
            self.ticker_task.cancel()


def _conversation_exists_on_disk(cid: str) -> bool:
    """Return True if agy has persisted state for conversation_id on disk."""
    if not cid:
        return False
    base = os.path.expanduser("~/.gemini/antigravity-cli")
    conv_db = os.path.join(base, "conversations", f"{cid}.db")
    brain_dir = os.path.join(base, "brain", cid)
    return os.path.exists(conv_db) or os.path.exists(brain_dir)


async def _execute_agy_internal(
    tg: "_TelegramLike", chat_id: int, prompt: str, msg: "InboundMessage",
    cs: "ChatState", cfg: "Config", agy_path: str,
) -> tuple[str, int]:
    """Run one agy turn with inline status."""
    prev_has_session = cs.has_session
    prev_conversation_id = cs.conversation_id
    observed_cid = ""

    status_msg_id = None
    run_nonce = secrets.token_urlsafe(6)
    try:
        sent = await tg.send_message(
            chat_id, 
            "🔄 Думаю...", 
            keyboard=[[{"text": "🛑 Остановить", "callback_data": f"stop_turn:{run_nonce}"}]],
            message_thread_id=msg.message_thread_id,
            reply_to_message_id=msg.message_id if chat_id < 0 else None
        )
        if sent:
            status_msg_id = sent
    except Exception:
        pass

    updater = None
    if status_msg_id:
        updater = StatusUpdater(tg, chat_id, status_msg_id, run_nonce)

    async def handle_event(data: dict):
        nonlocal observed_cid
        if data.get("event") == "init":
            cid = data.get("conversation_id")
            if cid:
                observed_cid = cid
                cs.conversation_id = cid
                # Add to projects if not exists
                if not any(p.get("id") == cid for p in cs.projects):
                    snippet = prompt[:40] + "..." if len(prompt) > 40 else prompt
                    cs.projects.insert(0, {"id": cid, "snippet": snippet})
                    # Keep max 10 projects
                    cs.projects = cs.projects[:10]
            if updater:
                await updater.update("💬 Подключение к модели...")
        
        if not updater:
            return
        if data.get("event") == "step_update":
            su = data.get("step_update", {})
            stype = su.get("step_type")
            state = su.get("state")
            if stype == "tool" and state == "ACTIVE":
                tool = str(su.get("tool_name") or "unknown")
                info = su.get("tool_info", {}).get("parameters", {})
                extra = ""
                summary = str(info.get("toolSummary") or "")

                if tool == "run_command":
                    cmd = str(info.get("CommandLine") or "").strip().split("\n")[0]
                    extra = f": {summary or cmd}"
                    tool_desc = "💻 Команда"
                elif tool in ("read_file", "edit_file", "write_file") or "Path" in info:
                    path = str(info.get("Path") or info.get("TargetFile") or info.get("AbsolutePath") or "")
                    fname = path.split("/")[-1] if path else ""
                    action = "📖 Чтение" if tool == "read_file" else ("✏️ Правка" if tool == "edit_file" else "📝 Создание")
                    extra = f": {fname}" if fname else ""
                    tool_desc = action
                elif tool == "search_web":
                    q = str(info.get("query") or "")
                    extra = f": {q}"
                    tool_desc = "🌐 Поиск"
                elif tool == "invoke_subagent":
                    role = str(info.get("Role") or info.get("TypeName") or "")
                    extra = f": {role}"
                    tool_desc = "🤖 Субагент"
                elif tool == "manage_task":
                    action = str(info.get("Action") or "")
                    extra = f": {action}"
                    tool_desc = "⚙️ Задача"
                else:
                    tool_desc = f"🛠 {tool}"
                    if "Path" in info:
                        extra = f": {str(info['Path']).split('/')[-1]}"
                    elif "query" in info:
                        extra = f": {info['query']}"

                if len(extra) > 55:
                    extra = extra[:52] + "..."

                await updater.update(f"{tool_desc}{extra}")
            elif stype == "agent_response" and state == "ACTIVE":
                await updater.update("💬 Формирую ответ...")

    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat(tg, chat_id, msg.message_thread_id, hb_stop))
    stop_event = asyncio.Event()
    key = session_key(chat_id, msg.message_thread_id)
    active_run = ActiveRun(stop_event, run_nonce, getattr(msg, "user_id", 0), status_msg_id)
    ACTIVE_STOPS[key] = active_run
    turn_start = time.perf_counter()
    try:
        result = await _run_agy_with_retry(
            prompt=prompt,
            cs=cs,
            cfg=cfg,
            agy_path=agy_path,
            stop_event=stop_event,
            on_event=handle_event,
            updater=updater,
        )
        if result.exit_code != 0:
            LOG.error("agy exited with %d. stderr: %s", result.exit_code, result.stderr or "(empty)")
    finally:
        if ACTIVE_STOPS.get(key) is active_run:
            ACTIVE_STOPS.pop(key, None)
        hb_stop.set()
        hb_task.cancel()
        if updater:
            await updater.close()
            try:
                await tg.delete_message(chat_id, status_msg_id)
            except Exception:
                pass
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass
    
    elapsed = int((time.perf_counter() - turn_start) * 1000)
    LOG.info("turn chat=%d cwd=%s exit=%d ms=%d reply_len=%d",
             chat_id, cs.chat_dir, result.exit_code, elapsed, len(result.text or ""))

    # Reconcile session state when a turn was stopped (130) or failed (non-zero):
    # - If agy emitted a new conversation_id, keep it ONLY if agy actually persisted it on disk.
    #   If it was killed before persisting, restore the previous session so the next turn
    #   doesn't pass a non-existent --conversation UUID or lose history.
    # - If a valid session exists on disk (either new or previous), ensure cs.has_session = True
    #   even when stopped, so the next message continues the dialog instead of starting fresh.
    if result.exit_code != 0:
        if observed_cid and _conversation_exists_on_disk(observed_cid):
            cs.conversation_id = observed_cid
            cs.has_session = True
        elif prev_has_session:
            cs.conversation_id = prev_conversation_id
            cs.has_session = True
        else:
            # First turn in a fresh session died before creating conversation DB
            if observed_cid and not _conversation_exists_on_disk(observed_cid):
                cs.projects = [p for p in cs.projects if p.get("id") != observed_cid]
            cs.conversation_id = ""
            cs.has_session = False

    if _is_quota_error(result):
        try:
            from src.quota import fetch_quota_summary, render_limits_card
            qs = await fetch_quota_summary(agy_path)
            if qs and qs.groups and not qs.error:
                card = render_limits_card(qs)
                return (
                    f"⚠️ <b>Исчерпана квота запросов к модели</b>\n\n"
                    f"{card}\n\n"
                    f"💡 <i>Переключитесь на модель Gemini Flash кнопкой ниже — она доступна без ограничений.</i>",
                    result.exit_code,
                )
        except Exception:
            pass
        return QUOTA_EXPLANATION_MSG, result.exit_code
    if _is_location_error(result):
        return LOCATION_EXPLANATION_MSG, result.exit_code
    if _is_invalid_model_error(result):
        return INVALID_MODEL_EXPLANATION_MSG, result.exit_code

    return clean_system_messages(result.text or ""), result.exit_code


async def execute_codex(
    tg: "_TelegramLike",
    chat_id: int,
    prompt: str,
    msg: "InboundMessage",
    cs: "ChatState",
    cfg: "Config",
    codex_path: str = "/usr/local/bin/codex",
) -> tuple[str, int]:
    """Run one codex turn with inline status updater and token tracking."""
    from src.codex_runner import run_codex

    status_msg_id = None
    run_nonce = secrets.token_urlsafe(6)
    try:
        sent = await tg.send_message(
            chat_id,
            "🔄 Думаю (Codex)...",
            keyboard=[[{"text": "🛑 Остановить", "callback_data": f"stop_turn:{run_nonce}"}]],
            message_thread_id=msg.message_thread_id,
            reply_to_message_id=msg.message_id if chat_id < 0 else None,
        )
        if sent:
            status_msg_id = sent
    except Exception:
        pass

    updater = None
    if status_msg_id:
        updater = StatusUpdater(tg, chat_id, status_msg_id, run_nonce)

    async def handle_codex_event(data: dict):
        if data.get("event") == "init":
            tid = data.get("conversation_id")
            if tid:
                cs.codex_thread_id = tid
                if not any(p.get("id") == tid for p in cs.projects):
                    snippet = prompt[:40] + "..." if len(prompt) > 40 else prompt
                    cs.projects.insert(0, {"id": tid, "snippet": f"[Codex] {snippet}"})
                    cs.projects = cs.projects[:10]
            if updater:
                await updater.update("💬 Подключение к OpenAI...")

        if not updater:
            return

        if data.get("event") == "step_update":
            su = data.get("step_update", {})
            stype = su.get("step_type")
            state = su.get("state")
            if stype == "thought":
                text = su.get("text", "")
                if text:
                    await updater.update_thought(text)
            elif stype == "tool" and state == "ACTIVE":
                info = su.get("tool_info", {}).get("parameters", {})
                cmd = str(info.get("CommandLine") or "").strip().split("\n")[0]
                if len(cmd) > 55:
                    cmd = cmd[:52] + "..."
                await updater.update_tool(f"💻 <b>Команда:</b> <code>{html.escape(cmd)}</code>")
            elif stype == "agent_response" and state == "ACTIVE":
                await updater.update("💬 Формирую ответ...")

    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat(tg, chat_id, msg.message_thread_id, hb_stop))
    stop_event = asyncio.Event()
    key = session_key(chat_id, msg.message_thread_id)
    active_run = ActiveRun(stop_event, run_nonce, getattr(msg, "user_id", 0), status_msg_id)
    ACTIVE_STOPS[key] = active_run
    turn_start = time.perf_counter()

    try:
        timeout_s = parse_timeout_seconds(cs.print_timeout, default=AGY_TIMEOUT_S)
        result = await run_codex(
            prompt,
            chat_dir=cs.chat_dir,
            thread_id=cs.codex_thread_id,
            model=cs.model,
            effort=cs.effort,
            codex_path=codex_path,
            timeout=timeout_s,
            on_event=handle_codex_event,
            stop_event=stop_event,
        )
        if result.thread_id:
            cs.codex_thread_id = result.thread_id
            cs.has_session = True
    finally:
        if ACTIVE_STOPS.get(key) is active_run:
            ACTIVE_STOPS.pop(key, None)
        hb_stop.set()
        hb_task.cancel()
        if updater:
            await updater.close()
            try:
                await tg.delete_message(chat_id, status_msg_id)
            except Exception:
                pass
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass

    elapsed = int((time.perf_counter() - turn_start) * 1000)
    LOG.info(
        "codex turn chat=%d cwd=%s exit=%d ms=%d in_tok=%d cached_tok=%d out_tok=%d reasoning_tok=%d reply_len=%d",
        chat_id, cs.chat_dir, result.exit_code, elapsed,
        result.input_tokens, result.cached_input_tokens, result.output_tokens,
        result.reasoning_output_tokens, len(result.text or ""),
    )
    if result.exit_code != 0:
        LOG.warning(
            "codex turn failed chat=%d exit=%d stderr=%s",
            chat_id, result.exit_code, (result.stderr or "").strip(),
        )

    clean_text = clean_system_messages(result.text or "")
    if result.exit_code == 0:
        thinking_mode = getattr(cs, "thinking", "live")
        if thinking_mode == "all" and result.thoughts:
            bullets = "\n".join(f"• {th.strip()}" for th in result.thoughts)
            clean_text = f"<blockquote expandable>💭 <b>Ход размышлений:</b>\n{bullets}</blockquote>\n\n{clean_text}"

        if result.input_tokens > 0:
            clean_text += (
                f"\n\n📊 <i>[Codex: in={result.input_tokens} (кэш={result.cached_input_tokens}), "
                f"out={result.output_tokens}, reasoning={result.reasoning_output_tokens}]</i>"
            )
    return clean_text, result.exit_code


async def execute_openai_compat(
    tg: "_TelegramLike",
    chat_id: int,
    prompt: str,
    msg: "InboundMessage",
    cs: "ChatState",
    cfg: "Config",
) -> tuple[str, int]:
    from src.openai_providers import get_active_provider
    from src.openai_compat_runner import run_openai_compat

    provider = get_active_provider()
    if not provider:
        return (
            "⚠️ Не настроен ни один OpenAI-совместимый API провайдер.\n"
            "Добавьте провайдер командой:\n"
            "<code>/provider add openrouter https://openrouter.ai/api/v1 sk-...</code>\n"
            "<i>(или просто: <code>/provider add https://&lt;любой-url&gt; &lt;key&gt;</code>)</i>\n"
            "или через меню <b>/settings ➔ 🌐 API Провайдеры</b>.",
            1,
        )

    run_nonce = f"{chat_id}:{msg.message_id}:{getattr(msg, 'user_id', 0)}:{time.monotonic()}"
    status_msg_id: int | None = None
    updater: StatusUpdater | None = None
    if getattr(cfg.telegram, "live_status", False):
        try:
            status_msg_id = await tg.send_message(
                chat_id,
                f"⏳ <i>Запрос к {html.escape(provider.name)}...</i>",
                message_thread_id=msg.message_thread_id,
            )
            if status_msg_id:
                updater = StatusUpdater(tg, chat_id, status_msg_id)
        except Exception:
            pass

    async def handle_thought(thought_text: str):
        if updater and thought_text:
            first_line = thought_text.strip().split("\n")[0][:60]
            await updater.update_thought(f"💭 {first_line}...")

    async def handle_tool(tool_text: str):
        if updater and tool_text:
            await updater.update_tool(tool_text)

    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat(tg, chat_id, msg.message_thread_id, hb_stop))
    stop_event = asyncio.Event()
    key = session_key(chat_id, msg.message_thread_id)
    active_run = ActiveRun(stop_event, run_nonce, getattr(msg, "user_id", 0), status_msg_id)
    ACTIVE_STOPS[key] = active_run
    turn_start = time.perf_counter()

    try:
        timeout_s = parse_timeout_seconds(cs.print_timeout, default=AGY_TIMEOUT_S)
        result = await run_openai_compat(
            prompt,
            chat_dir=cs.chat_dir,
            provider=provider,
            model=cs.model or provider.default_model,
            timeout=timeout_s,
            on_thought=handle_thought,
            on_tool=handle_tool,
            stop_event=stop_event,
        )
    finally:
        if ACTIVE_STOPS.get(key) is active_run:
            ACTIVE_STOPS.pop(key, None)
        hb_stop.set()
        hb_task.cancel()
        if updater:
            await updater.close()
            try:
                await tg.delete_message(chat_id, status_msg_id)
            except Exception:
                pass
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass

    elapsed = int((time.perf_counter() - turn_start) * 1000)
    LOG.info(
        "openai_compat turn chat=%d cwd=%s exit=%d ms=%d in_tok=%d out_tok=%d reasoning_tok=%d",
        chat_id, cs.chat_dir, result.exit_code, elapsed,
        result.input_tokens, result.output_tokens, result.reasoning_tokens,
    )

    clean_text = clean_system_messages(result.text or "")
    if result.exit_code == 0:
        thinking_mode = getattr(cs, "thinking", "live")
        if thinking_mode == "all" and result.thoughts:
            bullets = "\n".join(f"• {th.strip()}" for th in result.thoughts)
            clean_text = f"<blockquote expandable>💭 <b>Ход размышлений:</b>\n{bullets}</blockquote>\n\n{clean_text}"

        clean_text += (
            f"\n\n🌐 <i>[{provider.name}: in={result.input_tokens}, "
            f"out={result.output_tokens}"
            + (f", reasoning={result.reasoning_tokens}" if result.reasoning_tokens > 0 else "")
            + "]</i>"
        )
    elif not clean_text and result.stderr:
        clean_text = f"⚠️ Ошибка вызова {html.escape(provider.name)}: {html.escape(result.stderr)}"

    return clean_text, result.exit_code


async def execute_turn(
    tg: "_TelegramLike",
    chat_id: int,
    prompt: str,
    msg: "InboundMessage",
    cs: "ChatState",
    cfg: "Config",
    agy_path: str = "agy",
    codex_path: str = "/usr/local/bin/codex",
) -> tuple[str, int]:
    """Execute a turn with the appropriate engine (agy, codex, or openai), applying token handoff."""
    from src.models import get_engine_for_model
    from src.context_compactor import prepare_turn_prompt

    effective_model = cs.model or cfg.agy.model
    target_engine = get_engine_for_model(effective_model)
    if cs.engine and cs.engine in ("agy", "codex", "openai", "openai_compat"):
        target_engine = cs.engine

    # Apply token handoff if switching engines
    prompt_for_engine = prepare_turn_prompt(prompt, cs, target_engine)
    cs.engine = target_engine

    if target_engine in ("openai", "openai_compat"):
        return await execute_openai_compat(tg, chat_id, prompt_for_engine, msg, cs, cfg)
    elif target_engine == "codex":
        return await execute_codex(tg, chat_id, prompt_for_engine, msg, cs, cfg, codex_path)
    else:
        return await _execute_agy_internal(tg, chat_id, prompt_for_engine, msg, cs, cfg, agy_path)


# Backward compatibility alias
execute_agy = execute_turn


# Explanatory message when model/account quota is exceeded
QUOTA_EXPLANATION_MSG = (
    "⚠️ Исчерпана квота запросов к модели (Quota reached / exceeded).\n\n"
    "Достигнут лимит использования текущей модели или аккаунта. "
    "Пожалуйста, переключитесь на другую доступную модель командой /model "
    "или повторите запрос позже после сброса квоты."
)

LOCATION_EXPLANATION_MSG = (
    "⚠️ API отклонил текущий регион выхода в интернет. "
    "Проверьте VPN/proxy egress и повторите запрос."
)

INVALID_MODEL_EXPLANATION_MSG = (
    "⚠️ Выбранная модель устарела или недоступна в текущей версии agy.\n\n"
    "Пожалуйста, выберите актуальную модель командой /model."
)

# Retry policy: 15 attempts total. First 3 attempts are instant (0s),
# then remaining attempts wait a short delay (1.5s) to avoid burning all attempts on a brief network blip.
_INSTANT_RETRIES = 3
_MAX_RETRIES = 15
_RETRY_DELAY_S = 1.5

# Permanent errors that should NOT be retried (e.g. hard weekly/monthly quota or credit exhaustion).
_HARD_QUOTA_PHRASES = (
    "quota reached",
    "quota exceeded",
    "quota_exceeded",
    "exceeded your quota",
    "insufficient_quota",
    "insufficient quota",
    "out of credits",
    "credit balance is too low",
    "free tier limit",
    "billing not enabled",
    "you have exceeded your current quota",
    "quota exhausted",
)

# Transient rate limit phrases (TPM/RPM burst spikes) which recover quickly after backoff.
_TRANSIENT_RATE_LIMIT_PHRASES = (
    "rate limit exceeded",
    "rate_limit_exceeded",
    "too many requests",
    "429",
    "resource_exhausted",
    "resource has been exhausted",
    "check your quota",
    "quota limit",
    "exhausted: quota",
)

_QUOTA_PHRASES = _HARD_QUOTA_PHRASES + _TRANSIENT_RATE_LIMIT_PHRASES

_AUTH_ERROR_PHRASES = (
    "you are not logged into antigravity",
    "not logged into antigravity",
    "error getting token source",
    "unauthorized",
    "401",
)

_LOCATION_PHRASES = (
    "user location is not supported",
    "location is not supported for the api use",
)

_INVALID_MODEL_PHRASES = (
    "invalid model selection",
    "is not recognized as a known model",
)


def _is_quota_error(result: "AgyResult") -> bool:
    """Return True when error indicates quota/credits exhaustion."""
    # A successful execution (exit_code == 0 with text) is NEVER a quota error!
    if result.exit_code == 0 and result.text and result.text.strip():
        return False
    haystack = f"{result.stderr or ''} {result.text or ''}".lower()
    return any(p in haystack for p in _QUOTA_PHRASES)


def _is_hard_quota_error(result: "AgyResult") -> bool:
    """Return True only on permanent hard quota exhaustion (not transient burst rate limits)."""
    if result.exit_code == 0 and result.text and result.text.strip():
        return False
    haystack = f"{result.stderr or ''} {result.text or ''}".lower()
    return any(p in haystack for p in _HARD_QUOTA_PHRASES)


def _is_auth_error(result: "AgyResult") -> bool:
    """Return True on token expiry or unauthenticated status."""
    if result.exit_code == 0 and result.text and result.text.strip():
        return False
    haystack = f"{result.stderr or ''} {result.text or ''}".lower()
    return any(p in haystack for p in _AUTH_ERROR_PHRASES)


def _is_location_error(result: "AgyResult") -> bool:
    if result.exit_code == 0 and result.text and result.text.strip():
        return False
    haystack = f"{result.stderr or ''} {result.text or ''}".lower()
    return any(p in haystack for p in _LOCATION_PHRASES)


def _is_invalid_model_error(result: "AgyResult") -> bool:
    if result.exit_code == 0 and result.text and result.text.strip():
        return False
    haystack = f"{result.stderr or ''} {result.text or ''}".lower()
    return any(p in haystack for p in _INVALID_MODEL_PHRASES)


def _is_retryable_failure(result: "AgyResult") -> bool:
    """Check if the agy result is a failure that should be retried."""
    # Successful execution with non-empty response — no retry needed.
    if result.exit_code == 0 and result.text and result.text.strip():
        return False
    # User cancellation (130) or hard timeout (124) — do not retry.
    if result.exit_code in (130, 124):
        return False
    # Permanent hard quota, location, or invalid model failures — do not retry.
    if _is_hard_quota_error(result) or _is_location_error(result) or _is_invalid_model_error(result):
        return False
    # Transient rate limits, auth blips, server busy, exit_code != 0 are all retryable.
    return True


async def _run_agy_with_retry(
    prompt: str,
    *,
    cs: "ChatState",
    cfg: "Config",
    agy_path: str,
    stop_event: asyncio.Event,
    on_event: object,
    updater: object,
) -> "AgyResult":
    """Run agy, retrying on transient errors, burst rate limits, and auth blips.

    Retry schedule:
    - 3 attempts immediately (0s delay)
    - 12 attempts with 1.5s delay (total 15 attempts)
    - Progress during run resets attempt counter.
    - Permanent errors (hard quota reached, user cancel) are not retried.
    """
    # Proactively ensure OAuth token is not expired before starting turn
    try:
        from src.quota import is_token_expired_or_missing, _refresh_token_via_cli
        if is_token_expired_or_missing():
            LOG.info("OAuth token expired or missing, refreshing via CLI before turn...")
            await _refresh_token_via_cli(agy_path)
    except Exception as exc:
        LOG.debug("Pre-turn token check error: %s", exc)

    attempt = 0

    while True:
        made_progress = False

        async def tracking_on_event(data: dict):
            nonlocal made_progress
            if data.get("event") == "step_update":
                made_progress = True
            if on_event:
                await on_event(data)

        turn_timeout = parse_timeout_seconds(cs.print_timeout, default=AGY_TIMEOUT_S)
        result = await run_agy(
            prompt=prompt,
            chat_dir=cs.chat_dir,
            has_session=cs.has_session,
            model=cs.model or cfg.agy.model,
            mode=cs.mode or cfg.agy.mode,
            agy_path=agy_path,
            timeout=turn_timeout,
            inactivity_timeout=AGY_INACTIVITY_TIMEOUT_S,
            effort=cs.effort,
            print_timeout=cs.print_timeout or "30m",
            conversation_id=cs.conversation_id if cs.has_session else "",
            on_event=tracking_on_event if on_event else None,
            stop_event=stop_event,
        )

        # Success, user cancellation, hard quota reached, or stop requested
        if stop_event.is_set() or not _is_retryable_failure(result):
            return result

        # If agy made forward progress during this attempt, reset the retry counter!
        if made_progress:
            attempt = 0

        attempt += 1
        if attempt > _MAX_RETRIES:
            LOG.error(
                "agy giving up after %d attempts. exit=%d stderr=%s",
                _MAX_RETRIES, result.exit_code, (result.stderr or "unknown")[:200],
            )
            return result

        # Refresh token on authentication failures
        if _is_auth_error(result):
            try:
                from src.quota import _refresh_token_via_cli
                await _refresh_token_via_cli(agy_path)
            except Exception:
                pass

        reason = (result.stderr or result.text or "unknown")[:200]
        delay = 0.0 if attempt <= _INSTANT_RETRIES else _RETRY_DELAY_S

        # Transient rate limit burst (429 / resource exhausted): wait longer
        if any(p in reason.lower() for p in _TRANSIENT_RATE_LIMIT_PHRASES):
            delay = max(delay, 2.5)

        LOG.warning(
            "agy exit=%d (attempt %d/%d), retrying in %.1fs. reason: %s",
            result.exit_code, attempt, _MAX_RETRIES, delay, reason,
        )

        if updater:
            if any(p in reason.lower() for p in _TRANSIENT_RATE_LIMIT_PHRASES):
                await updater.update(
                    f"⏳ Превышен кратковременный лимит запросов, пауза {delay:.1f}с ({attempt}/{_MAX_RETRIES})…"
                )
            elif delay == 0.0:
                await updater.update(
                    f"🔄 Ошибка / Сервер занят, моментальный повтор ({attempt}/{_MAX_RETRIES})…"
                )
            else:
                await updater.update(
                    f"🌐 Сервер занят, повтор через {delay:.1f}с ({attempt}/{_MAX_RETRIES})…"
                )

        if delay > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return result  # user hit Stop
            except asyncio.TimeoutError:
                pass
        else:
            if stop_event.is_set():
                return result
            await asyncio.sleep(0)  # yield control to event loop

        continue



async def _heartbeat(
    tg: "_TelegramLike", chat_id: int, message_thread_id: int | None, stop_event: asyncio.Event
) -> None:
    """Typing indicator refresh every 4 s."""
    try:
        while not stop_event.is_set():
            try:
                await tg.send_chat_action(chat_id, "typing", message_thread_id=message_thread_id)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        return
