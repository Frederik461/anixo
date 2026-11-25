"""First-launch setup and dependency helpers for ANIXO.

This module deliberately avoids Textual and application imports so it can be
used from the setup screen, maintenance scripts, and tests without import
cycles.  Importing it never downloads or executes anything: every install is
an explicit, user-triggered function call.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from config import Config, expand_path
from platforms import get_platform_adapter
from version import APP_VERSION

# Official, pinned ani-cli release. Bytes are verified before they are written
# and never executed by this module.
ANI_CLI_URL = "https://raw.githubusercontent.com/pystardust/ani-cli/v5.1/ani-cli"
ANI_CLI_SHA256 = "F99A1DAC807004BECFB732A5518A0F2DED9DC08B467805ABD03DF5D580BECD46"
GPL3_URL = "https://www.gnu.org/licenses/gpl-3.0.html"
MPV_OFFICIAL_URL = "https://mpv.io/"

DependencyKey = Literal["ani-cli", "mpv", "git_bash", "ffmpeg", "fzf"]

ANI_CLI = "ani-cli"
MPV = "mpv"
GIT_BASH = "git_bash"
FFMPEG = "ffmpeg"
FZF = "fzf"
DEPENDENCY_KEYS: tuple[str, ...] = (ANI_CLI, MPV, GIT_BASH, FFMPEG, FZF)


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """Presentation-ready state for one external dependency."""

    key: str
    label: str
    description: str
    required: bool
    installed: bool
    version: str
    detail: str
    official_url: str
    winget_id: str | None
    installable: bool
    # Filled in by the dependency manager from the installation manifest.
    owned_by_anixo: bool = False
    managed: bool = False
    safe_to_remove: bool = False
    install_location: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class SetupSnapshot:
    """Immutable view of the machine captured for the setup wizard."""

    os_name: str
    os_version: str
    architecture: str
    app_version: str
    terminal_summary: str
    renderer: str
    network_ok: bool
    dependencies: tuple[DependencyStatus, ...]
    legacy_data_path: str | None
    legacy_data_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of an explicit install action."""

    ok: bool
    message: str
    detail: str
    source: str = ""
    version: str = ""
    install_location: str = ""
    sha256: str = ""


def _program_version(
    command: str,
    *,
    configured: str = "",
    args: tuple[str, ...] = ("--version",),
) -> tuple[bool, str]:
    """Return ``(installed, version_or_path)`` for a command on PATH."""
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    found = shutil.which(command)
    if found:
        candidates.append(found)
    for candidate in candidates:
        path = Path(expand_path(candidate))
        if not path.exists():
            continue
        executable = str(path.resolve())
        try:
            result = subprocess.run(
                [executable, *args],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return True, executable
        lines = (result.stdout or result.stderr or "").strip().splitlines()
        return True, (lines[0][:100] if lines else executable)
    return False, "not found"


def _find_git_bash(config: Config | None = None) -> Path | None:
    """Locate bash/Git Bash through the active platform adapter."""
    configured = config.git_bash_path if config is not None else ""
    return get_platform_adapter().bash_executable(configured)


def dependency_statuses(config: Config, ani_cli: Any) -> list[DependencyStatus]:
    """Describe ani-cli, mpv, bash, ffmpeg, and fzf for the current OS.

    ani-cli and mpv are required.  Bash is required on Windows only for legacy
    ``.cmd``/``.bat`` wrappers; POSIX systems ship a shell.  ffmpeg and fzf are
    optional helpers.  yt-dlp is not part of the source dependency chain.
    """
    adapter = get_platform_adapter()
    invocation = None
    if ani_cli is not None:
        try:
            invocation = ani_cli.resolve_invocation()
        except (AttributeError, OSError, RuntimeError):
            invocation = None

    display_path = str(getattr(invocation, "display_path", "") or "")
    if not display_path and config.ani_cli_path:
        display_path = str(Path(expand_path(config.ani_cli_path)).resolve())
    script = Path(expand_path(display_path)) if display_path else None
    ani_installed = invocation is not None or bool(script and script.exists())
    ani_detail = (
        f"Script: {script.resolve()}" if script and script.exists()
        else "Not detected. Install the pinned, checksum-verified v5.1 script."
    )

    # Each probe may run a subprocess with a multi-second timeout. They are
    # independent, so run them together instead of adding up their latencies.
    shell = _find_git_bash(config)
    with ThreadPoolExecutor(max_workers=4) as pool:
        mpv_future = pool.submit(_program_version, "mpv", configured=config.mpv_path)
        shell_future = (
            pool.submit(_program_version, "bash", configured=str(shell))
            if shell
            else None
        )
        ffmpeg_future = pool.submit(_program_version, "ffmpeg")
        fzf_future = pool.submit(_program_version, "fzf")
        mpv_installed, mpv_version = mpv_future.result()
        shell_installed, shell_version = (
            shell_future.result()
            if shell_future is not None
            else (False, "not found")
        )
        ffmpeg_installed, ffmpeg_version = ffmpeg_future.result()
        fzf_installed, fzf_version = fzf_future.result()

    if adapter.is_windows:
        mpv_detail = (
            f"Detected: {mpv_version}" if mpv_installed
            else "Not detected. Install mpv with winget or from mpv.io."
        )
    else:
        mpv_detail = (
            f"Detected: {mpv_version}" if mpv_installed
            else f"Not detected. Install mpv from {MPV_OFFICIAL_URL}."
        )

    winget_available = adapter.is_windows

    return [
        DependencyStatus(
            key=ANI_CLI,
            label="ani-cli",
            description="Streaming engine used to resolve and launch episodes.",
            required=True,
            installed=ani_installed,
            version="detected" if ani_installed else "not found",
            detail=ani_detail,
            official_url="https://github.com/pystardust/ani-cli",
            winget_id=None,
            installable=True,
        ),
        DependencyStatus(
            key=MPV,
            label="mpv",
            description="Local media player used by ani-cli for playback.",
            required=True,
            installed=mpv_installed,
            version=mpv_version,
            detail=mpv_detail,
            official_url=MPV_OFFICIAL_URL,
            winget_id="mpv.io.mpv" if winget_available else None,
            installable=winget_available,
        ),
        DependencyStatus(
            key=GIT_BASH,
            label="Git Bash" if adapter.is_windows else "Bash shell",
            description=(
                "Needed only for .cmd/.bat wrappers on Windows."
                if adapter.is_windows
                else "The POSIX shell used to run ani-cli; provided by macOS."
            ),
            required=False,
            installed=shell_installed,
            version=shell_version,
            detail=(
                f"Detected: {shell}" if shell
                else "Not detected; a POSIX shell is required to run ani-cli."
            ),
            official_url=(
                "https://git-scm.com/download/win"
                if adapter.is_windows
                else "https://www.gnu.org/software/bash/"
            ),
            winget_id="Git.Git" if winget_available else None,
            installable=winget_available,
        ),
        DependencyStatus(
            key=FFMPEG,
            label="ffmpeg",
            description="Optional media diagnostics helper; not required for playback.",
            required=False,
            installed=ffmpeg_installed,
            version=ffmpeg_version,
            detail=ffmpeg_version,
            official_url="https://ffmpeg.org/",
            winget_id="Gyan.FFmpeg" if winget_available else None,
            installable=winget_available,
        ),
        DependencyStatus(
            key=FZF,
            label="fzf",
            description="Optional fuzzy selector used by some provider helpers.",
            required=False,
            installed=fzf_installed,
            version=fzf_version,
            detail=fzf_version,
            official_url="https://github.com/junegunn/fzf",
            winget_id="junegunn.fzf" if winget_available else None,
            installable=winget_available,
        ),
    ]


def check_network(url: str = "https://graphql.anilist.co", timeout: float = 3.0) -> bool:
    """Return True when ``url`` responds; any transport failure means offline."""
    try:
        httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "ANIXO/0.1.0"},
        )
    except httpx.HTTPError:
        return False
    return True


def build_snapshot(
    *,
    app_version: str,
    config: Config,
    ani_cli: Any,
    capabilities: Any,
    renderer: str,
    renderer_note: str,
    legacy_path: Path | None,
    legacy_counts: dict[str, int] | None = None,
) -> SetupSnapshot:
    """Capture machine, terminal, network, and dependency state at once.

    ``renderer`` is preserved verbatim; any renderer fallback note is appended
    to ``terminal_summary`` so the wizard can show both without losing the
    exact resolved renderer identifier.
    """
    try:
        terminal_summary = str(capabilities.summary())
    except AttributeError:
        terminal_summary = str(capabilities)
    if renderer_note:
        terminal_summary = f"{terminal_summary} — {renderer_note}".strip(" —")

    with ThreadPoolExecutor(max_workers=2) as pool:
        network_future = pool.submit(check_network)
        dependencies_future = pool.submit(dependency_statuses, config, ani_cli)
        network_ok = network_future.result()
        dependencies = tuple(dependencies_future.result())

    return SetupSnapshot(
        os_name=platform.system(),
        os_version=platform.version(),
        architecture=platform.machine() or platform.architecture()[0],
        app_version=app_version,
        terminal_summary=terminal_summary,
        renderer=renderer,
        network_ok=network_ok,
        dependencies=dependencies,
        legacy_data_path=str(legacy_path) if legacy_path else None,
        legacy_data_counts=dict(legacy_counts or {}),
    )


def install_with_winget(dependency: DependencyStatus) -> InstallResult:
    """Install a winget-backed dependency at *user* scope, on request only.

    No elevation is requested and no bundled binaries are used.  The caller is
    responsible for asking for confirmation before invoking this function.
    """
    if not dependency.winget_id:
        return InstallResult(
            False,
            f"No winget package is registered for {dependency.label}.",
            "Use the official installer from the dependency's website instead.",
        )
    if platform.system() != "Windows":
        return InstallResult(
            False,
            f"winget is only available on Windows; {dependency.label} was not installed.",
            f"See {dependency.official_url} for other platforms.",
        )
    executable = shutil.which("winget")
    if not executable:
        return InstallResult(
            False,
            "winget is not available on this system.",
            "Install App Installer from the Microsoft Store, or use the official "
            f"installer at {dependency.official_url}.",
        )

    command = [
        executable,
        "install",
        "--id",
        dependency.winget_id,
        "--scope",
        "user",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return InstallResult(
            False,
            f"winget timed out while installing {dependency.label}.",
            "The install may still be running; check winget before retrying.",
        )
    except (OSError, subprocess.SubprocessError) as error:
        return InstallResult(
            False,
            f"winget could not install {dependency.label}.",
            str(error),
        )

    detail = "\n".join(
        part for part in (completed.stdout or "", completed.stderr or "") if part
    ).strip()[-1500:]
    if completed.returncode != 0:
        return InstallResult(
            False,
            f"winget failed to install {dependency.label} (exit {completed.returncode}).",
            detail or "winget produced no output.",
        )
    return InstallResult(
        True,
        f"{dependency.label} installed with winget.",
        detail or "winget completed successfully.",
        source=f"winget:{dependency.winget_id}",
        version=dependency.version,
        install_location="winget://user-scope",
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes to ``path`` via a same-directory temporary file."""
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    temporary.write_bytes(data)
    temporary.replace(path)


def _make_executable(path: Path) -> None:
    """Best-effort POSIX executable bit; harmless where unsupported."""
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _windows_to_posix(value: str) -> str:
    """Convert ``C:\\path`` to ``/c/path`` for Git Bash invocation."""
    normalized = value.replace("\\", "/")
    drive = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    return f"/{drive.group(1).lower()}/{drive.group(2)}" if drive else normalized


def _cmd_wrapper(script: Path, bash: Path) -> bytes:
    """Build a wrapper understood by ``services.ani_cli.parse_cmd_wrapper``."""
    script_windows = script.resolve()
    script_posix = (
        _windows_to_posix(str(script_windows))
        if os.name == "nt"
        else str(script_windows)
    )
    bash_windows = bash.resolve()
    text = (
        "@echo off\r\n"
        f'set "ANI_CLI={script_windows}"\r\n'
        f'set "GIT_BASH={bash_windows}"\r\n'
        f'"{bash_windows}" "{script_posix}" %*\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    )
    return text.encode("utf-8")


def install_ani_cli(destination: Path, *, overwrite: bool = False) -> InstallResult:
    """Download, verify, and install the pinned official ani-cli v5.1 script.

    Existing scripts are never replaced unless the caller proves ANIXO owns
    them by passing ``overwrite=True``.  The SHA256 digest is checked before
    any file is created and the bytes are never executed.  On Windows a
    ``.cmd`` wrapper is written when Git Bash is available.
    """
    target_dir = Path(destination).expanduser()
    script = target_dir / "ani-cli"
    wrapper = target_dir / "ani-cli.cmd"
    if (script.exists() or wrapper.exists()) and not overwrite:
        return InstallResult(
            False,
            "An ani-cli script already exists at this location.",
            "ANIXO did not overwrite it. Remove or update the existing install "
            "only if it was installed by ANIXO.",
            install_location=str(script.resolve()),
        )
    try:
        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "ANIXO/0.1.0"},
        ) as client:
            response = client.get(ANI_CLI_URL)
            response.raise_for_status()
            payload = response.content
    except httpx.HTTPError as error:
        return InstallResult(
            False,
            "Could not download the official ani-cli script.",
            str(error),
        )

    digest = hashlib.sha256(payload).hexdigest().upper()
    if digest != ANI_CLI_SHA256:
        return InstallResult(
            False,
            "ani-cli download failed SHA256 verification; nothing was installed.",
            f"Expected: {ANI_CLI_SHA256}\nReceived: {digest}",
        )

    return install_verified_ani_cli_payload(
        target_dir,
        payload,
        expected_sha256=ANI_CLI_SHA256,
        overwrite=overwrite,
        version="v5.1",
        source=ANI_CLI_URL,
    )


def _looks_like_ani_cli(payload: bytes) -> bool:
    """Cheap structural validation before an already-verified script is written."""
    if len(payload) < 4000:
        return False
    head = payload[:512].lstrip()
    return head.startswith(b"#!/") and (
        b"hianime_search" in payload or b"base_api=" in payload
    )


def install_verified_ani_cli_payload(
    destination: Path,
    payload: bytes,
    *,
    expected_sha256: str,
    overwrite: bool = False,
    version: str = "",
    source: str = "",
    write_wrapper: bool = True,
) -> InstallResult:
    """Install a downloaded ani-cli script after an explicit checksum proof.

    This function never downloads or executes anything.  It also preserves an
    existing Windows CMD wrapper's custom lines, changing only the ANI_CLI path
    when needed.
    """
    target_dir = Path(destination).expanduser()
    script = target_dir / "ani-cli"
    wrapper = target_dir / "ani-cli.cmd"
    if (script.exists() or wrapper.exists()) and not overwrite:
        return InstallResult(
            False,
            "An ani-cli script already exists at this location.",
            "ANIXO did not overwrite it. Only ANIXO-managed installations may be replaced.",
            install_location=str(script.resolve()),
        )
    actual = hashlib.sha256(payload).hexdigest().upper()
    if actual != str(expected_sha256).upper():
        return InstallResult(
            False,
            "ani-cli download failed SHA256 verification; nothing was installed.",
            f"Expected: {expected_sha256}\nReceived: {actual}",
        )
    if not _looks_like_ani_cli(payload):
        return InstallResult(
            False,
            "The ani-cli candidate does not look like the official script.",
            "The download was rejected before the existing installation was touched.",
        )

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(script, payload)
        _make_executable(script)
    except OSError as error:
        return InstallResult(
            False,
            "The verified ani-cli script could not be written.",
            str(error),
        )

    details = [
        f"Installed: {script.resolve()}",
        f"Verified SHA256: {actual}",
        f"Source: {source or ANI_CLI_URL}",
        f"License: GPL-3.0 ({GPL3_URL})",
    ]
    if os.name == "nt" and write_wrapper:
        bash = _find_git_bash()
        if wrapper.exists():
            try:
                original = wrapper.read_text(encoding="utf-8", errors="replace")
                updated = re.sub(
                    r'(?im)^(set\s+"ANI_CLI=)[^"]*(".*)$',
                    lambda match: f'{match.group(1)}{script.resolve()}{match.group(2)}',
                    original,
                )
                if not re.search(r'(?im)^set\s+"ANI_CLI=', updated):
                    updated = original
                _atomic_write_bytes(wrapper, updated.encode("utf-8"))
                details.append(f"Preserved CMD wrapper: {wrapper.resolve()}")
            except OSError as error:
                details.append(f"Script installed, but wrapper preservation failed: {error}")
        elif bash:
            try:
                _atomic_write_bytes(wrapper, _cmd_wrapper(script, bash))
                details.append(f"CMD wrapper: {wrapper.resolve()}")
            except OSError as error:
                details.append(f"Script installed, but CMD wrapper failed: {error}")
        else:
            details.append(
                "Git Bash was not found. Install it (winget install --id Git.Git) "
                "before using the ani-cli.cmd wrapper."
            )

    return InstallResult(
        True,
        f"ani-cli {version or 'verified script'} installed and checksum-verified.",
        "\n".join(details),
        source=source or ANI_CLI_URL,
        version=version,
        install_location=str(script.resolve()),
        sha256=actual,
    )
