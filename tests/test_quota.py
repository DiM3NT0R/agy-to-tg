"""Unit tests for quota inspection and visual progress rendering."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import httpx

from src.quota import (
    QuotaBucket,
    QuotaGroup,
    QuotaSummary,
    _format_progress_bar,
    _format_time_left,
    _translate_bucket_name,
    fetch_quota_summary,
    render_limits_card,
)


def test_format_progress_bar() -> None:
    assert "100%" in _format_progress_bar(1.0)
    assert "🟢" in _format_progress_bar(1.0)
    assert "██████████" in _format_progress_bar(1.0)

    assert "50%" in _format_progress_bar(0.5)
    assert "🟢" in _format_progress_bar(0.5)

    assert "20%" in _format_progress_bar(0.2)
    assert "🟡" in _format_progress_bar(0.2)

    assert "5%" in _format_progress_bar(0.05)
    assert "🔴" in _format_progress_bar(0.05)

    # Clamping boundaries
    assert "0%" in _format_progress_bar(-0.5)
    assert "100%" in _format_progress_bar(1.5)


def test_format_time_left() -> None:
    assert _format_time_left("") == "—"
    # Past timestamp
    past_iso = "2020-01-01T00:00:00Z"
    assert "сейчас" in _format_time_left(past_iso)

    # Far future timestamp
    future_iso = "2099-10-30T10:00:00Z"
    res = _format_time_left(future_iso)
    assert "д" in res or "ч" in res
    assert "UTC" in res


def test_translate_bucket_name() -> None:
    b1 = QuotaBucket("3p-weekly", "Weekly Limit Remaining", "weekly", "", 1.0, "")
    assert "Недельный" in _translate_bucket_name(b1)

    b2 = QuotaBucket("3p-5h", "Five Hour Limit Remaining", "5h", "", 1.0, "")
    assert "5-часовой" in _translate_bucket_name(b2)

    b3 = QuotaBucket("custom", "Custom Bucket", "custom", "", 1.0, "")
    assert _translate_bucket_name(b3) == "Custom Bucket"


def test_render_limits_card() -> None:
    # Error case
    qs_err = QuotaSummary(groups=[], error="Test error occurred")
    text_err = render_limits_card(qs_err)
    assert "⚠️" in text_err
    assert "Test error occurred" in text_err

    # Empty groups
    qs_empty = QuotaSummary(groups=[], user_email="test@example.com")
    text_empty = render_limits_card(qs_empty)
    assert "test@example.com" in text_empty
    assert "не лимитирована" in text_empty

    # With groups and buckets
    qs = QuotaSummary(
        groups=[
            QuotaGroup(
                display_name="Claude and GPT models",
                description="Models within this group: Claude Sonnet",
                buckets=[
                    QuotaBucket(
                        bucket_id="3p-weekly",
                        display_name="Weekly Limit Remaining",
                        window="weekly",
                        reset_time="2099-10-30T07:58:32Z",
                        remaining_fraction=0.75,
                        description="",
                    )
                ],
            )
        ],
        user_email="user@test.com",
    )
    card = render_limits_card(qs)
    assert "Лимиты моделей" in card
    assert "Модели Claude и GPT" in card
    assert "75%" in card
    assert "user@test.com" in card


@pytest.mark.asyncio
async def test_fetch_quota_summary_success(monkeypatch) -> None:
    monkeypatch.setattr("src.quota._read_oauth_token", lambda: ("valid_token", "test@test.com"))

    mock_resp = httpx.Response(
        200,
        json={
            "groups": [
                {
                    "displayName": "Claude and GPT models",
                    "description": "test desc",
                    "buckets": [
                        {
                            "bucketId": "3p-weekly",
                            "displayName": "Weekly Limit Remaining",
                            "window": "weekly",
                            "resetTime": "2026-10-30T07:58:32Z",
                            "remainingFraction": 0.5,
                        }
                    ],
                }
            ]
        },
        request=httpx.Request("POST", "https://example.com"),
    )

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        qs = await fetch_quota_summary()
        assert not qs.error
        assert qs.user_email == "test@test.com"
        assert len(qs.groups) == 1
        assert len(qs.groups[0].buckets) == 1
        assert qs.groups[0].buckets[0].remaining_fraction == 0.5


@pytest.mark.asyncio
async def test_fetch_quota_summary_with_gemini(monkeypatch) -> None:
    monkeypatch.setattr("src.quota._read_oauth_token", lambda: ("valid_token", "test@test.com"))

    async def mock_post(url, *args, **kwargs):
        if "retrieveUserQuotaSummary" in str(url):
            return httpx.Response(
                200,
                json={
                    "groups": [
                        {
                            "displayName": "Claude and GPT models",
                            "buckets": [
                                {
                                    "bucketId": "3p-5h",
                                    "displayName": "Five Hour Limit Remaining",
                                    "window": "5h",
                                    "resetTime": "2026-10-30T15:19:27Z",
                                    "remainingFraction": 0.55,
                                }
                            ],
                        }
                    ]
                },
                request=httpx.Request("POST", str(url)),
            )
        else:
            return httpx.Response(
                200,
                json={
                    "models": {
                        "gemini-2.5-pro": {
                            "displayName": "Gemini 2.5 Pro",
                            "quotaInfo": {
                                "remainingFraction": 0.82,
                                "resetTime": "2026-10-30T15:09:23Z",
                            },
                        }
                    }
                },
                request=httpx.Request("POST", str(url)),
            )

    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        qs = await fetch_quota_summary()
        assert not qs.error
        assert len(qs.groups) == 2
        assert qs.groups[0].display_name == "Claude and GPT models"
        assert qs.groups[1].display_name == "Модели Gemini (Google 1st-Party)"
        assert qs.groups[1].buckets[0].remaining_fraction == 0.82
        assert qs.groups[1].buckets[0].bucket_id == "gemini-rolling"

        card = render_limits_card(qs)
        assert "Модели Gemini (Google 1st-Party)" in card
        assert "82%" in card
        assert "55%" in card
