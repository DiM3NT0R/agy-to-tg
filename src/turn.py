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
            await self.tg.edit_message_text(self.chat_id, self.message_id, display_text)
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
        sent = await tg.send_message(chat_id, "🔄 Думаю...", message_thread_id=msg.message_thread_id)
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
    turn_start = time.perf_counter()
    try:
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
            on_event=handle_event,
        )
        if result.exit_code != 0:
            LOG.error("agy exited with %d. stderr: %s", result.exit_code, result.stderr)
    finally:
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
