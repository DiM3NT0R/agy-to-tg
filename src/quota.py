"""Quota and rate limit inspection for Antigravity models (Claude, GPT, Gemini)."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

LOG = logging.getLogger("antigravity_telegram_bridge")

QUOTA_SUMMARY_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
MODELS_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
TOKEN_FILE_PATH = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

_QUOTA_CACHE: tuple[float, QuotaSummary] | None = None
_CACHE_TTL_SECONDS = 30.0


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


async def _fetch_from_cli(bin_path: str) -> QuotaSummary | None:
    """Fetch complete quota summary using `agy -p '/quota' --output-format json`."""
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path,
            "-p",
            "/quota",
            "--output-format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode())
        cmd_data = data.get("command", {}).get("data", {})
        raw_groups = cmd_data.get("groups")
        if not raw_groups:
            return None

        groups: list[QuotaGroup] = []
        for rg in raw_groups:
            buckets: list[QuotaBucket] = []
            for b in rg.get("buckets") or []:
                buckets.append(
                    QuotaBucket(
                        bucket_id=str(b.get("id") or ""),
                        display_name=str(b.get("name") or ""),
                        window=str(b.get("window") or ""),
                        reset_time=str(b.get("reset_time") or ""),
                        remaining_fraction=float(b.get("remaining_fraction", 0.0)),
                        description=str(b.get("description") or ""),
                    )
                )
            groups.append(
                QuotaGroup(
                    display_name=str(rg.get("name") or ""),
                    description=str(rg.get("description") or ""),
                    buckets=buckets,
                )
            )

        _, email = _read_oauth_token()
        return QuotaSummary(groups=groups, user_email=email)
    except Exception as exc:
        LOG.debug("fetch_quota_summary via CLI failed: %s", exc)
        return None


async def fetch_quota_summary(agy_path: str = "") -> QuotaSummary:
    """Fetch real-time quota limits from Antigravity CLI or directly from backend."""
    global _QUOTA_CACHE
    now = time.time()
    if _QUOTA_CACHE is not None:
        cached_time, cached_summary = _QUOTA_CACHE
        if (now - cached_time) < _CACHE_TTL_SECONDS and not cached_summary.error:
            return cached_summary

    bin_path = _resolve_agy_bin(agy_path)

    # 1. Primary: fetch official structured quota from agy CLI
    cli_summary = await _fetch_from_cli(bin_path)
    if cli_summary and cli_summary.groups:
        _QUOTA_CACHE = (now, cli_summary)
        return cli_summary

    # 2. Fallback: fetch directly via HTTP API if CLI not available or timed out
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
                    description="В группе: Gemini Flash, Gemini Pro",
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

    summary = QuotaSummary(groups=groups, user_email=email)
    _QUOTA_CACHE = (now, summary)
    return summary


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
    disp = (bucket.display_name or "").lower()
    if window == "weekly" or "weekly" in b_id or "weekly" in disp:
        return "Недельный лимит"
    if window == "5h" or "5h" in b_id or "five hour" in disp:
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
        g_lower = group_title.lower()
        if g_lower == "claude and gpt models":
            group_title = "Модели Claude и GPT (3rd-Party)"
        elif g_lower == "gemini models":
            group_title = "Модели Gemini Flash и Pro (Google 1st-Party)"
        elif "argon" in g_lower:
            group_title = "Модель Gemini 4 Argon (Google 1st-Party)"

        lines.append(f"🤖 <b>{html.escape(group_title)}</b>")
        if g.description:
            desc = g.description
            if desc.startswith("Models within this group:"):
                desc = desc.replace("Models within this group:", "В группе:")
            elif "Argon quota is separate" in desc:
                desc = "Отдельная еженедельная квота для Gemini 4 Argon."
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


@dataclass(frozen=True)
class CodexQuotaSummary:
    email: str = ""
    auth_mode: str = "chatgpt"  # chatgpt | api_key | unknown
    plan_type: str = ""
    plan_display_name: str = ""
    has_active_subscription: bool = False
    renews_at: str = ""
    expires_at: str = ""
    billing_currency: str = ""
    will_renew: bool = True
    account_id: str = ""
    key_masked: str = ""
    features_highlight: list[str] = field(default_factory=list)
    error: str = ""


async def fetch_codex_limits(timeout: float = 6.0) -> CodexQuotaSummary:
    """Fetch real-time subscription status and rate limits for active OpenAI Codex account."""
    try:
        from src.codex_accounts import read_active_auth_data, extract_jwt_claims
    except Exception as exc:
        return CodexQuotaSummary(error=f"Не удалось загрузить модуль аккаунтов: {exc}")

    auth_data = read_active_auth_data()
    if not auth_data:
        return CodexQuotaSummary(error="Codex не авторизован. Подключите аккаунт через /auth.")

    auth_mode = auth_data.get("auth_mode", "unknown")
    if auth_mode == "api_key":
        key = str(auth_data.get("OPENAI_API_KEY") or "")
        masked = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
        return CodexQuotaSummary(
            auth_mode="api_key",
            key_masked=masked,
            plan_display_name="API Key (Pay-as-you-go)",
            has_active_subscription=True,
        )

    tokens = auth_data.get("tokens") or {}
    id_token = str(tokens.get("id_token") or "")
    access_token = str(tokens.get("access_token") or "")
    account_id = str(tokens.get("account_id") or "")

    claims = extract_jwt_claims(id_token) if id_token else {}
    email = str(claims.get("email") or "")
    auth_claim = claims.get("https://api.openai.com/auth") or {}
    baseline_plan = str(auth_claim.get("chatgpt_plan_type") or "plus")
    baseline_until = str(auth_claim.get("chatgpt_subscription_active_until") or "")
    if not account_id and auth_claim.get("chatgpt_account_id"):
        account_id = str(auth_claim.get("chatgpt_account_id"))

    cqs = CodexQuotaSummary(
        email=email,
        auth_mode="chatgpt",
        plan_type=baseline_plan,
        plan_display_name=baseline_plan.capitalize(),
        has_active_subscription=bool(baseline_until),
        renews_at=baseline_until,
        expires_at=baseline_until,
        account_id=account_id,
    )

    if not access_token:
        return cqs

    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                accounts_dict = data.get("accounts") or {}
                acc_info = accounts_dict.get(account_id) or (list(accounts_dict.values())[0] if accounts_dict else {})
                acc_obj = acc_info.get("account") or {}
                entitlement = acc_info.get("entitlement") or {}
                last_sub = acc_info.get("last_active_subscription") or {}
                raw_features = acc_info.get("features") or []

                wanted_feat = ["o3", "o3-mini", "o4_mini", "gpt5", "canvas", "voice_advanced_ga"]
                highlights = [f for f in wanted_feat if f in raw_features]

                return CodexQuotaSummary(
                    email=email,
                    auth_mode="chatgpt",
                    plan_type=acc_obj.get("plan_type") or baseline_plan,
                    plan_display_name=acc_obj.get("plan_display_name") or baseline_plan.capitalize(),
                    has_active_subscription=bool(entitlement.get("has_active_subscription", True)),
                    renews_at=entitlement.get("renews_at") or baseline_until,
                    expires_at=entitlement.get("expires_at") or baseline_until,
                    billing_currency=entitlement.get("billing_currency") or "",
                    will_renew=bool(last_sub.get("will_renew", True)),
                    account_id=account_id,
                    features_highlight=highlights,
                )
    except Exception as exc:
        LOG.debug("Live accounts/check query error: %s, falling back to JWT baseline", exc)

    return cqs


def render_codex_limits_card(cqs: CodexQuotaSummary) -> str:
    """Format CodexQuotaSummary into Telegram HTML message."""
    if cqs.error:
        return f"⚠️ <b>Проверка лимитов OpenAI Codex:</b>\n{html.escape(cqs.error)}"

    lines = ["📊 <b>Лимиты и подписка OpenAI Codex</b>"]
    if cqs.email:
        lines.append(f"👤 Аккаунт: <code>{html.escape(cqs.email)}</code>")
    lines.append("───────────────")

    if cqs.auth_mode == "api_key":
        lines.append("🔑 <b>Режим авторизации:</b> OpenAI API Key")
        lines.append(f"▫️ Ключ: <code>{html.escape(cqs.key_masked)}</code>")
        lines.append("💳 <b>Тарификация:</b> Pay-as-you-go (по потребленным токенам)")
        lines.append("ℹ️ Скоростные лимиты определяются тиром вашего OpenAI аккаунта (Tier 1-5).")
    else:
        plan_name = cqs.plan_display_name or cqs.plan_type.capitalize() or "Plus"
        status_icon = "🟢" if cqs.has_active_subscription else "⚪️"
        status_text = "Активна" if cqs.has_active_subscription else "Неактивна"

        lines.append(f"⚡️ <b>План:</b> ChatGPT <b>{html.escape(plan_name)}</b> {status_icon} <i>({status_text})</i>")

        target_date = cqs.renews_at or cqs.expires_at
        if target_date:
            time_left_str = _format_time_left(target_date)
            lines.append(f"▫️ <b>Следующее продление:</b> {time_left_str}")
            if cqs.billing_currency:
                lines.append(f"▫️ <b>Валюта подписки:</b> <code>{html.escape(cqs.billing_currency)}</code>")

        lines.append(f"\n🤖 <b>Лимиты моделей подписки {html.escape(plan_name)}:</b>")
        if cqs.plan_type.lower() in ("pro", "team", "enterprise"):
            lines.append("▫️ <b>o3 / o1 / GPT-6 Astra:</b> Неограниченный доступ (High reasoning)")
            lines.append("▫️ <b>GPT-6 Sol / GPT-5:</b> Без ограничений")
            lines.append("▫️ <b>Advanced Voice Mode:</b> Включен (безлимитный аудио-стриминг)")
        else:
            lines.append("▫️ <b>o3 / o1 Reasoning:</b> ~50-100 запросов в неделю (скользящий лимит)")
            lines.append("▫️ <b>o3-mini / o4-mini:</b> Высокая квота (~150 сообщений в день)")
            lines.append("▫️ <b>GPT-6 Sol / GPT-5:</b> До 80 сообщений каждые 3 часа")
            lines.append("▫️ <b>Advanced Voice Mode:</b> Включен (стандартная квота)")

        if cqs.features_highlight:
            feat_str = ", ".join(cqs.features_highlight)
            lines.append(f"\n✨ <i>Включенные возможности: {html.escape(feat_str)}</i>")

    return "\n".join(lines).strip()


def render_openai_provider_limits_card(provider: Any) -> str:
    """Format OpenAI-compatible provider info for /limits."""
    lines = [
        f"📊 <b>Статус API Провайдера: {html.escape(provider.name)}</b>",
        f"🌐 <b>Base URL:</b> <code>{html.escape(provider.base_url)}</code>",
        f"🔑 <b>API Ключ:</b> <code>{html.escape(provider.masked_key)}</code>",
        "───────────────",
        f"🤖 <b>Текущая модель:</b> <code>{html.escape(provider.default_model or 'default')}</code>",
    ]
    if getattr(provider, "models", None):
        lines.append(f"📚 <b>Обнаружено моделей на сервере:</b> {len(provider.models)}")
    lines.append("💳 <b>Тарификация:</b> Баланс на стороне провайдера (pay-as-you-go / подписка).")
    lines.append("\n💡 <i>Для смены моделей используйте /provider models или /model</i>")
    return "\n".join(lines).strip()

