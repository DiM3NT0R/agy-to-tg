"""Tests for src.control — plugin tool entry point."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.control import handle


def _run_handle(action: str, **extra: object) -> dict:
    payload = {"action": action, **extra}
    return handle(payload)


def test_start_invokes_systemctl_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _run_handle("start")
    assert result["ok"] is True
    assert calls == [["systemctl", "--user", "start", "antigravity-telegram-bridge.service"]]


def test_stop_invokes_systemctl_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _run_handle("stop")
    assert result["ok"] is True
    assert calls == [["systemctl", "--user", "stop", "antigravity-telegram-bridge.service"]]


def test_status_returns_systemctl_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Active: active (running)\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _run_handle("status")
    assert "Active: active (running)" in result["output"]


def test_logs_tails_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = tmp_path / "bridge.log"
    log_file.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    monkeypatch.setattr("src.control.LOG_FILE", log_file)
    result = _run_handle("logs", lines=5)
    assert result["ok"] is True
    out_lines = result["output"].splitlines()
    assert len(out_lines) == 5
    assert out_lines[-1] == "line 99"


def test_logs_fallback_to_journalctl_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.control.LOG_FILE", tmp_path / "missing.log")
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="from journal\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _run_handle("logs", lines=10)
    assert "from journal" in result["output"]
    assert any("journalctl" in c[0] for c in captured)


def test_unknown_action_returns_error() -> None:
    result = _run_handle("nuke")
    assert result["ok"] is False
    assert "unknown action" in result["output"].lower()


def test_setup_reports_missing_agy_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When `agy` is not on PATH, setup reports it gracefully (no crash)."""
    import src.control as control_mod

    plugin_dir = Path(control_mod.__file__).resolve().parent.parent
    real_cfg = plugin_dir / "config.json"
    cleanup_needed = not real_cfg.exists()
    if cleanup_needed:
        real_cfg.write_text(
            '{"telegram": {"bot_token": "12345:abc", "allowed_user_ids": [42]}, '
            '"agy": {"mode": "code"}}'
        )
    try:
        original = subprocess.run

        def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
            if isinstance(cmd, list) and cmd and cmd[0] == "agy":
                raise FileNotFoundError("[Errno 2] No such file: 'agy'")
            return original(cmd, **kw)

        monkeypatch.setattr(subprocess, "run", fake_run)

        import httpx

        class _FakeResp:
            def json(self) -> dict:
                return {"ok": True, "result": {"username": "test_bot"}}

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp())

        result = control_mod.handle({"action": "setup"})
        assert "agy CLI not on PATH" in result["output"]
        assert result["ok"] is False
    finally:
        if cleanup_needed and real_cfg.exists():
            real_cfg.unlink()
