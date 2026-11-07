"""Unit tests for models visibility and customization in src.models_config."""
from __future__ import annotations

from pathlib import Path
import pytest

from src.models import get_engine_for_model
from src.models_config import (
    ModelsConfig,
    load_models_config,
    save_models_config,
    is_model_enabled,
    toggle_model_visibility,
    add_custom_model,
    remove_custom_model,
    reset_models_config,
    get_all_provider_models,
    get_active_models_for_picker,
    get_provider_summary_counts,
)


def test_models_config_dict_roundtrip() -> None:
    cfg = ModelsConfig(
        disabled_models=["gemini-2.0-flash", "gpt-4o"],
        custom_models={
            "agy": [{"id": "custom-gemini", "name": "Custom Gemini"}],
            "codex": [{"id": "gpt-5-preview", "name": "GPT 5"}],
            "openai": [{"id": "mistral-large", "name": "Mistral Large"}],
        },
    )
    d = cfg.to_dict()
    reloaded = ModelsConfig.from_dict(d)
    assert reloaded.disabled_models == ["gemini-2.0-flash", "gpt-4o"]
    assert len(reloaded.custom_models["agy"]) == 1
    assert reloaded.custom_models["agy"][0]["id"] == "custom-gemini"
    assert reloaded.custom_models["codex"][0]["name"] == "GPT 5"
    assert reloaded.custom_models["openai"][0]["id"] == "mistral-large"


def test_save_and_load_models_config(tmp_path: Path) -> None:
    p = tmp_path / "models_config.json"
    cfg = ModelsConfig(disabled_models=["m1", "m2"])
    save_models_config(cfg, storage_path=p)
    assert p.is_file()

    loaded = load_models_config(storage_path=p)
    assert loaded.disabled_models == ["m1", "m2"]


def test_is_model_enabled_and_toggle(tmp_path: Path) -> None:
    p = tmp_path / "models_config.json"
    cfg = ModelsConfig()
    save_models_config(cfg, storage_path=p)

    # Initially enabled
    assert is_model_enabled("gemini-2.5-pro", storage_path=p) is True

    # Toggle to disable
    is_now_on = toggle_model_visibility("gemini-2.5-pro", storage_path=p)
    assert is_now_on is False
    assert is_model_enabled("gemini-2.5-pro", storage_path=p) is False
    # Case-insensitive check
    assert is_model_enabled("GEMINI-2.5-PRO", storage_path=p) is False

    # Toggle to re-enable
    is_now_on_again = toggle_model_visibility("gemini-2.5-pro", storage_path=p)
    assert is_now_on_again is True
    assert is_model_enabled("gemini-2.5-pro", storage_path=p) is True


def test_add_and_remove_custom_model(tmp_path: Path) -> None:
    p = tmp_path / "models_config.json"

    # Invalid ID
    assert add_custom_model("codex", "", storage_path=p) is False
    assert add_custom_model("codex", "a", storage_path=p) is False
    assert add_custom_model("codex", "invalid model spaces", storage_path=p) is False

    # Valid add
    ok = add_custom_model("codex", "gpt-5-preview", "GPT-5 Preview", storage_path=p)
    assert ok is True

    cfg = load_models_config(storage_path=p)
    assert any(it["id"] == "gpt-5-preview" for it in cfg.custom_models["codex"])
    assert is_model_enabled("gpt-5-preview", storage_path=p) is True

    # Update display name
    ok_up = add_custom_model("codex", "gpt-5-preview", "GPT-5 Super", storage_path=p)
    assert ok_up is True
    cfg2 = load_models_config(storage_path=p)
    assert len(cfg2.custom_models["codex"]) == 1
    assert cfg2.custom_models["codex"][0]["name"] == "GPT-5 Super"

    # Remove
    del_ok = remove_custom_model("codex", "gpt-5-preview", storage_path=p)
    assert del_ok is True
    cfg3 = load_models_config(storage_path=p)
    assert not any(it["id"] == "gpt-5-preview" for it in cfg3.custom_models["codex"])

    # Remove non-existent
    assert remove_custom_model("codex", "non-existent", storage_path=p) is False


def test_reset_models_config(tmp_path: Path) -> None:
    p = tmp_path / "models_config.json"
    add_custom_model("agy", "custom-agy-1", storage_path=p)
    add_custom_model("codex", "custom-cdx-1", storage_path=p)
    toggle_model_visibility("gemini-2.5-flash", storage_path=p)

    # Reset only codex
    reset_models_config("codex", storage_path=p)
    cfg = load_models_config(storage_path=p)
    assert len(cfg.custom_models["codex"]) == 0
    assert len(cfg.custom_models["agy"]) == 1
    assert "gemini-2.5-flash" in cfg.disabled_models

    # Reset all
    reset_models_config(None, storage_path=p)
    cfg_all = load_models_config(storage_path=p)
    assert len(cfg_all.custom_models["agy"]) == 0
    assert len(cfg_all.disabled_models) == 0


def test_get_all_provider_models_and_picker(tmp_path: Path) -> None:
    p = tmp_path / "models_config.json"
    add_custom_model("codex", "gpt-custom", "Custom GPT", storage_path=p)

    models_cdx = get_all_provider_models("codex", storage_path=p)
    assert any(mid == "gpt-custom" and is_custom for mid, _, _, is_custom in models_cdx)

    # Picker returns active models
    active_cdx = get_active_models_for_picker("codex", storage_path=p)
    assert any(mid == "gpt-custom" for mid, _ in active_cdx)

    # Disable custom model
    toggle_model_visibility("gpt-custom", storage_path=p)
    active_after = get_active_models_for_picker("codex", storage_path=p)
    assert not any(mid == "gpt-custom" for mid, _ in active_after)


def test_get_provider_summary_counts(tmp_path: Path) -> None:
    p = tmp_path / "models_config.json"
    counts = get_provider_summary_counts(storage_path=p)
    assert "agy" in counts
    assert "codex" in counts
    assert "openai" in counts
    on, total = counts["agy"]
    assert total > 0
    assert on == total


def test_get_engine_for_custom_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "models_config.json"
    monkeypatch.setattr("src.models_config._DEFAULT_CONFIG_PATH", p)
    add_custom_model("codex", "custom-codex-brain", storage_path=p)
    add_custom_model("agy", "custom-gemini-experimental", storage_path=p)
    add_custom_model("openai", "special-api-llm", storage_path=p)

    assert get_engine_for_model("custom-codex-brain") == "codex"
    assert get_engine_for_model("custom-gemini-experimental") == "agy"
    assert get_engine_for_model("special-api-llm") == "openai"
