"""Long-running daemon: poll Telegram, route through agy, reply."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from src.agy_runner import AgyResult
from src.commands import BridgeReply, handle_callback, handle_text_command
from src.config import Config, load_config
from src.health import memory_healthy, record_error, record_turn, start_server
from src.media import build_media_prompt, clean_inbox
from src.okf_memory import attach_memory
from src.queue import TurnQueue
from src.state import ChatState, SessionKey, State, load_state, save_state
from src.telegram import CallbackQuery, InboundMessage, TelegramClient, is_authorized
from src.turn import ACTIVE_STOPS, execute_agy
from src.updater import auto_update_loop
from src.voice import cache_reply_text, get_cached_reply_text, synthesize_speech, transcribe_voice_groq
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
    async def send_voice(
        self,
        chat_id: int,
        voice_bytes: bytes,
        *,
        mime_type: str = "audio/ogg",
        caption: str = "",
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None: ...
    async def send_document(
        self,
        chat_id: int,
        document_bytes: bytes,
        filename: str,
        *,
        caption: str = "",
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None: ...


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


def _session_key(chat_id: int, message_thread_id: int | None) -> SessionKey:
    if message_thread_id is None:
        return chat_id
    return (chat_id, message_thread_id)


def _ensure_chat_state(
    state: State, chat_id: int, message_thread_id: int | None, chats_root: Path
) -> ChatState:
    key = _session_key(chat_id, message_thread_id)
    cs = state.chats.get(key)
    if cs is not None:
        Path(cs.chat_dir).mkdir(parents=True, exist_ok=True)
        return cs
    suffix = str(chat_id) if message_thread_id is None else f"{chat_id}_{message_thread_id}"
    chat_dir = chats_root / suffix
    chat_dir.mkdir(parents=True, exist_ok=True)
    parent = state.chats.get(chat_id) if message_thread_id is not None else None
    if parent is not None:
        cs = ChatState(
            chat_dir=str(chat_dir),
            has_session=parent.has_session,
            model=parent.model,
            mode=parent.mode,
            photo_enabled=parent.photo_enabled,
            turn_count=parent.turn_count,
            effort=parent.effort,
            print_timeout=parent.print_timeout,
            conversation_id=parent.conversation_id,
            projects=[dict(p) for p in parent.projects],
        )
    else:
        cs = ChatState(chat_dir=str(chat_dir))
    state.chats[key] = cs
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
    elif code == 130:
        reply = text or "🛑 Выполнение остановлено."
    elif code != 0:
        record_error()
        # `text` may carry a partial response or the API error string
        # (populated from the stream-json result event by agy_runner).
        if text:
            reply = text
        else:
            reply = (
                f"⚠️ agy завершился с ошибкой (код {code}). "
                "Если ошибка повторяется, попробуйте /reset"
            )
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
        if code == 0:
            reply_kb = [[{"text": "🔊 Озвучить", "callback_data": "tts"}]] if reply else None
        elif "исчерпана квота" in reply.lower() or "quota" in reply.lower():
            reply_kb = [
                [{"text": "⚡ Перейти на Gemini Flash", "callback_data": "m:gemini-3.8-flash-high"}],
                [{"text": "📊 Проверить лимиты", "callback_data": "nav:limits"}, {"text": "🤖 Другая модель", "callback_data": "nav:model"}],
            ]
        else:
            reply_kb = None
        sent_mid = await tg.send_message(
            msg.chat_id, 
            reply, 
            keyboard=reply_kb,
            message_thread_id=msg.message_thread_id,
            reply_to_message_id=msg.message_id if msg.chat_id < 0 else None
        )
        if sent_mid and reply:
            cache_reply_text(msg.chat_id, sent_mid, reply)
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

    cs = _ensure_chat_state(state, msg.chat_id, msg.message_thread_id, chats_root)
    save_state(state_path, state)

    voice_obj = msg.voice or msg.audio
    if voice_obj:
        file_id = voice_obj.get("file_id")
        if isinstance(file_id, str) and file_id:
            stt_msg_id = None
            try:
                stt_msg_id = await tg.send_message(
                    msg.chat_id,
                    "🎙 Распознаю голосовое сообщение...",
                    message_thread_id=msg.message_thread_id,
                    reply_to_message_id=msg.message_id if msg.chat_id < 0 else None,
                )
            except Exception:
                pass
            try:
                audio_bytes = await tg.get_file(file_id)  # type: ignore[attr-defined]
                fname = str(voice_obj.get("file_name") or "voice.ogg")
                transcribed = await transcribe_voice_groq(
                    audio_bytes, cfg.telegram.groq_api_key, filename=fname
                )
            except Exception as exc:
                LOG.error("Voice transcription failed: %s", exc)
                err_txt = f"⚠️ Ошибка распознавания голоса: {exc}"
                if stt_msg_id:
                    try:
                        await tg.edit_message_text(msg.chat_id, stt_msg_id, err_txt)
                    except Exception:
                        pass
                else:
                    try:
                        await tg.send_message(msg.chat_id, err_txt, message_thread_id=msg.message_thread_id)
                    except Exception:
                        pass
                return

            if not transcribed:
                empty_txt = "⚠️ Не удалось распознать речь (пустой результат)."
                if stt_msg_id:
                    try:
                        await tg.edit_message_text(msg.chat_id, stt_msg_id, empty_txt)
                    except Exception:
                        pass
                return

            shown = f"🎙 <i>«{transcribed}»</i>"
            if stt_msg_id:
                try:
                    await tg.edit_message_text(msg.chat_id, stt_msg_id, shown)
                except Exception:
                    pass
            combined_text = f"{msg.text}\n{transcribed}".strip() if msg.text else transcribed
            msg = replace(msg, text=combined_text)

    reply = await handle_text_command(msg, cs, cfg)
    if reply is not None:
        save_state(state_path, state)
        if reply.text:
            try:
                await tg.send_message(
                    msg.chat_id, reply.text, keyboard=reply.keyboard, message_thread_id=msg.message_thread_id
                )
            except Exception as err:
                LOG.error("sendMessage failed chat=%s: %s", msg.chat_id, err)
        if reply.document_bytes:
            try:
                await tg.send_document(
                    msg.chat_id,
                    reply.document_bytes,
                    reply.document_filename or "document.bin",
                    message_thread_id=msg.message_thread_id,
                    reply_to_message_id=msg.message_id if msg.chat_id < 0 else None,
                )
            except Exception as err:
                LOG.error("sendDocument failed chat=%s: %s", msg.chat_id, err)
        return

    status = await _QUEUE.submit(msg)
    if status is not None:
        await tg.send_message(msg.chat_id, status, message_thread_id=msg.message_thread_id)
        return

    try:
        await _do_turn(msg, tg, cs, cfg, agy_path)
    finally:
        _QUEUE.complete()
        save_state(state_path, state)

    while True:
        next_item = _QUEUE.next()
        if next_item is None:
            break
        nxt_msg, nxt_fut = next_item
        nxt_cs = _ensure_chat_state(
            state, nxt_msg.chat_id, nxt_msg.message_thread_id, chats_root
        )
        try:
            await _do_turn(nxt_msg, tg, nxt_cs, cfg, agy_path)
        finally:
            _QUEUE.complete()
            if not nxt_fut.done():
                nxt_fut.set_result(None)
            save_state(state_path, state)


async def _process_callback(
    cq: CallbackQuery, tg: _TelegramLike, state: State,
    state_path: Path, chats_root: Path, cfg: Config, info: DaemonInfo,
) -> None:
    if not is_authorized(
        InboundMessage(update_id=cq.update_id, chat_id=cq.chat_id,
                       user_id=cq.user_id, message_id=cq.message_id, text=""),
        cfg.telegram,
    ):
        LOG.info("drop unauth callback user=%s", cq.user_id)
        await tg.answer_callback_query(cq.callback_query_id, text="Not authorized")
        return

    if cq.data == "tts" or cq.data.startswith("tts:"):
        raw_text = get_cached_reply_text(cq.chat_id, cq.message_id) or cq.message_text
        if not raw_text:
            try:
                await tg.answer_callback_query(cq.callback_query_id, text="⚠️ Текст для озвучки не найден")
            except Exception:
                pass
            return
        try:
            await tg.answer_callback_query(cq.callback_query_id, text="🔊 Генерирую голосовое сообщение...")
        except Exception:
            pass
        try:
            await tg.send_chat_action(cq.chat_id, "record_voice", message_thread_id=cq.message_thread_id)  # type: ignore[call-arg]
        except Exception:
            pass
        try:
            audio_bytes, mime_type = await synthesize_speech(
                raw_text,
                yandex_api_key=cfg.telegram.yandex_api_key,
                cartesia_api_key=cfg.telegram.cartesia_api_key,
            )
            await tg.send_voice(  # type: ignore[attr-defined]
                cq.chat_id,
                audio_bytes,
                mime_type=mime_type,
                message_thread_id=cq.message_thread_id,
                reply_to_message_id=cq.message_id,
            )
        except Exception as exc:
            LOG.error("TTS synthesis/sendVoice failed: %s", exc)
            try:
                await tg.send_message(
                    cq.chat_id,
                    f"⚠️ Ошибка озвучивания ответа: {exc}",
                    message_thread_id=cq.message_thread_id,
                )
            except Exception:
                pass
        return

    cs = _ensure_chat_state(state, cq.chat_id, cq.message_thread_id, chats_root)
    reply = await handle_callback(cq, cs, cfg)
    save_state(state_path, state)

    try:
        await tg.answer_callback_query(cq.callback_query_id, text=reply.toast)
    except Exception:
        pass
    if reply.text:
        try:
            await tg.edit_message_text(
                cq.chat_id, cq.message_id, reply.text, keyboard=reply.keyboard
            )
        except Exception as err:
            # For stop_turn callbacks, execute_agy deletes the status message upon stopping,
            # so failing to edit it is expected and should not send a stray message.
            if cq.data.startswith("stop_turn"):
                return
            LOG.warning("editMessageText failed (sending fresh): %s", err)
            try:
                await tg.send_message(cq.chat_id, reply.text, keyboard=reply.keyboard, message_thread_id=cq.message_thread_id)
            except Exception as err2:
                LOG.error("fallback sendMessage failed: %s", err2)

    if reply.document_bytes:
        try:
            await tg.send_document(
                cq.chat_id,
                reply.document_bytes,
                reply.document_filename or "document.bin",
                message_thread_id=cq.message_thread_id,
            )
        except Exception as err:
            LOG.error("callback sendDocument failed: %s", err)


async def _fetch_updates(tg: _TelegramLike, offset: int) -> list[dict[str, Any]]:
    try:
        return await asyncio.wait_for(
            tg.get_updates(offset=offset, timeout=LONG_POLL_TIMEOUT),
            timeout=LONG_POLL_TIMEOUT + 5,
        )
    except asyncio.TimeoutError:
        return []
    except Exception as err:
        # str(err) is empty for some httpx exceptions (e.g. bare
        # ReadTimeout), so include the exception type for diagnosability.
        LOG.warning("getUpdates failed: %s: %s", type(err).__name__, err)
        await asyncio.sleep(2)
        return []


async def run(
    *, cfg: Config, state_path: Path, chats_root: Path,
    tg: _TelegramLike, agy_path: str, info: DaemonInfo,
    stop_event: asyncio.Event,
) -> None:
    _QUEUE.owner_chat_id = cfg.telegram.allowed_user_ids[0] if cfg.telegram.allowed_user_ids else 0
    # Rate limiter uses its built-in defaults (5s cooldown, 10 msgs/60s
    # sliding window per non-owner user). See src/queue.py for tuning.
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
                    # Authorization is checked before rate limiting so that
                    # unauthorized senders can't pollute the rate limiter's
                    # per-user state (unbounded dict growth) or learn
                    # anything about the bot from its replies.
                    if not is_authorized(msg, cfg.telegram):
                        LOG.info("drop unauth user=%s", msg.user_id)
                        continue
                    allowed, wait = _QUEUE.check_rate_limit(msg.user_id)
                    if not allowed:
                        await tg.send_message(
                            msg.chat_id,
                            f"⏳ Слишком много сообщений. Подождите {wait:.0f}с.",
                            message_thread_id=msg.message_thread_id,
                        )
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
    bot_commands = [
        {"command": "start", "description": "Приветствие и справка"},
        {"command": "limits", "description": "Просмотр квот и лимитов моделей"},
        {"command": "diff", "description": "Просмотр изменений git в проекте"},
        {"command": "files", "description": "Файлы рабочей папки чата"},
        {"command": "get", "description": "Скачать файл из проекта (/get имя)"},
        {"command": "settings", "description": "Настройки чата и диалоги"},
        {"command": "model", "description": "Выбрать модель ИИ"},
        {"command": "mode", "description": "Режим работы (code / plan)"},
        {"command": "status", "description": "Статус сессии и системы"},
        {"command": "queue", "description": "Статус очереди сообщений"},
        {"command": "stop", "description": "Остановить текущий ответ"},
        {"command": "reset", "description": "Сбросить текущую сессию"},
        {"command": "image", "description": "Вкл/выкл обработку фото"},
        {"command": "files", "description": "Список недавних файлов"},
        {"command": "update", "description": "Проверить обновления из GitHub"},
    ]
    try:
        async with TelegramClient(cfg.telegram.bot_token) as tg:
            try:
                await tg.set_my_commands(bot_commands)
            except Exception as err:
                LOG.warning("setMyCommands failed: %s", err)
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
        upd_task: asyncio.Task | None = None
        wh_secret = ""
        wh_url = os.environ.get("CTI_WEBHOOK_URL", "")
        wh_port = int(os.environ.get("CTI_WEBHOOK_PORT") or "8080")

        tg = TelegramClient(cfg.telegram.bot_token)
        try:
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
                # Random per-process secret, independent of the bot token.
                # Re-registered with Telegram via setWebhook on every
                # start, so no persistence is needed.
                wh_secret = secrets.token_hex(32)
                wh_res = await tg.set_webhook(wh_url, wh_secret)
                LOG.info("webhook setup: %s", wh_res.get("description") if wh_res.get("ok") else wh_res)
                wh_task = asyncio.create_task(start_webhook_server(wh_port, tg, wh_secret))

            upd_task = asyncio.create_task(
                auto_update_loop(
                    stop,
                    is_busy=lambda: _QUEUE.active or bool(_QUEUE.pending) or bool(ACTIVE_STOPS),
                )
            )

            await run(
                cfg=cfg, state_path=state_path, chats_root=chats_root,
                tg=tg, agy_path=agy_path, info=info, stop_event=stop,
            )
        finally:
            if upd_task is not None:
                upd_task.cancel()
                try:
                    await upd_task
                except Exception:
                    pass
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
