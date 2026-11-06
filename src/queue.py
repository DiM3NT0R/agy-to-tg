"""Turn queue — FIFO async queue for multi-user concurrency."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.telegram import InboundMessage

LOG = logging.getLogger("antigravity_telegram_bridge")
MAX_QUEUE_DEPTH = 20

# Rate-limit defaults (sliding window + per-user cooldown). These apply to
# non-owner users only — the owner (first entry in allowed_user_ids) always
# bypasses the rate limiter.
DEFAULT_COOLDOWN_SECONDS = 5.0
DEFAULT_MAX_PER_WINDOW = 10
DEFAULT_WINDOW_SECONDS = 60.0

SessionKey = tuple[int, int]


def session_key(chat_id: int, message_thread_id: int | None = None) -> SessionKey:
    """Return canonical (chat_id, thread_id) tuple."""
    return (chat_id, message_thread_id or 0)


def _to_key(key_or_chat: SessionKey | int, thread_id: int | None = None) -> SessionKey:
    if isinstance(key_or_chat, tuple):
        return (key_or_chat[0], key_or_chat[1] or 0)
    return (key_or_chat, thread_id or 0)


@dataclass
class SessionQueue:
    """Per-session FIFO queue state."""
    active: bool = False
    pending: list[tuple[int, int, int | None, "InboundMessage", asyncio.Future[str | None]]] = field(default_factory=list)


@dataclass
class TurnQueue:
    """FIFO queue ensuring one turn at a time per chat/topic.

    Different chats and forum topics run concurrently in parallel.
    Within the same chat and topic, subsequent messages (including from the owner)
    are enqueued in FIFO order so concurrent agy sessions are never spawned
    in the same workspace.
    """
    owner_chat_id: int = 0
    max_queue_depth: int = MAX_QUEUE_DEPTH
    _sessions: dict[SessionKey, SessionQueue] = field(default_factory=dict)

    # Rate limiting state (per user_id).
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    max_per_window: int = DEFAULT_MAX_PER_WINDOW
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    _last_request: dict[int, float] = field(default_factory=dict)
    _window_events: dict[int, list[float]] = field(default_factory=dict)

    def _get_session(self, key: SessionKey) -> SessionQueue:
        sq = self._sessions.get(key)
        if sq is None:
            sq = SessionQueue()
            self._sessions[key] = sq
        return sq

    @property
    def active(self) -> bool:
        """True if any session is currently active."""
        return any(sq.active for sq in self._sessions.values())

    @active.setter
    def active(self, value: bool) -> None:
        """Backwards compatibility for tests setting active directly."""
        if not value:
            for sq in self._sessions.values():
                sq.active = False
        else:
            self._get_session((0, 0)).active = True

    @property
    def pending(self) -> list[tuple[int, int, int | None, "InboundMessage", asyncio.Future[str | None]]]:
        """Flattened list of all pending messages across all sessions."""
        res: list[tuple[int, int, int | None, "InboundMessage", asyncio.Future[str | None]]] = []
        for sq in self._sessions.values():
            res.extend(sq.pending)
        return res

    def is_busy(self, key: SessionKey | int | None = None, thread_id: int | None = None) -> bool:
        """Check if queue is busy for a specific session or globally."""
        if key is not None:
            sk = _to_key(key, thread_id)
            sq = self._sessions.get(sk)
            return bool(sq and (sq.active or sq.pending))
        return self.active or bool(self.pending)

    def check_rate_limit(self, user_id: int) -> tuple[bool, float]:
        """Sliding-window rate limiter with a minimum per-user cooldown.

        Returns (allowed, wait_seconds). The owner always passes. On
        success, records the request so subsequent calls see it.
        """
        if user_id == self.owner_chat_id:
            return True, 0.0

        now = time.monotonic()

        last = self._last_request.get(user_id)
        if last is not None:
            elapsed = now - last
            if elapsed < self.cooldown_seconds:
                return False, round(self.cooldown_seconds - elapsed, 1)

        events = self._window_events.setdefault(user_id, [])
        cutoff = now - self.window_seconds
        events[:] = [t for t in events if t > cutoff]
        if len(events) >= self.max_per_window:
            wait = round(self.window_seconds - (now - events[0]), 1)
            return False, max(wait, 0.1)

        self._last_request[user_id] = now
        events.append(now)
        return True, 0.0

    async def submit(self, msg: "InboundMessage") -> str | None:
        """Submit a message for processing. Returns queued status str or None to proceed.

        Returns None when the caller should execute immediately for this session.
        Returns a str when the message was enqueued (status for user).
        """
        cid = msg.chat_id
        uid = msg.user_id
        tid = msg.message_thread_id
        key = session_key(cid, tid)
        sq = self._get_session(key)

        # Already active in this session — enqueue in FIFO order (including owner messages)
        if sq.active:
            if len(sq.pending) >= self.max_queue_depth:
                return "🚫 Очередь переполнена. Попробуйте чуть позже."
            fut: asyncio.Future[str | None] = asyncio.Future()
            sq.pending.append((cid, uid, tid, msg, fut))
            pos = len(sq.pending)
            if tid:
                return f"⏳ В очереди темы (позиция #{pos}). Выполнится после завершения текущего ответа…"
            return f"⏳ В очереди (позиция #{pos}). Выполнится после завершения текущего ответа…"

        # Not active in this session — proceed
        sq.active = True
        return None

    def complete(self, key: SessionKey | int | None = None, thread_id: int | None = None) -> None:
        """Mark turn as complete for a session (or all active sessions if key is None)."""
        if key is not None:
            sk = _to_key(key, thread_id)
            sq = self._sessions.get(sk)
            if sq is not None:
                sq.active = False
                if not sq.pending:
                    self._sessions.pop(sk, None)
        else:
            for k, sq in list(self._sessions.items()):
                sq.active = False
                if not sq.pending:
                    self._sessions.pop(k, None)

    def next(self, key: SessionKey | int | None = None, thread_id: int | None = None) -> tuple["InboundMessage", asyncio.Future[str | None]] | None:
        """Return next queued message for the session (or any session if key is None) or None."""
        if key is not None:
            sk = _to_key(key, thread_id)
            sq = self._sessions.get(sk)
            if sq is None or not sq.pending:
                if sq is not None and not sq.active:
                    self._sessions.pop(sk, None)
                return None
            _, _, _, msg, fut = sq.pending.pop(0)
            sq.active = True
            return msg, fut

        for k, sq in list(self._sessions.items()):
            if sq.pending:
                _, _, _, msg, fut = sq.pending.pop(0)
                sq.active = True
                return msg, fut
            elif not sq.active:
                self._sessions.pop(k, None)
        return None

    def clear(self, key: SessionKey | int | None = None, thread_id: int | None = None) -> int:
        """Clear queued messages. If key is provided, clears only that session. Resolves futures."""
        cleared = 0
        if key is not None:
            sk = _to_key(key, thread_id)
            sq = self._sessions.get(sk)
            if sq:
                cleared = len(sq.pending)
                for _, _, _, _, fut in sq.pending:
                    if not fut.done():
                        fut.set_result(None)
                sq.pending.clear()
                if not sq.active:
                    self._sessions.pop(sk, None)
        else:
            for k, sq in list(self._sessions.items()):
                cleared += len(sq.pending)
                for _, _, _, _, fut in sq.pending:
                    if not fut.done():
                        fut.set_result(None)
                sq.pending.clear()
                if not sq.active:
                    self._sessions.pop(k, None)
        return cleared

    def status(self, key: SessionKey | int | None = None, thread_id: int | None = None) -> list[str]:
        if key is not None:
            sk = _to_key(key, thread_id)
            sq = self._sessions.get(sk)
            if not sq or (not sq.active and not sq.pending):
                return [f"Сессия chat={sk[0]} thread={sk[1]}: свободна"]
            lines = [f"Сессия chat={sk[0]} thread={sk[1]}: активна={'да' if sq.active else 'нет'}"]
            if sq.pending:
                lines.append(f"В очереди ({len(sq.pending)}):")
                for i, (cid, uid, tid, msg, _) in enumerate(sq.pending):
                    preview = msg.text[:40] + ("…" if len(msg.text) > 40 else "")
                    lines.append(f"  #{i+1} user={uid} \"{preview}\"")
            return lines

        active_sessions = [k for k, sq in self._sessions.items() if sq.active]
        all_pending = [(k, item) for k, sq in self._sessions.items() for item in sq.pending]

        lines = [f"Активных сессий: {len(active_sessions)}"]
        if active_sessions:
            for k in active_sessions:
                lines.append(f"  • chat={k[0]} thread={k[1]}")
        else:
            lines.append("  (нет активных сессий)")

        if all_pending:
            lines.append(f"Сообщений в очереди: {len(all_pending)}")
            for i, (k, (cid, uid, tid, msg, _)) in enumerate(all_pending):
                preview = msg.text[:40] + ("…" if len(msg.text) > 40 else "")
                lines.append(f"  #{i+1} chat={cid} thread={tid} user={uid} \"{preview}\"")
        else:
            lines.append("Очередь: пуста")
        return lines
