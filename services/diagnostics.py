"""Support-friendly ANIXO environment reports and export helpers."""

from __future__ import annotations

import platform
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from config import Config
from services.dependencies import diagnostics as dependency_diagnostics
from services.setup import check_network
from services.terminal import resolve_renderer

_SECRET_VALUE = re.compile(
    r"(?P<label>\b(?:api[ _-]?key|apikey|access[ _-]?token|refresh[ _-]?token|"
    r"token|password|passwd|pwd|secret|client[ _-]?secret|credential|"
    r"authorization|auth)\b)"
    r"(?P<separator>\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\s\"',;]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>https?://)(?P<credentials>[^/@\s]+:[^/@\s]+)@",
    re.IGNORECASE,
)


def _redact(value: object) -> str:
    """Remove tokens, passwords, secrets, and URL credentials from text."""
    text = str(value)
    text = _SECRET_VALUE.sub(
        lambda match: (
            f"{match.group('label')}{match.group('separator')}"
            f"{match.group('quote')}<redacted>"
        ),
        text,
    )
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _URL_CREDENTIALS.sub(
        lambda match: f"{match.group('scheme')}<redacted>@", text
    )
    return text


def build_report(
    *,
    app_version: str,
    install_dir: Path,
    data_dir: Path,
    log_dir: Path,
    config: Config,
    ani_cli: Any,
    capabilities: Any,
    cache_counts: Mapping[str, int],
) -> str:
    """Build a complete, redacted plain-text environment report."""
    dependency_lines = [str(line) for line in dependency_diagnostics(config, ani_cli)]

    invocation = None
    if ani_cli is not None:
        try:
            invocation = ani_cli.resolve_invocation()
        except (AttributeError, OSError, RuntimeError):
            invocation = None
    if invocation is not None:
        ani_status = (
            f"{getattr(invocation, 'display_path', 'detected')} "
            f"({getattr(invocation, 'kind', 'unknown')})"
        )
    else:
        ani_status = "not found"

    renderer, note = resolve_renderer(str(config.artwork_renderer), capabilities)
    try:
        terminal_summary = str(capabilities.summary())
    except AttributeError:
        terminal_summary = str(capabilities)
    width = getattr(capabilities, "cell_width", config.terminal_cell_width)
    height = getattr(capabilities, "cell_height", config.terminal_cell_height)

    frozen = bool(getattr(sys, "frozen", False))
    runtime = "frozen" if frozen else "source"
    runtime_path = (
        str(getattr(sys, "_MEIPASS", "bundled runtime"))
        if frozen
        else str(sys.executable)
    )

    report: list[str] = []

    def add(label: str, value: object) -> None:
        report.append(f"{label:<18}{_redact(value)}")

    def section(title: str) -> None:
        report.extend(("", title, "-" * len(title)))

    report.extend(("ANIXO DIAGNOSTIC REPORT", "=" * 23))
    add("Generated:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    add("ANIXO version:", app_version)
    add("Windows version:", f"{platform.system()} {platform.release()} ({platform.version()})")
    add("CPU architecture:", platform.machine() or platform.architecture()[0])
    add("Install path:", install_dir)
    add("User data path:", data_dir)
    add("Log path:", log_dir)

    section("Python runtime")
    add("Mode:", f"{runtime} (Python {platform.python_version()})")
    add("Executable:", runtime_path)

    section("Playback")
    add("ani-cli:", ani_status)
    for line in dependency_lines:
        if line.startswith(("mpv:", "Git Bash:")):
            report.append(_redact(line))

    section("External dependencies")
    for line in dependency_lines:
        report.append(_redact(line))

    section("Image rendering")
    add("Images:", config.images)
    add("Protocol:", f"{renderer} — {note}" if note else renderer)
    add("Terminal:", terminal_summary)
    add("Cell size:", f"{width}x{height} px")

    section("Network")
    add("AniList API:", "reachable" if check_network() else "unreachable")

    section("Cache")
    if cache_counts:
        for name in sorted(cache_counts):
            add(f"{name}:", cache_counts[name])
        add("Total:", sum(cache_counts.values()))
    else:
        report.append("No cache statistics available.")

    return "\n".join(report).rstrip() + "\n"


def export_report(report: str, log_dir: Path) -> Path:
    """Write ``report`` to a timestamped UTF-8 file in ``log_dir``."""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"anixo-diagnostic-{datetime.now():%Y%m%d-%H%M%S}.txt"
    text = report if str(report).endswith("\n") else f"{report}\n"
    target.write_text(str(text), encoding="utf-8")
    return target
