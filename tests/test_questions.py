"""Tests for interactive question options extraction and keyboard building."""
from __future__ import annotations

import time

from src.questions import (
    build_options_keyboard,
    extract_question_options,
    get_question_options,
    store_question_options,
)


def test_extract_question_options_bullets():
    text = (
        "На телефоне VPN сбоил через домашний Wi-Fi или через мобильный интернет?\n"
        "• Домашний Wi-Fi\n"
        "• Мобильный интернет\n"
        "• В обеих сетях"
    )
    opts = extract_question_options(text)
    assert opts == ["Домашний Wi-Fi", "Мобильный интернет", "В обеих сетях"]


def test_extract_question_options_numbered():
    text = (
        "Что сделать дальше?\n"
        "1. Перезапустить службу\n"
        "2. Проверить порты\n"
        "3. Оставить как есть"
    )
    opts = extract_question_options(text)
    assert opts == ["Перезапустить службу", "Проверить порты", "Оставить как есть"]


def test_extract_question_options_markdown_formatting():
    text = (
        "Какую модель выбрать?\n"
        "- `gemini-3.8-flash-high`\n"
        "- `gpt-6.1-sol`\n"
        "- `claude-opus-5-5-high`"
    )
    opts = extract_question_options(text)
    assert opts == ["gemini-3.8-flash-high", "gpt-6.1-sol", "claude-opus-5-5-high"]


def test_extract_question_options_not_a_question():
    text = (
        "Список выполненных шагов:\n"
        "• Обновили конфиг\n"
        "• Проверили соединение\n"
        "Все тесты прошли успешно."
    )
    assert extract_question_options(text) == []


def test_extract_question_options_too_few_or_too_many():
    # Only 1 option -> not multiple choice
    text = "Что выберем?\n• Единственный вариант"
    assert extract_question_options(text) == []

    # > 8 options -> too many for inline buttons
    text_many = "Выберите число:\n" + "\n".join(f"• Вариант {i}" for i in range(10))
    assert extract_question_options(text_many) == []


def test_store_and_get_question_options():
    opts = ["Вариант А", "Вариант Б"]
    cid = store_question_options(opts)
    assert isinstance(cid, str) and len(cid) == 8
    retrieved = get_question_options(cid)
    assert retrieved == opts


def test_build_options_keyboard_short_two_columns():
    opts = ["Direct", "Web"]
    kb = build_options_keyboard(opts, prefix="opt")
    # Should pack 2 short options in 1 row
    assert len(kb) == 1
    assert len(kb[0]) == 2
    assert kb[0][0]["text"] == "Direct"
    assert kb[0][0]["callback_data"].startswith("opt:")


def test_build_options_keyboard_long_single_column():
    opts = [
        "Домашний Wi-Fi на большой скорости",
        "Мобильный интернет через LTE",
    ]
    kb = build_options_keyboard(opts, prefix="opt_live")
    # Long options must be 1 per row
    assert len(kb) == 2
    assert len(kb[0]) == 1
    assert kb[0][0]["text"] == opts[0]
    assert kb[0][0]["callback_data"].startswith("opt_live:")
    # Ensure callback_data is well under Telegram's 64 byte limit
    for row in kb:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64
