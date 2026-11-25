"""Anime card widget. Selection changes only card chrome, never the poster."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from components.poster import Poster
from components.ui import Badge, anime_meta, plain_text
from models.anime import Anime
from services.artwork import ArtworkService

if TYPE_CHECKING:
    from app import AnixoApp


class AnimeCard(Horizontal, can_focus=False):
    """Mouse-selectable anime card. Clicking it opens the anime details."""

    selected = reactive(False)

    class Chosen(Message):
        def __init__(self, anime: Anime, source: AnimeCard | None = None) -> None:
            self.anime = anime
            self.source = source
            super().__init__()

    def __init__(
        self,
        anime: Anime,
        *,
        episode: str | None = None,
        detail: str | None = None,
        watchlisted: bool = False,
        width: int = 46,
        poster_width: int = 14,
        poster_height: int = 11,
        artwork: ArtworkService | None = None,
        renderer: str = "auto",
    ) -> None:
        super().__init__(classes="anime-card")
        self.anime = anime
        self.episode = episode
        self.detail = detail
        self.watchlisted = watchlisted
        self.poster_width = poster_width
        self.poster_height = poster_height
        self._artwork = artwork
        self._renderer = renderer
        self.styles.width = width

    def compose(self) -> ComposeResult:
        app = cast("AnixoApp", self.app)
        renderer = self._renderer or app.config.artwork_renderer
        letter = (self.anime.display_title()[:1] or "?").upper()
        yield Poster(
            url=self.anime.cover_url,
            width=self.poster_width,
            height=self.poster_height,
            renderer=renderer,  # type: ignore[arg-type]
            fallback_letter=letter,
            fallback_color=self.anime.cover_color or "#a78bfa",
        )
        with Vertical(classes="card-meta"):
            if self.episode:
                yield Badge(f"EP {self.episode}", variant="score")
            elif self.watchlisted:
                yield Badge("◆ SAVED", variant="accent")
            else:
                yield Badge(plain_text(self.anime.format or "ANIME").upper())
            yield Static(self.anime.display_title(), classes="card-title")
            if self.anime.title.romaji and self.anime.title.romaji != self.anime.display_title():
                yield Static(self.anime.title.romaji, classes="card-subtitle")
            yield Static(anime_meta(self.anime, include_score=False), classes="card-subtitle")
            if self.detail:
                yield Static(self.detail, classes="card-detail")

    def watch_selected(self, selected: bool) -> None:
        self.set_class(selected, "selected")

    def on_click(self, event: Click) -> None:
        if event.button != 1:
            return
        event.stop()
        self.post_message(self.Chosen(self.anime, self))
