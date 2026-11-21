"""Saved anime collection with poster-first browsing."""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, VerticalScroll
from textual.widgets import Input

from components.shell import ShellScreen
from components.ui import ComposedGrid, EmptyState, PosterTile, SearchField, SectionHeading
from models.anime import Anime


class WatchlistScreen(ShellScreen):
    BINDINGS = [
        Binding("left", "move(-1)", "Previous", show=False),
        Binding("right", "move(1)", "Next", show=False),
        Binding("down", "move(1)", "Next", show=False),
        Binding("up", "move(-1)", "Previous", show=False),
        Binding("enter", "details", "Details"),
        Binding("d,x,delete", "remove", "Remove"),
        Binding("/", "focus_filter", "Filter"),
        Binding("escape", "app.goto_dashboard", "Home", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.items: list[Anime] = []
        self.selection_index = 0
        self._render_generation = 0
        self._render_lock = asyncio.Lock()
        self._last_tier = ""

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            yield SearchField(
                input_id="watchlist-filter",
                placeholder="Filter your collection…",
            )
            with VerticalScroll(id="watchlist-body"):
                yield SectionHeading("Collection", "Saved anime")
                yield Grid(id="watchlist-grid")

        yield from self.compose_shell(
            active="nav-watchlist",
            title="Watchlist",
            hint="Your saved anime",
            content=content(),
        )

    async def on_mount(self) -> None:
        await self.refresh_items()
        self.query_one(SearchField).blur_field()

    def on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        super().on_resize(event)
        tier = self._tier()
        if self.is_mounted and tier != self._last_tier:
            self._last_tier = tier
            asyncio.create_task(self.render_cards())

    def _tier(self) -> str:
        if self.has_class("compact"):
            return "compact"
        return "wide" if self.has_class("wide") else "medium"

    async def refresh_items(self) -> None:
        self.items = [
            entry.anime for entry in await asyncio.to_thread(self.app.database.list_watchlist)
        ]
        await self.render_cards()

    @property
    def filtered_items(self) -> list[Anime]:
        query = (
            self.query_one("#watchlist-filter", Input).value
            if self.is_mounted
            else ""
        ).strip().casefold()
        if not query:
            return self.items
        return [anime for anime in self.items if query in anime.display_title().casefold()]

    async def render_cards(self) -> None:
        """Rebuild the grid, serialized so stale renders cannot remount old cards."""
        generation = self._render_generation + 1
        self._render_generation = generation
        async with self._render_lock:
            if generation != self._render_generation:
                return
            body = self.query_one("#watchlist-body", VerticalScroll)
            items = self.filtered_items
            await body.remove_children()
            if generation != self._render_generation:
                return
            await body.mount(SectionHeading("Collection", f"{len(items)} saved"))
            self._last_tier = self._tier()

            if not items:
                await body.mount(
                    EmptyState("No saved anime. Press G to find something new.")
                )
                return

            tiles: list[PosterTile] = []
            for anime in items:
                tile = PosterTile(
                    anime,
                    artwork=self.app.artwork,
                    renderer=self.app.config.artwork_renderer,
                    watchlisted=True,
                    poster_width=9 if self.has_class("compact") else 13,
                    poster_height=7 if self.has_class("compact") else 10,
                )
                tile.data = {"anime": anime}
                tiles.append(tile)

            grid = ComposedGrid(tiles, id="watchlist-grid")
            grid.styles.grid_size_columns = {
                "compact": 2,
                "medium": 3,
                "wide": 4,
            }[self._tier()]
            await body.mount(grid)
            if generation != self._render_generation:
                return
            self.selection_index = max(0, min(self.selection_index, len(items) - 1))
            self.update_selection()

    def update_selection(self) -> None:
        cards = self.cards
        for index, card in enumerate(cards):
            card.selected = index == self.selection_index

    @property
    def cards(self) -> list[PosterTile]:
        try:
            return list(self.query_one("#watchlist-grid", Grid).query(PosterTile))
        except Exception:
            return []

    def action_focus_filter(self) -> None:
        self.query_one(SearchField).focus_field()

    @on(Input.Changed, "#watchlist-filter")
    async def on_filter_changed(self, event: Input.Changed) -> None:
        self.selection_index = 0
        await self.render_cards()

    def action_move(self, delta: int) -> None:
        self.selection_index = max(0, min(len(self.cards) - 1, self.selection_index + delta))
        self.update_selection()

    def action_details(self) -> None:
        items = self.filtered_items
        if 0 <= self.selection_index < len(items):
            self.app.open_details(items[self.selection_index].id)

    async def action_remove(self) -> None:
        items = self.filtered_items
        if 0 <= self.selection_index < len(items):
            anime = items[self.selection_index]
            await asyncio.to_thread(self.app.database.remove_watchlist_anime, anime)
            self.app.notify_user(f"Removed {anime.display_title()}")
            await self.refresh_items()
