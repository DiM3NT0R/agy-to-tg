"""Loader and validator for config.json."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when config.json is missing, malformed, or fails validation."""


_MODEL_RE = re.compile(r"^[a-zA-Z0-9._][a-zA-Z0-9._\-]*$")
_VALID_MODES = frozenset({"code", "plan"})


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    allowed_user_ids: list[int]
    allowed_chat_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class AgyConfig:
    chats_root: str = ""
    default_workdir: str = ""
    model: str = ""
    mode: str = "code"  # "code" (auto) | "plan" (agent instructed not to auto-execute; no OS-level sandbox)


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    agy: AgyConfig


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config not found at {path}")

    try:
        raw: dict[str, Any] = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc

    tg_raw = raw.get("telegram") or {}
    import os
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    bot_token = ""
    if cred_dir:
        token_file = Path(cred_dir) / "tg_bot_token"
        if token_file.exists():
            bot_token = token_file.read_text().strip()
            
    if not bot_token:
        bot_token = os.environ.get("AGY_TELEGRAM_BOT_TOKEN") or ""
        
    if not bot_token:
        bot_token = tg_raw.get("bot_token") or ""
        
    if not isinstance(bot_token, str) or not bot_token:
        raise ConfigError("telegram.bot_token must be a non-empty string")

    allowed_user_ids = tg_raw.get("allowed_user_ids") or []
    if not isinstance(allowed_user_ids, list) or not allowed_user_ids:
        raise ConfigError(
            "telegram.allowed_user_ids must be a non-empty list (default-deny)"
        )
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in allowed_user_ids):
        raise ConfigError("telegram.allowed_user_ids entries must be integers")

    allowed_chat_ids = tg_raw.get("allowed_chat_ids") or []
    if not isinstance(allowed_chat_ids, list) or not all(
        isinstance(x, int) and not isinstance(x, bool) for x in allowed_chat_ids
    ):
        raise ConfigError("telegram.allowed_chat_ids must be a list of integers")

    a_raw = raw.get("agy") or {}

    model = str(a_raw.get("model") or "")
    if model and not _MODEL_RE.match(model):
        raise ConfigError(
            "agy.model must match ^[a-zA-Z0-9._][a-zA-Z0-9._-]*$ "
            "(no leading dash, no spaces) to be argv-safe"
        )

    mode = a_raw.get("mode")
    if mode is None:
        mode = "code"
    if not isinstance(mode, str) or mode not in _VALID_MODES:
        raise ConfigError(
            f"agy.mode must be one of {sorted(_VALID_MODES)}, got {mode!r}"
        )

    return Config(
        telegram=TelegramConfig(
            bot_token=bot_token,
            allowed_user_ids=list(allowed_user_ids),
            allowed_chat_ids=list(allowed_chat_ids),
        ),
        agy=AgyConfig(
            chats_root=str(a_raw.get("chats_root") or ""),
            default_workdir=str(a_raw.get("default_workdir") or ""),
            model=model,
            mode=mode,
        ),
    )
