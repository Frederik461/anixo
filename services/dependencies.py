"""Dependency diagnostics shared by the CLI and Settings screen."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from config import Config, expand_path

if TYPE_CHECKING:
    from services.ani_cli import AniCliService


def _program_version(command: str, args: tuple[str, ...] = ("--version",)) -> str:
    executable = shutil.which(command)
    if not executable:
        return "not found"
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return executable
    first = (result.stdout or result.stderr).strip().splitlines()
    return first[0][:80] if first else executable


def diagnostics(config: Config, ani_cli: "AniCliService | None" = None) -> list[str]:
    """Return a compact, human-readable dependency report."""
    invocation = ani_cli.resolve_invocation() if ani_cli else None
    report = [
        f"Python:        {platform.python_version()}",
        f"Platform:      {platform.system()} {platform.release()}",
        f"ANIXO data:    {config.cache_path or '(default)'}",
        f"Images:        {config.images} / {config.artwork_renderer}",
        f"Terminal:      {os.environ.get('TERM', 'Windows Terminal / Textual')}",
        f"ani-cli path:  {config.ani_cli_path or '(auto-detect)'}",
        f"ani-cli script:{invocation.display_path if invocation else ' not found'}",
        f"mpv:           {_program_version('mpv')}",
        f"ffmpeg:        {_program_version('ffmpeg')}",
        f"fzf:           {_program_version('fzf')}",
        f"Git Bash:      {_git_bash(config)}",
        f"Windows Term:  {'yes' if shutil.which('wt') else 'unknown'}",
    ]
    return report


def _git_bash(config: Config) -> str:
    candidates = [
        config.git_bash_path,
        os.environ.get("GIT_INSTALL_ROOT")
        and str(Path(os.environ["GIT_INSTALL_ROOT"]) / "bin" / "bash.exe"),
        str(Path.home() / "scoop" / "apps" / "git" / "current" / "bin" / "bash.exe"),
        r"C:\Program Files\Git\bin\bash.exe",
    ]
    for candidate in candidates:
        if candidate and Path(expand_path(candidate)).exists():
            return str(Path(expand_path(candidate)).resolve())
    return "not found"


def python_executable() -> str:
    return sys.executable
