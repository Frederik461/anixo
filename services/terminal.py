"""Terminal capability detection for image rendering.

This module must be imported before Textual takes ownership of stdin:
``textual-image`` queries the terminal once at import time and caches the
result.  Posters use these capabilities instead of assuming a generic VT340
cell size, which keeps processed raster dimensions aligned with the renderer.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from textual_image._terminal import TerminalError, probe_terminal


@dataclass(frozen=True, slots=True)
class TerminalCapabilities:
    """Cached terminal facts used to select and size an image renderer."""

    is_tty: bool
    sixel: bool
    tgp: bool
    cell_width: int
    cell_height: int

    @property
    def cell_size(self) -> tuple[int, int]:
        return self.cell_width, self.cell_height

    @property
    def preferred_renderer(self) -> str:
        """Return the best renderer this terminal can actually display."""
        if self.sixel:
            return "sixel"
        if self.tgp:
            return "tgp"
        if self.is_tty:
            return "halfcell"
        return "unicode"

    def summary(self) -> str:
        """Short human-readable description for status and test screens."""
        if not self.is_tty:
            return "Unicode fallback (no TTY)"
        if self.sixel:
            detail = "TGP also detected" if self.tgp else "TGP not supported"
            return f"Sixel · {detail}"
        if self.tgp:
            return "TGP · Sixel not detected"
        return "Half-block fallback · no Sixel/TGP detected"


def detect_capabilities() -> TerminalCapabilities:
    """Return the cached ``textual-image`` terminal probe result."""
    try:
        probe = probe_terminal()
    except TerminalError:
        probe = None

    if probe is None:
        return TerminalCapabilities(
            is_tty=False,
            sixel=False,
            tgp=False,
            cell_width=10,
            cell_height=20,
        )

    return TerminalCapabilities(
        is_tty=bool(sys.__stdout__ and sys.__stdout__.isatty()),
        sixel=bool(probe.sixel),
        tgp=bool(probe.tgp),
        cell_width=max(1, probe.cell_size.width),
        cell_height=max(1, probe.cell_size.height),
    )


def resolve_renderer(
    requested: str, capabilities: TerminalCapabilities
) -> tuple[str, str]:
    """Resolve a configured renderer to one that is actually available.

    Returns the effective renderer and a detail note.  An empty note means the
    requested protocol is in use.  Fallbacks and manual overrides are never
    silent: callers can show the note in diagnostics or the image test screen.
    """
    name = (requested or "auto").lower()
    if name == "ansi":
        name = "auto"
    if name == "off":
        return "off", ""

    available = capabilities.preferred_renderer
    native = {"sixel": capabilities.sixel, "tgp": capabilities.tgp}

    if name in {"auto", "sixel", "tgp"}:
        if name == "auto":
            if available == "sixel":
                return "sixel", ""
            if available == "tgp":
                return "tgp", ""
            if available == "unicode":
                fallback = "Sixel/TGP unavailable; using non-interactive Unicode fallback"
            else:
                fallback = "Sixel/TGP unavailable; using high-color half-block fallback"
            return available, fallback
        if native[name]:
            return name, ""
        if capabilities.sixel:
            return "sixel", f"{name.upper()} unavailable; using detected Sixel"
        if capabilities.tgp:
            return "tgp", f"{name.upper()} unavailable; using detected TGP"
        if not capabilities.is_tty:
            return "unicode", f"{name.upper()} unavailable; non-interactive Unicode fallback"
        return "halfcell", f"{name.upper()} unavailable; using high-color half-block fallback"

    if name in {"halfcell", "unicode"}:
        if capabilities.sixel or capabilities.tgp:
            return name, f"Manual override; detected {available.upper()}"
        return name, ""

    return "auto", f"Unknown renderer {requested!r}; using detected {available}"
