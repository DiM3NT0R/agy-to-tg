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
    derive_provider_from_url,
    is_url_like,
    save_provider,
    load_providers,
    get_active_provider,
    set_active_provider,
    delete_provider,
    update_provider_url,
    update_provider_key,
    update_provider_model,
    fetch_provider_models,
    PROVIDER_PRESETS,
)
from src.openai_compat_runner import (
    run_openai_compat,
    OpenAICompatResult,
)


def test_normalize_base_url() -> None:
    assert normalize_base_url("https://api.cheapai.io") == "https://api.cheapai.io/v1"
    assert normalize_base_url("https://api.cheapai.io/") == "https://api.cheapai.io/v1"
    assert normalize_base_url("https://api.cheapai.io/v1") == "https://api.cheapai.io/v1"
    assert normalize_base_url("https://api.cheapai.io/v1/") == "https://api.cheapai.io/v1"
    assert normalize_base_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert normalize_base_url("api.deepseek.com") == "https://api.deepseek.com/v1"
    assert normalize_base_url("") == ""


def test_derive_provider_from_url() -> None:
    assert derive_provider_from_url("https://api.cheapai.io/v1") == ("cheapai", "CheapAI")
    assert derive_provider_from_url("https://openrouter.ai/api/v1") == ("openrouter", "OpenRouter")
    assert derive_provider_from_url("https://api.together.xyz/v1") == ("together", "Together")
    assert derive_provider_from_url("http://localhost:11434/v1") == ("localhost_11434", "Localhost (11434)")
    assert derive_provider_from_url("http://192.168.1.50:8000/v1") == ("host_50_8000", "Server 192.168.1.50:8000")


def test_is_url_like() -> None:
    assert is_url_like("https://api.cheapai.io/v1") is True
    assert is_url_like("api.cheapai.io/v1") is True
    assert is_url_like("localhost:11434") is True
    assert is_url_like("cheapai") is False
    assert is_url_like("openrouter") is False


def test_provider_presets_contain_cheapai() -> None:
    assert "cheapai" in PROVIDER_PRESETS
    preset = PROVIDER_PRESETS["cheapai"]
    assert "cheapai.io" in preset["base_url"]
    assert preset["name"] == "CheapAI"
    assert preset["default_model"] == "gpt-4o"
    # ceapai alias
    assert "ceapai" in PROVIDER_PRESETS
    assert PROVIDER_PRESETS["ceapai"]["base_url"] == "https://api.cheapai.io/v1"


def test_save_and_load_providers(tmp_path: Path) -> None:
    storage = tmp_path / "providers.json"
    p1 = OpenAIProvider(
        id="cheapai",
        name="CheapAI",
        base_url="https://api.cheapai.io/v1",
        api_key="sk-cheapai-12345678",
        default_model="gpt-4o",
    )
    saved = save_provider(p1, make_active=True, storage_path=storage)
    assert saved.is_active is True
    assert saved.masked_key == "sk-c...5678"

    loaded = load_providers(storage)
    assert len(loaded) == 1
    assert loaded[0].id == "cheapai"
    assert loaded[0].base_url == "https://api.cheapai.io/v1"

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

    # Switch back to cheapai
    assert set_active_provider("cheapai", storage) is True
    active = get_active_provider(storage)
    assert active is not None
    assert active.id == "cheapai"

    # Delete deepseek
    assert delete_provider("deepseek", storage) is True
    assert len(load_providers(storage)) == 1


def test_update_provider_helpers(tmp_path: Path) -> None:
    storage = tmp_path / "providers.json"
    # Initially no provider; update_provider_url creates one
    p = update_provider_url("https://api.cheapai.io/v1", storage_path=storage)
    assert p.base_url == "https://api.cheapai.io/v1"
    assert p.id == "cheapai"

    # Update key
    p2 = update_provider_key("sk-new-secret-key-1234", storage_path=storage)
    assert p2.api_key == "sk-new-secret-key-1234"

    # Update model
    p3 = update_provider_model("claude-3-7-sonnet", storage_path=storage)
    assert p3 is not None
    assert p3.default_model == "claude-3-7-sonnet"
    assert "claude-3-7-sonnet" in p3.models


@pytest.mark.asyncio
async def test_fetch_provider_models(tmp_path: Path) -> None:
    storage = tmp_path / "providers.json"
    prov = OpenAIProvider(
        id="cheapai",
        name="CheapAI",
        base_url="https://api.cheapai.io/v1",
        api_key="sk-test",
    )
    save_provider(prov, make_active=True, storage_path=storage)

    mock_resp = httpx.Response(
        200,
        json={"data": [{"id": "gpt-4o"}, {"id": "claude-3-5-sonnet"}, {"id": "deepseek-chat"}]},
        request=httpx.Request("GET", "https://api.cheapai.io/v1/models"),
    )

    with patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
        models = await fetch_provider_models(prov)
        assert models == ["gpt-4o", "claude-3-5-sonnet", "deepseek-chat"]
        assert prov.models == models


@pytest.mark.asyncio
async def test_run_openai_compat_streaming(tmp_path: Path) -> None:
    prov = OpenAIProvider(
        id="cheapai",
        name="CheapAI",
        base_url="https://api.cheapai.io/v1",
        api_key="sk-test",
        default_model="gpt-4o",
    )

    chunks = [
        b"data: {\"choices\": [{\"delta\": {\"reasoning_content\": \"Thinking process...\"}}]}\n\n",
        b"data: {\"choices\": [{\"delta\": {\"content\": \"Hello from \"}}]}\n\n",
        b"data: {\"choices\": [{\"delta\": {\"content\": \"CheapAI!\"}}]}\n\n",
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
        assert result.text == "Hello from CheapAI!"
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

    # Test 1: Direct URL first
    msg_add = InboundMessage(1, 10, 1, 1, "/provider add https://api.cheapai.io/v1 sk-test-key gpt-4o")
    reply = await handle_text_command(msg_add, cs, cfg)
    assert reply is not None
    assert "CheapAI" in reply.text
    assert "успешно сохранен" in reply.text

    active = get_active_provider(storage)
    assert active is not None
    assert active.id == "cheapai"
    assert active.base_url == "https://api.cheapai.io/v1"
    assert active.default_model == "gpt-4o"

    # Test 2: /provider url
    msg_url = InboundMessage(2, 10, 1, 2, "/provider url https://new.cheapai.io/v1")
    reply_url = await handle_text_command(msg_url, cs, cfg)
    assert reply_url is not None
    assert "https://new.cheapai.io/v1" in reply_url.text
    active = get_active_provider(storage)
    assert active.base_url == "https://new.cheapai.io/v1"

    # Test 3: /provider key
    msg_key = InboundMessage(3, 10, 1, 3, "/provider key sk-secret-99999999")
    reply_key = await handle_text_command(msg_key, cs, cfg)
    assert reply_key is not None
    assert "успешно обновлен" in reply_key.text
    active = get_active_provider(storage)
    assert active.api_key == "sk-secret-99999999"

    # Test 4: /provider model
    msg_mod = InboundMessage(4, 10, 1, 4, "/provider model claude-3-7-sonnet")
    reply_mod = await handle_text_command(msg_mod, cs, cfg)
    assert reply_mod is not None
    assert "claude-3-7-sonnet" in reply_mod.text
    active = get_active_provider(storage)
    assert active.default_model == "claude-3-7-sonnet"

    # Test 5: /engine openai
    msg_eng = InboundMessage(5, 10, 1, 5, "/engine openai")
    reply_eng = await handle_text_command(msg_eng, cs, cfg)
    assert reply_eng is not None
    assert "OPENAI" in reply_eng.text
    assert cs.engine == "openai"


@pytest.mark.asyncio
async def test_provider_step_by_step_wizard_text_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = tmp_path / "providers.json"
    monkeypatch.setattr("src.openai_providers._DEFAULT_STORAGE_PATH", storage)

    from src.commands import (
        handle_text_command,
        clear_provider_wizard,
    )
    from src.telegram import InboundMessage
    from src.state import ChatState
    from src.config import Config, TelegramConfig, AgyConfig

    cfg = Config(telegram=TelegramConfig("tok", [1]), agy=AgyConfig("/tmp", "gemini-4-argon-low", "code"))
    cs = ChatState(chat_dir=str(tmp_path))
    clear_provider_wizard(10)

    # Step 0: Start wizard via /provider wizard or /provider add
    m0 = InboundMessage(1, 10, 1, 1, "/provider add")
    r0 = await handle_text_command(m0, cs, cfg)
    assert r0 is not None
    assert "Шаг 1 из 3: Base URL" in r0.text

    # Step 1: Send URL text
    m1 = InboundMessage(2, 10, 1, 2, "https://api.cheapai.io/v1")
    r1 = await handle_text_command(m1, cs, cfg)
    assert r1 is not None
    assert "Шаг 2 из 3: Ключ" in r1.text
    assert "https://api.cheapai.io/v1" in r1.text

    # Step 2: Send API key
    mock_resp = httpx.Response(
        200,
        json={"data": [{"id": "gpt-4o"}, {"id": "claude-3-7-sonnet"}]},
        request=httpx.Request("GET", "https://api.cheapai.io/v1/models"),
    )
    with patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
        m2 = InboundMessage(3, 10, 1, 3, "sk-secret-test-key-12345")
        r2 = await handle_text_command(m2, cs, cfg)
        assert r2 is not None
        assert "Шаг 3 из 3: Модель" in r2.text
        assert "Связь установлена" in r2.text

    # Step 3: Send Model name
    m3 = InboundMessage(4, 10, 1, 4, "claude-3-7-sonnet")
    r3 = await handle_text_command(m3, cs, cfg)
    assert r3 is not None
    assert "Провайдер успешно настроен и активирован" in r3.text
    assert "claude-3-7-sonnet" in r3.text

    # Verify saved provider in storage
    active = get_active_provider(storage)
    assert active is not None
    assert active.id == "cheapai"
    assert active.base_url == "https://api.cheapai.io/v1"
    assert active.api_key == "sk-secret-test-key-12345"
    assert active.default_model == "claude-3-7-sonnet"


@pytest.mark.asyncio
async def test_provider_step_by_step_wizard_callback_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = tmp_path / "providers.json"
    monkeypatch.setattr("src.openai_providers._DEFAULT_STORAGE_PATH", storage)

    from src.commands import (
        handle_callback,
        clear_provider_wizard,
    )
    from src.telegram import CallbackQuery
    from src.state import ChatState
    from src.config import Config, TelegramConfig, AgyConfig

    cfg = Config(telegram=TelegramConfig("tok", [1]), agy=AgyConfig("/tmp", "gemini-4-argon-low", "code"))
    cs = ChatState(chat_dir=str(tmp_path))
    clear_provider_wizard(10)

    # Step 1: Click start wizard
    cq0 = CallbackQuery(update_id=1, callback_query_id="q0", chat_id=10, user_id=1, message_id=1, data="prov:wiz:start")
    r0 = await handle_callback(cq0, cs, cfg)
    assert "Шаг 1 из 3: Base URL" in r0.text

    # Step 2: Choose preset cheapai
    cq1 = CallbackQuery(update_id=2, callback_query_id="q1", chat_id=10, user_id=1, message_id=1, data="prov:wiz:preset:cheapai")
    r1 = await handle_callback(cq1, cs, cfg)
    assert "Шаг 2 из 3: Ключ" in r1.text
    assert "CheapAI" in r1.text

    # Step 3: Skip key
    cq2 = CallbackQuery(update_id=3, callback_query_id="q2", chat_id=10, user_id=1, message_id=1, data="prov:wiz:skip_key")
    r2 = await handle_callback(cq2, cs, cfg)
    assert "Шаг 3 из 3: Модель" in r2.text

    # Step 4: Click model choice
    cq3 = CallbackQuery(update_id=4, callback_query_id="q3", chat_id=10, user_id=1, message_id=1, data="prov:wiz:model:gpt-4o")
    r3 = await handle_callback(cq3, cs, cfg)
    assert "Провайдер успешно настроен и активирован" in r3.text

    active = get_active_provider(storage)
    assert active is not None
    assert active.id == "cheapai"
    assert active.default_model == "gpt-4o"


@pytest.mark.asyncio
async def test_provider_wizard_cancel_and_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = tmp_path / "providers.json"
    monkeypatch.setattr("src.openai_providers._DEFAULT_STORAGE_PATH", storage)

    from src.commands import (
        handle_text_command,
        handle_callback,
        clear_provider_wizard,
    )
    from src.telegram import InboundMessage, CallbackQuery
    from src.state import ChatState
    from src.config import Config, TelegramConfig, AgyConfig

    cfg = Config(telegram=TelegramConfig("tok", [1]), agy=AgyConfig("/tmp", "gemini-4-argon-low", "code"))
    cs = ChatState(chat_dir=str(tmp_path))
    clear_provider_wizard(10)

    # Start wizard
    cq0 = CallbackQuery(update_id=1, callback_query_id="q0", chat_id=10, user_id=1, message_id=1, data="prov:wiz:start")
    r0 = await handle_callback(cq0, cs, cfg)
    assert "Шаг 1 из 3: Base URL" in r0.text

    # Invalid URL rejected gracefully
    m_inv = InboundMessage(2, 10, 1, 2, "not a url at all")
    r_inv = await handle_text_command(m_inv, cs, cfg)
    assert r_inv is not None
    assert "не похоже на валидный URL" in r_inv.text

    # Back to URL from Step 2
    cq_pre = CallbackQuery(update_id=3, callback_query_id="q1", chat_id=10, user_id=1, message_id=1, data="prov:wiz:preset:cheapai")
    await handle_callback(cq_pre, cs, cfg)
    cq_back = CallbackQuery(update_id=4, callback_query_id="q2", chat_id=10, user_id=1, message_id=1, data="prov:wiz:back:url")
    r_back = await handle_callback(cq_back, cs, cfg)
    assert "Шаг 1 из 3: Base URL" in r_back.text

    # Cancel via /cancel
    m_cancel = InboundMessage(5, 10, 1, 5, "/cancel")
    r_cancel = await handle_text_command(m_cancel, cs, cfg)
    assert r_cancel is not None
    assert "отменена" in r_cancel.text

    # Normal message now treated normally (returns None for agent turn)
    m_norm = InboundMessage(6, 10, 1, 6, "Hello world")
    r_norm = await handle_text_command(m_norm, cs, cfg)
    assert r_norm is None
