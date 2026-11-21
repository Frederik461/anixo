"""PyInstaller runtime hook for ANIXO (frozen Windows builds only).

This file is injected by ``anixo.spec`` and by the one-file validation build
in ``packaging/build_windows.ps1``. It runs before ``main.py`` is imported.

Responsibilities:

* expose the folder PyInstaller unpacked the bundle into as
  ``ANIXO_BUNDLE_DIR`` and as ``textual.app.App._BASE_PATH``;
* keep Textual's relative ``CSS_PATH`` resolution working when the base path
  is a directory (Textual normally expects a *file* path there). Unfrozen runs
  are left completely untouched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    _bundle_dir = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    os.environ.setdefault("ANIXO_BUNDLE_DIR", _bundle_dir)
    os.environ.setdefault("ANIXO_FROZEN", "1")

    try:
        from textual import app as _textual_app
        from textual import screen as _textual_screen
        from textual.app import App
    except Exception:  # pragma: no cover - Textual is always bundled
        App = None  # type: ignore[assignment]
    else:
        # The application reads App._BASE_PATH to locate bundled resources
        # (styles/theme.tcss, assets/anixo.ico, certifi/cacert.pem).
        if hasattr(sys, "_MEIPASS"):
            App._BASE_PATH = sys._MEIPASS
        else:
            App._BASE_PATH = _bundle_dir

        # Textual's own resolver does ``Path(base_path).parent / path`` because
        # its _BASE_PATH is normally an app *file*. Here it is a directory, so
        # patch the two internal call sites to use the directory as the root.
        # The patch is best-effort: a future Textual that changes this helper
        # must never prevent the application from starting.
        try:
            _original_resolve = _textual_app._make_path_object_relative

            def _resolve_frozen(path, obj):  # type: ignore[no-untyped-def]
                if isinstance(path, (str, os.PathLike)):
                    candidate = Path(path)
                    if not candidate.is_absolute():
                        base = getattr(obj, "_BASE_PATH", None)
                        if base and Path(base).is_dir():
                            return (Path(base) / candidate).resolve()
                return _original_resolve(path, obj)

            _textual_app._make_path_object_relative = _resolve_frozen
            _textual_screen._make_path_object_relative = _resolve_frozen
        except Exception:  # pragma: no cover - depends on Textual internals
            pass
