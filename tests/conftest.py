"""Shared pytest fixtures."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def sample_config_dict() -> dict[str, Any]:
    """A valid config.json structure used across tests."""
    return {
        "telegram": {
            "bot_token": "1234:abcXYZ",
            "allowed_user_ids": [99999999],
            "allowed_chat_ids": [],
        },
        "agy": {
            "chats_root": "",
            "default_workdir": "/tmp",
            "model": "",
            "mode": "code",
        },
    }


@pytest.fixture
def config_file(tmp_path: Path, sample_config_dict: dict[str, Any]) -> Path:
    """Write sample_config_dict to a temp config.json and return the path."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps(sample_config_dict))
    return p


@pytest.fixture(autouse=True)
def isolate_bridge_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate real bridge configuration paths so tests never touch production files."""
    fake_provs = tmp_path / "test_openai_providers.json"
    fake_models_cfg = tmp_path / "test_models_config.json"
    monkeypatch.setattr("src.openai_providers._DEFAULT_STORAGE_PATH", fake_provs)
    monkeypatch.setattr("src.models_config._DEFAULT_CONFIG_PATH", fake_models_cfg)
