"""ANIXO product identity.

This module is the single source of truth for the application name,
version, and public-facing description. Packaging, CLI output, diagnostics,
and the UI all import from here so the brand cannot drift.
"""

from __future__ import annotations

APP_NAME = "ANIXO"
APP_DESCRIPTION = "A modern anime discovery and streaming application."
APP_TAGLINE = "Your anime. Your world."
PUBLISHER = "ANIXO"

__version__ = "0.1.0-beta.5"
VERSION = __version__
APP_VERSION = VERSION

# Beta builds default to the beta release channel; stable builds default to
# stable.  Users can still override the channel in Settings.
DEFAULT_UPDATE_CHANNEL = "beta" if "-" in VERSION else "stable"

# The SQLite filename is internal, but kept in one place so migration can
# locate the legacy file without sprinkling old brand strings through code.
LEGACY_DATA_DIR_NAME = "AnimeHub"
LEGACY_DATABASE_NAME = "animehub.sqlite3"
DATABASE_NAME = "anixo.sqlite3"
