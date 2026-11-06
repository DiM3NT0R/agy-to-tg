"""Tests for OpenAI-compatible providers management and runner."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import httpx

from src.openai_providers import (
    OpenAIProvider,
    normalize_base_url,
    save_provider,
    load_providers,
    get_active_provider,
    set_active_provider,
    delete_provider,
    fetch_provider_models,
    PROVIDER_PRESETS,
)
from src.openai_compat_runner import (
    run_openai_compat,
    OpenAICompatResult,
)


def test_normalize_base_url() -> None:
    assert normalize_base_url("https://api.ceapai.io") == "https://api.ceapai.io/v1"
    assert normalize_base_url("https://api.ceapai.io/") == "https://api.ceapai.io/v1"
    assert normalize_base_url("https://api.ceapai.io/v1") == "https://api.ceapai.io/v1"
    assert normalize_base_url("https://api.ceapai.io/v1/") == "https://api.ceapai.io/v1"
    assert normalize_base_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert normalize_base_url("api.deepseek.com") == "https://api.deepseek.com/v1"
    assert normalize_base_url("") == ""


def test_provider_presets_contain_ceapai() -> None:
    assert "ceapai" in PROVIDER_PRESETS
    preset = PROVIDER_PRESETS["ceapai"]
    assert "ceapai.io" in preset["base_url"]
    assert preset["default_model"] == "gpt-4o"


def test_save_and_load_providers(tmp_path: Path) -> None:
    storage = tmp_path / "providers.json"
    p1 = OpenAIProvider(
        id="ceapai",
        name="CeapAI",
        base_url="https://api.ceapai.io/v1",
        api_key="sk-ceapai-12345678",
        default_model="gpt-4o",
    )
    saved = save_provider(p1, make_active=True, storage_path=storage)
    assert saved.is_active is True
    assert saved.masked_key == "sk-c...5678"

    loaded = load_providers(storage)
    assert len(loaded) == 1
    assert loaded[0].id == "ceapai"
    assert loaded[0].base_url == "https://api.ceapai.io/v1"

    # Add second provider
    p2 = OpenAIProvider(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key="sk-deepseek-87654321",
        default_model="deepseek-chat",
    )
    save_provider(p2, make_active=True, storage_path=storage)

    active = get_active_provider(storage)
    assert active is not None
    assert active.id == "deepseek"

    # Switch back to ceapai
    assert set_active_provider("ceapai", storage) is True
    active = get_active_provider(storage)
    assert active is not None
    assert active.id == "ceapai"

    # Delete deepseek
    assert delete_provider("deepseek", storage) is True
    assert len(load_providers(storage)) == 1


@pytest.mark.asyncio
async def test_fetch_provider_models(tmp_path: Path) -> None:
    storage = tmp_path / "providers.json"
    prov = OpenAIProvider(
        id="ceapai",
        name="CeapAI",
        base_url="https://api.ceapai.io/v1",
        api_key="sk-test",
    )
    save_provider(prov, make_active=True, storage_path=storage)

    mock_resp = httpx.Response(
        200,
        json={"data": [{"id": "gpt-4o"}, {"id": "claude-3-5-sonnet"}, {"id": "deepseek-chat"}]},
        request=httpx.Request("GET", "https://api.ceapai.io/v1/models"),
    )

    with patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
        models = await fetch_provider_models(prov)
        assert models == ["gpt-4o", "claude-3-5-sonnet", "deepseek-chat"]
        assert prov.models == models


@pytest.mark.asyncio
async def test_run_openai_compat_streaming(tmp_path: Path) -> None:
    prov = OpenAIProvider(
        id="ceapai",
        name="CeapAI",
        base_url="https://api.ceapai.io/v1",
        api_key="sk-test",
        default_model="gpt-4o",
    )

    chunks = [
        b"data: {\"choices\": [{\"delta\": {\"reasoning_content\": \"Thinking process...\"}}]}\n\n",
        b"data: {\"choices\": [{\"delta\": {\"content\": \"Hello from \"}}]}\n\n",
        b"data: {\"choices\": [{\"delta\": {\"content\": \"CeapAI!\"}}]}\n\n",
        b"data: {\"usage\": {\"prompt_tokens\": 10, \"completion_tokens\": 5}}\n\n",
        b"data: [DONE]\n\n",
    ]

    async def fake_aiter_lines():
        for c in chunks:
            yield c.decode("utf-8")

    mock_stream_response = AsyncMock()
    mock_stream_response.status_code = 200
    mock_stream_response.aiter_lines = fake_aiter_lines
    mock_stream_response.__aenter__ = AsyncMock(return_value=mock_stream_response)
    mock_stream_response.__aexit__ = AsyncMock(return_value=None)

    thoughts_collected = []
    async def on_thought(th: str):
        thoughts_collected.append(th)

    with patch.object(httpx.AsyncClient, "stream", return_value=mock_stream_response):
        result = await run_openai_compat(
            "Hi",
            chat_dir=str(tmp_path),
            provider=prov,
            on_thought=on_thought,
        )
        assert result.exit_code == 0
        assert result.text == "Hello from CeapAI!"
        assert "Thinking process..." in result.thoughts
        assert result.input_tokens == 10
        assert result.output_tokens == 5


@pytest.mark.asyncio
async def test_provider_slash_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = tmp_path / "providers.json"
    monkeypatch.setattr("src.openai_providers._DEFAULT_STORAGE_PATH", storage)

    from src.commands import handle_text_command
    from src.telegram import InboundMessage
    from src.state import ChatState
    from src.config import Config, TelegramConfig, AgyConfig

    cfg = Config(telegram=TelegramConfig("tok", [1]), agy=AgyConfig("/tmp", "gemini-4-argon-low", "code"))
    cs = ChatState(chat_dir=str(tmp_path))

    msg_add = InboundMessage(1, 10, 1, 1, "/provider add ceapai https://api.ceapai.io/v1 sk-test-key gpt-4o")
    reply = await handle_text_command(msg_add, cs, cfg)
    assert reply is not None
    assert "CeapAI" in reply.text
    assert "успешно сохранен" in reply.text

    active = get_active_provider(storage)
    assert active is not None
    assert active.id == "ceapai"
    assert active.default_model == "gpt-4o"

    # Test /engine openai
    msg_eng = InboundMessage(2, 10, 1, 2, "/engine openai")
    reply_eng = await handle_text_command(msg_eng, cs, cfg)
    assert reply_eng is not None
    assert "OPENAI" in reply_eng.text
    assert cs.engine == "openai"
