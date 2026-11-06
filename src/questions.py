"""Interactive question options extractor and inline keyboard builder."""
from __future__ import annotations

import re
import time
import uuid

_QUESTION_CACHE: dict[str, tuple[float, list[str]]] = {}
_MAX_CACHE_SIZE = 1000
_CACHE_TTL_S = 86400  # 24 hours


def extract_question_options(text: str) -> list[str]:
    """Extract interactive quick-reply options if the text ends with a multiple-choice question."""
    if not text:
        return []

    cleaned = text.strip()
    lines = [line.strip() for line in cleaned.split("\n") if line.strip()]
    if not lines:
        return []

    options: list[str] = []
    # Matches bullet points: •, -, *, 1., 2., 1), 2), a), b), A., B., [ ], [x]
    opt_re = re.compile(r"^(?:[•\-*]|(?:\d+|[a-zA-Z])[\.)]|\[[ xX]?\])\s+(.+)$")

    idx = len(lines) - 1
    while idx >= 0:
        line = lines[idx]
        m = opt_re.match(line)
        if m:
            opt_text = m.group(1).strip()
            # Clean markdown formatting like **bold** or `code`
            opt_text = re.sub(r"[*_`~]", "", opt_text).strip()
            # Remove trailing punctuation like semicolons or periods if short
            opt_text = opt_text.rstrip(";.")
            if not opt_text or len(opt_text) > 60:
                break
            options.insert(0, opt_text)
            idx -= 1
        else:
            break

    # We want 2 to 8 options
    if len(options) < 2 or len(options) > 8:
        return []

    # Check if preceding line contains a question mark or choice indicator
    if idx >= 0:
        preceding = lines[idx].lower()
        if "?" in preceding or any(kw in preceding for kw in (
            "выберите", "выбери", "вариант", "уточните", "уточни",
            "какой", "какое", "какая", "какие", "напиши", "подскажи",
            "или", "choose", "select", "option", "which"
        )):
            return options

    return []


def store_question_options(options: list[str]) -> str:
    """Store options in memory cache with unique short ID and return ID."""
    now = time.time()
    if len(_QUESTION_CACHE) > _MAX_CACHE_SIZE:
        expired = [k for k, (ts, _) in _QUESTION_CACHE.items() if now - ts > _CACHE_TTL_S]
        for k in expired:
            _QUESTION_CACHE.pop(k, None)
        if len(_QUESTION_CACHE) > _MAX_CACHE_SIZE:
            oldest = sorted(_QUESTION_CACHE.items(), key=lambda x: x[1][0])[:200]
            for k, _ in oldest:
                _QUESTION_CACHE.pop(k, None)

    cache_id = uuid.uuid4().hex[:8]
    _QUESTION_CACHE[cache_id] = (now, options)
    return cache_id


def get_question_options(cache_id: str) -> list[str] | None:
    """Retrieve options by cache ID if present and not expired."""
    entry = _QUESTION_CACHE.get(cache_id)
    if not entry:
        return None
    ts, options = entry
    if time.time() - ts > _CACHE_TTL_S:
        _QUESTION_CACHE.pop(cache_id, None)
        return None
    return options


def build_options_keyboard(options: list[str], prefix: str = "opt") -> list[list[dict[str, str]]]:
    """Build inline keyboard buttons for question options."""
    if not options:
        return []
    cid = store_question_options(options)
    rows: list[list[dict[str, str]]] = []
    # 2 columns if options are short and <= 4 options
    if len(options) <= 4 and all(len(o) <= 18 for o in options):
        for i in range(0, len(options), 2):
            row = [{"text": options[i], "callback_data": f"{prefix}:{cid}:{i}"}]
            if i + 1 < len(options):
                row.append({"text": options[i + 1], "callback_data": f"{prefix}:{cid}:{i + 1}"})
            rows.append(row)
    else:
        for i, opt in enumerate(options):
            rows.append([{"text": opt, "callback_data": f"{prefix}:{cid}:{i}"}])
    return rows
