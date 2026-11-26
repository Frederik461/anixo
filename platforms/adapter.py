"""Portable path, shell, and launcher adapters for ANIXO.

The adapter deliberately reads environment variables when a method is called
rather than at import time.  That keeps configuration overrides, portable
launchers, and tests deterministic on every host.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from version import APP_NAME

WINDOWS = "Windows"
MACOS = "Darwin"
LINUX = "Linux"

_INSTALL_IDS = {WINDOWS: "windows", MACOS: "macos", LINUX: "linux"}


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value or ""))


@dataclass(frozen=True, slots=True)
class PlatformAdapter:
    """OS-specific behavior behind a small, testable interface."""

    system: str
    app_name: str = APP_NAME

    @property
    def id(self) -> str:
        """Stable identifier used by manifest filenames and release assets."""
        return _INSTALL_IDS.get(self.system, self.system.lower() or "unknown")

    @property
    def is_windows(self) -> bool:
        return self.system == WINDOWS

    @property
    def is_macos(self) -> bool:
        return self.system == MACOS

    @property
    def is_linux(self) -> bool:
        return self.system == LINUX

    @property
    def display_name(self) -> str:
        return {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(
            self.system, self.system or "Unknown"
        )

    def _xdg(self, name: str, default: str) -> Path:
        value = os.environ.get(name)
        root = Path(_expand(value)) if value else Path.home() / default
        return root.resolve()

    def _env_path(self, *names: str) -> Path | None:
        for name in names:
            value = os.environ.get(name)
            if value:
                return Path(_expand(value)).resolve()
        return None

    # ------------------------------------------------------------------
    # User data locations

    def data_dir(self) -> Path:
        override = self._env_path("ANIXO_DATA_DIR", "ANIME_HUB_DATA_DIR")
        if override:
            return override
        if self.is_windows:
            appdata = os.environ.get("APPDATA")
            if appdata:
                return Path(_expand(appdata)) / self.app_name
            return Path.home() / ".config" / self.app_name
        if self.is_macos:
            return Path.home() / "Library" / "Application Support" / self.app_name
        return self._xdg("XDG_CONFIG_HOME", ".config") / self.app_name

    def config_path(self) -> Path:
        override = self._env_path("ANIXO_CONFIG", "ANIME_HUB_CONFIG")
        if override:
            return override
        return self.data_dir() / "config.json"

    def state_dir(self) -> Path:
        """Local (non-roaming) application state root."""
        if self.is_windows:
            localappdata = os.environ.get("LOCALAPPDATA")
            if localappdata:
                return Path(_expand(localappdata)) / self.app_name
            return Path.home() / ".local" / "state" / self.app_name
        if self.is_macos:
            return Path.home() / "Library" / "Application Support" / self.app_name
        return self._xdg("XDG_STATE_HOME", ".local/state") / self.app_name

    def cache_dir(self) -> Path:
        override = self._env_path("ANIXO_CACHE_DIR")
        if override:
            return override
        if self.is_windows:
            return self.state_dir() / "cache"
        if self.is_macos:
            return Path.home() / "Library" / "Caches" / self.app_name
        return self._xdg("XDG_CACHE_HOME", ".cache") / self.app_name

    def log_dir(self) -> Path:
        override = self._env_path("ANIXO_LOG_DIR")
        if override:
            return override
        if self.is_windows:
            return self.state_dir() / "logs"
        if self.is_macos:
            return Path.home() / "Library" / "Logs" / self.app_name
        return self._xdg("XDG_STATE_HOME", ".local/state") / self.app_name / "log"

    def legacy_data_dir(self) -> Path:
        """Legacy AnimeHub data location used by migration only."""
        if self.is_windows:
            appdata = os.environ.get("APPDATA")
            if appdata:
                return Path(_expand(appdata)) / "AnimeHub"
            return Path.home() / ".config" / "AnimeHub"
        if self.is_macos:
            return Path.home() / "Library" / "Application Support" / "AnimeHub"
        return self._xdg("XDG_CONFIG_HOME", ".config") / "AnimeHub"

    def tools_dir(self, cache_dir: Path) -> Path:
        """ANIXO-private directory for downloaded executables and scripts."""
        return Path(cache_dir).resolve().parent / "tools"

    def managed_ani_cli_dir(self, data_dir: Path, cache_dir: Path) -> Path:
        """Persistent, dedicated root for ANIXO's own ani-cli copy.

        On Windows this is the sibling of the cache under LOCALAPPDATA, so an
        ANIXO application update never replaces it.  On other platforms it
        lives under the persistent application data directory.  An explicit
        environment override keeps tests and portable layouts deterministic.
        """
        override = self._env_path("ANIXO_ANI_CLI_DIR")
        if override:
            return override
        if self.is_windows:
            return Path(cache_dir).resolve().parent / "ani-cli"
        return Path(data_dir).resolve() / "ani-cli"

    def downloads_dir(self) -> Path:
        return Path.home() / "Downloads"

    def expected_install_roots(self) -> tuple[Path, ...]:
        """Application directories a standard user install may occupy."""
        if self.is_windows:
            localappdata = os.environ.get("LOCALAPPDATA")
            roots = []
            if localappdata:
                roots.append(Path(_expand(localappdata)) / "Programs" / self.app_name)
            return tuple(path.resolve() for path in roots)
        if self.is_macos:
            return (
                (Path("/Applications") / f"{self.app_name}.app").resolve(),
                (Path.home() / "Applications" / f"{self.app_name}.app").resolve(),
            )
        return (
            (Path.home() / ".local" / "Applications" / self.app_name).resolve(),
            (Path.home() / "Applications" / self.app_name).resolve(),
        )

    def is_expected_install_root(self, path: Path, *, portable: bool = False) -> bool:
        """Return whether ``path`` is a standard or trusted portable install."""
        candidate = Path(path).resolve()
        if candidate in self.expected_install_roots():
            return True
        if not portable:
            return False
        home = Path.home().resolve()
        return candidate != home and home in candidate.parents

    # ------------------------------------------------------------------
    # External process integration

    def bash_executable(self, configured: str = "") -> Path | None:
        """Return a usable bash executable, including Windows Git Bash."""
        candidates: list[str] = []
        if configured:
            candidates.append(configured)
        if self.is_windows:
            git_root = os.environ.get("GIT_INSTALL_ROOT")
            if git_root:
                candidates.append(str(Path(git_root) / "bin" / "bash.exe"))
            candidates.extend(
                (
                    str(
                        Path.home()
                        / "scoop"
                        / "apps"
                        / "git"
                        / "current"
                        / "bin"
                        / "bash.exe"
                    ),
                    r"C:\Program Files\Git\bin\bash.exe",
                )
            )
            found = shutil.which("bash.exe") or shutil.which("bash")
        else:
            candidates.extend(("/bin/bash", "/usr/bin/bash"))
            found = shutil.which("bash")

        all_candidates = list(candidates)
        if found:
            all_candidates.append(found)
        for candidate in all_candidates:
            if not candidate:
                continue
            path = Path(_expand(candidate))
            if path.exists():
                return path.resolve()
        return None

    def to_shell_path(self, value: str | Path) -> str:
        """Convert a native path to the form expected by a shell invocation."""
        text = str(value)
        if not self.is_windows:
            return text
        normalized = text.replace("\\", "/")
        drive = normalized[:2]
        if len(normalized) > 2 and drive[1] == ":" and drive[0].isalpha():
            return f"/{drive[0].lower()}/{normalized[3:]}"
        return normalized

    def supports_cmd_wrappers(self) -> bool:
        return self.is_windows

    def open_command(self, path: Path) -> tuple[str, ...] | None:
        """Command that asks the desktop environment to open ``path``."""
        target = str(Path(path))
        if self.is_macos:
            return ("open", target)
        if self.is_linux:
            return ("xdg-open", target)
        return None  # Windows callers use os.startfile.

    def installer_suffixes(self) -> tuple[str, ...]:
        if self.is_windows:
            return (".exe",)
        if self.is_macos:
            return (".dmg", ".pkg", ".app.tar.gz", ".zip")
        return (".appimage", ".tar.gz", ".zip")

    def app_bundle_names(self) -> tuple[str, ...]:
        if self.is_macos:
            return ("ANIXO.app",)
        if self.is_windows:
            return ("ANIXO",)
        return ("ANIXO", "anixo")


def get_platform_adapter(system: str | None = None) -> PlatformAdapter:
    """Return the adapter for ``system`` or the current operating system."""
    return PlatformAdapter(system or platform.system() or sys_platform_fallback())


def sys_platform_fallback() -> str:
    import sys

    return {"win32": WINDOWS, "darwin": MACOS}.get(sys.platform, LINUX)


def get_installed_platform_id(system: str | None = None) -> str:
    return get_platform_adapter(system).id
