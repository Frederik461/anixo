"""Dashboard: featured presentation, continue watching, and trending."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Button, Static

from components.anime_card import AnimeCard
from components.navigation import Selection
from components.shell import ShellScreen
from components.ui import (
    ComposedRow,
    EmptyState,
    FeaturedPanel,
    LoadingState,
    SectionHeading,
)
from models.anime import Anime, HistoryEntry


def relative_time(value: datetime | None) -> str:
    if not value:
        return "Recently"
    now = datetime.now(timezone.utc)
    delta = now - value
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "Just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 1440:
        return f"{minutes // 60}h ago"
    return f"{minutes // 1440}d ago"


def _progress(entry: HistoryEntry) -> str | None:
    current = entry.progress_seconds
    total = entry.duration_seconds
    if not current or not total or total <= 0:
        return None
    percent = max(0, min(100, round(current / total * 100)))
    return f"{percent}% watched"


class DashboardScreen(ShellScreen):
    BINDINGS = [
        Binding("left", "move(-1)", "Previous", show=False),
        Binding("right", "move(1)", "Next", show=False),
        Binding("up", "section(-1)", "Continue", show=False),
        Binding("down", "section(1)", "Trending", show=False),
        Binding("enter", "open", "Open"),
        Binding("a", "watchlist", "Watchlist"),
        Binding("d", "details", "Details"),
    ]

    section: reactive[int] = reactive(0)
    selected: reactive[int] = reactive(0)

    def __init__(self) -> None:
        super().__init__()
        self.continue_entries: list[HistoryEntry] = []
        self.trending: list[Anime] = []
        self.selection = Selection(page_size=8)
        self.featured_data: tuple[Anime, str, str | None, str | None] | None = None
        self.featured_entry: HistoryEntry | None = None
        self._render_generation = 0
        self._render_lock = asyncio.Lock()
        self._last_tier = ""

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            with VerticalScroll(id="dashboard-content"):
                yield LoadingState("Curating your dashboard…")

        yield from self.compose_shell(
            active="nav-home",
            title="Home",
            hint="Continue watching • Trending now",
            content=content(),
        )

    async def on_mount(self) -> None:
        await self.refresh_data()

    def on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        super().on_resize(event)
        tier = self._tier()
        if self.is_mounted and tier != self._last_tier and self._has_data():
            self._last_tier = tier
            asyncio.create_task(self._render_dashboard())

    def _tier(self) -> str:
        if self.has_class("compact"):
            return "compact"
        if self.has_class("wide"):
            return "wide"
        return "medium"

    def _visible_count(self) -> int:
        return {"compact": 2, "medium": 3, "wide": 4}[self._tier()]

    def _has_data(self) -> bool:
        return bool(self.continue_entries or self.trending)

    async def refresh_data(self) -> None:
        app = self.app
        self.continue_entries = await asyncio.to_thread(app.database.list_history, 64)
        try:
            self.trending = await app.api.trending(per_page=12)
        except Exception:
            self.trending = []
        for entry in self.continue_entries:
            app.api.enrich_from_cache(entry.anime.display_title(), entry.anime)
        self._last_tier = self._tier()
        await self._render_dashboard()

    def _choose_featured(self) -> None:
        if self.continue_entries:
            entry = self.continue_entries[0]
            when = entry.watched_at or entry.last_watched
            self.featured_data = (
                entry.anime,
                "CONTINUE WATCHING",
                entry.episode,
                f"Last watched {relative_time(when)}",
            )
            self.featured_entry = entry
            return
        if self.trending:
            anime = max(
                self.trending,
                key=lambda item: (item.average_score or 0, item.popularity or 0),
            )
            self.featured_data = (anime, "TRENDING NOW", None, None)
            self.featured_entry = None
            return
        self.featured_data = None
        self.featured_entry = None

    async def _render_dashboard(self) -> None:
        """Rebuild the dashboard, discarding stale resize-driven renders."""
        generation = self._render_generation + 1
        self._render_generation = generation
        async with self._render_lock:
            if generation != self._render_generation:
                return
            await self._build_dashboard()

    async def _build_dashboard(self) -> None:
        container = self.query_one("#dashboard-content", VerticalScroll)
        self._choose_featured()
        await container.remove_children()
        widgets: list[ComposeResult] = []

        if self.featured_data:
            anime, kicker, episode, watched = self.featured_data
            # Terminal cells are about twice as tall as they are wide, so a
            # 2:3 cover needs roughly three rows for every four columns.
            poster_width, poster_height = (
                (16, 12)
                if self._tier() == "compact"
                else (24, 18)
                if self._tier() == "wide"
                else (20, 15)
            )
            widgets.append(
                FeaturedPanel(
                    anime,
                    artwork=self.app.artwork,
                    renderer=self.app.config.artwork_renderer,
                    kicker=kicker,
                    episode=episode,
                    watched_label=watched,
                    poster_width=poster_width,
                    poster_height=poster_height,
                )
            )

        limit = self._visible_count()
        if self.continue_entries:
            widgets.append(
                SectionHeading(
                    "Continue Watching", f"{min(limit, len(self.continue_entries))} shown"
                )
            )
            cards: list[AnimeCard] = []
            for entry in self.continue_entries[:limit]:
                card = AnimeCard(
                    entry.anime,
                    episode=entry.episode,
                    detail=_progress(entry)
                    or f"Last watched {relative_time(entry.watched_at or entry.last_watched)}",
                    renderer=self.app.config.artwork_renderer,
                )
                card.data = {"kind": "history", "entry": entry}
                cards.append(card)
            widgets.append(ComposedRow(cards, id="continue-row", classes="card-row"))

        if self.trending:
            widgets.append(SectionHeading("Trending Now", "Updated from AniList"))
            cards = []
            for anime in self.trending[:limit]:
                card = AnimeCard(
                    anime,
                    watchlisted=self.app.database.is_watchlisted(anime.id),
                    renderer=self.app.config.artwork_renderer,
                )
                card.data = {"kind": "trending", "anime": anime}
                cards.append(card)
            widgets.append(ComposedRow(cards, id="trending-row", classes="card-row"))

        if not widgets:
            widgets.append(EmptyState("No anime data yet. Try Discover to build your library."))

        await container.mount_all(widgets)
        self.selection.clamp(len(self.active_cards))
        self.update_selection()

    @property
    def active_cards(self) -> list[AnimeCard]:
        row_id = "#continue-row" if self.section == 0 else "#trending-row"
        try:
            row = self.query_one(row_id, HorizontalScroll)
        except Exception:
            return []
        return list(row.query("AnimeCard"))

    def update_selection(self) -> None:
        """Only toggles CSS classes; posters are never recomposed here."""
        cards = self.active_cards
        self.selection.clamp(len(cards))
        for index, card in enumerate(cards):
            card.selected = index == self.selection.index

    def action_move(self, delta: int) -> None:
        self.selection.move(delta, len(self.active_cards))
        self.update_selection()

    def action_section(self, delta: int) -> None:
        has_continue = bool(self.query("#continue-row"))
        has_trending = bool(self.query("#trending-row"))
        if not has_continue and not has_trending:
            return
        if delta < 0:
            self.section = 0 if has_continue else 1
        else:
            self.section = 1 if has_trending else 0
        self.selection.index = 0
        self.update_selection()

    async def action_open(self) -> None:
        card = self._selected_card()
        if not card or not card.data:
            return
        if card.data["kind"] == "history":
            await self.app.watch_anime(card.data["entry"].anime)
        else:
            self.app.open_details(card.data["anime"].id)

    async def action_watchlist(self) -> None:
        card = self._selected_card()
        if not card or not card.data:
            return
        anime = card.data.get("anime") or card.data["entry"].anime
        added = await self.app.toggle_watchlist(anime)
        self.app.notify_user(
            f"Saved {anime.display_title()}" if added else f"Removed {anime.display_title()}"
        )

    def action_details(self) -> None:
        card = self._selected_card()
        if card and card.data:
            anime = card.data.get("anime") or card.data["entry"].anime
            self.app.open_details(anime.id)

    def _selected_card(self) -> AnimeCard | None:
        cards = self.active_cards
        return cards[self.selection.index] if cards else None

    @on(Button.Pressed, "#featured-watch")
    async def on_featured_watch(self, event: Button.Pressed) -> None:
        event.stop()
        if not self.featured_data:
            return
        anime = self.featured_entry.anime if self.featured_entry else self.featured_data[0]
        await self.app.watch_anime(anime)

    @on(Button.Pressed, "#featured-details")
    def on_featured_details(self, event: Button.Pressed) -> None:
        event.stop()
        if self.featured_data:
            self.app.open_details(self.featured_data[0].id)
