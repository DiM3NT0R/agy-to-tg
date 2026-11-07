"""Model display configuration and customization across all providers (Antigravity, Codex, OpenAI-compatible).

Allows users to:
- Toggle visibility (show/hide) of individual models in the /model picker.
- Add custom models to any provider.
- Remove custom models.
- Reset configuration to defaults.
- Persist settings in ~/.antigravity/bridge/models_config.json.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("antigravity_telegram_bridge")

_DEFAULT_CONFIG_PATH = Path.home() / ".antigravity" / "bridge" / "models_config.json"


@dataclass
class ModelsConfig:
    # Model IDs hidden/disabled by user
    disabled_models: list[str] = field(default_factory=list)
    # Custom added models per provider: "agy", "codex", "openai"
    # Format: {"agy": [{"id": "...", "name": "..."}], ...}
    custom_models: dict[str, list[dict[str, str]]] = field(
        default_factory=lambda: {
            "agy": [],
            "codex": [],
            "openai": [],
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelsConfig":
        disabled = [str(m).strip() for m in data.get("disabled_models") or [] if str(m).strip()]
        raw_custom = data.get("custom_models") or {}
        custom: dict[str, list[dict[str, str]]] = {
            "agy": [],
            "codex": [],
            "openai": [],
        }
        if isinstance(raw_custom, dict):
            for prov in ("agy", "codex", "openai"):
                items = raw_custom.get(prov) or []
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and it.get("id"):
                            custom[prov].append({
                                "id": str(it["id"]).strip(),
                                "name": str(it.get("name") or it["id"]).strip(),
                            })
                        elif isinstance(it, str) and it.strip():
                            custom[prov].append({
                                "id": it.strip(),
                                "name": it.strip(),
                            })
        return cls(disabled_models=disabled, custom_models=custom)


def get_models_config_path(custom_path: Path | None = None) -> Path:
    return custom_path or _DEFAULT_CONFIG_PATH


def load_models_config(storage_path: Path | None = None) -> ModelsConfig:
    """Load configuration from disk or return default empty configuration."""
    path = get_models_config_path(storage_path)
    if not path.is_file():
        return ModelsConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return ModelsConfig.from_dict(raw)
    except Exception as exc:
        LOG.warning("Failed to load models config from %s: %s", path, exc)
    return ModelsConfig()


def save_models_config(config: ModelsConfig, storage_path: Path | None = None) -> None:
    """Save configuration to disk atomically."""
    path = get_models_config_path(storage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    data = json.dumps(config.to_dict(), indent=2, ensure_ascii=False)
    tmp_path.write_text(data, encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    tmp_path.replace(path)


def is_model_enabled(model_id: str, config: ModelsConfig | None = None, storage_path: Path | None = None) -> bool:
    """Check if model is enabled (not hidden in disabled_models)."""
    cfg = config or load_models_config(storage_path)
    clean_id = model_id.strip().lower()
    for d in cfg.disabled_models:
        if d.lower() == clean_id:
            return False
    return True


def toggle_model_visibility(
    model_id: str,
    storage_path: Path | None = None,
) -> bool:
    """Toggle model visibility in /model picker. Returns True if now enabled, False if disabled."""
    cfg = load_models_config(storage_path)
    clean_id = model_id.strip()
    clean_lower = clean_id.lower()

    found_idx = -1
    for i, d in enumerate(cfg.disabled_models):
        if d.lower() == clean_lower:
            found_idx = i
            break

    if found_idx >= 0:
        # Currently disabled -> Enable it
        cfg.disabled_models.pop(found_idx)
        save_models_config(cfg, storage_path)
        return True
    else:
        # Currently enabled -> Disable it
        cfg.disabled_models.append(clean_id)
        save_models_config(cfg, storage_path)
        return False


def add_custom_model(
    provider: str,
    model_id: str,
    display_name: str = "",
    storage_path: Path | None = None,
) -> bool:
    """Add a custom model to the specified provider ('agy', 'codex', or 'openai')."""
    prov = provider.strip().lower()
    if prov not in ("agy", "codex", "openai"):
        prov = "openai"

    clean_id = model_id.strip()
    if not clean_id or len(clean_id) < 2 or not re.match(r"^[a-zA-Z0-9_\-\./:]+$", clean_id):
        return False

    clean_name = display_name.strip() or clean_id
    cfg = load_models_config(storage_path)

    # Ensure list exists
    if prov not in cfg.custom_models:
        cfg.custom_models[prov] = []

    # If already exists, update name
    for item in cfg.custom_models[prov]:
        if item["id"].lower() == clean_id.lower():
            item["name"] = clean_name
            # Ensure not disabled
            cfg.disabled_models = [d for d in cfg.disabled_models if d.lower() != clean_id.lower()]
            save_models_config(cfg, storage_path)
            return True

    cfg.custom_models[prov].append({"id": clean_id, "name": clean_name})
    # If it was in disabled list, remove it
    cfg.disabled_models = [d for d in cfg.disabled_models if d.lower() != clean_id.lower()]
    save_models_config(cfg, storage_path)
    return True


def remove_custom_model(
    provider: str,
    model_id: str,
    storage_path: Path | None = None,
) -> bool:
    """Remove a custom model from the specified provider."""
    prov = provider.strip().lower()
    cfg = load_models_config(storage_path)
    clean_lower = model_id.strip().lower()

    if prov in cfg.custom_models:
        orig_len = len(cfg.custom_models[prov])
        cfg.custom_models[prov] = [
            it for it in cfg.custom_models[prov] if it["id"].lower() != clean_lower
        ]
        if len(cfg.custom_models[prov]) < orig_len:
            cfg.disabled_models = [d for d in cfg.disabled_models if d.lower() != clean_lower]
            save_models_config(cfg, storage_path)
            return True
    return False


def reset_models_config(
    provider: str | None = None,
    storage_path: Path | None = None,
) -> None:
    """Reset configuration to default for a provider or all providers."""
    cfg = load_models_config(storage_path)
    if provider:
        prov = provider.strip().lower()
        if prov in cfg.custom_models:
            custom_ids = {it["id"].lower() for it in cfg.custom_models[prov]}
            cfg.custom_models[prov] = []
            cfg.disabled_models = [d for d in cfg.disabled_models if d.lower() not in custom_ids]
    else:
        cfg = ModelsConfig()
    save_models_config(cfg, storage_path)


def get_all_provider_models(
    provider: str,
    agy_path: str = "agy",
    force_refresh: bool = False,
    storage_path: Path | None = None,
) -> list[tuple[str, str, bool, bool]]:
    """Return all models for a provider with their enabled state and custom flag.

    Returns list of: (model_id, display_name, is_enabled, is_custom)
    """
    from src.models import (
        get_available_models,
        get_available_codex_models,
        get_available_openai_models,
    )

    prov = provider.strip().lower()
    cfg = load_models_config(storage_path)

    base_models: list[tuple[str, str]] = []
    if prov == "agy":
        base_models = get_available_models(agy_path=agy_path, force_refresh=force_refresh, dedup_efforts=True)
    elif prov == "codex":
        base_models = get_available_codex_models(force_refresh=force_refresh)
    elif prov == "openai":
        base_models = get_available_openai_models()
        if not base_models:
            try:
                from src.openai_providers import load_providers
                all_provs = load_providers()
                for p in all_provs:
                    if p.default_model:
                        base_models.append((p.default_model, f"{p.default_model} ({p.name})"))
                    for m in p.models:
                        if m != p.default_model:
                            base_models.append((m, f"{m} ({p.name})"))
            except Exception:
                pass

    seen_ids: set[str] = set()
    result: list[tuple[str, str, bool, bool]] = []

    # 1. Base / discovered models
    for mid, mname in base_models:
        m_lower = mid.lower()
        if m_lower in seen_ids:
            continue
        seen_ids.add(m_lower)
        enabled = is_model_enabled(mid, cfg)
        result.append((mid, mname, enabled, False))

    # 2. Custom models for this provider
    custom_list = cfg.custom_models.get(prov) or []
    for it in custom_list:
        cid = it["id"]
        cname = it.get("name") or cid
        c_lower = cid.lower()
        if c_lower in seen_ids:
            continue
        seen_ids.add(c_lower)
        enabled = is_model_enabled(cid, cfg)
        result.append((cid, cname, enabled, True))

    return result


def get_active_models_for_picker(
    provider: str,
    agy_path: str = "agy",
    storage_path: Path | None = None,
) -> list[tuple[str, str]]:
    """Return only enabled models for the /model picker keyboard.

    If all models for a provider are disabled, returns empty list so the section is hidden.
    """
    all_models = get_all_provider_models(provider, agy_path=agy_path, storage_path=storage_path)
    return [(mid, mname) for mid, mname, is_on, _ in all_models if is_on]


def get_provider_summary_counts(
    agy_path: str = "agy",
    storage_path: Path | None = None,
) -> dict[str, tuple[int, int]]:
    """Return summary (enabled_count, total_count) for each provider."""
    counts = {}
    for prov in ("agy", "codex", "openai"):
        models = get_all_provider_models(prov, agy_path=agy_path, storage_path=storage_path)
        total = len(models)
        on = sum(1 for _, _, is_on, _ in models if is_on)
        counts[prov] = (on, total)
    return counts
