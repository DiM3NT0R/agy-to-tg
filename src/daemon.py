"""Long-running daemon: poll Telegram, route through agy, reply."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from src.agy_runner import AgyResult
from src.commands import BridgeReply, handle_callback, handle_text_command
from src.config import Config, load_config
from src.health import memory_healthy, record_error, record_turn, start_server
from src.media import build_media_prompt, clean_inbox
from src.okf_memory import attach_memory
from src.queue import TurnQueue
from src.state import ChatState, State, load_state, save_state
from src.telegram import CallbackQuery, InboundMessage, TelegramClient, is_authorized
from src.turn import execute_agy
from src.webhook import drain_webhook_updates, start_webhook_server

LOG = logging.getLogger("antigravity_telegram_bridge")
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
DEFAULT_STATE_PATH = Path.home() / ".antigravity" / "bridge" / "state.json"
DEFAULT_CHATS_ROOT = Path.home() / ".antigravity" / "bridge" / "chats"
DEFAULT_AGY_BIN = "agy"
LONG_POLL_TIMEOUT = 30
AGY_TIMEOUT_S = 900.0
_QUEUE = TurnQueue()


class _TelegramLike(Protocol):
    async def __aenter__(self) -> "_TelegramLike": ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def get_me(self) -> dict[str, Any]: ...
    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]: ...
    async def send_message(
        self, chat_id: int, text: str, *, keyboard: Any | None = None
    ) -> int | None: ...
    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, keyboard: Any | None = None
    ) -> None: ...
    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None: ...
    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None: ...
    async def delete_message(self, chat_id: int, message_id: int) -> None: ...


RunAgyFunc = Callable[..., Awaitable[AgyResult]]


@dataclass(frozen=True)
class DaemonInfo:
    bot_username: str
    started_at: float
    agy_version: str


def _resolve_agy_path() -> str:
    return os.environ.get("AGY_BIN") or DEFAULT_AGY_BIN


def _resolve_chats_root(cfg: Config) -> Path:
    if cfg.agy.chats_root:
        return Path(cfg.agy.chats_root).expanduser()
    return DEFAULT_CHATS_ROOT


def _detect_agy_version(agy_path: str) -> str:
    try:
        out = subprocess.run(
            [agy_path, "--version"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception as err:
        LOG.warning("agy --version failed: %s", err)
    return "unknown"


def _ensure_chat_state(state: State, chat_id: int, chats_root: Path) -> ChatState:
    cs = state.chats.get(chat_id)
    if cs is not None:
        Path(cs.chat_dir).mkdir(parents=True, exist_ok=True)
        return cs
    chat_dir = chats_root / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    cs = ChatState(chat_dir=str(chat_dir))
    state.chats[chat_id] = cs
    return cs


def _format_timeout_reply() -> str:
    minutes = AGY_TIMEOUT_S / 60
    return (
        f"⏱️ agy turn cut off at {minutes:.0f} min.\n\n"
        "• Send a shorter follow-up\n"
        "• Split into smaller steps\n"
        "• /reset to start fresh"
    )


async def _do_turn(
    msg: InboundMessage, tg: _TelegramLike, cs: ChatState,
    cfg: Config, agy_path: str,
) -> None:
    prompt = await build_media_prompt(msg, tg, cs, cfg)
    if prompt is None:
        return
    try:
        await tg.send_chat_action(msg.chat_id, "typing", message_thread_id=msg.message_thread_id)
    except Exception:
        pass
    text, code = await execute_agy(tg, msg.chat_id, prompt, msg, cs, cfg, agy_path)
    if code == 124:
        record_error()
        reply = "⚠️ Время ожидания ответа истекло."
    elif code != 0:
        record_error()
        reply = f"⚠️ Процесс agy завершился с ошибкой (код {code}). Попробуйте отправить команду /reset"
    else:
        record_turn()
        if not cs.has_session:
            cs.has_session = True
        cs.turn_count += 1
        reply = text or "(пустой ответ)"
    # Post-turn maintenance: update OKF memory layer, clean inbox
    try:
        attach_memory(cs.chat_dir, None, bridge_version="0.2.0")
        clean_inbox(cs.chat_dir)
    except Exception:
        pass
    try:
        await tg.send_message(msg.chat_id, reply, message_thread_id=msg.message_thread_id)
    except Exception as err:
        LOG.error("sendMessage failed chat=%s: %s", msg.chat_id, err)


async def _process_text(
    msg: InboundMessage, tg: _TelegramLike, state: State,
    state_path: Path, chats_root: Path, cfg: Config,
    agy_path: str, info: DaemonInfo,
) -> None:
    if not is_authorized(msg, cfg.telegram):
        LOG.info("drop unauth user=%s", msg.user_id)
        return

    cs = _ensure_chat_state(state, msg.chat_id, chats_root)
    save_state(state_path, state)

    reply = await handle_text_command(msg, cs, cfg)
    if reply is not None:
        save_state(state_path, state)
        try:
            await tg.send_message(msg.chat_id, reply.text, keyboard=reply.keyboard, message_thread_id=msg.message_thread_id)
        except Exception as err:
            LOG.error("sendMessage failed chat=%s: %s", msg.chat_id, err)
        return

    status = await _QUEUE.submit(msg)
    if status is not None:
        await tg.send_message(msg.chat_id, status, message_thread_id=msg.message_thread_id)
        return

    await _do_turn(msg, tg, cs, cfg, agy_path)
    _QUEUE.complete()

    while True:
        next_item = _QUEUE.next()
        if next_item is None:
            break
        nxt_msg, nxt_fut = next_item
        nxt_cs = _ensure_chat_state(state, nxt_msg.chat_id, chats_root)
        await _do_turn(nxt_msg, tg, nxt_cs, cfg, agy_path)
        _QUEUE.complete()
        nxt_fut.set_result(None)

    save_state(state_path, state)


async def _process_callback(
    cq: CallbackQuery, tg: _TelegramLike, state: State,
    state_path: Path, chats_root: Path, cfg: Config, info: DaemonInfo,
) -> None:
    if not is_authorized(
        InboundMessage(update_id=cq.update_id, chat_id=cq.chat_id,
                       user_id=cq.user_id, text=""),
        cfg.telegram,
    ):
        LOG.info("drop unauth callback user=%s", cq.user_id)
        await tg.answer_callback_query(cq.callback_query_id, text="Not authorized")
        return

    cs = _ensure_chat_state(state, cq.chat_id, chats_root)
    reply = handle_callback(cq, cs, cfg)
    save_state(state_path, state)

    try:
        await tg.answer_callback_query(cq.callback_query_id, text=reply.toast)
    except Exception:
        pass
    try:
        await tg.edit_message_text(
            cq.chat_id, cq.message_id, reply.text, keyboard=reply.keyboard
        )
    except Exception as err:
        LOG.warning("editMessageText failed (sending fresh): %s", err)
        try:
            await tg.send_message(cq.chat_id, reply.text, keyboard=reply.keyboard, message_thread_id=cq.message_thread_id)
        except Exception as err2:
            LOG.error("fallback sendMessage failed: %s", err2)


async def _fetch_updates(tg: _TelegramLike, offset: int) -> list[dict[str, Any]]:
    try:
        return await asyncio.wait_for(
            tg.get_updates(offset=offset, timeout=LONG_POLL_TIMEOUT),
            timeout=LONG_POLL_TIMEOUT + 5,
        )
    except asyncio.TimeoutError:
        return []
    except Exception as err:
        LOG.warning("getUpdates failed: %s", err)
        await asyncio.sleep(2)
        return []


async def run(
    *, cfg: Config, state_path: Path, chats_root: Path,
    tg: _TelegramLike, agy_path: str, info: DaemonInfo,
    stop_event: asyncio.Event,
) -> None:
    _QUEUE.owner_chat_id = cfg.telegram.allowed_user_ids[0] if cfg.telegram.allowed_user_ids else 0
    _QUEUE.max_per_user = getattr(cfg, 'safety', None).queue.max_per_user if hasattr(cfg, 'safety') else 10
    _QUEUE.cooldown_seconds = getattr(cfg, 'safety', None).queue.cooldown_seconds if hasattr(cfg, 'safety') else 5
    chats_root.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path, chats_root)
    tick = 0
    async with tg:
        while not stop_event.is_set():
            tick += 1
            # Health gate: check memory every ~30 loop iterations (~30s at idle,
            # faster when processing bursts). Self-terminate if over limit so
            # systemd Restart=on-failure brings up a clean replacement.
            if tick % 30 == 0 and not memory_healthy():
                LOG.critical("memory limit exceeded — triggering self-restart")
                stop_event.set()
                break

            updates = await _fetch_updates(tg, state.last_update_id + 1)
            wh_updates = drain_webhook_updates()
            updates.extend(wh_updates)
            for upd in updates:
                LOG.info("Received update: %s", upd)
                state.last_update_id = max(state.last_update_id, int(upd.get("update_id", 0)))

                msg = parse_update_for_daemon(upd)
                if msg is not None:
                    allowed, wait = True, 0
                    if not allowed:
                        await tg.send_message(msg.chat_id, f"⏳ Rate limit. Wait {wait}s.", message_thread_id=msg.message_thread_id)
                        continue
                    
                    async def safe_process_text(msg, tg, state, state_path, chats_root, cfg, agy_path, info):
                        try:
                            await _process_text(msg, tg, state, state_path, chats_root, cfg, agy_path, info)
                        except Exception as e:
                            import traceback
                            LOG.error("Error in _process_text: %s\n%s", e, traceback.format_exc())
                            try:
                                await tg.send_message(msg.chat_id, "❌ Произошла внутренняя ошибка моста при обработке запроса. Пожалуйста, повторите попытку.", message_thread_id=msg.message_thread_id)
                            except Exception:
                                pass
                    
                    asyncio.create_task(safe_process_text(
                        msg, tg, state, state_path, chats_root,
                        cfg, agy_path, info,
                    ))
                    continue

                cq = parse_callback_for_daemon(upd)
                if cq is not None:
                    async def safe_process_callback(cq, tg, state, state_path, chats_root, cfg, info):
                        try:
                            await _process_callback(cq, tg, state, state_path, chats_root, cfg, info)
                        except Exception as e:
                            import traceback
                            LOG.error("Error in _process_callback: %s\n%s", e, traceback.format_exc())
                            try:
                                await tg.answer_callback_query(cq.callback_query_id, text="❌ Внутренняя ошибка. Повторите попытку.")
                            except Exception:
                                pass
                    
                    asyncio.create_task(safe_process_callback(
                        cq, tg, state, state_path, chats_root, cfg, info,
                    ))
                    continue

            if not updates:
                await asyncio.sleep(0)
            save_state(state_path, state)


# Aliases to avoid name collisions in the loop above
from src.telegram import parse_callback_query as parse_callback_for_daemon
from src.telegram import parse_update as parse_update_for_daemon


async def _detect_bot_username(cfg: Config) -> str:
    try:
        async with TelegramClient(cfg.telegram.bot_token) as tg:
            me = await tg.get_me()
            uname = me.get("username")
            return f"@{uname}" if isinstance(uname, str) and uname else "?"
    except Exception as err:
        LOG.warning("getMe failed: %s", err)
    return "?"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    cfg_path = Path(os.environ.get("AGY_BRIDGE_CONFIG") or DEFAULT_CONFIG_PATH)
    state_path = Path(os.environ.get("AGY_BRIDGE_STATE") or DEFAULT_STATE_PATH)
    cfg = load_config(cfg_path)
    chats_root = _resolve_chats_root(cfg)
    agy_path = _resolve_agy_path()
    stop = asyncio.Event()

    async def _go() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        health_srv = await start_server()

        wh_task: asyncio.Task | None = None
        wh_secret = ""
        wh_url = os.environ.get("CTI_WEBHOOK_URL", "")
        wh_port = int(os.environ.get("CTI_WEBHOOK_PORT") or "8080")

        tg = TelegramClient(cfg.telegram.bot_token)
        try:
            # Set bot commands for Telegram Menu button
            bot_commands = [
                {"command": "start", "description": "Настройки и меню сессий"},
                {"command": "settings", "description": "Настройки"},
                {"command": "reset", "description": "Сбросить текущую сессию"},
                {"command": "image", "description": "Вкл/выкл анализ фото"},
                {"command": "files", "description": "Файлы в рабочей директории"},
                {"command": "queue", "description": "Очередь задач"},
            ]
            try:
                await tg.set_my_commands(bot_commands)
            except Exception as err:
                LOG.warning("setMyCommands failed: %s", err)

            bot_username = await _detect_bot_username(cfg)
            info = DaemonInfo(
                bot_username=bot_username,
                started_at=time.time(),
                agy_version=_detect_agy_version(agy_path),
            )
            LOG.info(
                "bridge starting bot=%s agy=%s chats_root=%s mode=%s model=%s",
                info.bot_username, info.agy_version, chats_root,
                cfg.agy.mode, cfg.agy.model or "(default)",
            )

            if wh_url:
                wh_secret = cfg.telegram.bot_token[:20]
                wh_res = await tg.set_webhook(wh_url, wh_secret)
                LOG.info("webhook setup: %s", wh_res.get("description") if wh_res.get("ok") else wh_res)
                wh_task = asyncio.create_task(start_webhook_server(wh_port, tg, wh_secret))

            await run(
                cfg=cfg, state_path=state_path, chats_root=chats_root,
                tg=tg, agy_path=agy_path, info=info, stop_event=stop,
            )
        finally:
            if wh_task is not None:
                wh_task.cancel()
                try:
                    await wh_task
                except Exception:
                    pass
            try:
                await tg.delete_webhook()
            except Exception:
                pass
            health_srv.close()
            await health_srv.wait_closed()

    asyncio.run(_go())


if __name__ == "__main__":
    main()
