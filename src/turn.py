"""Turn execution — agy print-mode invocation."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from src.agy_runner import run_agy

if TYPE_CHECKING:
    from src.config import Config
    from src.daemon import _TelegramLike
    from src.state import ChatState
    from src.telegram import InboundMessage

LOG = logging.getLogger("antigravity_telegram_bridge")
AGY_TIMEOUT_S = 900.0
# Retry settings for generic transient agy failures (empty response, etc.)
_RETRY_MAX = 2          # max number of retries after the first attempt
_RETRY_DELAY_S = 5.0    # seconds to wait between retries

ACTIVE_STOPS: dict[int, asyncio.Event] = {}

class StatusUpdater:
    def __init__(self, tg: "_TelegramLike", chat_id: int, message_id: int):
        self.tg = tg
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_update = 0.0
        self.current_text = "Инициализация..."
        self.pending_task = None
        self.closed = False
        self.start_time = time.time()
        self.spinner = ["🕛", "🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚"]
        self.tick = 0
        self.ticker_task = asyncio.create_task(self._ticker())

    async def _ticker(self):
        while not self.closed:
            await asyncio.sleep(5.0)
            if self.closed:
                break
            if self.current_text:
                await self._edit(self.current_text)

    async def update(self, new_text: str):
        if self.closed:
            return
        self.current_text = new_text
        now = time.time()
        if now - self.last_update > 2.0:
            self.last_update = now
            await self._edit(new_text)
        else:
            if not (self.pending_task and not self.pending_task.done()):
                delay = 2.0 - (now - self.last_update)
                self.pending_task = asyncio.create_task(self._delayed_edit(new_text, delay))

    async def _delayed_edit(self, text: str, delay: float):
        await asyncio.sleep(delay)
        if not self.closed:
            self.last_update = time.time()
            await self._edit(self.current_text)

    async def _edit(self, text: str):
        try:
            self.tick += 1
            spin = self.spinner[self.tick % len(self.spinner)]
            elapsed = int(time.time() - self.start_time)
            m = elapsed // 60
            s = elapsed % 60
            display_text = f"{spin} [{m:02d}:{s:02d}] {text}"
            await self.tg.edit_message_text(
                self.chat_id, 
                self.message_id, 
                display_text,
                keyboard=[[{"text": "🛑 Остановить", "callback_data": f"stop_turn:{self.chat_id}"}]]
            )
        except Exception:
            pass

    async def close(self):
        self.closed = True
        if self.pending_task:
            self.pending_task.cancel()
        if hasattr(self, 'ticker_task') and self.ticker_task:
            self.ticker_task.cancel()


async def execute_agy(
    tg: "_TelegramLike", chat_id: int, prompt: str, msg: "InboundMessage",
    cs: "ChatState", cfg: "Config", agy_path: str,
) -> tuple[str, int]:
    """Run one agy turn with inline status."""
    status_msg_id = None
    try:
        sent = await tg.send_message(
            chat_id, 
            "🔄 Думаю...", 
            keyboard=[[{"text": "🛑 Остановить", "callback_data": f"stop_turn:{chat_id}"}]],
            message_thread_id=msg.message_thread_id,
            reply_to_message_id=msg.message_id if chat_id < 0 else None
        )
        if sent:
            status_msg_id = sent
    except Exception:
        pass

    updater = None
    if status_msg_id:
        updater = StatusUpdater(tg, chat_id, status_msg_id)

    async def handle_event(data: dict):
        if data.get("event") == "init":
            cid = data.get("conversation_id")
            if cid:
                cs.conversation_id = cid
                # Add to projects if not exists
                if not any(p.get("id") == cid for p in cs.projects):
                    snippet = prompt[:40] + "..." if len(prompt) > 40 else prompt
                    cs.projects.insert(0, {"id": cid, "snippet": snippet})
                    # Keep max 10 projects
                    cs.projects = cs.projects[:10]
        
        if not updater:
            return
        if data.get("event") == "step_update":
            su = data.get("step_update", {})
            stype = su.get("step_type")
            state = su.get("state")
            if stype == "tool" and state == "ACTIVE":
                tool = su.get("tool_name", "unknown")
                info = su.get("tool_info", {}).get("parameters", {})
                extra = ""
                if tool == "run_command" and "CommandLine" in info:
                    cmd = info["CommandLine"].strip().split("\n")[0]
                    extra = f": {cmd}"
                elif "TargetFile" in info:
                    file_name = info["TargetFile"].split("/")[-1]
                    extra = f" ({file_name})"
                elif "AbsolutePath" in info:
                    file_name = info["AbsolutePath"].split("/")[-1]
                    extra = f" ({file_name})"
                elif "SearchPath" in info:
                    file_name = info["SearchPath"].split("/")[-1]
                    extra = f" ({file_name})"
                
                if len(extra) > 60:
                    extra = extra[:57] + "..."
                
                await updater.update(f"🛠 Выполняю: {tool}{extra}")
            elif stype == "agent_response" and state == "ACTIVE":
                await updater.update("💬 Формирую ответ...")

    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat(tg, chat_id, msg.message_thread_id, hb_stop))
    stop_event = asyncio.Event()
    ACTIVE_STOPS[chat_id] = stop_event
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
        ACTIVE_STOPS.pop(chat_id, None)
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
    return result.text or "", result.exit_code



# Patterns that indicate the API is overloaded or servers are busy — retry these.
_HIGH_TRAFFIC_PHRASES = (
    "high traffic",
    "experiencing high traffic",
    "please try again in a minute",
    "server is overloaded",
    "servers are busy",
    "server is busy",
    "busy",
    "overloaded",
    "rate limit",
    "rate_limit",
    "too many requests",
    "resource exhausted",
    "resource_exhausted",
    "quota exceeded",
    "service unavailable",
    "temporarily unavailable",
    "capacity",
    "503",
    "429",
    "502",
    "504",
    "gateway timeout",
    "bad gateway",
)

# High-traffic retry timing: 1st attempt immediately (0s), next 10 attempts 5s each (11 total).
_HT_DELAY_FIRST_S = 0.0
_HT_DELAY_REPEAT_S = 5.0
_HT_RETRY_MAX = 11


def _is_high_traffic(result: "AgyResult") -> bool:
    """Return True when stderr signals a transient server-overload or busy error."""
    haystack = (result.stderr or "").lower()
    return any(p in haystack for p in _HIGH_TRAFFIC_PHRASES)


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
    """Run agy, retrying on transient failures.

    Two strategies:
    - **High-traffic / servers are busy**: 1st retry immediately (0s), then 10 retries
      every 5s (total 11 attempts). The user sees live status and can hit 🛑 at any time.
    - **Other transient exit=1** (empty response, …):
      up to _RETRY_MAX retries with _RETRY_DELAY_S fixed delay.
    """
    attempt = 0        # counts all retry attempts regardless of type
    ht_attempt = 0     # counts only high-traffic retries (for backoff calc)

    while True:
        result = await run_agy(
            prompt=prompt,
            chat_dir=cs.chat_dir,
            has_session=cs.has_session,
            model=cs.model or cfg.agy.model,
            mode=cs.mode or cfg.agy.mode,
            agy_path=agy_path,
            timeout=AGY_TIMEOUT_S,
            effort=cs.effort,
            print_timeout=cs.print_timeout or "15m",
            conversation_id=cs.conversation_id if cs.has_session else "",
            on_event=on_event,
            stop_event=stop_event,
        )

        # Success or user-stop / hard timeout — return immediately.
        if result.exit_code in (0, 130, 124) or stop_event.is_set():
            return result

        is_transient = result.exit_code == 1 and not result.text

        if not is_transient:
            return result  # permanent error, give up

        reason = (result.stderr or "unknown")[:200]

        # ── HIGH-TRAFFIC / SERVERS BUSY: 1st immediate, next 10 every 5s (11 total) ──
        if _is_high_traffic(result):
            ht_attempt += 1
            if ht_attempt > _HT_RETRY_MAX:
                LOG.error(
                    "agy high-traffic: giving up after %d attempts. reason: %s",
                    _HT_RETRY_MAX, reason,
                )
                return result
            delay = _HT_DELAY_FIRST_S if ht_attempt == 1 else _HT_DELAY_REPEAT_S
            LOG.warning(
                "agy exit=1 high-traffic (ht_attempt=%d/%d), retrying in %.0fs. reason: %s",
                ht_attempt, _HT_RETRY_MAX, delay, reason,
            )
            if updater:
                if delay == 0.0:
                    await updater.update(
                        f"🌐 Сервер перегружен, повторный запрос (попытка {ht_attempt}/{_HT_RETRY_MAX})…"
                    )
                else:
                    await updater.update(
                        f"🌐 Сервер перегружен, повтор через {int(delay)}с "
                        f"(попытка {ht_attempt}/{_HT_RETRY_MAX})…"
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
                await asyncio.sleep(0)  # yield to event loop
            attempt += 1
            continue  # loop back

        # ── OTHER TRANSIENT: limited retries ──
        attempt += 1
        if attempt > _RETRY_MAX:
            return result

        LOG.warning(
            "agy exit=1 (attempt %d/%d), retrying in %.0fs. reason: %s",
            attempt, _RETRY_MAX + 1, _RETRY_DELAY_S, reason,
        )
        if updater:
            await updater.update(
                f"⚠️ Временная ошибка, повтор через {int(_RETRY_DELAY_S)}с "
                f"(попытка {attempt}/{_RETRY_MAX})…"
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_RETRY_DELAY_S)
            return result  # stop was set during wait
        except asyncio.TimeoutError:
            pass  # expected — delay elapsed, continue to next attempt



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
