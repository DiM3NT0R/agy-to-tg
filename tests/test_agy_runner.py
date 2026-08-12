"""Tests for src.agy_runner — subprocess spawn and argv construction."""
from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest

from src.agy_runner import AgyResult, run_agy


@pytest.fixture
def fake_agy(tmp_path: Path) -> Path:
    """Create an executable shell stub that echoes a plain-text reply."""
    script = tmp_path / "agy"
    script.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            cat > /dev/null
            echo 'fake reply'
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


async def test_run_agy_returns_stdout_text(fake_agy: Path, tmp_path: Path) -> None:
    result = await run_agy(
        prompt="hello",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(fake_agy),
    )
    assert isinstance(result, AgyResult)
    assert result.exit_code == 0
    assert result.text.strip() == "fake reply"


async def test_run_agy_surfaces_nonzero_exit(tmp_path: Path) -> None:
    script = tmp_path / "agy-fail"
    script.write_text("#!/bin/sh\necho boom 1>&2\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    result = await run_agy(
        prompt="x",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(script),
    )
    assert result.exit_code == 1
    assert "boom" in result.stderr
    assert result.text == ""


async def test_run_agy_builds_argv(tmp_path: Path) -> None:
    captured = tmp_path / "argv.txt"
    script = tmp_path / "agy-record"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            cat > /dev/null
            printf '%s\\n' "$@" > "{captured}"
            echo ok
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    workdir = tmp_path / "work"
    workdir.mkdir()
    await run_agy(
        prompt="hi",
        chat_dir=str(workdir),
        has_session=True,
        model="gemini-2.5-pro",
        mode="plan",
        agy_path=str(script),
    )
    args = captured.read_text().splitlines()
    assert "-p" in args and "hi" in args
    assert "--continue" in args
    assert "--model" in args and "gemini-2.5-pro" in args
    assert "--dangerously-skip-permissions" in args
    # NOTE: `plan` mode no longer wraps agy in a bwrap sandbox (removed
    # because it hung under the daemon's asyncio subprocess pipes — see
    # docs/design.md). It only changes the agent's instructions, so the
    # argv is identical to `code` mode aside from prompt/session flags.
    assert "--sandbox" not in args
    assert "--print-timeout" in args


async def test_run_agy_uses_new_project_for_fresh_session(tmp_path: Path) -> None:
    captured = tmp_path / "argv.txt"
    script = tmp_path / "agy-record"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            cat > /dev/null
            printf '%s\\n' "$@" > "{captured}"
            echo ok
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    workdir = tmp_path / "work"
    workdir.mkdir()
    await run_agy(
        prompt="hi",
        chat_dir=str(workdir),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(script),
    )
    args = captured.read_text().splitlines()
    assert "--new-project" in args
    assert "--continue" not in args


async def test_run_agy_returns_timeout_result(tmp_path: Path) -> None:
    script = tmp_path / "agy-hang"
    script.write_text("#!/bin/sh\ncat > /dev/null\nsleep 5\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result = await run_agy(
        prompt="x",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(script),
        timeout=0.3,
    )
    assert result.exit_code == 124
    assert "timed out" in result.stderr
    assert result.text == ""


async def test_run_agy_no_timeout_by_default(fake_agy: Path, tmp_path: Path) -> None:
    result = await run_agy(
        prompt="x",
        chat_dir=str(tmp_path),
        has_session=False,
        model="",
        mode="code",
        agy_path=str(fake_agy),
    )
    assert result.exit_code == 0
    assert result.text.strip() == "fake reply"


def test_build_args_plan_mode_has_no_os_level_sandbox(monkeypatch) -> None:
    """`plan` mode used to wrap agy in a bwrap sandbox; that was removed
    (it hung under the daemon's asyncio subprocess pipes — see
    docs/design.md). `plan` only changes the agent's own instructions, it
    does not provide OS-level isolation. Guard against silently
    reintroducing a broken/partial sandbox wrapper."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from src.agy_runner import _build_args

    args = _build_args(
        agy_path="/usr/bin/agy",
        prompt="run-security-scan",
        has_session=False,
        model="gemini-2.5-pro",
        mode="plan",
        print_timeout="15m",
        chat_dir="/home/i/chat_workspace",
    )

    assert args[0] == "/usr/bin/agy"
    assert "bwrap" not in args
    assert "--sandbox" not in args
    assert "-p" in args and "run-security-scan" in args
