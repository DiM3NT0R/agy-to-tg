"""Universal OpenAI-compatible API providers management.

Supports:
- cheapai.io, OpenRouter, DeepSeek, Groq, Ollama, vLLM, LM Studio, etc.
- Multi-provider configuration with custom base_url, api_key, and default_model
- Dynamic model discovery via GET {base_url}/models
- Secure local persistence in ~/.antigravity/bridge/openai_providers.json
- One-click active provider switching
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

LOG = logging.getLogger("antigravity_telegram_bridge")

_DEFAULT_STORAGE_PATH = Path.home() / ".antigravity" / "bridge" / "openai_providers.json"

# Common well-known presets for quick setup
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "cheapai": {
        "name": "CheapAI",
        "base_url": "https://api.cheapai.io/v1",
        "default_model": "gpt-4o",
        "description": "CheapAI Aggregator (Claude, GPT, Gemini, Grok)",
    },
    "ceapai": {
        "name": "CheapAI",
        "base_url": "https://api.cheapai.io/v1",
        "default_model": "gpt-4o",
        "description": "CheapAI Aggregator (cheapai.io alias)",
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-3.5-sonnet",
        "description": "Unified routing across 200+ models",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "description": "Official DeepSeek API (V3 / R1 Reasoner)",
    },
    "groq": {
        "name": "Groq Cloud",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "description": "Ultra-fast LPU inference (Llama, Mixtral)",
    },
    "ollama": {
        "name": "Ollama (Local)",
        "base_url": "http://127.0.0.1:11434/v1",
        "default_model": "qwen2.5-coder:32b",
        "description": "Local self-hosted LLMs",
    },
    "vllm": {
        "name": "vLLM / LM Studio",
        "base_url": "http://127.0.0.1:8000/v1",
        "default_model": "default",
        "description": "Local high-throughput OpenAI-compatible server",
    },
}


def normalize_base_url(url: str) -> str:
    """Ensure base_url is well-formed and has /v1 if missing and appropriate."""
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned
    # If the URL ends with a domain or port without a path, append /v1
    # e.g. https://api.cheapai.io -> https://api.cheapai.io/v1
    parts = cleaned.split("://", 1)
    scheme = parts[0]
    rest = parts[1]
    if "/" not in rest:
        cleaned = f"{scheme}://{rest}/v1"
    return cleaned


def is_url_like(val: str) -> bool:
    """Check if a string looks like an endpoint URL or host:port."""
    s = val.strip().lower()
    if s.startswith(("http://", "https://")):
        return True
    if "/" in s:
        return True
    if "." in s and not s.isdigit():
        return True
    if ":" in s and any(c.isdigit() for c in s.split(":", 1)[1]):
        return True
    return False


def derive_provider_from_url(url: str) -> tuple[str, str]:
    """Derive sensible (id, display_name) from any arbitrary URL."""
    norm = normalize_base_url(url)
    try:
        host = norm.split("://", 1)[1].split("/", 1)[0]
        port = ""
        if ":" in host:
            host_no_port, port = host.split(":", 1)
        else:
            host_no_port = host

        parts = host_no_port.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            slug = f"host_{parts[-1]}"
            if port and port not in ("80", "443"):
                slug += f"_{port}"
            display = f"Server {host}"
            return slug, display

        if len(parts) >= 2:
            domain_part = parts[-2]
            if domain_part in ("co", "com", "net", "org", "io", "ai", "ru", "app", "dev") and len(parts) >= 3:
                domain_part = parts[-3]
        else:
            domain_part = parts[0]

        slug = re.sub(r"[^a-zA-Z0-9_]", "_", domain_part.lower())
        if port and port not in ("80", "443"):
            slug = f"{slug}_{port}"

        if slug in PROVIDER_PRESETS:
            return slug, PROVIDER_PRESETS[slug]["name"]

        display = domain_part.replace("-", " ").title()
        if port and port not in ("80", "443"):
            display += f" ({port})"
        return slug or "custom", display or "Custom Provider"
    except Exception:
        return "custom", "Custom Provider"


@dataclass
class OpenAIProvider:
    id: str
    name: str
    base_url: str
    api_key: str
    default_model: str = ""
    models: list[str] = field(default_factory=list)
    is_active: bool = False
    added_at: str = ""
    last_used_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "default_model": self.default_model,
            "models": self.models,
            "is_active": self.is_active,
            "added_at": self.added_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OpenAIProvider:
        return cls(
            id=d.get("id", ""),
            name=d.get("name") or d.get("id", ""),
            base_url=normalize_base_url(d.get("base_url", "")),
            api_key=d.get("api_key", ""),
            default_model=d.get("default_model", ""),
            models=list(d.get("models") or []),
            is_active=bool(d.get("is_active", False)),
            added_at=d.get("added_at", ""),
            last_used_at=d.get("last_used_at", ""),
        )

    @property
    def masked_key(self) -> str:
        if not self.api_key:
            return "(нет ключа)"
        if len(self.api_key) <= 8:
            return "***"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


def _get_storage_path(custom_path: Path | None = None) -> Path:
    p = custom_path or _DEFAULT_STORAGE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_providers(storage_path: Path | None = None) -> list[OpenAIProvider]:
    path = _get_storage_path(storage_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [OpenAIProvider.from_dict(item) for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            provs = data.get("providers", [])
            return [OpenAIProvider.from_dict(item) for item in provs if isinstance(item, dict)]
    except Exception as exc:
        LOG.warning("Failed to load openai providers from %s: %s", path, exc)
    return []


def save_providers(providers: list[OpenAIProvider], storage_path: Path | None = None) -> None:
    path = _get_storage_path(storage_path)
    payload = [p.to_dict() for p in providers]
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)


def get_active_provider(storage_path: Path | None = None) -> OpenAIProvider | None:
    providers = load_providers(storage_path)
    for p in providers:
        if p.is_active:
            return p
    return providers[0] if providers else None


def get_provider(provider_id: str, storage_path: Path | None = None) -> OpenAIProvider | None:
    for p in load_providers(storage_path):
        if p.id.lower() == provider_id.lower():
            return p
    return None


def save_provider(
    provider: OpenAIProvider,
    make_active: bool = True,
    storage_path: Path | None = None,
) -> OpenAIProvider:
    providers = load_providers(storage_path)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not provider.added_at:
        provider.added_at = now_iso

    updated = False
    for i, p in enumerate(providers):
        if p.id.lower() == provider.id.lower():
            if make_active:
                provider.is_active = True
            providers[i] = provider
            updated = True
            break

    if not updated:
        if make_active or not providers:
            provider.is_active = True
        providers.append(provider)

    if provider.is_active:
        for p in providers:
            if p.id.lower() != provider.id.lower():
                p.is_active = False

    save_providers(providers, storage_path)
    return provider


def set_active_provider(provider_id: str, storage_path: Path | None = None) -> bool:
    providers = load_providers(storage_path)
    found = False
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for p in providers:
        if p.id.lower() == provider_id.lower():
            p.is_active = True
            p.last_used_at = now_iso
            found = True
        else:
            p.is_active = False

    if found:
        save_providers(providers, storage_path)
    return found


def delete_provider(provider_id: str, storage_path: Path | None = None) -> bool:
    providers = load_providers(storage_path)
    remaining = [p for p in providers if p.id.lower() != provider_id.lower()]
    if len(remaining) == len(providers):
        return False

    # If deleted provider was active, make first remaining active
    if remaining and not any(p.is_active for p in remaining):
        remaining[0].is_active = True

    save_providers(remaining, storage_path)
    return True


def update_provider_url(
    new_url: str,
    provider_id: str = "",
    storage_path: Path | None = None,
) -> OpenAIProvider:
    """Update base_url of specified or active provider, or create one."""
    norm_url = normalize_base_url(new_url)
    prov = get_provider(provider_id, storage_path) if provider_id else get_active_provider(storage_path)
    if not prov:
        slug, display = derive_provider_from_url(norm_url)
        prov = OpenAIProvider(
            id=slug,
            name=display,
            base_url=norm_url,
            api_key="",
            default_model="default",
            is_active=True,
        )
    else:
        prov.base_url = norm_url
    return save_provider(prov, make_active=prov.is_active, storage_path=storage_path)


def update_provider_key(
    new_key: str,
    provider_id: str = "",
    storage_path: Path | None = None,
) -> OpenAIProvider:
    """Update api_key of specified or active provider."""
    prov = get_provider(provider_id, storage_path) if provider_id else get_active_provider(storage_path)
    if not prov:
        prov = OpenAIProvider(
            id="cheapai",
            name="CheapAI",
            base_url="https://api.cheapai.io/v1",
            api_key=new_key.strip(),
            default_model="gpt-4o",
            is_active=True,
        )
    else:
        prov.api_key = new_key.strip()
    return save_provider(prov, make_active=prov.is_active, storage_path=storage_path)


def update_provider_model(
    new_model: str,
    provider_id: str = "",
    storage_path: Path | None = None,
) -> OpenAIProvider | None:
    """Update default_model of specified or active provider."""
    prov = get_provider(provider_id, storage_path) if provider_id else get_active_provider(storage_path)
    if not prov:
        return None
    cleaned = new_model.strip()
    prov.default_model = cleaned
    if cleaned and cleaned not in prov.models:
        prov.models.append(cleaned)
    return save_provider(prov, make_active=prov.is_active, storage_path=storage_path)


async def fetch_provider_models(
    provider: OpenAIProvider,
    timeout: float = 6.0,
    storage_path: Path | None = None,
) -> list[str]:
    """Discover models from provider GET {base_url}/models."""
    url = f"{provider.base_url.rstrip('/')}/models"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "User-Agent": "agent-tg-bridge/0.2.0",
    }
    models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data") or data.get("models") or []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            mid = str(item.get("id") or item.get("name") or "")
                            if mid and mid not in models:
                                models.append(mid)
                        elif isinstance(item, str) and item not in models:
                            models.append(item)
    except Exception as exc:
        LOG.debug("fetch_provider_models failed for %s: %s", provider.id, exc)

    if models:
        provider.models = models
        save_provider(provider, make_active=provider.is_active, storage_path=storage_path)
    return models
