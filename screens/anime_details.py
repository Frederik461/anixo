"""Cinematic anime details with data-derived progress and episode actions."""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.widgets import Button, Static

from components.poster import Poster
from components.shell import ShellScreen
from components.ui import EpisodeStrip, SectionHeading, anime_meta, plain_text
from models.anime import Anime, HistoryEntry
from models.episode import (
    COMPLETED,
    WATCHING,
    Episode,
    build_episodes,
    is_series_completed,
)


class AnimeDetailsScreen(ShellScreen):
    # Keep Enter/Space for the action bindings instead of the nav rail.
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("enter,w", "watch", "Watch"),
        Binding("e", "select_episode", "Select episode"),
        Binding("a", "watchlist", "Watchlist"),
        Binding("m", "toggle_marked", "Mark watched / unwatched"),
        Binding("M,backspace", "mark_unwatched", "Mark unwatched"),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self, anime_id: str) -> None:
        super().__init__()
        self.anime_id = anime_id
        self.anime: Anime | None = None
        self.entry: HistoryEntry | None = None
        self.states: dict[str, str] = {}
        self.episodes: list[Episode] = []
        self._last_marked_episode: str | None = None
        self._last_detail_tier = ""

    # ------------------------------------------------------------------
    # Composition

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            with Horizontal(id="detail-head"):
                yield Poster(classes="poster detail-poster")
                yield Static("Loading anime details…", id="detail-summary")
            with HorizontalScroll(id="detail-actions"):
                yield Button(
                    "▶  Watch", id="detail-watch", classes="action-button primary"
                )
                yield Button(
                    "▤  Select Episode", id="detail-select", classes="action-button"
                )
                yield Button(
                    "▣  Watchlist", id="detail-watchlist", classes="action-button"
                )
                yield Button(
                    "✓  Mark Watched", id="detail-mark", classes="action-button"
                )
                yield Button(
                    "←  Back", id="detail-back", classes="action-button ghost"
                )
            with VerticalScroll(id="detail-scroll"):
                yield SectionHeading("About", "Synopsis")
                yield Static("", id="detail-description", classes="detail-description")
                with Vertical(id="detail-episodes"):
                    yield SectionHeading("Episodes", "")
                    yield Static("", id="detail-episode-note", classes="meta-text")
                    yield Vertical(id="detail-episode-strip-host")

        yield from self.compose_shell(
            active="nav-home",
            title="Details",
            hint="Anime information",
            content=content(),
        )

    # ------------------------------------------------------------------
    # Lifecycle

    def on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        super().on_resize(event)
        tier = "compact" if self.has_class("compact") else "regular"
        if tier == self._last_detail_tier:
            return
        self._last_detail_tier = tier
        if not self.is_mounted or not self.anime:
            return
        self._render_poster()

    async def on_mount(self) -> None:
        anime = await asyncio.to_thread(self.app.database.get_anime, self.anime_id)
        if anime is None and self.anime_id.isdigit():
            try:
                anime = await self.app.api.details(int(self.anime_id))
            except Exception:
                anime = None
            if anime:
                await asyncio.to_thread(self.app.database.upsert_anime, anime)
        self.anime = anime
        if not anime:
            self.query_one("#detail-summary", Static).update("Anime not found.")
            self.query_one("#detail-episode-note", Static).update("")
            self._set_actions_enabled(False)
            self.set_focus(None)
            return

        self._render_poster()
        await self._load_progress()
        # The nav rail is the first focusable widget; clear focus so Enter and
        # Space reach the details bindings instead of activating navigation.
        self.set_focus(None)

    async def on_screen_resume(self) -> None:
        self._last_marked_episode = None
        if self.anime:
            await self._load_progress()

    async def refresh_progress(self) -> None:
        """Re-read history after playback launched from a modal action."""
        await self._load_progress()

    # ------------------------------------------------------------------
    # Rendering

    def _render_poster(self) -> None:
        if not self.anime:
            return
        compact = self.has_class("compact")
        poster = self.query_one(".detail-poster", Poster)
        poster.width_cells = 14 if compact else 20
        poster.height_cells = 11 if compact else 15
        poster.renderer = self.app.config.artwork_renderer  # type: ignore[assignment]
        poster.fallback_letter = (self.anime.display_title()[:1] or "?").upper()
        poster.fallback_color = self.anime.cover_color or "#a78bfa"
        poster.url = self.anime.cover_url

    async def _load_progress(self) -> None:
        if not self.anime:
            return
        entry = await asyncio.to_thread(
            self.app.database.find_history_for_anime, self.anime
        )
        records = await asyncio.to_thread(
            self.app.database.get_episode_records, self.anime.id
        )
        states = {number: str(record["state"]) for number, record in records.items()}
        self.entry = entry
        self.states = states
        self.episodes = build_episodes(
            self.anime,
            states=states,
            progress=records,
            last_episode=entry.episode if entry else None,
        )
        self._render_summary()
        self._render_description()
        await self._render_strip()
        self._update_actions()

    def _render_summary(self) -> None:
        if not self.anime:
            return
        anime = self.anime
        alternatives = [
            value
            for value in (anime.title.romaji, anime.title.native)
            if value and value != anime.display_title()
        ]
        lines = [f"[b]{anime.display_title()}[/b]"]
        if alternatives:
            lines.append("  •  ".join(alternatives))
        lines.append(f"[cyan]{anime_meta(anime)}[/cyan]")
        if anime.genres:
            lines.append("  •  ".join(anime.genres))
        if anime.studios:
            lines.append(f"Studio: {', '.join(anime.studios)}")
        lines.append(self._progress_text())
        if anime.status:
            lines.append(f"Status: {anime.status.title()}")
        self.query_one("#detail-summary", Static).update(
            "\n".join(part for part in lines if part)
        )

    def _progress_text(self) -> str:
        if not self.anime:
            return ""
        if not self.entry or not self.entry.episode:
            return "Progress: Not started"
        completed = sum(1 for episode in self.episodes if episode.state == COMPLETED)
        if self.anime.episodes:
            return (
                f"Progress: Episode {self.entry.episode} of {self.anime.episodes}"
                f"  •  {completed} watched"
            )
        return f"Progress: Episode {self.entry.episode}"

    def _render_description(self) -> None:
        if not self.anime:
            return
        self.query_one("#detail-description", Static).update(
            plain_text(self.anime.description, "No description available.")
        )

    async def _render_strip(self) -> None:
        if not self.anime:
            return
        host = self.query_one("#detail-episode-strip-host", Vertical)
        note = self.query_one("#detail-episode-note", Static)
        await host.remove_children()
        if self.anime.episodes:
            note.update("Use Select Episode to browse and play a specific episode.")
            await host.mount(
                EpisodeStrip(
                    self.anime,
                    watched_episode=self.entry.episode if self.entry else None,
                    states=self.states,
                )
            )
        else:
            note.update(
                "Episode information is unavailable for this title. "
                "Select Episode still lets you enter a number directly."
            )

    def _update_actions(self) -> None:
        if not self.anime:
            return
        watch = self.query_one("#detail-watch", Button)
        watchlist = self.query_one("#detail-watchlist", Button)
        mark = self.query_one("#detail-mark", Button)

        last = self.entry.episode if self.entry else None
        if not last:
            watch.label = "▶  Watch"
        elif is_series_completed(self.anime, last, self.states):
            watch.label = "▤  Browse Episodes"
        else:
            watch.label = f"▶  Resume EP {last}"

        saved = self.app.database.is_watchlisted(self.anime.id)
        watchlist.label = "−  Remove Watchlist" if saved else "+  Add Watchlist"

        target = self._last_marked_episode or last
        watched = bool(target and self.states.get(str(target)) in {COMPLETED, WATCHING})
        mark.label = "↺  Mark Unwatched" if watched else "✓  Mark Watched"
        mark.disabled = not last

    def _set_actions_enabled(self, enabled: bool) -> None:
        for button_id in (
            "#detail-watch",
            "#detail-select",
            "#detail-watchlist",
            "#detail-mark",
        ):
            self.query_one(button_id, Button).disabled = not enabled

    # ------------------------------------------------------------------
    # Actions

    def action_watch(self) -> None:
        self._last_marked_episode = None
        if self.anime:
            asyncio.create_task(self.app.watch_anime(self.anime))

    def action_select_episode(self) -> None:
        self._last_marked_episode = None
        if self.anime:
            self.app.open_episode_selector(self.anime)

    async def action_watchlist(self) -> None:
        if not self.anime:
            return
        added = await self.app.toggle_watchlist(self.anime)
        self.app.notify_user("Added to watchlist" if added else "Removed from watchlist")
        self._update_actions()

    async def action_toggle_marked(self) -> None:
        episode = self._last_marked_episode or (
            self.entry.episode if self.entry else None
        )
        if not self.anime or not episode:
            self.app.notify_user("No episode progress to mark.")
            return
        if self.states.get(str(episode)) in {COMPLETED, WATCHING}:
            await self.app.mark_episode_unwatched(self.anime, episode)
            self._last_marked_episode = None
            self.app.notify_user(f"Marked episode {episode} as unwatched")
        else:
            await self.app.mark_episode_watched(self.anime, episode)
            self._last_marked_episode = str(episode)
            self.app.notify_user(f"Marked episode {episode} as watched")
        await self._load_progress()

    async def action_mark_unwatched(self) -> None:
        episode = self._last_marked_episode or (
            self.entry.episode if self.entry else None
        )
        if not self.anime or not episode:
            self.app.notify_user("No episode progress to mark.")
            return
        await self.app.mark_episode_unwatched(self.anime, episode)
        self._last_marked_episode = None
        self.app.notify_user(f"Marked episode {episode} as unwatched")
        await self._load_progress()

    def action_back(self) -> None:
        self.app.pop_screen()

    # ------------------------------------------------------------------
    # Button events

    @on(Button.Pressed, "#detail-watch")
    def on_watch(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_watch()

    @on(Button.Pressed, "#detail-select")
    def on_select_episode(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_select_episode()

    @on(Button.Pressed, "#detail-watchlist")
    async def on_watchlist(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_watchlist()

    @on(Button.Pressed, "#detail-mark")
    async def on_mark_watched(self, event: Button.Pressed) -> None:
        event.stop()
        await self.action_toggle_marked()

    @on(Button.Pressed, "#detail-back")
    def on_back(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_back()
