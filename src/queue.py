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
MAX_QUEUE_DEPTH = 5

# Rate-limit defaults (sliding window + per-user cooldown). These apply to
# non-owner users only — the owner (first entry in allowed_user_ids) always
# bypasses both the turn queue and the rate limiter.
DEFAULT_COOLDOWN_SECONDS = 5.0
DEFAULT_MAX_PER_WINDOW = 10
DEFAULT_WINDOW_SECONDS = 60.0


@dataclass
class TurnQueue:
    """FIFO queue ensuring one agy turn at a time across all chats.

    When a turn is active, subsequent messages are enqueued.
    Each chat may have at most one pending message.
    Owner (first in allowed_user_ids) always skips the queue.
    """
    active: bool = False
    pending: list[tuple[int, int, int | None, "InboundMessage", asyncio.Future[str | None]]] = field(default_factory=list)
    owner_chat_id: int = 0

    # Rate limiting state (per user_id).
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    max_per_window: int = DEFAULT_MAX_PER_WINDOW
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    _last_request: dict[int, float] = field(default_factory=dict)
    _window_events: dict[int, list[float]] = field(default_factory=dict)

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

    def _pos(self, chat_id: int, user_id: int, thread_id: int | None) -> int:
        for i, (cid, uid, tid, _, _) in enumerate(self.pending):
            if cid == chat_id and uid == user_id and tid == thread_id:
                return i + 1
        return len(self.pending) + 1

    def _already_queued(self, chat_id: int, user_id: int, thread_id: int | None) -> bool:
        return any(cid == chat_id and uid == user_id and tid == thread_id for cid, uid, tid, _, _ in self.pending)

    async def submit(self, msg: "InboundMessage") -> str | None:
        """Submit a message for processing. Returns queued status str or None to proceed.

        Returns None when the caller should execute immediately.
        Returns a str when the message was enqueued (status for user).
        """
        cid = msg.chat_id
        uid = msg.user_id
        tid = msg.message_thread_id

        # Owner bypass
        if msg.user_id == self.owner_chat_id:
            return None

        # Already active — enqueue
        if self.active:
            if self._already_queued(cid, uid, tid):
                for i, (c, u, t, m, f) in enumerate(self.pending):
                    if c == cid and u == uid and t == tid:
                        self.pending[i] = (cid, uid, tid, msg, f)
                        return f"⏳ Сообщение обновлено (очередь #{i+1})"
            if len(self.pending) >= MAX_QUEUE_DEPTH:
                return "🚫 Очередь переполнена. Попробуйте чуть позже."
            fut: asyncio.Future[str | None] = asyncio.Future()
            self.pending.append((cid, uid, tid, msg, fut))
            return f"⏳ В очереди (позиция #{len(self.pending)}). Скоро начнется обработка…"

        # Not active — proceed
        self.active = True
        return None

    def complete(self) -> None:
        """Mark current turn as complete."""
        self.active = False

    def next(self) -> tuple["InboundMessage", asyncio.Future[str | None]] | None:
        """Return next queued message or None."""
        if not self.pending:
            return None
        _, _, _, msg, fut = self.pending.pop(0)
        self.active = True
        return msg, fut

    def status(self) -> list[str]:
        lines = [f"Active: {'yes' if self.active else 'no'}"]
        if self.pending:
            lines.append(f"Queue ({len(self.pending)}):")
            for i, (cid, uid, tid, msg, _) in enumerate(self.pending):
                preview = msg.text[:40] + ("…" if len(msg.text) > 40 else "")
                lines.append(f"  #{i+1} chat={cid} user={uid} thread={tid} \"{preview}\"")
        else:
            lines.append("Queue: empty")
        return lines
