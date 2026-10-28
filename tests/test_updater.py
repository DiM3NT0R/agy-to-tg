"""Tests for src.updater — auto-update polling and /update command."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.commands import handle_text_command
from src.config import AgyConfig, Config, TelegramConfig
from src.state import ChatState
from src.telegram import InboundMessage
from src.updater import auto_update_loop, check_and_pull_update, detect_git_remote


def test_detect_git_remote_prefers_dim3nt0r(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="origin\ndim3nt0r\n", stderr="")

    monkeypatch.setattr("src.updater.subprocess.run", fake_run)
    assert detect_git_remote(tmp_path) == "dim3nt0r"


def test_check_and_pull_update_fast_forwards_when_behind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["git", "remote"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin\n", stderr="")
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="1111111111111111\n", stderr="")
        if cmd == ["git", "rev-parse", "origin/main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="2222222222222222\n", stderr="")
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "merge", "--ff-only"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Updating...\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("src.updater.subprocess.run", fake_run)
    updated, old_sha, new_sha = check_and_pull_update(tmp_path)
    assert updated is True
    assert old_sha.startswith("1111111")
    assert new_sha.startswith("2222222")


def test_check_and_pull_update_noop_when_up_to_date(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "remote"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin\n", stderr="")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="aaaaaaa\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("src.updater.subprocess.run", fake_run)
    updated, old_sha, new_sha = check_and_pull_update(tmp_path)
    assert updated is False
    assert old_sha == "aaaaaaa"


@pytest.mark.asyncio
async def test_auto_update_loop_waits_for_idle_then_restarts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    restarted = False
    busy_states = [True, False, False]

    def fake_is_busy() -> bool:
        return busy_states.pop(0) if busy_states else False

    def fake_check(repo_dir: Path) -> tuple[bool, str, str]:
        return True, "1111111", "2222222"

    def fake_restart() -> None:
        nonlocal restarted
        restarted = True

    monkeypatch.setattr("src.updater.check_and_pull_update", fake_check)
    monkeypatch.setattr("src.updater.trigger_service_restart", fake_restart)

    stop = asyncio.Event()
    await auto_update_loop(stop, is_busy=fake_is_busy, interval_s=0.01, repo_dir=tmp_path)
    assert restarted is True
    assert stop.is_set() is True


@pytest.mark.asyncio
async def test_update_command_reports_already_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.updater.check_and_pull_update", lambda: (False, "abc1234", "abc1234"))
    msg = InboundMessage(update_id=1, chat_id=10, user_id=42, message_id=1, text="/update")
    cs = ChatState(chat_dir="/tmp/c")
    cfg = Config(telegram=TelegramConfig(bot_token="t", allowed_user_ids=[42]), agy=AgyConfig())
    rep = await handle_text_command(msg, cs, cfg)
    assert rep is not None
    assert "abc1234" in rep.text
