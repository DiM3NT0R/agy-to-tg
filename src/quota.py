"""Quota and rate limit inspection for Antigravity models (Claude, GPT, Gemini)."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

LOG = logging.getLogger("antigravity_telegram_bridge")

QUOTA_SUMMARY_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
MODELS_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
TOKEN_FILE_PATH = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


@dataclass(frozen=True)
class QuotaBucket:
    bucket_id: str
    display_name: str
    window: str  # "weekly", "5h", etc.
    reset_time: str
    remaining_fraction: float
    description: str


@dataclass(frozen=True)
class QuotaGroup:
    display_name: str
    description: str
    buckets: list[QuotaBucket]


@dataclass(frozen=True)
class QuotaSummary:
    groups: list[QuotaGroup]
    user_email: str = ""
    error: str = ""


def _resolve_agy_bin(agy_path: str = "") -> str:
    if agy_path:
        return agy_path
    return os.environ.get("AGY_BIN") or "agy"


def _read_oauth_token() -> tuple[str, str]:
    """Read access_token and user email from ~/.gemini/antigravity-cli/antigravity-oauth-token."""
    if not TOKEN_FILE_PATH.exists():
        return "", ""
    try:
        data = json.loads(TOKEN_FILE_PATH.read_text(encoding="utf-8"))
        email = str(data.get("id_token_email") or "")
        token_obj = data.get("token") or {}
        access_token = str(token_obj.get("access_token") or "")
        return access_token, email
    except Exception as exc:
        LOG.warning("Failed to read oauth token: %s", exc)
        return "", ""


def is_token_expired_or_missing() -> bool:
    """Return True if token file does not exist, has no access token, or expires in < 90s."""
    if not TOKEN_FILE_PATH.exists():
        return True
    try:
        data = json.loads(TOKEN_FILE_PATH.read_text(encoding="utf-8"))
        token_obj = data.get("token") or {}
        access_token = str(token_obj.get("access_token") or "")
        if not access_token:
            return True
        expiry_str = str(token_obj.get("expiry") or "")
        if not expiry_str:
            return False
        # Normalize nanoseconds to microseconds
        cleaned_iso = re.sub(r"(\.\d{6})\d+Z$", r"\1+00:00", expiry_str).replace("Z", "+00:00")
        target = datetime.fromisoformat(cleaned_iso)
        now = datetime.now(timezone.utc)
        return (target - now).total_seconds() < 90
    except Exception as exc:
        LOG.debug("is_token_expired_or_missing parse error: %s", exc)
        return False


async def _refresh_token_via_cli(agy_path: str = "") -> tuple[str, str]:
    """Run `agy models` to trigger silent OAuth refresh via the CLI if the access token expired."""
    bin_path = _resolve_agy_bin(agy_path)
    try:
        await asyncio.to_thread(
            subprocess.run,
            [bin_path, "models"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except Exception as exc:
        LOG.warning("Failed to refresh token via agy CLI (%s): %s", bin_path, exc)
    return _read_oauth_token()


async def fetch_quota_summary(agy_path: str = "") -> QuotaSummary:
    """Fetch real-time quota limits directly from Google Cloud Code Assist backend."""
    access_token, email = _read_oauth_token()
    if not access_token:
        access_token, email = await _refresh_token_via_cli(agy_path)

    if not access_token:
        return QuotaSummary(groups=[], error="Токен авторизации Antigravity не найден.")

    async def _do_requests(tok: str) -> tuple[dict[str, Any] | int, dict[str, Any] | None]:
        headers = {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "User-Agent": "antigravity-cli/1.4.8",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp_quota, resp_models = await asyncio.gather(
                client.post(QUOTA_SUMMARY_URL, headers=headers, json={}),
                client.post(MODELS_URL, headers=headers, json={}),
                return_exceptions=True,
            )
            q_res: dict[str, Any] | int = (
                resp_quota.json()
                if not isinstance(resp_quota, Exception) and resp_quota.status_code == 200
                else (getattr(resp_quota, "status_code", 500) if not isinstance(resp_quota, Exception) else 500)
            )
            m_res: dict[str, Any] | None = (
                resp_models.json()
                if not isinstance(resp_models, Exception) and resp_models.status_code == 200
                else None
            )
            return q_res, m_res

    res_quota, res_models = await _do_requests(access_token)
    if res_quota == 401 or (isinstance(res_models, int) and res_models == 401):
        # Token expired; refresh via CLI and retry once
        access_token, email = await _refresh_token_via_cli(agy_path)
        if access_token:
            res_quota, res_models = await _do_requests(access_token)

    if isinstance(res_quota, int):
        return QuotaSummary(
            groups=[],
            user_email=email,
            error=f"Ошибка API квот (HTTP {res_quota}). Попробуйте позже.",
        )

    if not isinstance(res_quota, dict):
        return QuotaSummary(
            groups=[],
            user_email=email,
            error="Неверный ответ от сервера квот.",
        )

    groups: list[QuotaGroup] = []
    for g in res_quota.get("groups") or []:
        buckets: list[QuotaBucket] = []
        for b in g.get("buckets") or []:
            buckets.append(
                QuotaBucket(
                    bucket_id=str(b.get("bucketId") or ""),
                    display_name=str(b.get("displayName") or ""),
                    window=str(b.get("window") or ""),
                    reset_time=str(b.get("resetTime") or ""),
                    remaining_fraction=float(b.get("remainingFraction", 0.0)),
                    description=str(b.get("description") or ""),
                )
            )
        groups.append(
            QuotaGroup(
                display_name=str(g.get("displayName") or ""),
                description=str(g.get("description") or ""),
                buckets=buckets,
            )
        )

    # Extract Gemini 1st-party models quota from fetchAvailableModels
    if res_models and isinstance(res_models, dict):
        models_map = res_models.get("models") or {}
        gemini_qi: dict[str, Any] | None = None
        for mid, mdata in models_map.items():
            if "gemini" in str(mid).lower():
                qi = mdata.get("quotaInfo")
                if isinstance(qi, dict) and "remainingFraction" in qi:
                    gemini_qi = qi
                    break

        if gemini_qi:
            rf = float(gemini_qi.get("remainingFraction", 1.0))
            rt = str(gemini_qi.get("resetTime") or "")
            groups.append(
                QuotaGroup(
                    display_name="Модели Gemini (Google 1st-Party)",
                    description="В группе: Gemini 4 Argon, Gemini 3.8 Flash, Pro",
                    buckets=[
                        QuotaBucket(
                            bucket_id="gemini-rolling",
                            display_name="Скользящий лимит",
                            window="rolling",
                            reset_time=rt,
                            remaining_fraction=rf,
                            description="Скользящий лимит запросов Google Gemini",
                        )
                    ],
                )
            )

    return QuotaSummary(groups=groups, user_email=email)


def _format_progress_bar(fraction: float, length: int = 10) -> str:
    """Render a visual progress bar e.g. 🟢 [██████░░░░] 60%."""
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * length)
    empty = length - filled
    pct = round(fraction * 100)
    bar = "█" * filled + "░" * empty
    if pct >= 50:
        icon = "🟢"
    elif pct >= 20:
        icon = "🟡"
    else:
        icon = "🔴"
    return f"{icon} <code>[{bar}]</code> <b>{pct}%</b>"


def _format_time_left(reset_time_iso: str) -> str:
    """Calculate and format relative and absolute time remaining until quota reset."""
    if not reset_time_iso:
        return "—"
    try:
        target = datetime.fromisoformat(reset_time_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = target - now
        total_seconds = int(diff.total_seconds())
        if total_seconds <= 0:
            return f"<b>сейчас</b> ({target.strftime('%H:%M UTC')})"
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        days = hours // 24
        hours = hours % 24
        parts = []
        if days > 0:
            parts.append(f"{days}д")
            if hours > 0:
                parts.append(f"{hours}ч")
            utc_str = target.strftime("%d.%m %H:%M UTC")
        else:
            if hours > 0:
                parts.append(f"{hours}ч")
            if minutes > 0 or not parts:
                parts.append(f"{minutes}мин")
            utc_str = target.strftime("в %H:%M UTC")
        time_part = " ".join(parts)
        return f"<b>{time_part}</b> ({utc_str})"
    except Exception:
        return reset_time_iso


def _translate_bucket_name(bucket: QuotaBucket) -> str:
    window = bucket.window.lower()
    b_id = bucket.bucket_id.lower()
    if window == "weekly" or "weekly" in b_id:
        return "Недельный лимит"
    if window == "5h" or "5h" in b_id:
        return "5-часовой лимит"
    if window == "rolling" or "gemini" in b_id:
        return "Скользящий лимит"
    return bucket.display_name or bucket.bucket_id


def render_limits_card(qs: QuotaSummary) -> str:
    """Format QuotaSummary into Telegram HTML message."""
    if qs.error:
        return f"⚠️ <b>Проверка лимитов:</b>\n{html.escape(qs.error)}"

    lines: list[str] = ["📊 <b>Лимиты моделей Antigravity</b>"]
    if qs.user_email:
        lines.append(f"👤 Аккаунт: <code>{html.escape(qs.user_email)}</code>")
    lines.append("───────────────")

    if not qs.groups:
        lines.append("ℹ️ Нет активных ограничений по группам моделей (квота не лимитирована).")
        return "\n".join(lines)

    for i, g in enumerate(qs.groups):
        if i > 0:
            lines.append("───────────────")
        group_title = g.display_name
        if group_title.lower() == "claude and gpt models":
            group_title = "Модели Claude и GPT (3rd-Party)"
        lines.append(f"🤖 <b>{html.escape(group_title)}</b>")
        if g.description:
            desc = g.description
            if desc.startswith("Models within this group:"):
                desc = desc.replace("Models within this group:", "В группе:")
            lines.append(f"<i>{html.escape(desc)}</i>\n")
        else:
            lines.append("")

        for b in g.buckets:
            name = _translate_bucket_name(b)
            bar = _format_progress_bar(b.remaining_fraction)
            time_left = _format_time_left(b.reset_time)
            lines.append(f"▫️ <b>{html.escape(name)}:</b>")
            lines.append(bar)
            lines.append(f"⏳ Сброс: {time_left}\n")

    return "\n".join(lines).strip()

