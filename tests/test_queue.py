"""Tests for src.queue — FIFO turn queue and per-user rate limiting."""
from __future__ import annotations

import pytest

from src.queue import TurnQueue
from src.telegram import InboundMessage


pytestmark = pytest.mark.asyncio


def _msg(chat_id: int = 1, user_id: int = 1, message_id: int = 1, text: str = "hi") -> InboundMessage:
    return InboundMessage(
        update_id=1, chat_id=chat_id, user_id=user_id, message_id=message_id, text=text,
    )


# --- FIFO queue --------------------------------------------------------

async def test_submit_first_message_proceeds_immediately() -> None:
    q = TurnQueue()
    status = await q.submit(_msg())
    assert status is None
    assert q.active is True


async def test_submit_second_message_while_active_is_enqueued() -> None:
    q = TurnQueue()
    await q.submit(_msg(user_id=1))
    status = await q.submit(_msg(user_id=2))
    assert status is not None
    assert "очередь" in status.lower() or "#1" in status


async def test_owner_bypasses_queue_even_when_active() -> None:
    q = TurnQueue(owner_chat_id=42)
    await q.submit(_msg(user_id=1))
    status = await q.submit(_msg(user_id=42))
    assert status is None


async def test_queue_overflow_returns_rejection() -> None:
    q = TurnQueue()
    await q.submit(_msg(user_id=1000))  # occupies "active" slot
    for uid in range(1, 6):
        await q.submit(_msg(user_id=uid))
    status = await q.submit(_msg(user_id=99))
    assert "переполнена" in status.lower()


async def test_next_pops_fifo_order() -> None:
    q = TurnQueue()
    await q.submit(_msg(user_id=1000))  # occupies "active" slot
    await q.submit(_msg(user_id=1, message_id=10))
    await q.submit(_msg(user_id=2, message_id=20))
    q.complete()
    item = q.next()
    assert item is not None
    msg, _fut = item
    assert msg.user_id == 1


# --- Rate limiter -------------------------------------------------------

async def test_rate_limit_allows_first_request() -> None:
    q = TurnQueue()
    allowed, wait = q.check_rate_limit(user_id=7)
    assert allowed is True
    assert wait == 0.0


async def test_rate_limit_blocks_within_cooldown() -> None:
    q = TurnQueue(cooldown_seconds=100.0)
    allowed1, _ = q.check_rate_limit(user_id=7)
    allowed2, wait2 = q.check_rate_limit(user_id=7)
    assert allowed1 is True
    assert allowed2 is False
    assert wait2 > 0


async def test_rate_limit_owner_never_blocked() -> None:
    q = TurnQueue(owner_chat_id=7, cooldown_seconds=100.0)
    for _ in range(20):
        allowed, _ = q.check_rate_limit(user_id=7)
        assert allowed is True


async def test_rate_limit_sliding_window_cap() -> None:
    q = TurnQueue(cooldown_seconds=0.0, max_per_window=3, window_seconds=60.0)
    results = [q.check_rate_limit(user_id=1)[0] for _ in range(4)]
    assert results == [True, True, True, False]


async def test_rate_limit_tracks_users_independently() -> None:
    q = TurnQueue(cooldown_seconds=100.0)
    allowed_a, _ = q.check_rate_limit(user_id=1)
    allowed_b, _ = q.check_rate_limit(user_id=2)
    assert allowed_a is True
    assert allowed_b is True
