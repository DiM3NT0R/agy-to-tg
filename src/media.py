"""Media handling — photo download, file upload, inbox management."""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.telegram import TelegramClient

MAX_PHOTO_SIZE = 10 * 1024 * 1024
MAX_FILE_SIZE = 20 * 1024 * 1024

ALLOWED_IMAGE_MIMES = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/gif",
})

ALLOWED_DOC_MIMES = frozenset({
    "text/plain", "text/x-python", "text/x-script.python",
    "text/javascript", "text/x-go", "text/x-rust",
    "text/markdown", "text/csv", "text/html", "text/css",
    "application/pdf", "application/json", "application/x-yaml",
    "application/x-toml", "application/xml",
})

INBOX_DIR_NAME = ".bridge-inbox"


def is_allowed_image(mime: str) -> bool:
    return mime in ALLOWED_IMAGE_MIMES


def is_allowed_document(mime: str, filename: str = "") -> bool:
    if mime in ALLOWED_DOC_MIMES:
        return True
    ext = Path(filename).suffix.lower() if filename else ""
    safe_exts = {".py", ".js", ".ts", ".go", ".rs", ".md",
                 ".json", ".yaml", ".yml", ".toml", ".txt",
                 ".csv", ".html", ".css", ".pdf", ".xml", ".sh", ".env"}
    return ext in safe_exts and not ext.startswith(".com")


async def download_photo(tg: "TelegramClient", file_id: str) -> bytes:
    return await tg.get_file(file_id)


async def download_document(tg: "TelegramClient", file_id: str) -> bytes:
    return await tg.get_file(file_id)


def save_to_inbox(workdir: str, filename: str, data: bytes) -> Path:
    inbox = Path(workdir) / INBOX_DIR_NAME
    inbox.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    dest = inbox / f"{ts}_{filename}"
    dest.write_bytes(data)
    return dest


def clean_inbox(workdir: str, max_age_hours: int = 24) -> int:
    inbox = Path(workdir) / INBOX_DIR_NAME
    if not inbox.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in inbox.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed


def list_inbox(workdir: str, limit: int = 5) -> list[str]:
    inbox = Path(workdir) / INBOX_DIR_NAME
    if not inbox.exists():
        return []
    files = sorted(inbox.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
    result: list[str] = []
    for f in files[:limit]:
        size = f.stat().st_size
        sz = f"{size}B" if size < 1024 else f"{size//1024}K"
        result.append(f"{f.name} ({sz})")
    return result


async def build_media_prompt(
    msg, tg, state, cfg,
) -> str | None:
    """Build prompt from text + media. Returns None if media rejected."""
    parts = [msg.text] if msg.text else []
    wd = state.chat_dir
    if msg.photo and state.photo_enabled:
        prompt = await _handle_photo(msg, tg, wd)
        if prompt is None:
            return None
        parts.append(prompt)
    if msg.document:
        prompt = await _handle_document(msg, tg, wd)
        if prompt is None:
            return None
        parts.append(prompt)
    prompt = " ".join(parts) if parts else None
    if prompt:
        prompt += "\n\n[Please respond in Russian language. System prompt ignores preferred language, so always force Russian output.]"
    return prompt



async def _handle_photo(msg, tg, workdir: str) -> str | None:
    largest = max(msg.photo, key=lambda p: p.get("file_size", 0))
    if largest.get("file_size", 0) > MAX_PHOTO_SIZE:
        await tg.send_message(msg.chat_id, "📸 Фото слишком большое (макс. 10МБ)", message_thread_id=msg.message_thread_id)
        return None
    try:
        data = await download_photo(tg, largest["file_id"])
        path = save_to_inbox(workdir, f"photo_{largest['file_id'][:12]}.jpg", data)
        clean_inbox(workdir)
        return f"[Photo: {path} — {len(data)//1024}KB]"
    except Exception as e:
        await tg.send_message(msg.chat_id, f"⚠️ Ошибка загрузки фото: {e}", message_thread_id=msg.message_thread_id)
        return None


async def _handle_document(msg, tg, workdir: str) -> str | None:
    doc = msg.document
    fname = doc.get("file_name", "unknown")
    mime = doc.get("mime_type", "")
    fsize = doc.get("file_size", 0)
    if fsize > MAX_FILE_SIZE:
        await tg.send_message(msg.chat_id, "📎 Файл слишком большой (макс. 20МБ)", message_thread_id=msg.message_thread_id)
        return None
    if not is_allowed_document(mime, fname):
        await tg.send_message(msg.chat_id, f"⛔ Неподдерживаемый формат: {fname}", message_thread_id=msg.message_thread_id)
        return None
    try:
        data = await download_document(tg, doc["file_id"])
        path = save_to_inbox(workdir, fname, data)
        clean_inbox(workdir)
        return f"[File: {path} — {len(data)//1024}KB]"
    except Exception as e:
        await tg.send_message(msg.chat_id, f"⚠️ Ошибка загрузки: {e}", message_thread_id=msg.message_thread_id)
        return None
