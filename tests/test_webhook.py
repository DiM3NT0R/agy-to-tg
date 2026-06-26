"""Stanford-level edge case tests for webhook.py — target 75%+ coverage.

Covers: _verify_secret, drain_webhook_updates, webhook_handler edge cases.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.webhook import (
    WEBHOOK_PATH, _verify_secret, drain_webhook_updates,
    webhook_handler, _webhook_updates,
)


# ── _verify_secret edge cases ─────────────────────────────────────

def test_verify_secret_valid() -> None:
    token = "secret123"
    body = json.dumps({"update_id": 1}).encode()
    sig = hmac.new(hashlib.sha256(token.encode()).digest(),
                   body, hashlib.sha256).hexdigest()
    assert _verify_secret(body, sig, token) is True


def test_verify_secret_wrong_signature() -> None:
    assert _verify_secret(b"{}", "deadbeef", "token") is False


def test_verify_secret_empty_header() -> None:
    assert _verify_secret(b"{}", "", "token") is False


def test_verify_secret_empty_token() -> None:
    assert _verify_secret(b"{}", "abc123", "") is False


def test_verify_secret_empty_both() -> None:
    assert _verify_secret(b"{}", "", "") is False


def test_verify_secret_different_body_same_token() -> None:
    token = "t"
    b1 = b'{"u":1}'
    b2 = b'{"u":2}'
    sig = hmac.new(hashlib.sha256(token.encode()).digest(),
                   b1, hashlib.sha256).hexdigest()
    assert _verify_secret(b2, sig, token) is False


def test_verify_secret_large_body() -> None:
    token = "t"
    body = b"x" * 10000
    sig = hmac.new(hashlib.sha256(token.encode()).digest(),
                   body, hashlib.sha256).hexdigest()
    assert _verify_secret(body, sig, token) is True


def test_verify_secret_binary_safe() -> None:
    # Token with weird chars
    token = "t\x00k\xff"
    body = b"body"
    sig = hmac.new(hashlib.sha256(token.encode()).digest(),
                   body, hashlib.sha256).hexdigest()
    assert _verify_secret(body, sig, token) is True


# ── drain_webhook_updates edge cases ─────────────────────────────

def test_drain_returns_empty_when_nothing() -> None:
    _webhook_updates.clear()
    assert drain_webhook_updates() == []


def test_drain_returns_and_clears() -> None:
    _webhook_updates.extend([{"update_id": 1}, {"update_id": 2}])
    result = drain_webhook_updates()
    assert len(result) == 2
    assert _webhook_updates == []  # cleared


# ── webhook_handler edge cases ────────────────────────────────────

def _http_request(method: str = "POST", path: str = "/webhook",
                  body: bytes = b"{}",
                  headers: dict[str, str] | None = None) -> bytes:
    """Build a raw HTTP request for the asyncio reader."""
    hdrs = headers or {}
    lines = [f"{method} {path} HTTP/1.1"]
    for k, v in hdrs.items():
        lines.append(f"{k}: {v}")
    lines.append(f"Content-Length: {len(body)}")
    lines.append("")
    return "\r\n".join(lines).encode() + b"\r\n" + body


class _MockWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_handler_rejects_invalid_secret() -> None:
    tg = AsyncMock()
    token = "abc"
    body = json.dumps({"update_id": 1}).encode()
    req = _http_request(body=body, headers={
        "Host": "localhost",
        "X-Telegram-Bot-Api-Secret-Token": "wrong",
    })
    reader = asyncio.StreamReader()
    reader.feed_data(req)
    reader.feed_eof()
    writer = _MockWriter()
    await webhook_handler(reader, writer, tg, token)  # type: ignore[arg-type]
    assert b"403" in writer.written[0]


@pytest.mark.asyncio
async def test_handler_accepts_valid_secret() -> None:
    token = "abc123"
    body = json.dumps({"update_id": 1}).encode()
    sig = hmac.new(hashlib.sha256(token.encode()).digest(),
                   body, hashlib.sha256).hexdigest()
    req = _http_request(body=body, headers={
        "Host": "localhost",
        "X-Telegram-Bot-Api-Secret-Token": sig,
    })
    reader = asyncio.StreamReader()
    reader.feed_data(req)
    reader.feed_eof()
    writer = _MockWriter()
    tg = AsyncMock()
    await webhook_handler(reader, writer, tg, token)  # type: ignore[arg-type]
    # Should have accepted
    assert b"200" in writer.written[0]
    # Update should be queued
    updates = drain_webhook_updates()
    assert len(updates) == 1
    assert updates[0]["update_id"] == 1


@pytest.mark.asyncio
async def test_handler_missing_secret_header() -> None:
    token = "abc"
    body = json.dumps({"update_id": 1}).encode()
    req = _http_request(body=body, headers={"Host": "localhost"})
    reader = asyncio.StreamReader()
    reader.feed_data(req)
    reader.feed_eof()
    writer = _MockWriter()
    await webhook_handler(reader, writer, AsyncMock(), token)  # type: ignore[arg-type]
    assert b"403" in writer.written[0]
