"""Voice support: Speech-to-Text (Groq Whisper) and Neural Text-to-Speech synthesis."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from collections import OrderedDict
from typing import Any

import httpx

LOG = logging.getLogger("antigravity_telegram_bridge")

DEFAULT_GROQ_STT_MODEL = "whisper-large-v3"
GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
YANDEX_TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_DEFAULT_VOICE_ID = "069ff31a-5524-4945-a403-f746ee617507"  # Alexei (Russian male)
GOOGLE_TTS_URL = "https://translate.google.com/translate_tts"

# Keep last 200 bot replies in memory so tapping "🔊 Озвучить" can synthesize them
_MAX_CACHED_REPLIES = 200
_REPLY_TEXT_CACHE: OrderedDict[tuple[int, int], str] = OrderedDict()


def cache_reply_text(chat_id: int, message_id: int, text: str) -> None:
    """Store reply text for a sent bot message so it can be synthesized on demand."""
    if not text or not message_id:
        return
    key = (chat_id, message_id)
    _REPLY_TEXT_CACHE[key] = text
    _REPLY_TEXT_CACHE.move_to_end(key)
    while len(_REPLY_TEXT_CACHE) > _MAX_CACHED_REPLIES:
        _REPLY_TEXT_CACHE.popitem(last=False)


def get_cached_reply_text(chat_id: int, message_id: int) -> str | None:
    """Retrieve cached reply text for a message."""
    return _REPLY_TEXT_CACHE.get((chat_id, message_id))


def strip_markdown_for_speech(text: str) -> str:
    """Clean Markdown, code blocks, URLs, and formatting symbols for natural TTS reading."""
    if not text:
        return ""
    # Replace fenced code blocks with brief notice or omit
    cleaned = re.sub(r"```[a-zA-Z0-9_+-]*\n.*?```", " [блок кода] ", text, flags=re.DOTALL)
    # Extract link labels: [label](url) -> label
    cleaned = re.sub(r"\[([^\]\n]+)\]\([^)\n]+\)", r"\1", cleaned)
    # Remove raw URLs
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    # Remove HTML tags if any
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    # Remove markdown formatting characters (*, _, `, #, ~, >, |)
    cleaned = re.sub(r"[*_`#~>|]", " ", cleaned)
    # Remove markdown table separator lines
    cleaned = re.sub(r"(?m)^[ \t]*[:\-| \t]{3,}$", " ", cleaned)
    # Collapse whitespace
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{2,}", ". ", cleaned)
    cleaned = cleaned.replace("\n", ". ")
    cleaned = re.sub(r"\.\s*\.", ".", cleaned)
    return cleaned.strip()


def _split_tts_chunks(text: str, max_chars: int = 180) -> list[str]:
    """Split text into sentence/phrase chunks <= max_chars for TTS requests."""
    cleaned = strip_markdown_for_speech(text)
    if not cleaned:
        return []
    # Cap total length at ~4000 chars
    if len(cleaned) > 4000:
        cleaned = cleaned[:4000] + "..."

    chunks: list[str] = []
    remaining = cleaned
    while remaining:
        remaining = remaining.strip()
        if not remaining:
            break
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        # Prefer splitting at sentence end (. ! ? ;), then comma, then space
        cut = -1
        for sep in (". ", "! ", "? ", "; ", ", ", " "):
            pos = remaining.rfind(sep, 0, max_chars)
            if pos > max_chars // 3:
                cut = pos + (1 if sep != " " else 0)
                break
        if cut == -1:
            cut = max_chars
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()
    return chunks


async def transcribe_voice_groq(
    audio_bytes: bytes,
    api_key: str,
    *,
    filename: str = "voice.ogg",
    language: str = "ru",
    model: str = DEFAULT_GROQ_STT_MODEL,
) -> str:
    """Transcribe voice/audio bytes via Groq Whisper API."""
    if not api_key:
        raise RuntimeError("Groq API key is not configured (groq_api_key)")
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": (filename, audio_bytes, "audio/ogg")}
    data: dict[str, Any] = {
        "model": model,
        "temperature": "0",
    }
    if language:
        data["language"] = language

    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as client:
        resp = await client.post(GROQ_TRANSCRIPTIONS_URL, headers=headers, data=data, files=files)
        if resp.status_code != 200:
            raise RuntimeError(f"Groq STT HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        return str(payload.get("text") or "").strip()


VOICE_INTENTS: dict[str, str] = {
    # limits
    "лимиты": "/limits",
    "лимит": "/limits",
    "покажи лимиты": "/limits",
    "какие лимиты": "/limits",
    "квота": "/limits",
    "квоты": "/limits",
    "проверь лимиты": "/limits",
    "проверь квоту": "/limits",
    # status
    "статус": "/status",
    "покажи статус": "/status",
    "статус бота": "/status",
    "инфо": "/status",
    "информация": "/status",
    # reset
    "сбрось сессию": "/reset",
    "сброс сессии": "/reset",
    "сброс": "/reset",
    "очисти сессию": "/reset",
    "новый диалог": "/reset",
    "новая сессия": "/reset",
    "чистая сессия": "/reset",
    # compact
    "сжатие": "/compact",
    "сожми сессию": "/compact",
    "сожми контекст": "/compact",
    "компакт": "/compact",
    # files
    "файлы": "/files",
    "покажи файлы": "/files",
    "список файлов": "/files",
    # diff
    "дифф": "/diff",
    "покажи дифф": "/diff",
    "изменения": "/diff",
    "изменения в гите": "/diff",
    # settings
    "настройки": "/settings",
    "открой настройки": "/settings",
    # voice
    "голос": "/voice",
    "настройки голоса": "/voice",
    # pwd
    "рабочая папка": "/pwd",
    "где я": "/pwd",
    "путь": "/pwd",
    # stop
    "стоп": "/stop",
    "остановись": "/stop",
    "хватит": "/stop",
    # restart
    "перезапуск": "/restart",
    "перезагрузка": "/restart",
    "перезагрузи": "/restart",
    "перезагрузи мост": "/restart",
    "рестарт": "/restart",
}


def map_voice_intent(text: str) -> str | None:
    """Map spoken phrases like 'покажи лимиты' directly to slash commands like '/limits'."""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("/"):
        return raw
    clean = re.sub(r"[.!?,]+$", "", raw).strip().lower()
    if clean.startswith("слеш ") or clean.startswith("слэш "):
        cmd_part = clean.split(maxsplit=1)[1].strip()
        return f"/{cmd_part}"
    return VOICE_INTENTS.get(clean)


def _convert_mp3_to_ogg_opus(mp3_bytes: bytes) -> bytes | None:
    """Convert MP3 bytes to OGG Opus using ffmpeg if installed on the system."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return None
    try:
        res = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-vbr",
                "on",
                "-f",
                "ogg",
                "pipe:1",
            ],
            input=mp3_bytes,
            capture_output=True,
            timeout=20.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout:
            return res.stdout
    except Exception as exc:
        LOG.warning("ffmpeg mp3->ogg conversion failed: %s", exc)
    return None


async def _synthesize_yandex(
    text: str, api_key: str, *, voice: str = "filipp", speed: str = "1.1"
) -> tuple[bytes, str]:
    """Synthesize speech using Yandex SpeechKit Neural TTS (returns native OGG Opus)."""
    chunks = _split_tts_chunks(text, max_chars=4500)
    if not chunks:
        raise ValueError("Нет текста для озвучивания")
    headers = {"Authorization": f"Api-Key {api_key}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
        # Use first chunk up to 4500 chars so Telegram gets a single valid OGG Opus container with waveform
        data = {
            "text": chunks[0],
            "lang": "ru-RU",
            "voice": voice or "filipp",
            "format": "oggopus",
            "speed": speed or "1.1",
        }
        resp = await client.post(YANDEX_TTS_URL, headers=headers, data=data)
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"Yandex TTS HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.content, "audio/ogg"


async def _synthesize_cartesia(
    text: str, api_key: str, *, voice_id: str = CARTESIA_DEFAULT_VOICE_ID, speed: str = "1.0"
) -> tuple[bytes, str]:
    """Synthesize speech using Cartesia Sonic 3.5 Neural TTS."""
    chunks = _split_tts_chunks(text, max_chars=2500)
    if not chunks:
        raise ValueError("Нет текста для озвучивания")
    headers = {
        "Cartesia-Version": "2024-06-10",
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "model_id": "sonic-3.5",
        "transcript": chunks[0],
        "voice": {
            "mode": "id",
            "id": voice_id or CARTESIA_DEFAULT_VOICE_ID,
        },
        "output_format": {
            "container": "mp3",
            "encoding": "mp3",
            "sample_rate": 44100,
        },
        "language": "ru",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
        resp = await client.post(CARTESIA_TTS_URL, headers=headers, json=payload)
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"Cartesia TTS HTTP {resp.status_code}: {resp.text[:200]}")
        mp3_data = resp.content
        ogg_data = await asyncio.to_thread(_convert_mp3_to_ogg_opus, mp3_data)
        if ogg_data:
            return ogg_data, "audio/ogg"
        return mp3_data, "audio/mpeg"


async def synthesize_speech(
    text: str,
    *,
    lang: str = "ru",
    voice: str = "filipp",
    speed: str = "1.1",
    yandex_api_key: str = "",
    cartesia_api_key: str = "",
) -> tuple[bytes, str]:
    """Synthesize speech audio from text.

    Priority:
    1. Yandex SpeechKit Neural (`filipp`, native `oggopus` with Telegram waveform)
    2. Cartesia Sonic-3.5 Neural (`Alexei`)
    3. Google TTS fallback
    """
    if yandex_api_key:
        try:
            return await _synthesize_yandex(text, yandex_api_key, voice=voice, speed=speed)
        except Exception as exc:
            LOG.warning("Yandex TTS failed, trying next provider: %s", exc)

    if cartesia_api_key:
        try:
            return await _synthesize_cartesia(text, cartesia_api_key, speed=speed)
        except Exception as exc:
            LOG.warning("Cartesia TTS failed, falling back to Google TTS: %s", exc)
        try:
            return await _synthesize_cartesia(text, cartesia_api_key)
        except Exception as exc:
            LOG.warning("Cartesia TTS failed, falling back to Google TTS: %s", exc)

    chunks = _split_tts_chunks(text, max_chars=180)
    if not chunks:
        raise ValueError("Нет текста для озвучивания")

    mp3_parts: list[bytes] = []
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers=headers) as client:
        for chunk in chunks:
            params = {
                "ie": "UTF-8",
                "q": chunk,
                "tl": lang,
                "client": "tw-ob",
            }
            resp = await client.get(GOOGLE_TTS_URL, params=params)
            if resp.status_code != 200 or not resp.content:
                raise RuntimeError(f"TTS HTTP {resp.status_code}")
            mp3_parts.append(resp.content)

    mp3_data = b"".join(mp3_parts)
    ogg_data = await asyncio.to_thread(_convert_mp3_to_ogg_opus, mp3_data)
    if ogg_data:
        return ogg_data, "audio/ogg"
    return mp3_data, "audio/mpeg"
