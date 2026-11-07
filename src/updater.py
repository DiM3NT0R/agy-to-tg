"""Auto-updater: periodically checks GitHub remote and safely updates when idle."""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Callable

LOG = logging.getLogger("antigravity_telegram_bridge")

REPO_DIR = Path(__file__).resolve().parent.parent
SERVICE_NAME = "antigravity-telegram-bridge.service"
CANONICAL_REPO_URL = "https://github.com/DiM3NT0R/agent-tg-bridge.git"
DEFAULT_CHECK_INTERVAL_S = 120.0  # Check every 2 minutes


def detect_git_remote(repo_dir: Path = REPO_DIR) -> str:
    """Detect or migrate git remote pointing to the canonical agent-tg-bridge repo."""
    try:
        res = subprocess.run(
            ["git", "remote", "-v"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        remotes_dict: dict[str, str] = {}
        for line in res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            parts = line_str.split()
            if len(parts) >= 2:
                if "(fetch)" in line_str or parts[0] not in remotes_dict:
                    remotes_dict[parts[0]] = parts[1]
            elif len(parts) == 1:
                remotes_dict[parts[0]] = ""

        # 1. Any remote already pointing to agent-tg-bridge
        for r_name, r_url in remotes_dict.items():
            if "agent-tg-bridge" in r_url:
                return r_name

        # 2. Any remote pointing to the old repo name agy-to-tg: migrate URL automatically
        for r_name, r_url in remotes_dict.items():
            if "agy-to-tg" in r_url:
                subprocess.run(
                    ["git", "remote", "set-url", r_name, CANONICAL_REPO_URL],
                    cwd=str(repo_dir),
                    timeout=5.0,
                    check=False,
                )
                return r_name

        # 3. Prefer 'dim3nt0r' remote if present
        if "dim3nt0r" in remotes_dict:
            return "dim3nt0r"

        # 4. If 'origin' points to hah23255 (old upstream fork), repoint to DiM3NT0R
        if "origin" in remotes_dict:
            orig_url = remotes_dict["origin"]
            if "hah23255" in orig_url or "agy-to-im" in orig_url:
                subprocess.run(
                    ["git", "remote", "set-url", "origin", CANONICAL_REPO_URL],
                    cwd=str(repo_dir),
                    timeout=5.0,
                    check=False,
                )
            return "origin"
    except Exception as exc:
        LOG.warning("detect_git_remote failed: %s", exc)

    return "origin"


def check_and_pull_update(
    repo_dir: Path = REPO_DIR,
    *,
    branch: str = "main",
    remote: str | None = None,
) -> tuple[bool, str, str]:
    """Fetch remote branch and fast-forward if behind.

    Returns (updated, old_sha, new_sha).
    """
    rem = remote or detect_git_remote(repo_dir)
    target_ref = f"{rem}/{branch}"

    try:
        # Step 1: fetch from configured remote
        fetch_res = subprocess.run(
            ["git", "fetch", "--quiet", rem, branch],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=25.0,
            check=False,
        )
        if fetch_res.returncode != 0:
            # Fallback to direct fetch from canonical GitHub URL
            fetch_direct = subprocess.run(
                ["git", "fetch", "--quiet", CANONICAL_REPO_URL, branch],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=25.0,
                check=False,
            )
            if fetch_direct.returncode != 0:
                LOG.warning("auto-update git fetch failed for %s and direct URL", rem)
                return False, "", ""
            target_ref = "FETCH_HEAD"

        local_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        remote_res = subprocess.run(
            ["git", "rev-parse", target_ref],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        local_sha = local_res.stdout.strip()
        remote_sha = remote_res.stdout.strip()
        if not local_sha or not remote_sha:
            return False, "", ""

        if local_sha == remote_sha:
            return False, local_sha, remote_sha

        # Check if local_sha is an ancestor of remote_sha
        anc_res = subprocess.run(
            ["git", "merge-base", "--is-ancestor", local_sha, remote_sha],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if anc_res.returncode != 0:
            LOG.info(
                "auto-update skipped: local %s is ahead or diverged from %s (%s)",
                local_sha[:7], target_ref, remote_sha[:7],
            )
            return False, local_sha, remote_sha

        ff_res = subprocess.run(
            ["git", "merge", "--ff-only", target_ref],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        if ff_res.returncode == 0:
            LOG.info(
                "auto-update pulled new commits: %s -> %s (%s)",
                local_sha[:7], remote_sha[:7], target_ref,
            )
            return True, local_sha, remote_sha
        else:
            LOG.warning("auto-update git merge --ff-only failed: %s", ff_res.stderr.strip())
    except Exception as exc:
        LOG.warning("auto-update check failed: %s", exc)
    return False, "", ""


def trigger_service_restart() -> None:
    """Restart the user systemd service cleanly and touch .restart_pending as fallback."""
    try:
        marker = Path.home() / ".antigravity" / "bridge" / ".restart_pending"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except Exception:
        pass

    try:
        subprocess.Popen(
            ["systemctl", "--user", "restart", SERVICE_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        LOG.warning("auto-update systemctl restart failed: %s", exc)


async def auto_update_loop(
    stop_event: asyncio.Event,
    *,
    is_busy: Callable[[], bool],
    interval_s: float = DEFAULT_CHECK_INTERVAL_S,
    repo_dir: Path = REPO_DIR,
) -> None:
    """Background loop that checks GitHub for updates and restarts when idle."""
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                break
            except asyncio.TimeoutError:
                pass

            if is_busy():
                continue

            updated, old_sha, new_sha = await asyncio.to_thread(
                check_and_pull_update, repo_dir
            )
            if not updated:
                continue

            # Wait until any turn that started during git fetch finishes
            while is_busy() and not stop_event.is_set():
                await asyncio.sleep(2.0)

            if stop_event.is_set():
                break

            LOG.info(
                "Applying auto-update (%s -> %s): restarting service",
                old_sha[:7], new_sha[:7],
            )
            trigger_service_restart()
            stop_event.set()
            break
    except asyncio.CancelledError:
        return
