"""Dynamic model discovery from agy CLI."""
from __future__ import annotations

import logging
import re
import subprocess
import time

from src.state import is_valid_model

LOG = logging.getLogger("antigravity_telegram_bridge")

FALLBACK_MODELS: list[tuple[str, str]] = [
    ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"),
    ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
    ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
    ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
    ("claude-opus-5-5-low", "Claude Opus 5.5 (Low)"),
    ("claude-opus-5-5-medium", "Claude Opus 5.5 (Medium)"),
    ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"),
    ("claude-sonnet-5-5-low", "Claude Sonnet 5.5 (Low)"),
    ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)"),
    ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
    ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
]

_CACHE_TTL_S = 300.0  # 5 minutes
_cached_models: list[tuple[str, str]] | None = None
_cached_at: float = 0.0

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def parse_models_output(output: str) -> list[tuple[str, str]]:
    """Parse output from `agy models` into (model_id, display_name) pairs."""
    models: list[tuple[str, str]] = []
    clean = _ANSI_ESCAPE_RE.sub("", output.replace("\r", "\n"))
    for line in clean.splitlines():
        line = line.strip()
        if not line:
            continue
        if "fetching available models" in line.lower():
            idx = line.lower().rfind("fetching available models...")
            if idx != -1:
                line = line[idx + len("fetching available models..."):].strip()
        if not line:
            continue
        parts = re.split(r"\s{2,}|\t+", line, maxsplit=1)
        if len(parts) == 2:
            model_id, display_name = parts[0].strip(), parts[1].strip()
            if is_valid_model(model_id):
                models.append((model_id, display_name))
        elif len(parts) == 1:
            model_id = parts[0].strip()
            if is_valid_model(model_id):
                models.append((model_id, model_id))
    return models


def get_available_models(agy_path: str = "agy", force_refresh: bool = False) -> list[tuple[str, str]]:
    """Fetch available models from `agy models` with TTL cache and fallback."""
    global _cached_models, _cached_at
    now = time.time()
    if not force_refresh and _cached_models is not None and (now - _cached_at) < _CACHE_TTL_S:
        return _cached_models

    try:
        res = subprocess.run(
            [agy_path, "models"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout:
            parsed = parse_models_output(res.stdout)
            if parsed:
                _cached_models = parsed
                _cached_at = now
                return parsed
        LOG.warning("agy models returned non-zero (%d) or empty: stderr=%s", res.returncode, res.stderr)
    except Exception as e:
        LOG.warning("Failed to run '%s models': %s", agy_path, e)

    if _cached_models:
        return _cached_models
    return list(FALLBACK_MODELS)
