"""Atomic JSON-backed bridge state at ~/.antigravity/bridge/state.json.

Per-chat state tracks the chat working directory, whether a session exists,
model/mode overrides, and turn count. agy resumes sessions by cwd/project,
so we do not store opaque session UUIDs.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Defense against state.json tampering: chat_dir paths must look like a tail
# segment we generated ourselves (digits/letters/underscores/dashes, no slashes,
# no '..'). Real chat_ids are integers, so the dir name is the stringified id.
_CHAT_DIR_RE = re.compile(r"^-?[0-9]+(?:_[0-9]+)?$")

SessionKey = int | tuple[int, int]

# Per-chat overrides have to be argv-safe. The regex rejects any character
# that's not strictly needed for valid model identifiers (e.g. "gemini-3.5-flash").
# Crucially it forbids a leading `-` so a tampered state file can't inject flags.
_MODEL_RE = re.compile(r"^[a-zA-Z0-9._][a-zA-Z0-9._\-]*$")

_ALLOWED_MODES = frozenset({"", "code", "plan"})


@dataclass
class ChatState:
    chat_dir: str  # absolute path; verified-on-load to be under chats_root
    has_session: bool = False  # True after first successful agy turn
    model: str = ""  # "" → use cfg.agy.model
    mode: str = ""  # "" → use cfg.agy.mode; values: "code" | "plan"
    photo_enabled: bool = True  # toggle for photo processing
    turn_count: int = 0  # successful turns served on this chat
    effort: str = "" # "low", "medium", "high"
    print_timeout: str = "" # e.g. "30m"
    conversation_id: str = "" # UUID of the currently selected conversation
    projects: list[dict[str, str]] = field(default_factory=list) # [{'id': '...', 'snippet': '...'}]
    voice: str = ""  # "" → default "filipp"
    voice_speed: str = ""  # "" → default "1.1"
    auto_voice: bool = False  # True → speak replies automatically

@dataclass
class State:
    last_update_id: int = 0
    chats: dict[SessionKey, ChatState] = field(default_factory=dict)


def is_valid_model(name: str) -> bool:
    return bool(name) and bool(_MODEL_RE.match(name))


def _safe_model(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    return raw if _MODEL_RE.match(raw) else ""


def _safe_mode(raw: object) -> str:
    if isinstance(raw, str) and raw in _ALLOWED_MODES:
        return raw
    return ""


def _safe_turn_count(raw: object) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def _safe_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    return True


def _safe_chat_state(chats_root: Path, raw: dict) -> ChatState | None:
    chat_dir = raw.get("chat_dir")
    if not isinstance(chat_dir, str):
        return None
    p = Path(chat_dir)
    try:
        p.resolve().relative_to(chats_root.resolve())
    except (ValueError, OSError):
        return None
    if not _CHAT_DIR_RE.match(p.name):
        return None
    raw_projects = raw.get("projects", [])
    projects = []
    if isinstance(raw_projects, list):
        for rp in raw_projects:
            if isinstance(rp, dict):
                pid = rp.get("id", "")
                snippet = rp.get("snippet", "")
                if isinstance(pid, str) and isinstance(snippet, str) and pid:
                    projects.append({"id": pid, "snippet": snippet})

    return ChatState(
        chat_dir=str(p),
        has_session=bool(raw.get("has_session", False)),
        model=_safe_model(raw.get("model", "")),
        mode=_safe_mode(raw.get("mode", "")),
        photo_enabled=_safe_bool(raw.get("photo_enabled", True)),
        turn_count=_safe_turn_count(raw.get("turn_count", 0)),
        effort=raw.get("effort", "") if isinstance(raw.get("effort"), str) else "",
        print_timeout=raw.get("print_timeout", "") if isinstance(raw.get("print_timeout"), str) else "",
        conversation_id=raw.get("conversation_id", "") if isinstance(raw.get("conversation_id"), str) else "",
        projects=projects,
        voice=raw.get("voice", "") if isinstance(raw.get("voice"), str) else "",
        voice_speed=raw.get("voice_speed", "") if isinstance(raw.get("voice_speed"), str) else "",
        auto_voice=bool(raw.get("auto_voice", False)),
    )


def load_state(path: Path, chats_root: Path) -> State:
    if not path.exists():
        return State()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return State()
    chats_raw = raw.get("chats") or {}
    chats: dict[SessionKey, ChatState] = {}
    for k, v in chats_raw.items():
        try:
            if isinstance(k, str) and ":" in k:
                chat_raw, thread_raw = k.split(":", 1)
                session_key: SessionKey = (int(chat_raw), int(thread_raw))
            else:
                session_key = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        cs = _safe_chat_state(chats_root, v)
        if cs is not None:
            chats[session_key] = cs
    return State(
        last_update_id=int(raw.get("last_update_id", 0)),
        chats=chats,
    )


def save_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    def encode_key(key: SessionKey) -> str:
        if isinstance(key, tuple):
            return f"{key[0]}:{key[1]}"
        return str(key)

    payload = {
        "last_update_id": state.last_update_id,
        "chats": {encode_key(k): asdict(v) for k, v in state.chats.items()},
    }
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)
