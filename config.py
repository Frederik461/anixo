"""Configuration loading, validation, and legacy migration for ANIXO."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from platforms import get_platform_adapter
from version import DEFAULT_UPDATE_CHANNEL



IMAGE_MODES = ("auto", "on", "off")
RENDERERS = ("auto", "sixel", "tgp", "halfcell", "unicode", "off")
THEMES = ("midnight", "aurora", "crimson")

# Hosts whose HTTPS asset URLs are trusted by the in-app updater.  ANIXO reads
# this from Config (or ANIXO_UPDATE_TRUSTED_HOSTS); it is never a hardcoded
# allowlist inside the updater.  Suffix entries are dot-boundary matches only.
DEFAULT_UPDATE_TRUSTED_HOSTS: tuple[str, ...] = (
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "githubusercontent.com",
)

THEME_PALETTES: dict[str, dict[str, str]] = {
    "midnight": {
        "surface": "#08080d",
        "panel": "#11111a",
        "boost": "#1c1c2a",
        "muted": "#8e8ea6",
        "text": "#f7f7fb",
        "secondary": "#4ea8ff",
        "line": "#282838",
        "subtle": "#151521",
    },
    "aurora": {
        "surface": "#061316",
        "panel": "#0c252a",
        "boost": "#143b42",
        "muted": "#8fb6bc",
        "text": "#eafcff",
        "secondary": "#5eead4",
        "line": "#1d4a52",
        "subtle": "#0a1d21",
    },
    "crimson": {
        "surface": "#12090c",
        "panel": "#241318",
        "boost": "#3a1f28",
        "muted": "#bd98a4",
        "text": "#fff3f5",
        "secondary": "#fb7185",
        "line": "#4b2934",
        "subtle": "#1c0e13",
    },
}
ACCENT_PATTERN = re.compile(r"^#[0-9a-f]{6}$", re.IGNORECASE)

_DEFAULT_EXTRA: dict[str, Any] = {
    "page_size": 12,
    "cache_max_entries": 128,
    "terminal_cell_width": 10,
    "terminal_cell_height": 20,
    "ani_cli_base_url": "https://hianime.at",
    "ani_cli_history_path": "",
    "ani_cli_cmd_wrapper": False,
    "confirm_before_clear_history": True,
    "check_for_ani_cli_updates": True,
    "renderer_auto_upgraded": False,
    "preferred_audio": "sub",
    "setup_completed": False,
    "setup_version": "",
    "migration_completed": False,
    "update_channel": DEFAULT_UPDATE_CHANNEL,
    # The source repository is private.  Testers need a public manifest feed
    # URL; no token is ever stored in config.
    "update_repo": "Frederik461/anixo",
    # Release manager settings.  The workflow remains the source of truth;
    # these values only tell the CLI which ref and remote to publish from.
    # An empty release_repo means "derive it from the configured Git remote",
    # independently of the updater's update_repo feed setting.
    "release_repo": "",
    "release_branch": "main",
    "release_remote": "origin",
    "release_workflow": ".github/workflows/release.yml",
    "release_timeout_minutes": 45,
    "update_feed_url": "",
    "update_public_key": "",
    "update_trusted_hosts": list(DEFAULT_UPDATE_TRUSTED_HOSTS),
    "check_for_app_updates": True,
    "last_update_check": "",
    "last_ani_cli_check": "",
    "franchise_selections": {},
}

def _fresh_extra() -> dict[str, Any]:
    """Return an isolated copy of the default extra settings."""
    return {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in _DEFAULT_EXTRA.items()
    }


_FIELD_ALIASES: dict[str, str] = {
    "metadataEndpoint": "metadata_endpoint",
    "anilist_url": "metadata_endpoint",
    "metadataCacheTtlHours": "metadata_cache_ttl_hours",
    "metadata_ttl_hours": "metadata_cache_ttl_hours",
    "artworkRenderer": "artwork_renderer",
    "artworkRendererMode": "artwork_renderer",
    "aniCliPath": "ani_cli_path",
    "mpvPath": "mpv_path",
    "gitBashPath": "git_bash_path",
    "watchlistPath": "watchlist_path",
    "historyPath": "history_path",
    "cachePath": "cache_path",
    "pageSize": "page_size",
    "cacheMaxEntries": "cache_max_entries",
    "terminalCellWidth": "terminal_cell_width",
    "terminalCellHeight": "terminal_cell_height",
    "aniCliBaseUrl": "ani_cli_base_url",
    "aniCliHistoryPath": "ani_cli_history_path",
    "aniCliCmdWrapper": "ani_cli_cmd_wrapper",
    "confirmBeforeClearHistory": "confirm_before_clear_history",
    "checkForAniCliUpdates": "check_for_ani_cli_updates",
    "rendererAutoUpgraded": "renderer_auto_upgraded",
    "preferredAudio": "preferred_audio",
    "audio": "preferred_audio",
    "audioLanguage": "preferred_audio",
    "setupCompleted": "setup_completed",
    "setupVersion": "setup_version",
    "migrationCompleted": "migration_completed",
    "updateChannel": "update_channel",
    "updateRepo": "update_repo",
    "releaseRepo": "release_repo",
    "releaseBranch": "release_branch",
    "releaseRemote": "release_remote",
    "releaseWorkflow": "release_workflow",
    "releaseTimeoutMinutes": "release_timeout_minutes",
    "updateFeedUrl": "update_feed_url",
    "updatePublicKey": "update_public_key",
    "updateTrustedHosts": "update_trusted_hosts",
    "update_trusted_host": "update_trusted_hosts",
    "checkForAppUpdates": "check_for_app_updates",
    "lastUpdateCheck": "last_update_check",
    "lastAniCliCheck": "last_ani_cli_check",
}


def expand_path(value: str) -> str:
    """Expand environment variables and the user home in a path."""
    return os.path.expandvars(os.path.expanduser(value or ""))


def get_franchise_selection(config: "Config", key: str) -> dict[str, str] | None:
    """Return the remembered provider entry for a franchise, if any."""
    selections = config.extra.get("franchise_selections")
    if not isinstance(selections, Mapping):
        return None
    value = selections.get(key)
    if not isinstance(value, Mapping):
        return None
    source_id = str(value.get("source_id") or "").strip()
    title = str(value.get("title") or "").strip()
    if not source_id or not title:
        return None
    return {
        "source_id": source_id,
        "title": title,
        "episode": str(value.get("episode") or "").strip(),
    }


def remember_franchise_selection(
    config: "Config",
    key: str,
    source_id: str,
    title: str,
    episode: str | None = None,
) -> dict[str, str]:
    """Persist the exact provider entry chosen for a franchise."""
    selections = config.extra.get("franchise_selections")
    if not isinstance(selections, dict):
        selections = {}
        config.extra["franchise_selections"] = selections
    record = {
        "source_id": str(source_id),
        "title": str(title),
        "episode": str(episode) if episode else "",
    }
    selections[key] = record
    return record


def remember_franchise_episode(config: "Config", source_id: str, episode: str) -> None:
    """Update the remembered episode for the entry with this provider ID."""
    selections = config.extra.get("franchise_selections")
    if not isinstance(selections, dict):
        return
    for value in selections.values():
        if isinstance(value, dict) and str(value.get("source_id") or "") == source_id:
            value["episode"] = str(episode)
            return


def default_data_dir() -> Path:
    """Return the OS-correct user data directory."""
    return get_platform_adapter().data_dir()


def default_config_path() -> Path:
    return get_platform_adapter().config_path()


def default_cache_dir() -> Path:
    return get_platform_adapter().cache_dir()


def default_log_dir() -> Path:
    return get_platform_adapter().log_dir()


def legacy_data_dir() -> Path:
    """Return the legacy AnimeHub data directory for migration only."""
    return get_platform_adapter().legacy_data_dir()


@dataclass(slots=True, init=False)
class Config:
    """User-adjustable settings, compatible with the legacy TypeScript schema."""

    theme: str = "midnight"
    accent: str = "#A78BFA"
    animations: bool = True
    images: str = "auto"
    artwork_renderer: str = "auto"
    ani_cli_path: str = ""
    mpv_path: str = ""
    git_bash_path: str = ""
    metadata_endpoint: str = "https://graphql.anilist.co"
    metadata_cache_ttl_hours: int = 12
    watchlist_path: str = ""
    history_path: str = ""
    cache_path: str = ""
    extra: dict[str, Any] = field(default_factory=_fresh_extra)

    def __init__(self, **values: Any) -> None:
        """Accept canonical fields, legacy aliases, and extra settings as kwargs."""
        known = {item.name for item in fields(type(self))}
        extra = _fresh_extra()
        for name in known:
            setattr(self, name, None)
        self.extra = extra

        nested = values.pop("nested", None)
        if isinstance(values.get("extra"), Mapping):
            extra.update(values.pop("extra"))
        if isinstance(nested, Mapping):
            values.update(nested)

        for key, value in values.items():
            canonical = _FIELD_ALIASES.get(key, key)
            if canonical in known:
                setattr(self, canonical, value)
            elif canonical in extra:
                extra[canonical] = value

        defaults = Config._field_defaults()
        for name in known:
            if getattr(self, name) is None:
                setattr(self, name, defaults[name])
        if not isinstance(self.extra, dict):
            self.extra = _fresh_extra()
        self.validate()

    @classmethod
    def _field_defaults(cls) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in fields(cls):
            if item.default is not MISSING:
                result[item.name] = item.default
            elif item.default_factory is not MISSING:  # type: ignore[misc]
                result[item.name] = item.default_factory()  # type: ignore[misc]
        return result

    # ------------------------------------------------------------------
    # Compatibility properties used by the Python implementation.

    @property
    def anilist_url(self) -> str:
        return self.metadata_endpoint

    @anilist_url.setter
    def anilist_url(self, value: str) -> None:
        self.metadata_endpoint = str(value)

    @property
    def metadata_ttl_hours(self) -> int:
        return self.metadata_cache_ttl_hours

    @metadata_ttl_hours.setter
    def metadata_ttl_hours(self, value: int) -> None:
        self.metadata_cache_ttl_hours = int(value)

    @property
    def ani_cli_base_url(self) -> str:
        return str(self.extra.get("ani_cli_base_url") or "https://hianime.at")

    @ani_cli_base_url.setter
    def ani_cli_base_url(self, value: str) -> None:
        self.extra["ani_cli_base_url"] = str(value)

    @property
    def ani_cli_history_path(self) -> str:
        return str(self.extra.get("ani_cli_history_path") or "")

    @ani_cli_history_path.setter
    def ani_cli_history_path(self, value: str) -> None:
        self.extra["ani_cli_history_path"] = str(value)

    @property
    def ani_cli_cmd_wrapper(self) -> bool:
        return bool(self.extra.get("ani_cli_cmd_wrapper", False))

    @ani_cli_cmd_wrapper.setter
    def ani_cli_cmd_wrapper(self, value: bool) -> None:
        self.extra["ani_cli_cmd_wrapper"] = bool(value)

    @property
    def page_size(self) -> int:
        return int(self.extra.get("page_size", 12))

    @page_size.setter
    def page_size(self, value: int) -> None:
        self.extra["page_size"] = int(value)

    @property
    def cache_max_entries(self) -> int:
        return int(self.extra.get("cache_max_entries", 128))

    @cache_max_entries.setter
    def cache_max_entries(self, value: int) -> None:
        self.extra["cache_max_entries"] = int(value)

    @property
    def terminal_cell_width(self) -> int:
        return int(self.extra.get("terminal_cell_width", 10))

    @terminal_cell_width.setter
    def terminal_cell_width(self, value: int) -> None:
        self.extra["terminal_cell_width"] = int(value)

    @property
    def terminal_cell_height(self) -> int:
        return int(self.extra.get("terminal_cell_height", 20))

    @terminal_cell_height.setter
    def terminal_cell_height(self, value: int) -> None:
        self.extra["terminal_cell_height"] = int(value)

    @property
    def confirm_before_clear_history(self) -> bool:
        return bool(self.extra.get("confirm_before_clear_history", True))

    @confirm_before_clear_history.setter
    def confirm_before_clear_history(self, value: bool) -> None:
        self.extra["confirm_before_clear_history"] = bool(value)

    @property
    def check_for_ani_cli_updates(self) -> bool:
        return bool(self.extra.get("check_for_ani_cli_updates", True))

    @check_for_ani_cli_updates.setter
    def check_for_ani_cli_updates(self, value: bool) -> None:
        self.extra["check_for_ani_cli_updates"] = bool(value)

    @property
    def preferred_audio(self) -> str:
        """Audio track requested from ani-cli: ``sub`` or ``dub``."""
        value = str(self.extra.get("preferred_audio") or "sub").lower()
        return "dub" if value == "dub" else "sub"

    @preferred_audio.setter
    def preferred_audio(self, value: str) -> None:
        normalized = str(value or "sub").lower()
        self.extra["preferred_audio"] = "dub" if normalized == "dub" else "sub"

    @property
    def setup_completed(self) -> bool:
        return bool(self.extra.get("setup_completed", False))

    @setup_completed.setter
    def setup_completed(self, value: bool) -> None:
        self.extra["setup_completed"] = bool(value)

    @property
    def setup_version(self) -> str:
        return str(self.extra.get("setup_version") or "")

    @setup_version.setter
    def setup_version(self, value: str) -> None:
        self.extra["setup_version"] = str(value)

    @property
    def migration_completed(self) -> bool:
        return bool(self.extra.get("migration_completed", False))

    @migration_completed.setter
    def migration_completed(self, value: bool) -> None:
        self.extra["migration_completed"] = bool(value)

    @property
    def update_channel(self) -> str:
        value = str(self.extra.get("update_channel") or "stable").lower()
        return "beta" if value == "beta" else "stable"

    @update_channel.setter
    def update_channel(self, value: str) -> None:
        normalized = str(value or "stable").lower()
        self.extra["update_channel"] = "beta" if normalized == "beta" else "stable"

    @property
    def update_repo(self) -> str:
        value = str(self.extra.get("update_repo") or "").strip()
        return (
            value
            or os.environ.get("ANIXO_UPDATE_REPO", "").strip()
            or "Frederik461/anixo"
        )

    @update_repo.setter
    def update_repo(self, value: str) -> None:
        self.extra["update_repo"] = str(value).strip()

    @property
    def release_repo(self) -> str:
        return str(self.extra.get("release_repo") or "").strip()

    @release_repo.setter
    def release_repo(self, value: str) -> None:
        self.extra["release_repo"] = str(value or "").strip()

    @property
    def release_branch(self) -> str:
        return str(self.extra.get("release_branch") or "main").strip() or "main"

    @release_branch.setter
    def release_branch(self, value: str) -> None:
        self.extra["release_branch"] = str(value).strip() or "main"

    @property
    def release_remote(self) -> str:
        return str(self.extra.get("release_remote") or "origin").strip() or "origin"

    @release_remote.setter
    def release_remote(self, value: str) -> None:
        self.extra["release_remote"] = str(value).strip() or "origin"

    @property
    def release_workflow(self) -> str:
        value = str(self.extra.get("release_workflow") or "").strip()
        return value or ".github/workflows/release.yml"

    @release_workflow.setter
    def release_workflow(self, value: str) -> None:
        self.extra["release_workflow"] = str(value).strip()

    @property
    def release_timeout_minutes(self) -> int:
        try:
            minutes = int(self.extra.get("release_timeout_minutes", 45))
        except (TypeError, ValueError):
            return 45
        return max(5, min(180, minutes))

    @release_timeout_minutes.setter
    def release_timeout_minutes(self, value: int | str) -> None:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            minutes = 45
        self.extra["release_timeout_minutes"] = max(5, min(180, minutes))

    @property
    def update_feed_url(self) -> str:
        return str(
            self.extra.get("update_feed_url")
            or os.environ.get("ANIXO_UPDATE_FEED_URL", "")
            or ""
        ).strip()

    @update_feed_url.setter
    def update_feed_url(self, value: str) -> None:
        self.extra["update_feed_url"] = str(value).strip()

    @property
    def update_public_key(self) -> str:
        return str(
            self.extra.get("update_public_key")
            or os.environ.get("ANIXO_UPDATE_PUBLIC_KEY", "")
            or ""
        ).strip()

    @update_public_key.setter
    def update_public_key(self, value: str) -> None:
        self.extra["update_public_key"] = str(value).strip()

    @staticmethod
    def _normalize_hosts(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            parts: list[Any] = re.split(r"[,\s]+", value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            parts = list(value)
        else:
            return ()
        hosts: list[str] = []
        for item in parts:
            text = str(item or "").strip()
            if not text:
                continue
            host = urlparse(text).hostname if "://" in text else text
            host = str(host or "").strip().lower()
            if host and host not in hosts:
                hosts.append(host)
        return tuple(hosts)

    @property
    def update_trusted_hosts(self) -> tuple[str, ...]:
        """HTTPS hosts trusted for update feeds and downloaded assets."""
        configured = self.extra.get("update_trusted_hosts")
        hosts = self._normalize_hosts(configured)
        if not hosts:
            hosts = self._normalize_hosts(
                os.environ.get("ANIXO_UPDATE_TRUSTED_HOSTS", "")
            )
        return hosts or DEFAULT_UPDATE_TRUSTED_HOSTS

    @update_trusted_hosts.setter
    def update_trusted_hosts(self, value: Any) -> None:
        self.extra["update_trusted_hosts"] = list(self._normalize_hosts(value))

    @property
    def check_for_app_updates(self) -> bool:
        value = self.extra.get("check_for_app_updates", True)
        return value if isinstance(value, bool) else str(value).lower() != "false"

    @check_for_app_updates.setter
    def check_for_app_updates(self, value: bool) -> None:
        self.extra["check_for_app_updates"] = bool(value)

    @property
    def last_update_check(self) -> str:
        return str(self.extra.get("last_update_check") or "")

    @last_update_check.setter
    def last_update_check(self, value: str) -> None:
        self.extra["last_update_check"] = str(value)

    @property
    def last_ani_cli_check(self) -> str:
        return str(self.extra.get("last_ani_cli_check") or "")

    @last_ani_cli_check.setter
    def last_ani_cli_check(self, value: str) -> None:
        self.extra["last_ani_cli_check"] = str(value)

    # ------------------------------------------------------------------

    def validate(self) -> None:
        if self.theme not in THEMES:
            self.theme = "midnight"
        if not isinstance(self.accent, str) or not ACCENT_PATTERN.match(self.accent):
            self.accent = "#A78BFA"
        else:
            self.accent = self.accent.upper()
        self.animations = bool(self.animations)
        if self.images not in IMAGE_MODES:
            self.images = "auto"
        if self.artwork_renderer not in RENDERERS:
            self.artwork_renderer = "auto"
        for attribute in (
            "ani_cli_path",
            "mpv_path",
            "git_bash_path",
            "watchlist_path",
            "history_path",
            "cache_path",
        ):
            value = getattr(self, attribute)
            setattr(self, attribute, str(value) if value is not None else "")
        if not self.metadata_endpoint:
            self.metadata_endpoint = "https://graphql.anilist.co"
        try:
            ttl = int(self.metadata_cache_ttl_hours)
        except (TypeError, ValueError):
            ttl = 12
        self.metadata_cache_ttl_hours = max(1, min(168, ttl))

        merged_extra = _fresh_extra()
        if isinstance(self.extra, Mapping):
            for key, value in self.extra.items():
                if key in _DEFAULT_EXTRA:
                    merged_extra[key] = value
        self.extra = merged_extra
        self.page_size = max(4, min(48, int(self.page_size)))
        self.cache_max_entries = max(8, min(2048, int(self.cache_max_entries)))
        self.terminal_cell_width = max(4, min(40, int(self.terminal_cell_width)))
        self.terminal_cell_height = max(6, min(60, int(self.terminal_cell_height)))
        self.ani_cli_base_url = str(self.ani_cli_base_url).strip() or "https://hianime.at"
        self.preferred_audio = self.preferred_audio
        self.setup_completed = self.setup_completed
        self.setup_version = self.setup_version
        self.migration_completed = self.migration_completed
        self.update_channel = self.update_channel
        self.update_repo = self.update_repo
        self.release_repo = self.release_repo
        self.release_branch = self.release_branch
        self.release_remote = self.release_remote
        self.release_workflow = self.release_workflow
        self.release_timeout_minutes = self.release_timeout_minutes
        self.update_feed_url = self.update_feed_url
        self.update_public_key = self.update_public_key
        self.update_trusted_hosts = self.update_trusted_hosts
        self.check_for_app_updates = self.check_for_app_updates
        self.last_update_check = self.last_update_check
        self.last_ani_cli_check = self.last_ani_cli_check

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Config":
        known = {item.name for item in fields(cls)}
        values: dict[str, Any] = {}
        extra: dict[str, Any] = _fresh_extra()

        for key, value in data.items():
            if key == "extra" and isinstance(value, Mapping):
                extra.update(value)
                continue
            canonical = _FIELD_ALIASES.get(key, key)
            if canonical in known:
                values[canonical] = value
            elif canonical in _DEFAULT_EXTRA:
                extra[canonical] = value

        nested = {
            "anilist": ("metadata_endpoint", "url"),
            "aniCli": ("ani_cli_path", "path"),
            "ani_cli": ("ani_cli_path", "path"),
            "metadata": ("metadata_cache_ttl_hours", "ttlHours"),
            "cache": ("cache_max_entries", "maxEntries"),
        }
        for section, (target, child) in nested.items():
            raw = data.get(section)
            if isinstance(raw, Mapping) and child in raw and target not in values:
                values[target] = raw[child]

        config = cls(**values, extra=extra)
        config.validate()
        return config

    def to_mapping(self) -> dict[str, Any]:
        result = asdict(self)
        extra = result.pop("extra", {}) or {}
        for key, value in extra.items():
            result.setdefault(key, value)
        return result


class ConfigStore:
    """Reads and writes config.json with legacy-compatible validation."""

    def __init__(
        self,
        path: Path | str | None = None,
        data_dir: Path | str | None = None,
    ) -> None:
        if path is None:
            path = default_config_path()
        self.path = Path(path).resolve()
        self.data_dir = Path(data_dir).resolve() if data_dir else self.path.parent
        self.legacy_path = self.path

    def load(self) -> Config:
        if not self.path.exists():
            return Config()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._quarantine()
            return Config()
        if not isinstance(raw, Mapping):
            self._quarantine()
            return Config()
        config = Config.from_mapping(raw)
        # Earlier Python builds downgraded the legacy "auto" setting to the
        # deterministic half-block renderer. Upgrade that generated value once
        # so existing installs get Sixel when the terminal supports it. A user
        # who explicitly picks "halfcell" afterwards is left alone.
        upgraded = bool(config.extra.get("renderer_auto_upgraded", False))
        if (
            config.artwork_renderer == "halfcell"
            and raw.get("artworkRenderer") != "halfcell"
            and not upgraded
        ):
            config.artwork_renderer = "auto"
            config.extra["renderer_auto_upgraded"] = True
            self.save(config)
        return config

    def save(self, config: Config) -> Config:
        config.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
        )
        temporary.write_text(
            json.dumps(config.to_mapping(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return config

    def _quarantine(self) -> None:
        try:
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-{int(time.time() * 1000)}.bak"
            )
            self.path.replace(backup)
        except OSError:
            pass
