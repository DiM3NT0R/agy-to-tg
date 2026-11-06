"""Tests for Codex multi-account manager (src.codex_accounts) and bot commands."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.codex_accounts import (
    CodexAccount,
    backup_active_auth_for_login,
    clean_login_backup,
    extract_jwt_claims,
    load_saved_accounts,
    logout_active_account,
    parse_auth_data,
    read_active_auth_data,
    remove_account,
    restore_backup_after_aborted_login,
    save_account_data,
    switch_account,
)
from src.commands import BridgeReply, handle_callback, handle_text_command
from src.config import AgyConfig, Config, TelegramConfig
from src.state import ChatState
from src.telegram import CallbackQuery, InboundMessage


def _dummy_cfg() -> Config:
    return Config(
        telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]),
        agy=AgyConfig(default_workdir="/tmp", model="gemini-3.8-flash-high", mode="code"),
    )


def _make_dummy_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.signature"


@pytest.fixture
def fake_env(tmp_path: Path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    accounts_dir = tmp_path / "bridge" / "codex_accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    auth_file = codex_home / "auth.json"
    return codex_home, accounts_dir, auth_file


def test_extract_jwt_claims():
    payload = {"email": "coder@example.com", "name": "Alice Bob"}
    token = _make_dummy_jwt(payload)
    claims = extract_jwt_claims(token)
    assert claims.get("email") == "coder@example.com"
    assert claims.get("name") == "Alice Bob"

    assert extract_jwt_claims("invalid.jwt") == {}


def test_parse_auth_data_chatgpt():
    token = _make_dummy_jwt({"email": "user1@openai.com", "name": "User One"})
    auth_data = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": token,
            "access_token": "acc123",
            "account_id": "00000000-1111-2222-3333-444444444444",
        },
    }
    meta = parse_auth_data(auth_data)
    assert meta["email"] == "user1@openai.com"
    assert meta["name"] == "User One"
    assert meta["auth_mode"] == "chatgpt"
    assert "user1_openai_com" in meta["id"]
    assert meta["label"] == "user1@openai.com"


def test_parse_auth_data_api_key():
    auth_data = {
        "auth_mode": "api_key",
        "OPENAI_API_KEY": "sk-proj-1234567890abcdef",
        "tokens": None,
    }
    meta = parse_auth_data(auth_data)
    assert meta["auth_mode"] == "api_key"
    assert meta["email"] is None
    assert "cdef" in meta["label"]
    assert meta["id"].startswith("api_key_")


def test_save_and_load_saved_accounts(fake_env):
    codex_home, accounts_dir, auth_file = fake_env

    # 1. Initially no accounts
    accs = load_saved_accounts(accounts_dir=accounts_dir, auth_path=auth_file)
    assert accs == []

    # 2. Save account A
    token_a = _make_dummy_jwt({"email": "a@test.com", "name": "Agent A"})
    data_a = {"auth_mode": "chatgpt", "tokens": {"id_token": token_a, "account_id": "acc-a"}}
    saved_a = save_account_data(data_a, accounts_dir=accounts_dir, auth_path=auth_file, make_active=True)
    assert saved_a.email == "a@test.com"
    assert auth_file.is_file()

    # 3. Save account B without making active
    token_b = _make_dummy_jwt({"email": "b@test.com", "name": "Agent B"})
    data_b = {"auth_mode": "chatgpt", "tokens": {"id_token": token_b, "account_id": "acc-b"}}
    saved_b = save_account_data(data_b, accounts_dir=accounts_dir, auth_path=auth_file, make_active=False)

    # 4. Load saved accounts
    accs = load_saved_accounts(accounts_dir=accounts_dir, auth_path=auth_file)
    assert len(accs) == 2
    assert accs[0].email == "a@test.com"
    assert accs[0].is_active is True
    assert accs[1].email == "b@test.com"
    assert accs[1].is_active is False


def test_switch_account(fake_env):
    codex_home, accounts_dir, auth_file = fake_env

    token_a = _make_dummy_jwt({"email": "a@test.com", "name": "Agent A"})
    data_a = {"auth_mode": "chatgpt", "tokens": {"id_token": token_a, "account_id": "acc-a"}}
    acc_a = save_account_data(data_a, accounts_dir=accounts_dir, auth_path=auth_file, make_active=True)

    token_b = _make_dummy_jwt({"email": "b@test.com", "name": "Agent B"})
    data_b = {"auth_mode": "chatgpt", "tokens": {"id_token": token_b, "account_id": "acc-b"}}
    acc_b = save_account_data(data_b, accounts_dir=accounts_dir, auth_path=auth_file, make_active=False)

    # Switch to B
    ok, msg, current = switch_account(acc_b.id, accounts_dir=accounts_dir, auth_path=auth_file)
    assert ok is True
    assert "b@test.com" in msg
    assert current is not None
    assert current.email == "b@test.com"

    # Verify auth.json now contains account B
    active_now = read_active_auth_data(auth_file)
    assert active_now is not None
    meta = parse_auth_data(active_now)
    assert meta["email"] == "b@test.com"

    # Check that A is still saved and can be switched back to
    ok2, msg2, current2 = switch_account(acc_a.id, accounts_dir=accounts_dir, auth_path=auth_file)
    assert ok2 is True
    assert "a@test.com" in msg2
    active_again = read_active_auth_data(auth_file)
    assert parse_auth_data(active_again)["email"] == "a@test.com"


def test_remove_and_logout_account(fake_env):
    codex_home, accounts_dir, auth_file = fake_env

    token_a = _make_dummy_jwt({"email": "remove@test.com"})
    data_a = {"auth_mode": "chatgpt", "tokens": {"id_token": token_a}}
    acc_a = save_account_data(data_a, accounts_dir=accounts_dir, auth_path=auth_file, make_active=True)

    # Logout
    ok, msg = logout_active_account(auth_path=auth_file, accounts_dir=accounts_dir)
    assert ok is True
    assert not auth_file.exists()

    # Remove saved file
    ok_rem, msg_rem = remove_account(acc_a.id, accounts_dir=accounts_dir, auth_path=auth_file)
    assert ok_rem is True
    assert not (accounts_dir / f"{acc_a.id}.json").exists()


def test_backup_and_restore_login(fake_env):
    codex_home, accounts_dir, auth_file = fake_env

    auth_file.write_text('{"initial": true}', encoding="utf-8")
    backup_file = backup_active_auth_for_login(auth_file)
    assert backup_file is not None
    assert backup_file.is_file()

    # Simulate codex login wiping auth.json
    auth_file.unlink()
    assert not auth_file.exists()

    # Aborted login restores previous auth
    restored = restore_backup_after_aborted_login(auth_file)
    assert restored is True
    assert auth_file.is_file()
    assert '{"initial": true}' in auth_file.read_text()

    # Clean backup
    backup_active_auth_for_login(auth_file)
    clean_login_backup(auth_file)
    assert not (codex_home / "auth.json.pre_login_backup").exists()


@pytest.mark.asyncio
async def test_commands_codex_account_card(fake_env, monkeypatch):
    codex_home, accounts_dir, auth_file = fake_env
    monkeypatch.setattr("src.codex_accounts.get_accounts_dir", lambda *_: accounts_dir)
    monkeypatch.setattr("src.codex_accounts.get_active_auth_path", lambda *_: auth_file)
    monkeypatch.setattr("src.codex_runner.check_codex_auth", AsyncMock(return_value=(True, "Logged in using ChatGPT")))

    token = _make_dummy_jwt({"email": "main@test.com", "name": "Main Dev"})
    save_account_data({"auth_mode": "chatgpt", "tokens": {"id_token": token}}, accounts_dir=accounts_dir, auth_path=auth_file, make_active=True)

    cs = ChatState(chat_dir=str(codex_home))
    cfg = _dummy_cfg()

    msg = InboundMessage(update_id=1, message_id=10, chat_id=100, user_id=42, text="/account")
    reply = await handle_text_command(msg, cs, cfg)
    assert reply is not None
    assert "main@test.com" in reply.text
    assert "ChatGPT Subscription" in reply.text


@pytest.mark.asyncio
async def test_commands_codex_key_set(fake_env, monkeypatch):
    codex_home, accounts_dir, auth_file = fake_env
    monkeypatch.setattr("src.codex_accounts.get_accounts_dir", lambda *_: accounts_dir)
    monkeypatch.setattr("src.codex_accounts.get_active_auth_path", lambda *_: auth_file)

    tg_mock = AsyncMock()
    cs = ChatState(chat_dir=str(codex_home))
    cfg = _dummy_cfg()

    msg = InboundMessage(update_id=2, message_id=11, chat_id=100, user_id=42, text="/codex_key sk-1234567890abcdef")
    reply = await handle_text_command(msg, cs, cfg, tg=tg_mock)
    assert reply is not None
    assert "API ключ OpenAI успешно сохранен" in reply.text

    active = read_active_auth_data(auth_file)
    assert active is not None
    assert active["OPENAI_API_KEY"] == "sk-1234567890abcdef"
    assert active["auth_mode"] == "api_key"


@pytest.mark.asyncio
async def test_callback_account_switch(fake_env, monkeypatch):
    codex_home, accounts_dir, auth_file = fake_env
    monkeypatch.setattr("src.codex_accounts.get_accounts_dir", lambda *_: accounts_dir)
    monkeypatch.setattr("src.codex_accounts.get_active_auth_path", lambda *_: auth_file)
    monkeypatch.setattr("src.codex_runner.check_codex_auth", AsyncMock(return_value=(True, "Logged in using ChatGPT")))

    token_a = _make_dummy_jwt({"email": "first@test.com"})
    acc_a = save_account_data({"auth_mode": "chatgpt", "tokens": {"id_token": token_a}}, accounts_dir=accounts_dir, auth_path=auth_file, make_active=True)

    token_b = _make_dummy_jwt({"email": "second@test.com"})
    acc_b = save_account_data({"auth_mode": "chatgpt", "tokens": {"id_token": token_b}}, accounts_dir=accounts_dir, auth_path=auth_file, make_active=False)

    cs = ChatState(chat_dir=str(codex_home))
    cfg = _dummy_cfg()
    cq = CallbackQuery(
        update_id=1,
        callback_query_id="cq1",
        user_id=42,
        chat_id=100,
        message_id=20,
        data=f"acc:switch:{acc_b.id}",
    )

    reply = await handle_callback(cq, cs, cfg)
    assert reply is not None
    assert "Переключено" in reply.toast or "second@test.com" in reply.toast
    active = read_active_auth_data(auth_file)
    assert parse_auth_data(active)["email"] == "second@test.com"
