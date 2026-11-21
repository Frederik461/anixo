"""Discover screen: searchable, filterable poster grid."""

from __future__ import annotations

import asyncio
from typing import Union

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.events import Click
from textual.reactive import reactive
from textual.widgets import Input, Static

from components.anime_card import AnimeCard
from components.navigation import Selection
from components.shell import ShellScreen
from components.ui import (
    ComposedGrid,
    EmptyState,
    LoadingState,
    PosterTile,
    SearchField,
    SectionHeading,
)
from models.anime import Anime

_CATEGORIES = ("Trending", "Popular", "Airing", "Movies", "Recent")
_GENRES = (
    "All",
    "Action",
    "Adventure",
    "Comedy",
    "Drama",
    "Fantasy",
    "Horror",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Slice of Life",
    "Sports",
    "Supernatural",
    "Thriller",
)
_SORTS = ("Default", "Score", "A–Z", "Latest")
_ResultCard = Union[PosterTile, AnimeCard]


class DiscoverScreen(ShellScreen):
    BINDINGS = [
        Binding("left", "move(-1)", "Previous", show=False),
        Binding("right", "move(1)", "Next", show=False),
        Binding("up", "page(-1)", "Prev page", show=False),
        Binding("down", "page(1)", "Next page", show=False),
        Binding("enter", "open", "Watch"),
        Binding("a", "watchlist", "Watchlist"),
        Binding("i", "details", "Details"),
        Binding("space", "toggle_view", "Grid/list"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "focus_search", "Search"),
        Binding("c", "category", "Category"),
        Binding("[", "genre(-1)", "Prev genre"),
        Binding("]", "genre(1)", "Next genre"),
        Binding("s", "sort", "Sort"),
        Binding("escape", "app.goto_dashboard", "Home", show=False),
    ]

    selected: reactive[int] = reactive(0)
    view_mode: reactive[str] = reactive("grid")

    def __init__(self) -> None:
        super().__init__()
        self.results: list[Anime] = []
        self.query_text = ""
        self.category_index = 0
        self.category = _CATEGORIES[0]
        self.genres: tuple[str, ...] = _GENRES
        self.genre_index = 0
        self.sort_index = 0
        self.selection = Selection(page_size=self.app.config.page_size if self.is_mounted else 12)
        self._search_timer = None
        self._search_generation = 0
        self._render_generation = 0
        self._search_lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._last_tier = ""

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            yield SearchField(
                input_id="search-input",
                placeholder="Search anime by title…",
            )
            category_chip = Static(
                self.category, id="filter-category", classes="filter-chip active"
            )
            category_chip.tooltip = "Cycle category (C). Click to change."
            genre_chip = Static("All", id="filter-genre", classes="filter-chip")
            genre_chip.tooltip = "Cycle genre ([ / ]). Right-click for previous."
            sort_chip = Static("Default", id="filter-sort", classes="filter-chip")
            sort_chip.tooltip = "Cycle sort order (S). Click to change."
            with Horizontal(classes="filter-bar"):
                yield Static("CATEGORY", classes="filter-label")
                yield category_chip
                yield Static("GENRE", classes="filter-label")
                yield genre_chip
                yield Static("SORT", classes="filter-label")
                yield sort_chip
            with VerticalScroll(id="discover-body"):
                yield SectionHeading("Results", "Poster-first browsing")
                yield LoadingState("Loading anime…")
                yield Grid(id="results-grid")

        yield from self.compose_shell(
            active="nav-discover",
            title="Discover",
            hint="Search • filter • browse",
            content=content(),
        )

    async def on_mount(self) -> None:
        self.selection.page_size = self.app.config.page_size
        await self.run_search("")

    def on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        super().on_resize(event)
        tier = self._tier()
        if self.is_mounted and tier != self._last_tier:
            self._last_tier = tier
            asyncio.create_task(self._render_page())

    def _tier(self) -> str:
        if self.has_class("compact"):
            return "compact"
        return "wide" if self.has_class("wide") else "medium"

    def _columns(self) -> int:
        if self.view_mode == "list":
            return 1
        return {"compact": 2, "medium": 3, "wide": 4}[self._tier()]

    @property
    def current_category(self) -> str:
        return _CATEGORIES[self.category_index] if _CATEGORIES else "Trending"

    @property
    def current_genre(self) -> str:
        return self.genres[self.genre_index] if self.genres else "All"

    @property
    def current_sort(self) -> str:
        return _SORTS[self.sort_index] if _SORTS else "Default"

    @property
    def visible_results(self) -> list[Anime]:
        items = self.results
        genre = self.current_genre
        if genre != "All":
            items = [item for item in items if genre in item.genres]
        sort = self.current_sort
        if sort == "Score":
            items = sorted(items, key=lambda item: item.average_score or 0, reverse=True)
        elif sort == "A–Z":
            items = sorted(items, key=lambda item: item.display_title().casefold())
        elif sort == "Latest":
            items = sorted(
                items,
                key=lambda item: item.season_year or item.updated_at.year,
                reverse=True,
            )
        return items

    def _cancel_pending_search(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None

    def _current_query(self) -> str:
        try:
            return self.query_one("#search-input", Input).value
        except Exception:
            return self.query_text

    async def run_search(self, query: str) -> None:
        """Run the newest search only; older requests are discarded."""
        generation = self._search_generation + 1
        self._search_generation = generation
        async with self._search_lock:
            if generation != self._search_generation:
                return
            await self._perform_search(query, generation)

    async def _perform_search(self, query: str, generation: int) -> None:
        self.query_text = query
        is_search = bool(query.strip())
        self.category = "Search" if is_search else self.current_category
        body = self.query_one("#discover-body", VerticalScroll)
        await body.remove_children()
        await body.mount(SectionHeading("Results", "Loading…"))
        await body.mount(LoadingState("Loading anime…"))
        try:
            if is_search:
                self.results = await self.app.api.search(query, per_page=48)
            else:
                self.results = await self.app.api.browse(
                    category=self.current_category,
                    genre=self.current_genre,
                    sort=self.current_sort,
                    per_page=48,
                )
        except Exception:
            self.results = []
        if generation != self._search_generation:
            return
        self.selection.clamp(len(self.visible_results))
        await self._render_page()

    async def _render_page(self) -> None:
        """Serialize grid rebuilds so stale pages cannot remount old posters."""
        generation = self._render_generation + 1
        self._render_generation = generation
        async with self._render_lock:
            if generation != self._render_generation:
                return
            await self._build_page()

    async def _build_page(self) -> None:
        body = self.query_one("#discover-body", VerticalScroll)
        items = self.visible_results
        await body.remove_children()

        await body.mount(
            SectionHeading(
                "Results",
                f"{len(items)} titles" if items else "No matches",
            )
        )

        category_chip = self.query_one("#filter-category", Static)
        genre_chip = self.query_one("#filter-genre", Static)
        sort_chip = self.query_one("#filter-sort", Static)
        category_chip.update(self.category)
        genre_chip.update(self.current_genre)
        sort_chip.update(self.current_sort)
        category_chip.set_class(
            self.category != "Trending" or bool(self.query_text), "active"
        )
        genre_chip.set_class(self.current_genre != "All", "active")
        sort_chip.set_class(self.current_sort != "Default", "active")

        if not items:
            await body.mount(EmptyState("No anime matched this search and filter."))
            return

        start = self.selection.page_start
        page = items[start : self.selection.page_end]
        children: list[_ResultCard] = []
        for offset, anime in enumerate(page):
            if self.view_mode == "list":
                card = AnimeCard(
                    anime,
                    watchlisted=self.app.database.is_watchlisted(anime.id),
                    poster_width=10,
                    poster_height=8,
                    renderer=self.app.config.artwork_renderer,
                )
                card.data = {"anime": anime, "index": start + offset}
                card.styles.width = "100%"
                children.append(card)
            else:
                tile = PosterTile(
                    anime,
                    artwork=self.app.artwork,
                    renderer=self.app.config.artwork_renderer,
                    watchlisted=self.app.database.is_watchlisted(anime.id),
                    poster_width=9 if self.has_class("compact") else 13,
                    poster_height=7 if self.has_class("compact") else 10,
                )
                tile.data = {"anime": anime, "index": start + offset}
                children.append(tile)

        grid = ComposedGrid(children, id="results-grid")
        grid.styles.grid_size_columns = self._columns()
        await body.mount(grid)
        self.selection.clamp(len(items))
        self.update_selection()

    def update_selection(self) -> None:
        cards = self.cards
        for index, card in enumerate(cards):
            card.selected = index == self.selection.index - self.selection.page_start

    @property
    def cards(self) -> list[_ResultCard]:
        try:
            return list(self.query_one("#results-grid", Grid).query("PosterTile,AnimeCard"))
        except Exception:
            return []

    async def _reselect_after_page(self) -> None:
        self.selection.clamp(len(self.visible_results))
        await self._render_page()

    def action_move(self, delta: int) -> None:
        old_page = self.selection.page_start
        self.selection.move(delta, len(self.visible_results))
        if self.selection.page_start != old_page:
            asyncio.create_task(self._reselect_after_page())
        else:
            self.update_selection()

    def action_page(self, delta: int) -> None:
        old_page = self.selection.page_start
        self.selection.page_move(delta, len(self.visible_results))
        if self.selection.page_start != old_page:
            asyncio.create_task(self._reselect_after_page())
        else:
            self.update_selection()

    def action_focus_search(self) -> None:
        self.query_one(SearchField).focus_field()

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        # Queued change messages from rapid typing must not override a filter
        # change that happened after them.
        if event.value != self._current_query():
            return
        self._schedule_search(event.value)

    def _schedule_search(self, query: str) -> None:
        if self._search_timer:
            self._search_timer.stop()
        self._search_timer = self.set_timer(
            0.35, lambda: asyncio.create_task(self._run_deferred_search(query))
        )

    async def _run_deferred_search(self, query: str) -> None:
        if query == self._current_query():
            await self.run_search(query)

    @on(Input.Submitted, "#search-input")
    async def on_search_submitted(self, event: Input.Submitted) -> None:
        self.query_one(SearchField).blur_field()
        self._cancel_pending_search()
        await self.run_search(event.value)

    # ------------------------------------------------------------------
    # Filter chips (click and keyboard)

    @on(Click, "#filter-category")
    async def _on_category_click(self, event: Click) -> None:
        event.stop()
        await self.action_category()

    @on(Click, "#filter-genre")
    async def _on_genre_click(self, event: Click) -> None:
        event.stop()
        await self.action_genre(-1 if event.button == 2 else 1)

    @on(Click, "#filter-sort")
    async def _on_sort_click(self, event: Click) -> None:
        event.stop()
        await self.action_sort()

    async def action_open(self) -> None:
        items = self.visible_results
        if 0 <= self.selection.index < len(items):
            await self.app.watch_anime(items[self.selection.index])

    def action_details(self) -> None:
        items = self.visible_results
        if 0 <= self.selection.index < len(items):
            self.app.open_details(items[self.selection.index].id)

    async def action_watchlist(self) -> None:
        items = self.visible_results
        if 0 <= self.selection.index < len(items):
            anime = items[self.selection.index]
            added = await self.app.toggle_watchlist(anime)
            self.app.notify_user("Added to watchlist" if added else "Removed from watchlist")

    async def action_toggle_view(self) -> None:
        self.view_mode = "list" if self.view_mode == "grid" else "grid"
        await self._render_page()

    async def action_refresh(self) -> None:
        self.selection.index = 0
        self._cancel_pending_search()
        await self.run_search(self._current_query())

    async def action_category(self) -> None:
        field = self.query_one("#search-input", Input)
        if self.query_text or field.value.strip():
            field.value = ""
            self.category_index = 0
        else:
            self.category_index = (self.category_index + 1) % len(_CATEGORIES)
        self.category = self.current_category
        self.selection.index = 0
        self._cancel_pending_search()
        await self.run_search("")

    async def action_genre(self, delta: int) -> None:
        if not self.genres:
            return
        self.genre_index = (self.genre_index + delta) % len(self.genres)
        self.selection.index = 0
        self._cancel_pending_search()
        await self.run_search(self._current_query())

    async def action_sort(self) -> None:
        self.sort_index = (self.sort_index + 1) % len(_SORTS)
        self.selection.index = 0
        self._cancel_pending_search()
        await self.run_search(self._current_query())
