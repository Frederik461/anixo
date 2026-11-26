"""ANIXO Textual application shell."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from textual import on
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from components.confirm_dialog import ConfirmDialog
from components.resume_dialog import ResumeDialog
from config import (
    THEME_PALETTES,
    Config,
    ConfigStore,
    default_cache_dir,
    default_data_dir,
    default_log_dir,
    expand_path,
    get_franchise_selection,
    legacy_data_dir as default_legacy_data_dir,
    remember_franchise_episode,
    remember_franchise_selection,
)
from data.database import Database
from models.anime import Anime, HistoryEntry, is_same_anime, next_episode
from models.episode import episode_number, is_series_completed
from platforms import get_platform_adapter
from screens.anime_details import AnimeDetailsScreen
from screens.crash_report import CrashReportScreen
from screens.dashboard import DashboardScreen
from screens.diagnostics import DiagnosticsScreen
from screens.discover import DiscoverScreen
from screens.episode_selector import EpisodeSelectorScreen
from screens.history import HistoryScreen
from screens.render_test import RenderTestScreen
from screens.season_selector import SeasonSelectorScreen
from screens.setup import SetupScreen
from screens.settings import SettingsScreen
from screens.updates import UpdateScreen
from screens.watchlist import WatchlistScreen
from services.ani_cli import AniCliService
from services.ani_cli_updater import AniCliUpdateService
from services.anime_api import AnimeAPI
from services.dependency_manager import DependencyManager
from services.franchise import Franchise, group_franchises
from services.artwork import ArtworkService
from services.crash_report import CrashReporter
from services.diagnostics import build_report, export_report
from services.image_cache import ImageCache
from services.install_manifest import COMPONENT_APP, ManifestStore
from services.migration import import_legacy_artwork
from services.provider import ProviderAnime, ProviderError, ProviderService
from services.setup import SetupSnapshot, build_snapshot, dependency_statuses
from services.terminal import detect_capabilities, resolve_renderer
from services.update_log import redact_update_text
from services.uninstall import (
    UninstallMode,
    UninstallPlanner,
    UninstallSafetyError,
    write_pending,
)
from services.updater import (
    DownloadProgress,
    ReleaseInfo,
    UpdateService,
    UpdateState,
)
from version import APP_NAME, APP_TAGLINE, DATABASE_NAME, LEGACY_DATABASE_NAME, VERSION

_CSS = ""  # Styles live in styles/theme.tcss.


def _dim_color(value: str, factor: float = 0.55) -> str:
    raw = value.lstrip("#")
    if len(raw) != 6:
        return "#6d28d9"
    channels = [
        max(0, min(255, int(int(raw[index : index + 2], 16) * factor)))
        for index in (0, 2, 4)
    ]
    return "#{:02x}{:02x}{:02x}".format(*channels)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        with Container(id="help-dialog"):
            yield Static(f"{APP_NAME}  •  CONTROLS", classes="help-title")
            yield Static(
                "D / G / W / H / S   Jump to a screen\n"
                "Tab / Shift+Tab     Cycle screens\n"
                "Alt+← / Alt+→       Back / forward\n"
                "← / →               Move selection\n"
                "↑ / ↓               Section / page / row\n"
                "Enter               Watch, resume, or open\n"
                "Esc                 Back\n"
                "a                   Watchlist\n"
                "d                   Details / remove\n"
                "e                   Select episode\n"
                "m                   Mark watched / unwatched\n"
                "Shift+M             Mark unwatched\n"
                "u                   Toggle sub / dub (episode browser)\n"
                "↑↓←→ / PgUp PgDn     Episode browser navigation\n"
                "/                   Search or filter\n"
                "Space               Grid / list (Discover)\n"
                "c / [ / ] / s       Discover category / genre / sort\n"
                "?                   This help\n"
                "V                   Image rendering test\n"
                "X                   Diagnostics\n"
                "Q                   Quit",
                classes="help-body",
            )
            with Horizontal(classes="help-actions"):
                yield Button("Close", id="help-close", classes="action-button primary")

    @on(Button.Pressed, "#help-close")
    def on_help_close(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.pop_screen()


class AnixoApp(App[None]):
    TITLE = APP_NAME
    CSS = _CSS
    CSS_PATH = "styles/theme.tcss"
    BINDINGS = [
        Binding("D", "goto_dashboard", "Dashboard", show=False),
        Binding("G", "goto_discover", "Discover", show=False),
        Binding("W", "goto_watchlist", "Watchlist", show=False),
        Binding("H", "goto_history", "History", show=False),
        Binding("S", "goto_settings", "Settings", show=False),
        Binding("U", "open_updates", "Updates", show=False),
        Binding("V", "goto_render_test", "Image test", show=False),
        Binding("X", "goto_diagnostics", "Diagnostics", show=False),
        Binding("question_mark,f1", "help", "Help"),
        Binding("tab", "cycle_route(1)", "Next screen", show=False, priority=True),
        Binding(
            "shift+tab",
            "cycle_route(-1)",
            "Previous screen",
            show=False,
            priority=True,
        ),
        Binding("alt+left", "navigate_back", "Back", show=False),
        Binding("alt+right", "navigate_forward", "Forward", show=False),
        Binding("q", "quit", "Quit", show=False),
        Binding("escape", "goto_dashboard", "Dashboard", show=False),
    ]

    def __init__(
        self,
        config: Config | None = None,
        data_dir: Path | None = None,
        legacy_data_dir: Path | None = None,
        config_path: Path | None = None,
        force_setup: bool = False,
        crash_reporter: CrashReporter | None = None,
    ) -> None:
        self.config = config or Config()
        self.config.validate()
        self.capabilities = detect_capabilities()
        if getattr(sys, "frozen", False):
            # Textual resolves CSS_PATH relative to this attribute when frozen.
            self._BASE_PATH = str(getattr(sys, "_MEIPASS", Path.cwd()))
        super().__init__()

        self._explicit_data_dir = data_dir is not None
        self.data_dir = data_dir or default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(
            config_path or (self.data_dir / "config.json")
        ).resolve()
        self.config_store = ConfigStore(
            path=self.config_path,
            data_dir=self.data_dir,
        )
        detected_legacy = legacy_data_dir
        if detected_legacy is None:
            candidate = default_legacy_data_dir()
            detected_legacy = (
                candidate if candidate.exists() and candidate.resolve() != self.data_dir.resolve() else None
            )
        self.legacy_data_dir = detected_legacy.resolve() if detected_legacy else None

        if self.config.cache_path:
            self.cache_dir = Path(expand_path(self.config.cache_path)).resolve()
        elif self._explicit_data_dir or os.environ.get("ANIXO_CACHE_DIR"):
            self.cache_dir = self.data_dir / "cache"
        else:
            self.cache_dir = default_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = default_log_dir()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.crash_reporter = crash_reporter

        self.install_dir = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent
        )
        self.platform = get_platform_adapter()
        self.manifest_store = ManifestStore(self.data_dir, self.platform)
        self.dependencies = DependencyManager(
            self.config,
            self.data_dir,
            self.cache_dir,
            self.platform,
            self.manifest_store,
        )
        self.dependencies.backfill_from_config()
        if self.dependencies.manifest.get(COMPONENT_APP) is None and getattr(
            sys, "frozen", False
        ):
            portable = (self.install_dir / "PORTABLE.txt").exists()
            self.dependencies.record_app_install(
                self.install_dir,
                installation_kind="portable" if portable else f"{self.platform.id}-installer",
                installer_source="first-run detection",
            )
        self._adopt_same_directory_database()
        self.database = Database(self.data_dir / DATABASE_NAME)
        self.image_cache = ImageCache(
            self.cache_dir / "artwork",
            max_entries=self.config.cache_max_entries,
        )
        self.artwork = ArtworkService(self.image_cache)
        self.api = AnimeAPI(self.config, self.database)
        self.ani_cli = AniCliService(self.config)
        self.provider = ProviderService(self.ani_cli.provider_base_url())
        self.updates = UpdateService(self.config, data_dir=self.data_dir, adapter=self.platform)
        self.ani_cli_updates = AniCliUpdateService(
            self.config,
            self.data_dir,
            ani_cli=self.ani_cli,
            manager=self.dependencies,
            adapter=self.platform,
        )
        self.update_state = UpdateState(
            current=VERSION, channel=self.config.update_channel
        )
        self.ani_cli_status = self.ani_cli_updates.detect_installed()
        self.ani_cli_check = None
        self._update_cancel: asyncio.Event | None = None
        self._playback_in_progress = False
        self._playback_ready: bool | None = None
        self._force_setup = bool(force_setup)
        self._setup_screen: SetupScreen | None = None
        self._setup_snapshot: SetupSnapshot | None = None
        self._update_status = "No update check yet."
        self._shown_update_messages: set[str] = set()
        self._route_history: list[type] = []
        self._route_index = -1

    def _adopt_same_directory_database(self) -> None:
        """Copy an in-place legacy database when no ANIXO database exists."""
        target = self.data_dir / DATABASE_NAME
        legacy = self.data_dir / LEGACY_DATABASE_NAME
        if not target.exists() and legacy.exists():
            shutil.copy2(legacy, target)

    # ------------------------------------------------------------------
    # Theme / CSS

    def get_theme_variable_defaults(self) -> dict[str, str]:
        palette = THEME_PALETTES.get(self.config.theme, THEME_PALETTES["midnight"])
        return {
            "accent": self.config.accent,
            "accent-dim": _dim_color(self.config.accent),
            **palette,
        }

    def get_css_variables(self) -> dict[str, str]:
        variables = super().get_css_variables()
        variables.update(self.get_theme_variable_defaults())
        return variables

    @property
    def cell_size(self) -> tuple[int, int]:
        """The cell geometry textual-image will actually render into."""
        if self.capabilities.is_tty:
            return self.capabilities.cell_size
        return self.config.terminal_cell_width, self.config.terminal_cell_height

    # ------------------------------------------------------------------
    # Lifecycle

    def compose(self) -> ComposeResult:
        if False:  # pragma: no cover - generator typing only
            yield Static("")

    def _handle_exception(self, error: Exception) -> None:
        if self.crash_reporter is not None:
            self.crash_reporter.handle_exception(error, source="textual")
        super()._handle_exception(error)

    async def on_mount(self) -> None:
        if self.crash_reporter is not None:
            self.crash_reporter.attach_loop()
        # A separate legacy directory is offered in the setup wizard instead of
        # being silently imported. Same-directory legacy JSON is still migrated
        # automatically for compatibility with older launch configurations.
        if self.legacy_data_dir is None or self.legacy_data_dir == self.data_dir:
            source = self.data_dir
            await asyncio.to_thread(
                self.database.migrate_legacy,
                source,
                self.config.metadata_ttl_hours,
            )
            await asyncio.gather(
                asyncio.to_thread(
                    import_legacy_artwork, source, self.image_cache, self.database
                ),
                asyncio.to_thread(self.ani_cli.import_history, self.database),
            )

        needs_setup = not self.config.setup_completed or self._force_setup
        # Headless runs are automation; the wizard is for an interactive shell.
        if needs_setup and (self._force_setup or not self.is_headless):
            self._setup_snapshot = await asyncio.to_thread(self.setup_snapshot)
            self._setup_screen = SetupScreen(self._setup_snapshot)
            await self.push_screen(self._setup_screen)
        else:
            await self.push_screen(DashboardScreen())
            self._remember_route(DashboardScreen)
        self.call_after_refresh(self._sync_navigation_buttons)
        self.call_after_refresh(self._focus_route_content)
        self.call_after_refresh(self._notify_applied_update)
        if not self.is_headless and self.config.check_for_app_updates:
            self.run_worker(
                self._auto_check_updates(),
                exclusive=True,
                group="update-check",
            )
        if not self.is_headless and self.config.check_for_ani_cli_updates:
            self.run_worker(
                self._auto_check_ani_cli_updates(),
                exclusive=True,
                group="ani-cli-check",
            )

    async def on_unmount(self) -> None:
        await asyncio.to_thread(self.database.close)

    # ------------------------------------------------------------------
    # Navigation

    @property
    def _route_types(self) -> tuple[type, ...]:
        return (
            DashboardScreen,
            DiscoverScreen,
            WatchlistScreen,
            HistoryScreen,
            SettingsScreen,
            UpdateScreen,
            RenderTestScreen,
            DiagnosticsScreen,
        )

    def _editing(self) -> bool:
        focused = self.focused
        return isinstance(focused, Input) and bool(getattr(focused, "can_focus", True))

    def action_goto_dashboard(self) -> None:
        if not self._editing():
            self._switch(DashboardScreen())

    def action_goto_discover(self) -> None:
        if not self._editing():
            self._switch(DiscoverScreen())

    def action_goto_watchlist(self) -> None:
        if not self._editing():
            self._switch(WatchlistScreen())

    def action_goto_history(self) -> None:
        if not self._editing():
            self._switch(HistoryScreen())

    def action_goto_settings(self) -> None:
        if not self._editing():
            self._switch(SettingsScreen())

    def action_goto_render_test(self) -> None:
        if not self._editing():
            self._switch(RenderTestScreen())

    def action_goto_diagnostics(self) -> None:
        if self._editing():
            return
        screen = DiagnosticsScreen()
        screen.set_status("Loading environment report…")
        self._switch(screen)
        # Dependency probes include subprocesses and a network check; keep
        # them off the UI thread so the screen appears immediately.
        self.run_worker(
            self._populate_diagnostics(screen),
            exclusive=True,
            group="diagnostics",
        )

    async def _populate_diagnostics(self, screen: DiagnosticsScreen) -> None:
        try:
            report = await asyncio.to_thread(self.diagnostics_report)
        except (OSError, ValueError) as error:
            if screen in self.screen_stack:
                screen.set_status(f"Could not build report: {error}")
            self.notify_user(f"Diagnostics report failed: {error}")
            return
        if screen not in self.screen_stack:
            return
        screen.set_report(report)
        screen.set_status(f"Report refreshed {datetime.now():%H:%M:%S}.")

    def action_cycle_route(self, delta: int) -> None:
        if self._editing():
            return
        routes = self._route_types
        current = next(
            (route for route in routes if isinstance(self.screen, route)),
            routes[0],
        )
        index = routes.index(current)
        target = routes[(index + delta) % len(routes)]
        self._switch(target())

    def action_navigate_back(self) -> None:
        """Return to the previously visited primary screen."""
        if self._editing():
            focused = self.focused
            if isinstance(focused, Input):
                focused.blur()
        if self._route_index <= 0:
            return
        self._route_index -= 1
        self._switch(self._route_history[self._route_index](), remember=False)

    def action_navigate_forward(self) -> None:
        """Revisit a primary screen after going back."""
        if self._editing():
            focused = self.focused
            if isinstance(focused, Input):
                focused.blur()
        if self._route_index + 1 >= len(self._route_history):
            return
        self._route_index += 1
        self._switch(self._route_history[self._route_index](), remember=False)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def open_details(self, anime_id: str) -> None:
        self.push_screen(AnimeDetailsScreen(anime_id))

    def open_episode_selector(
        self,
        anime: Anime,
        *,
        entries: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.push_screen(EpisodeSelectorScreen(anime, entries=entries))

    async def refresh_airing_progress(self, anime: Anime) -> Anime:
        """Refresh stale AniList metadata that controls released episodes.

        Existing installs may have metadata cached from before ANIXO tracked
        ``nextAiringEpisode``. Without it, a releasing series with unknown
        progress is capped at zero metadata-derived episodes. The refresh is
        skipped when progress is already known, and failures leave the cached
        data untouched. A freshly finished series is updated even when AniList
        no longer reports a next airing episode.
        """
        if (
            not anime.anilist_id
            or anime.next_airing_episode is not None
            or (anime.status or "").upper() != "RELEASING"
        ):
            return anime
        try:
            fresh = await self.api.details(int(anime.anilist_id), refresh=True)
        except Exception:
            return anime
        if fresh is None:
            return anime

        changed = False
        if (
            fresh.next_airing_episode is not None
            and fresh.next_airing_episode != anime.next_airing_episode
        ):
            anime.next_airing_episode = fresh.next_airing_episode
            changed = True
        if fresh.status and fresh.status != anime.status:
            anime.status = fresh.status
            changed = True
        if fresh.episodes and fresh.episodes != anime.episodes:
            anime.episodes = fresh.episodes
            changed = True
        if changed:
            await asyncio.to_thread(self.database.upsert_anime, anime)
        return anime

    async def _provider_results(self, anime: Anime) -> list[Any]:
        """Resolve exact provider rows using every title AniList supplied."""
        self.provider.base_url = self.ani_cli.provider_base_url()
        queries: list[str] = []
        for value in (anime.display_title(), *anime.title.values()):
            if value and value not in queries:
                queries.append(value)
        for query in queries:
            try:
                results = await asyncio.to_thread(self.provider.search, query)
            except ProviderError as error:
                self.notify_user(str(error))
                return []
            if results:
                return list(results)
        return []

    def open_season_selector(
        self,
        anime: Anime,
        results: Sequence[Any],
        *,
        remembered_source_id: str | None = None,
    ) -> None:
        franchises = group_franchises(results)
        self.push_screen(
            SeasonSelectorScreen(
                anime,
                franchises,
                remembered_source_id=remembered_source_id,
            )
        )

    @staticmethod
    def _anime_for_provider_entry(anime: Anime, entry: ProviderAnime) -> Anime:
        """Attach an exact provider ID while preserving AniList artwork."""
        count = int(entry.episode_count) if (entry.episode_count or "").isdigit() else None
        return replace(
            anime,
            source_id=entry.source_id,
            provider="hianime",
            provider_title=entry.title,
            format=entry.format or anime.format,
            episodes=count or anime.episodes,
        )

    async def select_provider_entry(
        self,
        anime: Anime,
        franchise: Franchise,
        entry: ProviderAnime,
    ) -> None:
        """Persist an exact franchise choice, then open its episode picker."""
        remember_franchise_selection(
            self.config, franchise.key, entry.source_id, entry.title
        )
        try:
            self.config_store.save(self.config)
        except OSError:
            self.notify_user("Could not save the franchise preference.")

        try:
            episodes = await asyncio.to_thread(self.provider.episodes, entry.source_id)
        except ProviderError as error:
            self.notify_user(str(error))
            episodes = []

        selected = self._anime_for_provider_entry(anime, entry)
        entries = [
            {
                "number": episode.number,
                "title": episode.title,
                "provider_episode_id": episode.provider_episode_id,
            }
            for episode in episodes
        ]
        self.open_episode_selector(selected, entries=entries)

    async def watch_anime(
        self, anime: Anime, *, force_selector: bool = False
    ) -> None:
        """Route WATCH through ANIXO's own franchise and episode selectors.

        Provider results are resolved first so the exact selected season, movie,
        or special is carried all the way to playback. ani-cli's interactive
        picker is never used. ``force_selector`` skips the resume prompt so the
        Details screen's Select Episode action can browse provider episodes
        directly. If the provider is unavailable, existing local progress still
        works; a title with no known provider ID cannot start.
        """
        results = await self._provider_results(anime)
        if results:
            franchises = group_franchises(results)
            remembered = None
            remembered_entry = None
            for franchise in franchises:
                candidate = get_franchise_selection(self.config, franchise.key)
                if not candidate:
                    continue
                remembered = candidate
                remembered_entry = next(
                    (
                        item
                        for item in franchise.entries
                        if item.source_id == candidate["source_id"]
                    ),
                    None,
                )
                if remembered_entry:
                    break

            if (
                not force_selector
                and remembered
                and remembered.get("episode")
                and remembered_entry
            ):
                selected = self._anime_for_provider_entry(anime, remembered_entry)

                async def on_remembered(choice: str | None) -> None:
                    if choice == "resume":
                        await self.play_episode(
                            selected,
                            remembered["episode"],
                            dub=self.config.preferred_audio == "dub",
                        )
                        await self._refresh_details(anime)
                    elif choice == "select":
                        self.open_season_selector(
                            anime,
                            results,
                            remembered_source_id=remembered["source_id"],
                        )

                self.push_screen(
                    ResumeDialog(
                        remembered["title"],
                        remembered["episode"],
                        selected.episodes,
                    ),
                    callback=lambda choice: asyncio.create_task(on_remembered(choice)),
                )
                return

            self.open_season_selector(
                anime,
                results,
                remembered_source_id=remembered["source_id"] if remembered else None,
            )
            return

        entry = await asyncio.to_thread(self.database.find_history_for_anime, anime)
        states = await asyncio.to_thread(self.database.get_episode_states, anime.id)
        resume_anime = entry.anime if entry else anime
        last = entry.episode if entry else None
        if force_selector or not last or is_series_completed(resume_anime, last, states):
            if not resume_anime.source_id:
                self.notify_user(
                    "Provider results are unavailable; exact playback needs a fresh search."
                )
            self.open_episode_selector(resume_anime)
            return
        self.push_screen(
            ResumeDialog(resume_anime.display_title(), last, resume_anime.episodes),
            callback=lambda choice: asyncio.create_task(
                self._on_watch_choice(resume_anime, last, choice)
            ),
        )

    async def _refresh_details(self, anime: Anime) -> None:
        for screen in reversed(self.screen_stack):
            if isinstance(screen, AnimeDetailsScreen):
                await screen.refresh_progress()
                break

    async def _on_watch_choice(
        self, anime: Anime, episode: str, choice: str | None
    ) -> None:
        if choice == "resume":
            await self.play_episode(anime, episode)
            await self._refresh_details(anime)
        elif choice == "select":
            self.open_episode_selector(anime)

    async def play_episode(
        self, anime: Anime, episode: str, *, dub: bool | None = None
    ) -> bool:
        """Play one exact episode without ani-cli's episode picker."""
        return await self.play_anime(
            anime,
            episode=str(episode),
            dub=dub,
            exact=True,
        )

    async def mark_episode_watched(self, anime: Anime, episode: str) -> None:
        """Mark one episode complete and advance the persisted resume point."""
        await asyncio.to_thread(
            self.database.mark_episode_completed, anime.id, episode
        )
        entry = await asyncio.to_thread(
            self.database.find_history_for_anime, anime
        )
        candidate = next_episode(episode)
        if (
            candidate
            and anime.episodes
            and (episode_number(candidate) or 0) > anime.episodes
        ):
            candidate = None
        resume_episode = candidate or episode
        if entry is None:
            entry_id = f"ani-cli:{anime.source_id}" if anime.source_id else None
            await asyncio.to_thread(
                self.database.record_history,
                anime,
                resume_episode,
                entry_id=entry_id,
                source_id=anime.source_id,
                source="ani-cli" if anime.source_id else None,
            )
        else:
            await asyncio.to_thread(
                self.database.record_history,
                anime,
                resume_episode,
                entry_id=entry.id,
                source_id=entry.source_id,
                source=entry.source,
            )

    async def mark_episode_unwatched(self, anime: Anime, episode: str) -> None:
        """Clear one episode's watched state and rewind the resume point."""
        await asyncio.to_thread(
            self.database.mark_episode_unwatched, anime.id, str(episode)
        )
        entry = await asyncio.to_thread(
            self.database.find_history_for_anime, anime
        )
        if not entry:
            return
        cleared = episode_number(episode)
        current = episode_number(entry.episode)
        if cleared is None or current is None:
            return
        if current == cleared:
            # The cleared episode was the active resume point.
            previous = str(cleared - 1) if cleared > 1 else None
        elif current == cleared + 1:
            # Marking this episode watched had advanced the resume point.
            previous = str(cleared)
        else:
            return
        await asyncio.to_thread(
            self.database.update_history_episode,
            entry.id,
            previous,
            clear_progress=True,
        )

    def _remember_route(self, route_type: type) -> None:
        """Maintain a small browser-style history of primary screens."""
        if self._route_index >= 0 and self._route_history[self._route_index] is route_type:
            return
        if self._route_index < len(self._route_history) - 1:
            del self._route_history[self._route_index + 1 :]
        self._route_history.append(route_type)
        self._route_index = len(self._route_history) - 1

    def _sync_navigation_buttons(self) -> None:
        """Reflect available back/forward history in every shell top bar."""
        try:
            back = self.screen.query_one("#nav-back", Button)
            forward = self.screen.query_one("#nav-forward", Button)
        except Exception:
            return
        back.disabled = self._route_index <= 0
        forward.disabled = self._route_index + 1 >= len(self._route_history)

    def _switch(self, screen: object, *, remember: bool = True) -> None:
        # A focused widget from the old screen (usually a nav rail button) can
        # survive the screen swap and swallow Enter/arrow keys on the new one.
        focused = self.focused
        if focused is not None:
            focused.blur()
        if remember and isinstance(screen, self._route_types):
            self._remember_route(type(screen))
        # A route change from a details/episode screen must discard that whole
        # stack, otherwise the old screen would stay underneath the new route.
        for index in range(len(self.screen_stack) - 1, -1, -1):
            if isinstance(self.screen_stack[index], self._route_types):
                while len(self.screen_stack) > index:
                    self.pop_screen()
                break
        self.push_screen(screen)  # type: ignore[arg-type]
        self.call_after_refresh(self._sync_navigation_buttons)
        self.call_after_refresh(self._focus_route_content)

    def _focus_route_content(self) -> None:
        """Move focus off nav buttons so screen-level Enter/arrows work."""
        try:
            pane = self.screen.query_one("#content-pane")
        except Exception:
            return
        if getattr(pane, "can_focus", False):
            pane.focus()

    # ------------------------------------------------------------------
    # Shared actions / services

    def notify_user(self, message: str, *, timeout: float = 3) -> None:
        self.notify(redact_update_text(str(message)), timeout=timeout)

    async def confirm(self, title: str, message: str = "") -> bool:
        """Show a confirmation modal. Call this from a Textual worker.

        ``wait_for_dismiss=True`` requires an active worker. Ordinary screen
        actions run inside the message pump and would block the modal's keys,
        so callers wrap confirmation flows in ``run_worker``.
        """
        return bool(await self.push_screen_wait(ConfirmDialog(title, message)))

    async def toggle_watchlist(self, anime: Anime) -> bool:
        """Toggle by ID or normalized title; returns True when now saved."""
        entries = await asyncio.to_thread(self.database.list_watchlist)
        if any(is_same_anime(entry.anime, anime) for entry in entries):
            await asyncio.to_thread(self.database.remove_watchlist_anime, anime)
            return False
        added = await asyncio.to_thread(self.database.add_watchlist, anime)
        return added

    def refresh_theme(self) -> None:
        self.refresh_css(animate=False)

    def save_config(self) -> None:
        self.config.validate()
        self.config_store.save(self.config)
        self.refresh_theme()

    def set_preferred_audio(self, language: str) -> str:
        """Persist the SUB/DUB choice used by exact and resume playback."""
        self.config.preferred_audio = language
        self.config_store.save(self.config)
        return self.config.preferred_audio

    # ------------------------------------------------------------------
    # First-launch setup, migration, diagnostics, and updates

    @staticmethod
    def _count_json_records(path: Path) -> int:
        """Count records in a legacy JSON file without modifying it."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if isinstance(payload, list):
            return len(payload)
        if isinstance(payload, dict):
            for key in ("history", "watchlist", "entries", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return len(value)
        return 0

    def legacy_data_counts(self) -> dict[str, int]:
        """Summarize legacy data for the setup wizard and migration prompt."""
        legacy = self.legacy_data_dir
        if not legacy:
            return {}
        counts = {
            "history": self._count_json_records(legacy / "history.json"),
            "watchlist": self._count_json_records(legacy / "watchlist.json"),
            "database": 1 if (legacy / LEGACY_DATABASE_NAME).exists() else 0,
        }
        return {key: value for key, value in counts.items() if value}

    def setup_snapshot(self) -> SetupSnapshot:
        """Capture current environment state for the setup wizard."""
        renderer, note = resolve_renderer(
            self.config.artwork_renderer, self.capabilities
        )
        snapshot = build_snapshot(
            app_version=VERSION,
            config=self.config,
            ani_cli=self.ani_cli,
            capabilities=self.capabilities,
            renderer=renderer,
            renderer_note=note,
            legacy_path=self.legacy_data_dir,
            legacy_counts=self.legacy_data_counts(),
        )
        annotated = tuple(self.dependencies.annotate_statuses(snapshot.dependencies))
        return replace(snapshot, dependencies=annotated)

    async def _refresh_setup_snapshot(self) -> None:
        snapshot = await asyncio.to_thread(self.setup_snapshot)
        self._setup_snapshot = snapshot
        if self._setup_screen:
            self._setup_screen.set_snapshot(snapshot)

    def uninstall_planner(self) -> UninstallPlanner:
        """Create a planner bound to this installation's manifest and paths."""
        return UninstallPlanner(
            data_dir=self.data_dir,
            cache_dir=self.cache_dir,
            log_dir=self.log_dir,
            store=self.manifest_store,
            adapter=self.platform,
            config_store=ConfigStore(path=self.config_path, data_dir=self.data_dir),
        )

    def build_uninstall_plan(self, mode: UninstallMode = "app_only"):
        """Build an uninstall plan for Settings without deleting anything."""
        return self.uninstall_planner().plan(mode)

    def _uninstaller_command(self) -> tuple[str, ...]:
        executable = "ANIXO-Uninstaller.exe" if self.platform.is_windows else "ANIXO-Uninstaller"
        candidate = self.install_dir / executable
        if candidate.exists():
            base = (str(candidate),)
        elif getattr(sys, "frozen", False):
            raise UninstallSafetyError(
                "The standalone uninstaller is missing from this installation. "
                "Use Windows Settings or the Finder/Applications method instead."
            )
        else:
            base = (sys.executable, str(Path(__file__).with_name("uninstaller.py")))
        paths = (
            "--data-dir",
            str(self.data_dir),
            "--cache-dir",
            str(self.cache_dir),
            "--log-dir",
            str(self.log_dir),
            "--config",
            str(self.config_path),
        )
        return base + paths + ("--execute-pending",)

    async def action_uninstall(
        self,
        mode: UninstallMode,
        selections: dict[str, bool] | None = None,
    ) -> None:
        """Hand a confirmed uninstall request to the standalone uninstaller."""
        pending = write_pending(
            self.data_dir,
            mode=mode,
            selections=selections or {},
            wait_for_pid=os.getpid(),
            cache_dir=self.cache_dir,
            log_dir=self.log_dir,
            config_path=self.config_path,
        )
        try:
            command = self._uninstaller_command()
        except UninstallSafetyError as exc:
            pending.unlink(missing_ok=True)
            self.notify_user(str(exc))
            return
        options: dict[str, object] = {"cwd": str(self.install_dir)}
        if self.platform.is_windows:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            options["creationflags"] = flags
        else:
            options["start_new_session"] = True
        try:
            subprocess.Popen(command, **options)  # noqa: S603 - fixed local command
        except OSError as exc:
            pending.unlink(missing_ok=True)
            self.notify_user(f"Could not start the uninstaller: {exc}")
            return
        self.notify_user("ANIXO will finish uninstallation after it closes.")
        self.exit()

    def _setup_screen_or_none(self) -> SetupScreen | None:
        if self._setup_screen and self._setup_screen in self.screen_stack:
            return self._setup_screen
        return next(
            (screen for screen in self.screen_stack if isinstance(screen, SetupScreen)),
            None,
        )

    async def action_setup_next(self) -> None:
        screen = self._setup_screen_or_none()
        if not screen:
            return
        if screen.current_step < 5:
            screen.current_step += 1
            screen.render_step()
        else:
            await self.action_setup_finish()

    def action_setup_back(self) -> None:
        screen = self._setup_screen_or_none()
        if not screen:
            return
        screen.current_step = max(0, screen.current_step - 1)
        screen.render_step()

    async def action_setup_finish(self) -> None:
        screen = self._setup_screen_or_none()
        if screen:
            snapshot = screen.snapshot
            missing = [
                item.label
                for item in snapshot.dependencies
                if item.required and not item.installed
            ]
            if missing:
                approved = await self.confirm(
                    "Finish setup anyway?",
                    "These required playback components are still missing: "
                    + ", ".join(missing)
                    + "\n\nANIXO can start, but Play will ask to reopen setup.",
                )
                if not approved:
                    screen.current_step = 2
                    screen.render_step()
                    return
        self.config.setup_completed = True
        self.config.setup_version = VERSION
        self.config_store.save(self.config)
        self._setup_screen = None
        self.pop_screen()
        await self.push_screen(DashboardScreen())
        self._remember_route(DashboardScreen)
        self.call_after_refresh(self._sync_navigation_buttons)
        self.notify_user("ANIXO setup complete.")

    async def action_open_setup(self) -> None:
        """Open the wizard again from Settings or a playback prompt."""
        existing = self._setup_screen_or_none()
        if existing:
            existing.render_step()
            return
        snapshot = await asyncio.to_thread(self.setup_snapshot)
        self._setup_snapshot = snapshot
        self._setup_screen = SetupScreen(snapshot)
        await self.push_screen(self._setup_screen)

    async def action_setup_install(self, key: str) -> None:
        """Install one explicitly selected dependency after confirmation."""
        if key in {"legacy_data", "migrate"}:
            await self.migrate_legacy_data()
            await self._refresh_setup_snapshot()
            return

        snapshot = self._setup_snapshot or await asyncio.to_thread(self.setup_snapshot)
        status = next((item for item in snapshot.dependencies if item.key == key), None)
        if not status:
            self.notify_user(f"Unknown setup item: {key}")
            return
        if status.installed:
            self.notify_user(f"{status.label} is already installed.")
            return
        if not status.installable and key != "ani-cli":
            self.notify_user(f"Install {status.label} from {status.official_url}")
            return

        approved = await self.confirm(
            f"Install {status.label}?",
            "ANIXO records what it installs so it can remove only its own files "
            "later. Existing software is never overwritten or removed. "
            f"Official source: {status.official_url}\n\n{status.description}",
        )
        if not approved:
            return

        result = await asyncio.to_thread(self.dependencies.install, key, status)
        if result.ok:
            self.config_store.save(self.config)
        self.notify_user(result.message)
        if result.detail:
            self.notify_user(result.detail[:240])
        await self._refresh_setup_snapshot()

    def _merge_legacy_config(self, legacy: Path) -> None:
        """Carry over compatible preferences without overwriting ANIXO choices."""
        path = legacy / "config.json"
        if not path.exists():
            return
        try:
            legacy_config = ConfigStore(path=path, data_dir=legacy).load()
        except (OSError, ValueError):
            return
        defaults = Config()
        direct = (
            "theme",
            "accent",
            "animations",
            "images",
            "artwork_renderer",
            "ani_cli_path",
            "mpv_path",
            "git_bash_path",
            "metadata_endpoint",
            "metadata_cache_ttl_hours",
        )
        for name in direct:
            if getattr(self.config, name) == getattr(defaults, name):
                setattr(self.config, name, getattr(legacy_config, name))
        extra = (
            "preferred_audio",
            "page_size",
            "cache_max_entries",
            "terminal_cell_width",
            "terminal_cell_height",
            "ani_cli_base_url",
            "confirm_before_clear_history",
            "check_for_ani_cli_updates",
        )
        for name in extra:
            if (
                name in legacy_config.extra
                and self.config.extra.get(name) == defaults.extra.get(name)
            ):
                self.config.extra[name] = legacy_config.extra[name]

    async def migrate_legacy_data(self) -> bool:
        """Offer a backed-up, non-destructive migration of legacy app data."""
        legacy = self.legacy_data_dir
        if not legacy or not legacy.exists() or legacy.resolve() == self.data_dir.resolve():
            self.notify_user("No separate legacy data folder was found.")
            return False
        approved = await self.confirm(
            "Migrate legacy app data?",
            "History, watchlist, preferences, and artwork will be imported. "
            "A backup is made first. Existing ANIXO data is never overwritten "
            "and the old data folder is left untouched.",
        )
        if not approved:
            return False

        existing_history = await asyncio.to_thread(self.database.list_history)
        existing_watchlist = await asyncio.to_thread(self.database.list_watchlist)
        if existing_history or existing_watchlist:
            self.notify_user(
                "ANIXO already has saved data; legacy data was left untouched."
            )
            return False

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.data_dir / "backups" / f"animehub-{timestamp}"
        legacy_db = legacy / LEGACY_DATABASE_NAME

        def copy_backup() -> None:
            backup.mkdir(parents=True, exist_ok=False)
            for name in ("config.json", "history.json", "watchlist.json"):
                source = legacy / name
                if source.exists():
                    shutil.copy2(source, backup / name)
            metadata = legacy / "cache" / "metadata.json"
            if metadata.exists():
                (backup / "cache").mkdir(exist_ok=True)
                shutil.copy2(metadata, backup / "cache" / "metadata.json")
            if legacy_db.exists():
                shutil.copy2(legacy_db, backup / LEGACY_DATABASE_NAME)

        try:
            await asyncio.to_thread(copy_backup)
            if legacy_db.exists():
                await asyncio.to_thread(self.database.replace_with, legacy_db)
            await asyncio.to_thread(
                self.database.migrate_legacy, legacy, self.config.metadata_ttl_hours
            )
            await asyncio.to_thread(
                import_legacy_artwork, legacy, self.image_cache, self.database
            )
            await asyncio.to_thread(self.ani_cli.import_history, self.database)
            self._merge_legacy_config(legacy)
            self.config.migration_completed = True
            self.config_store.save(self.config)
        except (OSError, ValueError) as error:
            self.notify_user(f"Migration failed: {error}")
            return False
        self.notify_user(f"Legacy data imported. Backup: {backup}")
        return True

    def diagnostics_report(self) -> str:
        """Build the redacted report shown by the diagnostics screen."""
        return build_report(
            app_version=VERSION,
            install_dir=self.install_dir,
            data_dir=self.data_dir,
            log_dir=self.log_dir,
            config=self.config,
            ani_cli=self.ani_cli,
            capabilities=self.capabilities,
            cache_counts=self.image_cache.counts(),
        )

    async def action_diagnostics_refresh(self) -> None:
        screen = next(
            (item for item in self.screen_stack if isinstance(item, DiagnosticsScreen)),
            None,
        )
        if not screen:
            return
        screen.set_status("Refreshing environment report…")
        report = await asyncio.to_thread(self.diagnostics_report)
        screen.set_report(report)
        screen.set_status(f"Report refreshed {datetime.now():%H:%M:%S}.")

    async def action_diagnostics_export(self) -> None:
        report = await asyncio.to_thread(self.diagnostics_report)
        try:
            path = await asyncio.to_thread(export_report, report, self.log_dir)
        except OSError as error:
            self.notify_user(f"Could not export diagnostics: {error}")
            return
        screen = next(
            (item for item in self.screen_stack if isinstance(item, DiagnosticsScreen)),
            None,
        )
        if screen:
            screen.set_status(f"Report exported to {path}")
        self.notify_user(f"Diagnostics exported: {path}")

    def action_diagnostics_back(self) -> None:
        self.action_goto_settings()

    def action_crash_report(self) -> None:
        """Open the redacted crash report screen from any mounted context."""
        reporter = self.crash_reporter or CrashReporter(self.log_dir)
        report = reporter.latest()
        text = (
            report.summary() + "\n" + report.traceback
            if report is not None
            else "No crash report has been recorded."
        )
        self.push_screen(CrashReportScreen(text))

    # ------------------------------------------------------------------
    # Updates (ANIXO and ani-cli)

    def _update_screen(self) -> UpdateScreen | None:
        return next(
            (item for item in self.screen_stack if isinstance(item, UpdateScreen)),
            None,
        )

    def _set_update_status(
        self,
        status: str,
        *,
        progress: str = "",
        error: str = "",
        busy: bool | None = None,
    ) -> None:
        state = self.update_state
        status = redact_update_text(status)
        progress = redact_update_text(progress)
        error = redact_update_text(error)
        state.status = status
        state.error = error
        state.progress = progress
        if busy is not None:
            state.busy = busy
        self._update_status = status
        screen = self._update_screen()
        if screen:
            screen.set_state(state)

    def _update_progress(self, value: DownloadProgress) -> None:
        message = value.message
        if value.total and value.completed:
            percent = min(100, int(value.completed * 100 / value.total))
            message = f"{percent}% — {message}"
        self._set_update_status(
            f"{value.stage.replace('_', ' ').title()}…",
            progress=message,
            busy=True,
        )

    def action_open_updates(self) -> None:
        """Open the dedicated update center without keyboard-only navigation."""
        if not self._editing():
            self._switch(UpdateScreen())

    def _report_auto_update_status(self, message: str) -> None:
        """Show a concise auto-check status once per distinct message."""
        text = redact_update_text(str(message)).strip()
        if not text or text in self._shown_update_messages:
            return
        self._shown_update_messages.add(text)
        self._set_update_status(text, busy=False)
        self.notify_user(text, timeout=5)

    async def _auto_check_updates(self) -> None:
        check = await self.updates.check(self.config.update_channel)
        if check.checked_at:
            self.config.last_update_check = check.checked_at
            self.config_store.save(self.config)
        state = self.update_state
        state.current = check.current
        state.available = check.available
        state.latest = check.available
        state.channel = check.channel
        state.release = check.release
        state.notes = check.release.notes if check.release else ""
        state.error = redact_update_text(check.error)
        if check.error and not check.cached:
            self._report_auto_update_status(f"Update check failed — {check.error}")
            return
        if check.cached:
            self._report_auto_update_status(
                check.message or check.error or "Cached update result."
            )
            return
        if check.update_available:
            state.status = redact_update_text(
                f"UPDATE AVAILABLE — ANIXO {check.available}"
            )
            self.notify_user(state.status)
        else:
            state.status = redact_update_text(check.message or "UP TO DATE")

    async def action_check_updates(self) -> None:
        """Explicitly check for a compatible release."""
        self._set_update_status("Checking for updates…", progress="", busy=True)
        check = await self.updates.check(self.config.update_channel, force=True)
        self.config.last_update_check = check.checked_at
        self.config_store.save(self.config)
        state = self.update_state
        state.current = check.current
        state.available = check.available
        state.latest = check.available
        state.channel = check.channel
        state.release = check.release
        state.notes = check.release.notes if check.release else ""
        state.error = redact_update_text(check.error)
        state.busy = False
        if check.error:
            self._set_update_status(f"UPDATE FAILED — {check.error}", error=check.error)
            self.notify_user(self.update_state.status, timeout=10)
        elif check.update_available:
            self._set_update_status(f"UPDATE AVAILABLE — ANIXO {check.available}")
            self.notify_user(self.update_state.status)
        else:
            self._set_update_status(
                check.message or f"UP TO DATE — ANIXO {check.current}"
            )

    async def action_update_anixo(self) -> None:
        """Start the download/verify/install flow in a cancellable worker."""
        self.run_worker(
            self._update_anixo_flow(),
            exclusive=True,
            group="update-flow",
        )

    async def _update_anixo_flow(self) -> None:
        state = self.update_state
        cancel: asyncio.Event | None = None
        exiting_for_restart = False
        try:
            if state.prepared is None:
                check = await self.updates.check(self.config.update_channel)
                if not check.update_available or not check.release:
                    self._set_update_status(check.message or "No update is available.")
                    return
                notes = " ".join(check.release.notes.split())[:280] or "No release notes."
                approved = await self.confirm(
                    f"Update ANIXO to {check.available}?",
                    f"Installed: {check.current}\nChannel: {check.channel}\n\n{notes}\n\n"
                    "The update is downloaded to staging, checksum-verified, and "
                    "installed by a separate helper after ANIXO closes. "
                    "Your settings, history, watchlist and dependencies are preserved.",
                )
                if not approved:
                    self._set_update_status("Update cancelled before download.")
                    return
                state.release = check.release
                state.notes = check.release.notes
                cancel = asyncio.Event()
                self._update_cancel = cancel
                state.can_cancel = True
                self._set_update_status(
                    "DOWNLOADING — downloading and verifying…", busy=True
                )
                try:
                    prepared = await self.updates.prepare_update(
                        check.release,
                        progress=self._update_progress,
                        should_cancel=cancel.is_set,
                    )
                except asyncio.CancelledError:
                    self._set_update_status("UPDATE FAILED — download cancelled.", busy=False)
                    return
                except Exception as error:
                    status = f"UPDATE FAILED — {error}"
                    self.updates.log.log("prepare_failed", error=str(error))
                    self._set_update_status(status, error=str(error), busy=False)
                    self.notify_user(status, timeout=10)
                    return
                state.prepared = prepared
                state.verification = prepared.verification
                state.can_cancel = False
                self._set_update_status(
                    f"READY TO INSTALL — ANIXO {prepared.version} passed verification.",
                    progress="Restart is required to complete installation.",
                    busy=False,
                )
            restart = await self.confirm(
                "Restart and install now?",
                "ANIXO will close and a separate updater helper will install the "
                "verified package, check it, then reopen ANIXO. The previous "
                "installation is restored automatically if anything fails.",
            )
            if restart:
                exiting_for_restart = True
                self.action_restart_update()
        finally:
            if cancel is not None and self._update_cancel is cancel:
                self._update_cancel = None
            state.can_cancel = False
            if not exiting_for_restart:
                state.busy = False

    def action_cancel_update(self) -> None:
        if self._update_cancel is not None:
            self._update_cancel.set()
        # Keep the flow busy until the worker has actually stopped.
        self._set_update_status("Cancelling update…", busy=True)
        self.update_state.can_cancel = False

    def action_restart_update(self) -> None:
        prepared = self.update_state.prepared
        if prepared is None:
            self.notify_user("There is no verified update waiting to be installed.")
            return
        if not self.updates.launch_helper(prepared.plan_path):
            status = "UPDATE FAILED — updater helper could not be started."
            self._set_update_status(
                status,
                error="ANIXO-Updater was not found.",
            )
            self.notify_user(status, timeout=10)
            return
        self.update_state.restart_required = False
        self._set_update_status("INSTALLING — ANIXO will restart…", busy=True)
        self.exit()

    def action_view_release_notes(self) -> None:
        notes = self.update_state.notes or "No release notes are available yet."
        self.notify_user(notes[:300])

    async def _notify_applied_update(self) -> None:
        marker = self.data_dir / "updates" / "last-result.json"
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        log_path = str(
            getattr(self.updates.log, "path", self.data_dir / "updates" / "update.log")
        )
        if not payload.get("ok"):
            detail = redact_update_text(
                str(
                    payload.get("detail")
                    or payload.get("message")
                    or "Unknown update failure."
                )
            )
            status = (
                "UPDATE FAILED — the previous update was rolled back. "
                f"{detail} See {log_path}"
            )
            self._set_update_status(
                status,
                error=detail,
                progress="Open Updates to try again.",
                busy=False,
            )
            self.update_state.restart_required = False
            self.notify_user(status, timeout=10)
            # Keep the marker so the failure remains visible after another restart.
            return
        if str(payload.get("version")) != VERSION:
            return
        self.update_state.restart_required = False
        self.notify_user(payload.get("message") or f"ANIXO {VERSION} is ready.")
        try:
            marker.unlink()
        except OSError:
            pass

    async def _auto_check_ani_cli_updates(self) -> None:
        check = await self.ani_cli_updates.check_async()
        self.ani_cli_check = check
        if check.error or check.cached:
            return
        self.config.last_ani_cli_check = check.last_checked
        self.config_store.save(self.config)
        if check.update_available:
            self.notify_user(check.message)

    async def action_check_ani_cli_updates(self) -> None:
        check = await self.ani_cli_updates.check_async(force=True)
        self.ani_cli_check = check
        self.ani_cli_status = self.ani_cli_updates.detect_installed()
        if not check.error:
            self.config.last_ani_cli_check = check.last_checked
            self.config_store.save(self.config)
        self.notify_user(check.message or check.error or "ani-cli check complete.")

    async def action_update_ani_cli(self) -> Any:
        """Run the staged ani-cli update off the UI thread."""
        return await asyncio.to_thread(self.ani_cli_updates.update, self.ani_cli_check)

    async def _missing_playback_components(self) -> list[str]:
        statuses = await asyncio.to_thread(dependency_statuses, self.config, self.ani_cli)
        return [item.label for item in statuses if item.required and not item.installed]

    async def play_anime(
        self,
        anime: Anime,
        *,
        episode: str | None = None,
        history_id: str | None = None,
        source_id: str | None = None,
        resume: bool = False,
        dub: bool | None = None,
        exact: bool = False,
    ) -> bool:
        """Launch ani-cli with the terminal released, then refresh history."""
        if self._playback_in_progress:
            self.notify_user("Playback is already running.")
            return False
        if not self.is_headless and self.config.setup_completed:
            missing = await self._missing_playback_components()
            if missing:
                self.notify_user(
                    "Playback needs " + ", ".join(missing) + ". Opening setup…"
                )
                await self.action_open_setup()
                return False

        use_dub = self.config.preferred_audio == "dub" if dub is None else bool(dub)
        resolved_source = source_id or anime.source_id
        if not resolved_source and history_id and history_id.startswith("ani-cli:"):
            resolved_source = history_id.split(":", 1)[1]

        title = anime.display_title()
        # Explicit episode selections are never advanced or replaced here.
        provider_id = resolved_source if exact and resolved_source and not resume else None
        if exact and not provider_id and not resume:
            self.notify_user("Choose a season/movie in ANIXO before playing.")
            return False

        self._playback_in_progress = True
        success = False
        process = None
        try:
            try:
                with self.suspend():
                    process = await self.ani_cli.spawn(
                        title=title,
                        source_id=resolved_source,
                        episode=episode,
                        resume=resume,
                        dub=use_dub,
                        exact=exact,
                        provider_id=provider_id,
                    )
                    success = process is not None and await process.wait() == 0
            except SuspendNotSupported:
                process = await self.ani_cli.spawn(
                    title=title,
                    source_id=resolved_source,
                    episode=episode,
                    resume=resume,
                    dub=use_dub,
                    exact=exact,
                    provider_id=provider_id,
                )
                success = process is not None and await process.wait() == 0
        except OSError as error:
            self.notify_user(f"Could not launch ani-cli: {error}")
        finally:
            self._playback_in_progress = False

        if process is None:
            self.notify_user(
                "Exact playback could not start through ANIXO's verified launcher."
            )

        if success:
            await asyncio.to_thread(self.ani_cli.import_history, self.database)
            key = history_id or (f"ani-cli:{resolved_source}" if resolved_source else anime.id)
            existing = await asyncio.to_thread(self.database.get_history_entry, key)
            # Always persist an explicitly selected episode so the resume point
            # follows exact playback; a bare WATCH must not overwrite progress.
            if episode is not None or existing is None:
                await asyncio.to_thread(
                    self.database.record_history,
                    anime,
                    episode,
                    entry_id=key,
                    source_id=resolved_source,
                    source="ani-cli" if resolved_source else None,
                )
            if episode is not None:
                await asyncio.to_thread(
                    self.database.mark_episode_watching, anime.id, episode
                )
                if provider_id:
                    remember_franchise_episode(self.config, provider_id, episode)
                    try:
                        self.config_store.save(self.config)
                    except OSError:
                        pass
        self.notify_user("Playback finished." if success else "Playback failed or was cancelled.")
        return success

    async def play_history_entry(self, entry: HistoryEntry) -> bool:
        source_id = entry.source_id or entry.anime.source_id
        if not source_id:
            self.notify_user(
                "This history entry has no provider ID; open Discover and "
                "choose the season again."
            )
            return False
        return await self.play_anime(
            entry.anime,
            episode=entry.episode,
            history_id=entry.id,
            source_id=source_id,
            exact=True,
        )
