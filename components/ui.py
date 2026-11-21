"""Reusable ANIXO design-system widgets.

These widgets are presentation only. They do not fetch data, render terminal
graphics themselves, or mutate application state.
"""

from __future__ import annotations

import re
from typing import Iterable

from textual.app import ComposeResult
from textual.containers import Container, Grid, Horizontal, HorizontalScroll, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from components.poster import Poster
from models.anime import Anime
from models.episode import COMPLETED, WATCHING

_DIGITS = re.compile(r"(\d+)")


def plain_text(value: str | None, fallback: str = "") -> str:
    """Strip lightweight markup from API descriptions."""
    if not value:
        return fallback
    text = re.sub(r"<br\s*/?>", " ", value)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split())


def score_text(anime: Anime) -> str | None:
    if not anime.average_score:
        return None
    return f"★ {round(anime.average_score)}%"


def anime_meta(anime: Anime, *, include_score: bool = True) -> str:
    parts: list[str] = []
    if anime.format:
        parts.append(anime.format)
    if anime.season_year:
        parts.append(str(anime.season_year))
    if anime.episodes:
        parts.append(f"{anime.episodes} eps")
    if include_score:
        score = score_text(anime)
        if score:
            parts.append(score)
    return "  •  ".join(parts) if parts else "Anime"


class SectionHeading(Horizontal, can_focus=False):
    """Consistent section title with optional trailing context."""

    def __init__(
        self,
        label: str,
        hint: str = "",
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=f"section-heading {classes or ''}".strip())
        self.label = label
        self.hint = hint

    def compose(self) -> ComposeResult:
        yield Static(f"▰ {self.label}", classes="section-title")
        if self.hint:
            yield Static(self.hint, classes="section-hint")


class MetaText(Static):
    """Muted one-line metadata."""

    def __init__(self, value: str, *, classes: str | None = None) -> None:
        super().__init__(value, classes=f"meta-text {classes or ''}".strip())


class ComposedGrid(Grid):
    """Grid whose children are supplied at construction time.

    Mounting the grid and its children as one operation avoids detached-widget
    races when a screen is resized or filtered rapidly.
    """

    def __init__(
        self,
        children: Iterable[Widget],
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._children = list(children)

    def compose(self) -> ComposeResult:
        yield from self._children


class ComposedRow(HorizontalScroll):
    """Horizontal row whose children are supplied at construction time."""

    def __init__(
        self,
        children: Iterable[Widget],
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._children = list(children)

    def compose(self) -> ComposeResult:
        yield from self._children


class Badge(Static):
    """Small status badge without a heavy container."""

    def __init__(
        self,
        value: str,
        *,
        variant: str = "",
        classes: str | None = None,
    ) -> None:
        name = f"badge {variant} {classes or ''}".strip()
        super().__init__(value, classes=name)


class SearchField(Container, can_focus=False):
    """Search/filter input with a consistent icon and shortcut hint."""

    def __init__(
        self,
        *,
        input_id: str,
        placeholder: str,
        value: str = "",
        hint: str = "/",
    ) -> None:
        super().__init__(classes="search-field")
        self.input_id = input_id
        self.placeholder_text = placeholder
        self.initial_value = value
        self.hint_text = hint

    def compose(self) -> ComposeResult:
        with Horizontal(classes="search-box"):
            yield Static("⌕", classes="search-icon")
            field = Input(
                value=self.initial_value,
                placeholder=self.placeholder_text,
                id=self.input_id,
                classes="search-input",
            )
            field.can_focus = False
            yield field
            if self.hint_text:
                yield Static(self.hint_text, classes="keycap")

    @property
    def field(self) -> Input:
        return self.query_one(f"#{self.input_id}", Input)

    def focus_field(self) -> None:
        field = self.field
        field.can_focus = True
        field.focus()

    def blur_field(self) -> None:
        field = self.field
        field.can_focus = False
        field.blur()


class StateMessage(Static):
    """Loading, empty, and unavailable states share one visual language."""

    def __init__(
        self,
        value: str,
        *,
        variant: str = "",
        classes: str | None = None,
    ) -> None:
        name = f"state-message {variant} {classes or ''}".strip()
        super().__init__(value, classes=name)


class LoadingState(StateMessage):
    def __init__(self, value: str = "Loading anime…") -> None:
        super().__init__(f"◌  {value}", variant="loading")


class EmptyState(StateMessage):
    def __init__(self, value: str = "Nothing here yet.") -> None:
        super().__init__(f"◇  {value}")


class PosterTile(Container, can_focus=False):
    """Poster-first card for grids. Selection never recomposes its poster."""

    selected = reactive(False)

    def __init__(
        self,
        anime: Anime,
        *,
        artwork,
        renderer: str,
        poster_width: int = 13,
        poster_height: int = 10,
        watchlisted: bool = False,
        classes: str | None = None,
    ) -> None:
        super().__init__(classes=f"poster-tile {classes or ''}".strip())
        self.anime = anime
        self.artwork = artwork
        self.renderer = renderer
        self.poster_width = poster_width
        self.poster_height = poster_height
        self.watchlisted = watchlisted
        self.data: dict[str, object] = {"anime": anime}

    def compose(self) -> ComposeResult:
        letter = (self.anime.display_title()[:1] or "?").upper()
        yield Poster(
            url=self.anime.cover_url,
            width=self.poster_width,
            height=self.poster_height,
            renderer=self.renderer,  # type: ignore[arg-type]
            fallback_letter=letter,
            fallback_color=self.anime.cover_color or "#a78bfa",
            classes="poster tile-poster",
        )
        yield Static(self.anime.display_title(), classes="tile-title")
        yield Static(anime_meta(self.anime), classes="tile-meta")
        if self.watchlisted:
            yield Badge("◆ SAVED", variant="accent")

    def watch_selected(self, selected: bool) -> None:
        self.set_class(selected, "selected")


class FeaturedPanel(Container, can_focus=False):
    """Large hero presentation using only known anime data."""

    def __init__(
        self,
        anime: Anime,
        *,
        artwork,
        renderer: str,
        kicker: str,
        episode: str | None = None,
        watched_label: str | None = None,
        poster_width: int = 20,
        poster_height: int = 15,
    ) -> None:
        super().__init__(classes="featured-panel")
        self.anime = anime
        self.artwork = artwork
        self.renderer = renderer
        self.kicker = kicker
        self.episode = episode
        self.watched_label = watched_label
        self.poster_width = poster_width
        self.poster_height = poster_height

    def compose(self) -> ComposeResult:
        letter = (self.anime.display_title()[:1] or "?").upper()
        with Horizontal(classes="featured-inner"):
            yield Poster(
                url=self.anime.cover_url,
                width=self.poster_width,
                height=self.poster_height,
                renderer=self.renderer,  # type: ignore[arg-type]
                fallback_letter=letter,
                fallback_color=self.anime.cover_color or "#a78bfa",
                classes="poster featured-poster",
            )
            with Vertical(classes="featured-info"):
                with Vertical(classes="featured-copy"):
                    yield Static(self.kicker, classes="featured-kicker")
                    yield Static(self.anime.display_title(), classes="featured-title")
                    yield Static(anime_meta(self.anime), classes="featured-meta")
                    if self.episode:
                        yield Static(f"Episode {self.episode}", classes="featured-meta")
                    if self.watched_label:
                        yield Static(self.watched_label, classes="featured-meta")
                    yield Static(
                        plain_text(self.anime.description, "No description available."),
                        classes="featured-description",
                    )
                with Horizontal(classes="featured-actions"):
                    yield Button("▶  Watch", id="featured-watch", classes="action-button primary")
                    yield Button("ⓘ  Details", id="featured-details", classes="action-button ghost")


class EpisodeStrip(Container, can_focus=False):
    """Episode overview derived from known episode counts and history."""

    def __init__(
        self,
        anime: Anime,
        *,
        watched_episode: str | None = None,
        states: dict[str, str] | None = None,
        limit: int = 24,
    ) -> None:
        super().__init__(classes="episode-strip")
        self.anime = anime
        self.watched_episode = watched_episode
        self.states = states or {}
        self.limit = limit

    @property
    def current_number(self) -> int | None:
        if not self.watched_episode:
            return None
        match = _DIGITS.search(self.watched_episode)
        return int(match.group(1)) if match else None

    def compose(self) -> ComposeResult:
        total = self.anime.episodes or 0
        if total <= 0:
            yield EmptyState("Episode information is unavailable for this title.")
            return
        shown = min(total, self.limit)
        current = self.current_number
        yield SectionHeading("Episodes", f"{total} known")
        with HorizontalScroll(classes="episode-scroll"):
            for number in range(1, shown + 1):
                state = self.states.get(str(number))
                classes = "episode-chip"
                if state == COMPLETED:
                    classes += " watched"
                elif state == WATCHING:
                    classes += " current"
                elif current and number == current:
                    classes += " current"
                elif current and number < current:
                    classes += " watched"
                yield Static(str(number), classes=classes)
        if total > shown:
            yield MetaText(f"Showing {shown} of {total} episodes.")
