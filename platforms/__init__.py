"""Operating-system adapters used by ANIXO.

Application code should ask for an adapter instead of branching on
``sys.platform`` in feature code.  The adapters own path conventions, shell
discovery, and OS launcher details; everything else consumes their results.
"""

from __future__ import annotations

from .adapter import (
    LINUX,
    MACOS,
    WINDOWS,
    PlatformAdapter,
    get_installed_platform_id,
    get_platform_adapter,
)

__all__ = [
    "LINUX",
    "MACOS",
    "WINDOWS",
    "PlatformAdapter",
    "get_installed_platform_id",
    "get_platform_adapter",
]
