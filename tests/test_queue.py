"""Tests for src.queue — FIFO turn queue and per-user rate limiting."""
from __future__ import annotations

import pytest

from src.queue import MAX_QUEUE_DEPTH, TurnQueue
from src.telegram import InboundMessage


pytestmark = pytest.mark.asyncio


def _msg(
    chat_id: int = 1,
    user_id: int = 1,
    message_id: int = 1,
    text: str = "hi",
    message_thread_id: int | None = None,
) -> InboundMessage:
    return InboundMessage(
        update_id=1,
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        text=text,
        message_thread_id=message_thread_id,
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


async def test_owner_is_enqueued_when_active() -> None:
    q = TurnQueue(owner_chat_id=42)
    await q.submit(_msg(user_id=42, text="first"))
    status1 = await q.submit(_msg(user_id=42, text="second"))
    status2 = await q.submit(_msg(user_id=42, text="third"))
    assert status1 is not None and "#1" in status1
    assert status2 is not None and "#2" in status2
    assert len(q.pending) == 2


async def test_queue_overflow_returns_rejection() -> None:
    q = TurnQueue()
    await q.submit(_msg(user_id=1000))  # occupies "active" slot
    for uid in range(1, MAX_QUEUE_DEPTH + 1):
        await q.submit(_msg(user_id=uid))
    status = await q.submit(_msg(user_id=99))
    assert status is not None
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


async def test_concurrent_sessions_different_threads_in_same_chat() -> None:
    q = TurnQueue()
    # Topic 100
    st1 = await q.submit(_msg(chat_id=1, message_thread_id=100, text="topic 100 msg 1"))
    assert st1 is None

    # Topic 200 should proceed immediately without waiting for Topic 100
    st2 = await q.submit(_msg(chat_id=1, message_thread_id=200, text="topic 200 msg 1"))
    assert st2 is None

    # Topic 100 msg 2 should be enqueued specifically for Topic 100
    st3 = await q.submit(_msg(chat_id=1, message_thread_id=100, text="topic 100 msg 2"))
    assert st3 is not None
    assert "#1" in st3
    assert "темы" in st3

    # Topic 100 has 1 pending, Topic 200 has 0 pending
    assert q.is_busy((1, 100)) is True
    assert q.is_busy((1, 200)) is True
    assert q.is_busy((1, 300)) is False

    # Complete topic 100 turn 1
    q.complete((1, 100))
    # Pop next for topic 100
    item = q.next((1, 100))
    assert item is not None
    nxt_msg, _ = item
    assert nxt_msg.text == "topic 100 msg 2"

    # Topic 200 next has nothing
    assert q.next((1, 200)) is None


async def test_concurrent_sessions_different_chats() -> None:
    q = TurnQueue()
    st1 = await q.submit(_msg(chat_id=10, text="chat 10"))
    st2 = await q.submit(_msg(chat_id=20, text="chat 20"))
    assert st1 is None
    assert st2 is None

    # Second message to chat 10 is queued
    st3 = await q.submit(_msg(chat_id=10, text="chat 10 second"))
    assert st3 is not None
    assert "#1" in st3


async def test_clear_queue() -> None:
    q = TurnQueue()
    await q.submit(_msg(chat_id=1, message_thread_id=10, text="msg 1"))
    await q.submit(_msg(chat_id=1, message_thread_id=10, text="msg 2"))
    await q.submit(_msg(chat_id=1, message_thread_id=20, text="msg 3"))
    await q.submit(_msg(chat_id=1, message_thread_id=20, text="msg 4"))

    assert len(q.pending) == 2
    cleared = q.clear((1, 10))
    assert cleared == 1
    assert len(q.pending) == 1

    cleared_all = q.clear()
    assert cleared_all == 1
    assert len(q.pending) == 0


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
