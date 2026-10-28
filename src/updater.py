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
DEFAULT_CHECK_INTERVAL_S = 120.0  # Check every 2 minutes


def detect_git_remote(repo_dir: Path = REPO_DIR) -> str:
    """Prefer 'dim3nt0r' remote if present and pointing to agy-to-tg, else 'origin'."""
    try:
        res = subprocess.run(
            ["git", "remote"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        remotes = [r.strip() for r in res.stdout.splitlines() if r.strip()]
        if "dim3nt0r" in remotes:
            return "dim3nt0r"
    except Exception:
        pass
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
    try:
        fetch_res = subprocess.run(
            ["git", "fetch", "--quiet", rem, branch],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=25.0,
            check=False,
        )
        if fetch_res.returncode != 0:
            return False, "", ""

        local_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        remote_res = subprocess.run(
            ["git", "rev-parse", f"{rem}/{branch}"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        local_sha = local_res.stdout.strip()
        remote_sha = remote_res.stdout.strip()
        if not local_sha or not remote_sha or local_sha == remote_sha:
            return False, local_sha, remote_sha

        # Check if local_sha is an ancestor of remote_sha (so we only fast-forward, never overwrite local unpushed commits)
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
                "auto-update skipped: local %s is ahead or diverged from %s/%s (%s)",
                local_sha[:7], rem, branch, remote_sha[:7],
            )
            return False, local_sha, remote_sha

        ff_res = subprocess.run(
            ["git", "merge", "--ff-only", f"{rem}/{branch}"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        if ff_res.returncode == 0:
            LOG.info(
                "auto-update pulled new commits: %s -> %s (%s/%s)",
                local_sha[:7], remote_sha[:7], rem, branch,
            )
            return True, local_sha, remote_sha
        else:
            LOG.warning("auto-update git merge --ff-only failed: %s", ff_res.stderr.strip())
    except Exception as exc:
        LOG.warning("auto-update check failed: %s", exc)
    return False, "", ""


def trigger_service_restart() -> None:
    """Restart the user systemd service cleanly in the background."""
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
