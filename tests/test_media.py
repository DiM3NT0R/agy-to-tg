"""Edge-case tests for src/media.py."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from src.media import (
    ALLOWED_DOC_MIMES,
    INBOX_DIR_NAME,
    MAX_FILE_SIZE,
    MAX_PHOTO_SIZE,
    build_media_prompt,
    clean_inbox,
    is_allowed_document,
    is_allowed_image,
    list_inbox,
    save_to_inbox,
)


class _FakeTG:
    def __init__(self, *, get_file_data: bytes = b"", get_file_error: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self._data = get_file_data
        self._error = get_file_error
        self.get_file_calls: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def get_file(self, file_id: str) -> bytes:
        self.get_file_calls.append(file_id)
        if self._error:
            raise self._error
        return self._data


class _FakeMsg:
    def __init__(
        self,
        text: str = "",
        chat_id: int = 42,
        photo: list[dict] | None = None,
        document: dict | None = None,
    ) -> None:
        self.text = text
        self.chat_id = chat_id
        self.user_id = 99
        self.photo = photo
        self.document = document


class _FakeState:
    def __init__(self, chat_dir: str = "/tmp", photo_enabled: bool = True) -> None:
        self.chat_dir = chat_dir
        self.photo_enabled = photo_enabled


class _FakeCfg:
    pass


# ── MIME validation edge cases ────────────────────────────────────


def test_empty_mime_rejected() -> None:
    assert is_allowed_image("") is False


def test_none_filename_ok() -> None:
    assert is_allowed_document("text/plain", "") is True  # MIME trusted


def test_double_extension() -> None:
    """tar.gz is .gz — rejected unless MIME is trusted."""
    assert is_allowed_document("application/gzip", "archive.tar.gz") is False


def test_uppercase_extension() -> None:
    assert is_allowed_document("application/octet-stream", "README.MD") is True


def test_no_extension() -> None:
    assert is_allowed_document("application/octet-stream", "Dockerfile") is False


def test_hidden_file_dotfile_no_extension() -> None:
    assert is_allowed_document("text/plain", ".env") is True  # MIME trusted
    assert is_allowed_document("application/octet-stream", ".env") is False  # no ext


def test_xml_in_allowlist() -> None:
    assert is_allowed_document("application/xml", "data.xml") is True


def test_csv_in_allowlist() -> None:
    assert is_allowed_document("text/csv", "data.csv") is True


def test_html_in_allowlist() -> None:
    assert is_allowed_document("text/html", "page.html") is True


def test_all_mime_allowlist_self_consistent() -> None:
    for mime in ALLOWED_DOC_MIMES:
        assert is_allowed_document(mime, f"test{os.extsep}bin") is True


# ── save_to_inbox edge cases ──────────────────────────────────────


def test_save_to_inbox_creates_dir(tmp_path: Path) -> None:
    path = save_to_inbox(str(tmp_path), "test.py", b"print(1)")
    assert path.exists()
    assert path.read_bytes() == b"print(1)"
    assert path.parent.name == INBOX_DIR_NAME


def test_save_to_inbox_unique_names(tmp_path: Path) -> None:
    a = save_to_inbox(str(tmp_path), "f.py", b"a")
    time.sleep(0.02)
    b = save_to_inbox(str(tmp_path), "g.py", b"b")
    assert a != b
    assert a.name.startswith(tuple("0123456789"))
    assert b.name.startswith(tuple("0123456789"))


def test_save_to_inbox_empty_file(tmp_path: Path) -> None:
    path = save_to_inbox(str(tmp_path), "empty.txt", b"")
    assert path.exists()
    assert path.read_bytes() == b""


# ── clean_inbox edge cases ────────────────────────────────────────


def test_clean_inbox_nonexistent_dir() -> None:
    assert clean_inbox("/tmp/nonexistent-inbox-xyz") == 0


def test_clean_inbox_removes_old_files(tmp_path: Path) -> None:
    wd = str(tmp_path)
    path = save_to_inbox(wd, "old.py", b"x")
    old_mtime = time.time() - 25 * 3600
    os.utime(path, (old_mtime, old_mtime))
    removed = clean_inbox(wd, max_age_hours=24)
    assert removed == 1
    assert not path.exists()


def test_clean_inbox_keeps_new_files(tmp_path: Path) -> None:
    wd = str(tmp_path)
    path = save_to_inbox(wd, "new.py", b"x")
    removed = clean_inbox(wd, max_age_hours=24)
    assert removed == 0
    assert path.exists()


# ── list_inbox edge cases ─────────────────────────────────────────


def test_list_inbox_empty_dir(tmp_path: Path) -> None:
    assert list_inbox(str(tmp_path / "nonexistent")) == []


def test_list_inbox_respects_limit(tmp_path: Path) -> None:
    wd = str(tmp_path)
    for i in range(10):
        save_to_inbox(wd, f"f{i}.py", b"x")
        time.sleep(0.01)
    files = list_inbox(wd, limit=3)
    assert len(files) == 3


# ── build_media_prompt edge cases ─────────────────────────────────


async def test_prompt_text_only() -> None:
    tg = _FakeTG()
    msg = _FakeMsg(text="hello")
    result = await build_media_prompt(msg, tg, _FakeState(), _FakeCfg())
    assert result == "hello"


async def test_prompt_photo_disabled() -> None:
    tg = _FakeTG()
    msg = _FakeMsg(text="", photo=[{"file_id": "abc", "file_size": 100}])
    state = _FakeState(photo_enabled=False)
    result = await build_media_prompt(msg, tg, state, _FakeCfg())
    assert result is None


async def test_prompt_photo_too_large() -> None:
    tg = _FakeTG()
    msg = _FakeMsg(photo=[{"file_id": "abc", "file_size": MAX_PHOTO_SIZE + 1}])
    result = await build_media_prompt(msg, tg, _FakeState(), _FakeCfg())
    assert result is None
    assert any("too large" in s for _, s in tg.sent)


async def test_prompt_document_too_large() -> None:
    tg = _FakeTG()
    msg = _FakeMsg(
        document={
            "file_id": "abc",
            "file_name": "big.pdf",
            "mime_type": "application/pdf",
            "file_size": MAX_FILE_SIZE + 1,
        }
    )
    result = await build_media_prompt(msg, tg, _FakeState(), _FakeCfg())
    assert result is None
    assert any("too large" in s for _, s in tg.sent)


async def test_prompt_document_unsupported_mime() -> None:
    tg = _FakeTG()
    msg = _FakeMsg(
        document={
            "file_id": "abc",
            "file_name": "bad.exe",
            "mime_type": "application/x-msdownload",
            "file_size": 100,
        }
    )
    result = await build_media_prompt(msg, tg, _FakeState(), _FakeCfg())
    assert result is None
    assert any("Unsupported" in s for _, s in tg.sent)


async def test_prompt_document_injects_path(tmp_path: Path) -> None:
    tg = _FakeTG(get_file_data=b"contents")
    msg = _FakeMsg(
        text="check this",
        document={
            "file_id": "xyz",
            "file_name": "lib.py",
            "mime_type": "text/x-python",
            "file_size": 100,
        },
    )
    result = await build_media_prompt(msg, tg, _FakeState(str(tmp_path)), _FakeCfg())
    assert result is not None
    assert "check this" in result
    assert "[File:" in result
    assert ".bridge-inbox" in result


async def test_prompt_download_error_reported(tmp_path: Path) -> None:
    tg = _FakeTG(get_file_error=RuntimeError("network down"))
    msg = _FakeMsg(
        document={
            "file_id": "xyz",
            "file_name": "lib.py",
            "mime_type": "text/x-python",
            "file_size": 100,
        }
    )
    result = await build_media_prompt(msg, tg, _FakeState(str(tmp_path)), _FakeCfg())
    assert result is None
    assert any("Download failed" in s for _, s in tg.sent)


async def test_prompt_both_text_and_media(tmp_path: Path) -> None:
    tg = _FakeTG(get_file_data=b"data")
    msg = _FakeMsg(text="analyze", photo=[{"file_id": "p1", "file_size": 100}])
    result = await build_media_prompt(msg, tg, _FakeState(str(tmp_path)), _FakeCfg())
    assert result is not None
    assert result.startswith("analyze [Photo:")
