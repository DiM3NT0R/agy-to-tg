"""Unit tests for src.health — health endpoint and counters."""
from __future__ import annotations

import pytest

from src import health


@pytest.mark.asyncio
async def test_start_server_returns_server() -> None:
    srv = await health.start_server(port=0)  # port 0 = OS-assigned
    assert srv is not None
    srv.close()
    await srv.wait_closed()


def test_record_turn_increments() -> None:
    before = health._turns_total
    health.record_turn()
    assert health._turns_total == before + 1


def test_record_error_increments() -> None:
    before = health._errors_total
    health.record_error()
    assert health._errors_total == before + 1
