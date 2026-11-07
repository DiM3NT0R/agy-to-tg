"""Management of OpenAI Codex accounts and authorization profiles.

Supports:
- Inspecting active Codex credentials from ~/.codex/auth.json (ChatGPT OAuth JWT or API key)
- Extracting account metadata (email, name, account ID, auth mode, expiration)
- Storing multiple accounts in ~/.antigravity/bridge/codex_accounts/
- Switching active account safely with atomic file writes and backup
- Adding new accounts via OAuth Device Code flow or API key
- Deleting saved accounts or logging out
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

LOG = logging.getLogger("antigravity_telegram_bridge")

_DEFAULT_ACCOUNTS_DIR = Path.home() / ".antigravity" / "bridge" / "codex_accounts"
_DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"


@dataclass
class CodexAccount:
    id: str
    label: str
    email: str | None = None
    name: str | None = None
    account_id: str | None = None
    auth_mode: str = "chatgpt"  # chatgpt | api_key | unknown
    added_at: str = ""
    last_used_at: str = ""
    is_active: bool = False
    auth_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "email": self.email,
            "name": self.name,
            "account_id": self.account_id,
            "auth_mode": self.auth_mode,
            "added_at": self.added_at,
            "last_used_at": self.last_used_at,
            "auth_data": self.auth_data,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], is_active: bool = False) -> CodexAccount:
        return cls(
            id=d.get("id", ""),
            label=d.get("label", ""),
            email=d.get("email"),
            name=d.get("name"),
            account_id=d.get("account_id"),
            auth_mode=d.get("auth_mode", "unknown"),
            added_at=d.get("added_at", ""),
            last_used_at=d.get("last_used_at", ""),
            is_active=is_active,
            auth_data=d.get("auth_data", {}),
        )


def extract_jwt_claims(id_token: str) -> dict[str, Any]:
    """Decode JWT claims without external libraries."""
    try:
        parts = id_token.split(".")
        if len(parts) >= 2:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            data = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8", errors="replace")
            return json.loads(data)
    except Exception as exc:
        LOG.debug("Failed to decode JWT claims: %s", exc)
    return {}


def parse_auth_data(auth_data: dict[str, Any]) -> dict[str, Any]:
    """Extract profile identifiers from an auth.json dictionary."""
    mode = auth_data.get("auth_mode", "unknown")
    tokens = auth_data.get("tokens") or {}
    api_key = auth_data.get("OPENAI_API_KEY")
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None

    email: str | None = None
    name: str | None = None
    if id_token:
        claims = extract_jwt_claims(id_token)
        email = claims.get("email")
        name = claims.get("name")

    if email:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", email.lower())
        acc_id = f"acc_{slug}"
        label = email
    elif account_id:
        acc_id = f"acc_{str(account_id).replace('-', '_')}"
        label = f"ChatGPT ({str(account_id)[:8]}...)"
    elif api_key:
        h = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]
        acc_id = f"api_key_{h}"
        label = f"API Key (...{api_key[-4:] if len(api_key) >= 4 else '***'})"
    else:
        raw_repr = json.dumps(auth_data, sort_keys=True)
        h = hashlib.sha256(raw_repr.encode("utf-8")).hexdigest()[:8]
        acc_id = f"acc_{h}"
        label = f"Account {h}"

    return {
        "id": acc_id,
        "label": label,
        "email": email,
        "name": name,
        "account_id": account_id,
        "auth_mode": mode,
    }


def get_active_auth_path(codex_home: Path | None = None) -> Path:
    base = codex_home or _DEFAULT_CODEX_HOME
    return base / "auth.json"


def get_accounts_dir(custom_dir: Path | None = None) -> Path:
    d = custom_dir or _DEFAULT_ACCOUNTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def read_active_auth_data(auth_path: Path | None = None) -> dict[str, Any] | None:
    path = auth_path or get_active_auth_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOG.warning("Failed to read active auth file %s: %s", path, exc)
        return None


def _is_same_account(auth_a: dict[str, Any], auth_b: dict[str, Any]) -> bool:
    """Compare two auth dictionaries to see if they describe the same credentials."""
    info_a = parse_auth_data(auth_a)
    info_b = parse_auth_data(auth_b)
    if info_a["id"] == info_b["id"]:
        return True
    tok_a = (auth_a.get("tokens") or {}).get("account_id")
    tok_b = (auth_b.get("tokens") or {}).get("account_id")
    if tok_a and tok_b and tok_a == tok_b:
        return True
    key_a = auth_a.get("OPENAI_API_KEY")
    key_b = auth_b.get("OPENAI_API_KEY")
    if key_a and key_b and key_a == key_b:
        return True
    return False


def load_saved_accounts(
    accounts_dir: Path | None = None,
    auth_path: Path | None = None,
) -> list[CodexAccount]:
    """Load all saved Codex accounts, syncing current active credentials if needed."""
    adir = get_accounts_dir(accounts_dir)
    active_path = auth_path or get_active_auth_path()
    active_data = read_active_auth_data(active_path)

    saved_map: dict[str, CodexAccount] = {}

    for f in adir.glob("*.json"):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            acc = CodexAccount.from_dict(raw)
            if acc.id:
                saved_map[acc.id] = acc
        except Exception as exc:
            LOG.warning("Failed to load saved account %s: %s", f, exc)

    # If active auth exists, sync it into saved accounts so it is tracked
    active_account_id: str | None = None
    if active_data:
        meta = parse_auth_data(active_data)
        acc_id = meta["id"]
        now_str = datetime.now(timezone.utc).isoformat()

        if acc_id in saved_map:
            # Update auth_data in case tokens refreshed during runtime
            saved_map[acc_id].auth_data = active_data
            saved_map[acc_id].last_used_at = now_str
            active_account_id = acc_id
            _write_account_file(saved_map[acc_id], adir)
        else:
            # Find if matching by token/key
            matched = False
            for k, acc in saved_map.items():
                if _is_same_account(acc.auth_data, active_data):
                    acc.auth_data = active_data
                    acc.last_used_at = now_str
                    active_account_id = k
                    _write_account_file(acc, adir)
                    matched = True
                    break
            if not matched:
                new_acc = CodexAccount(
                    id=acc_id,
                    label=meta["label"],
                    email=meta["email"],
                    name=meta["name"],
                    account_id=meta["account_id"],
                    auth_mode=meta["auth_mode"],
                    added_at=now_str,
                    last_used_at=now_str,
                    is_active=True,
                    auth_data=active_data,
                )
                saved_map[acc_id] = new_acc
                active_account_id = acc_id
                _write_account_file(new_acc, adir)

    result: list[CodexAccount] = []
    for acc in saved_map.values():
        acc.is_active = (acc.id == active_account_id)
        result.append(acc)

    # Sort: active first, then most recently used
    result.sort(key=lambda a: (not a.is_active, a.last_used_at or ""), reverse=False)
    return result


def _write_account_file(acc: CodexAccount, adir: Path) -> None:
    target = adir / f"{acc.id}.json"
    tmp = adir / f"{acc.id}.json.tmp"
    data = json.dumps(acc.to_dict(), indent=2, ensure_ascii=False)
    tmp.write_text(data, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(target)


def save_account_data(
    auth_data: dict[str, Any],
    accounts_dir: Path | None = None,
    make_active: bool = True,
    auth_path: Path | None = None,
) -> CodexAccount:
    """Save auth credentials into accounts storage, optionally setting it as active."""
    adir = get_accounts_dir(accounts_dir)
    meta = parse_auth_data(auth_data)
    now_str = datetime.now(timezone.utc).isoformat()

    existing_file = adir / f"{meta['id']}.json"
    added_at = now_str
    if existing_file.is_file():
        try:
            prev = json.loads(existing_file.read_text(encoding="utf-8"))
            added_at = prev.get("added_at", now_str)
        except Exception:
            pass

    acc = CodexAccount(
        id=meta["id"],
        label=meta["label"],
        email=meta["email"],
        name=meta["name"],
        account_id=meta["account_id"],
        auth_mode=meta["auth_mode"],
        added_at=added_at,
        last_used_at=now_str,
        is_active=make_active,
        auth_data=auth_data,
    )
    _write_account_file(acc, adir)

    if make_active:
        active_path = auth_path or get_active_auth_path()
        _write_active_auth_file(auth_data, active_path)

    return acc


def _write_active_auth_file(auth_data: dict[str, Any], active_path: Path) -> None:
    active_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = active_path.with_suffix(".json.tmp")
    data = json.dumps(auth_data, indent=2, ensure_ascii=False)
    tmp.write_text(data, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(active_path)

    # Also keep auth.json.bak in sync for safety
    bak = active_path.with_suffix(".json.bak")
    try:
        shutil.copy2(active_path, bak)
        os.chmod(bak, 0o600)
    except Exception:
        pass

    # Remove any stale lock
    lock = active_path.parent / "auth.lock"
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass


async def refresh_active_codex_tokens(
    timeout: float = 8.0,
    auth_path: Path | None = None,
    accounts_dir: Path | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Attempt to refresh active Codex account OAuth tokens using refresh_token.

    Returns (success, error_or_info_message, updated_auth_data).
    """
    path = auth_path or get_active_auth_path()
    auth_data = read_active_auth_data(path)
    if not auth_data:
        return False, "Codex не авторизован", None

    tokens = auth_data.get("tokens") or {}
    rt = str(tokens.get("refresh_token") or "")
    if not rt:
        return False, "Refresh-токен отсутствует", None

    payload = {
        "client_id": CODEX_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": rt,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(CODEX_TOKEN_ENDPOINT, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                new_tokens = dict(tokens)
                if data.get("access_token"):
                    new_tokens["access_token"] = data["access_token"]
                if data.get("refresh_token"):
                    new_tokens["refresh_token"] = data["refresh_token"]
                if data.get("id_token"):
                    new_tokens["id_token"] = data["id_token"]

                new_auth_data = dict(auth_data)
                new_auth_data["tokens"] = new_tokens
                new_auth_data["last_refresh"] = datetime.now(timezone.utc).isoformat()

                # Persist to active auth.json
                path.write_text(json.dumps(new_auth_data, indent=2), encoding="utf-8")
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass

                # Also update corresponding saved account file
                adir = get_accounts_dir(accounts_dir)
                for f in adir.glob("*.json"):
                    try:
                        raw = json.loads(f.read_text(encoding="utf-8"))
                        if _is_same_account(raw.get("auth_data", {}), new_auth_data):
                            raw["auth_data"] = new_auth_data
                            raw["last_used_at"] = datetime.now(timezone.utc).isoformat()
                            f.write_text(json.dumps(raw, indent=2), encoding="utf-8")
                            break
                    except Exception:
                        pass

                return True, "Токены успешно обновлены", new_auth_data
            else:
                try:
                    err_data = resp.json()
                    err_code = err_data.get("error", {}).get("code") or resp.status_code
                    err_msg = err_data.get("error", {}).get("message") or resp.text
                except Exception:
                    err_code = resp.status_code
                    err_msg = resp.text
                return False, f"Ошибка обновления ({err_code}): {err_msg}", None
    except Exception as exc:
        return False, f"Ошибка сети при обновлении токена: {exc}", None


def switch_account(
    account_id: str,
    accounts_dir: Path | None = None,
    auth_path: Path | None = None,
) -> tuple[bool, str, CodexAccount | None]:
    """Switch active Codex credentials to a saved account."""
    adir = get_accounts_dir(accounts_dir)
    target_file = adir / f"{account_id}.json"
    if not target_file.is_file():
        return False, f"Аккаунт '{account_id}' не найден", None

    try:
        raw = json.loads(target_file.read_text(encoding="utf-8"))
        target_acc = CodexAccount.from_dict(raw)
    except Exception as exc:
        return False, f"Не удалось прочитать аккаунт: {exc}", None

    active_path = auth_path or get_active_auth_path()
    current_active = read_active_auth_data(active_path)
    now_str = datetime.now(timezone.utc).isoformat()

    # Back up current active auth before switching
    if current_active:
        cur_meta = parse_auth_data(current_active)
        cur_file = adir / f"{cur_meta['id']}.json"
        if cur_file.is_file():
            try:
                cur_acc = CodexAccount.from_dict(json.loads(cur_file.read_text(encoding="utf-8")))
                cur_acc.auth_data = current_active
                cur_acc.last_used_at = now_str
                _write_account_file(cur_acc, adir)
            except Exception:
                pass
        else:
            save_account_data(current_active, accounts_dir=adir, make_active=False)

    # Activate target
    _write_active_auth_file(target_acc.auth_data, active_path)
    target_acc.last_used_at = now_str
    target_acc.is_active = True
    _write_account_file(target_acc, adir)

    return True, f"Аккаунт успешно переключен на {target_acc.label}", target_acc


def remove_account(
    account_id: str,
    accounts_dir: Path | None = None,
    auth_path: Path | None = None,
) -> tuple[bool, str]:
    """Remove a saved account. If active, removes active auth as well."""
    adir = get_accounts_dir(accounts_dir)
    target_file = adir / f"{account_id}.json"
    if not target_file.is_file():
        return False, "Аккаунт не найден"

    active_path = auth_path or get_active_auth_path()
    active_data = read_active_auth_data(active_path)
    is_active = False
    if active_data:
        meta = parse_auth_data(active_data)
        if meta["id"] == account_id:
            is_active = True

    try:
        target_file.unlink()
    except OSError as exc:
        return False, f"Ошибка удаления файла аккаунта: {exc}"

    if is_active:
        try:
            if active_path.exists():
                active_path.unlink()
        except OSError:
            pass

    return True, "Аккаунт успешно удален"


def logout_active_account(
    auth_path: Path | None = None,
    accounts_dir: Path | None = None,
) -> tuple[bool, str]:
    """Log out active Codex account (keeping saved accounts)."""
    active_path = auth_path or get_active_auth_path()
    current_active = read_active_auth_data(active_path)
    if current_active:
        # Save before logging out so credentials aren't lost from archive
        save_account_data(current_active, accounts_dir=accounts_dir, make_active=False)

    try:
        if active_path.exists():
            active_path.unlink()
        bak = active_path.with_suffix(".json.bak")
        if bak.exists():
            bak.unlink()
        lock = active_path.parent / "auth.lock"
        if lock.exists():
            lock.unlink()
        return True, "Вы успешно вышли из текущего аккаунта Codex"
    except OSError as exc:
        return False, f"Ошибка при выходе: {exc}"


def backup_active_auth_for_login(auth_path: Path | None = None) -> Path | None:
    """Create a temporary safeguard backup of auth.json before running device-auth."""
    path = auth_path or get_active_auth_path()
    if not path.is_file():
        return None
    backup_file = path.parent / "auth.json.pre_login_backup"
    try:
        shutil.copy2(path, backup_file)
        return backup_file
    except Exception as exc:
        LOG.warning("Failed to backup auth before login: %s", exc)
        return None


def restore_backup_after_aborted_login(auth_path: Path | None = None) -> bool:
    """Restore auth.json from safeguard backup if login was cancelled or failed."""
    path = auth_path or get_active_auth_path()
    backup_file = path.parent / "auth.json.pre_login_backup"
    lock = path.parent / "auth.lock"
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass

    if backup_file.is_file():
        try:
            shutil.copy2(backup_file, path)
            os.chmod(path, 0o600)
            backup_file.unlink()
            return True
        except Exception as exc:
            LOG.warning("Failed to restore auth backup: %s", exc)
    return False


def clean_login_backup(auth_path: Path | None = None) -> None:
    """Remove login backup file after successful new authentication."""
    path = auth_path or get_active_auth_path()
    backup_file = path.parent / "auth.json.pre_login_backup"
    if backup_file.is_file():
        try:
            backup_file.unlink()
        except OSError:
            pass
