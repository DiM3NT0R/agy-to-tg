"""Tests for dynamic model discovery in src.models."""
from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.models import (
    FALLBACK_MODELS,
    get_available_models,
    parse_models_output,
)


def test_parse_models_output_with_ansi_and_spinner() -> None:
    sample_output = """⠋ Fetching available models...⠙ Fetching available models...gemini-3.7-flash-high     Gemini 3.7 Flash (High)
gemini-3.7-flash-medium   Gemini 3.7 Flash (Medium)
claude-sonnet-4-6         Claude Sonnet 4.6 (Thinking)
invalid model with spaces
-invalid-dash             Invalid
gpt-oss-120b-medium       GPT-OSS 120B (Medium)
"""
    parsed = parse_models_output(sample_output)
    expected_ids = ["gemini-3.7-flash-high", "gemini-3.7-flash-medium", "claude-sonnet-4-6", "gpt-oss-120b-medium"]
    assert [p[0] for p in parsed] == expected_ids
    assert parsed[0] == ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)")
    assert parsed[2] == ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)")


def test_get_available_models_subprocess_success(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock()
    mock_run.return_value = subprocess.CompletedProcess(
        args=["agy", "models"],
        returncode=0,
        stdout="gemini-4.0-pro-high     Gemini 4.0 Pro (High)\nclaude-opus-5-0     Claude Opus 5.0\n",
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    models = get_available_models(force_refresh=True)
    assert models == [
        ("gemini-4.0-pro-high", "Gemini 4.0 Pro (High)"),
        ("claude-opus-5-0", "Claude Opus 5.0"),
    ]


def test_get_available_models_fallback_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_run_fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError("agy binary not found")

    monkeypatch.setattr(subprocess, "run", mock_run_fail)
    import src.models
    src.models._cached_models = None

    models = get_available_models(force_refresh=True)
    assert models == FALLBACK_MODELS
