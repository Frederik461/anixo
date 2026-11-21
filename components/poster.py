"""Capability-driven poster widget backed by the shared artwork cache."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from textual import on
from textual.app import ComposeResult
from textual.containers import Container
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static
from textual_image.widget import (
    AutoImage,
    HalfcellImage,
    SixelImage,
    TGPImage,
    UnicodeImage,
)

from services.artwork import ArtworkResult
from services.terminal import resolve_renderer

if TYPE_CHECKING:
    from app import AnixoApp

RendererName = Literal[
    "auto", "sixel", "tgp", "halfcell", "unicode", "off", "ansi"
]

_WIDGETS = {
    "auto": AutoImage,
    "sixel": SixelImage,
    "tgp": TGPImage,
    "halfcell": HalfcellImage,
    # Legacy "ansi" mode is resolved to the high-color half-block renderer.
    "ansi": HalfcellImage,
    "unicode": UnicodeImage,
}


class PosterLoaded(Message):
    def __init__(
        self,
        token: int,
        result: ArtworkResult | None,
        poster_id: str | None = None,
    ) -> None:
        super().__init__()
        self.token = token
        self.result = result
        self.poster_id = poster_id


class Poster(Container, can_focus=False):
    """Renders one cached poster without any manual terminal cursor control."""

    DEFAULT_CSS = """
    Poster {
        width: auto;
        height: auto;
        background: $surface;
    }
    Poster .poster-placeholder {
        width: 100%;
        height: 100%;
        content-align: center middle;
        text-style: bold;
    }
    Poster .poster-image {
        width: 100%;
        height: 100%;
    }
    """

    url: reactive[str | None] = reactive(None)
    width_cells: reactive[int] = reactive(14)
    height_cells: reactive[int] = reactive(11)
    renderer: reactive[RendererName] = reactive("auto")
    fallback_letter: reactive[str] = reactive("?")
    fallback_color: reactive[str] = reactive("#a78bfa")

    def __init__(
        self,
        *,
        url: str | None = None,
        width: int = 14,
        height: int = 11,
        renderer: RendererName = "auto",
        fallback_letter: str = "?",
        fallback_color: str = "#a78bfa",
        id: str | None = None,
        classes: str = "poster",
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._token = 0
        self._result: ArtworkResult | None = None
        self._render_rows = 0
        self._render_width = 0
        self._effective: str = "auto"
        self._fallback_note = ""
        self._initializing = True
        self.url = url
        self.width_cells = max(1, width)
        self.height_cells = max(1, height)
        self.renderer = renderer
        self.fallback_letter = fallback_letter[:1].upper() or "?"
        self.fallback_color = fallback_color
        self._initializing = False
        self._apply_size_styles()

    def _apply_size_styles(self) -> None:
        """Reserve a box, then use the exact layout derived from the source."""
        width = self._render_width or self.width_cells
        rows = self._render_rows or self.height_cells
        self.styles.width = width
        self.styles.height = rows
        self.styles.max_width = self.width_cells
        self.styles.max_height = self.height_cells

    def compose(self) -> ComposeResult:
        result = self._result
        widget_class = _WIDGETS.get(self._effective)
        if result and widget_class:
            image_widget = widget_class(result.path)
            image_widget.add_class("poster-image")
            image_widget.styles.width = self._render_width or self.width_cells
            image_widget.styles.height = self._render_rows or self.height_cells
            yield image_widget
            return
        placeholder = Static(self.fallback_letter, classes="poster-placeholder")
        placeholder.styles.color = self.fallback_color
        placeholder.styles.width = self.width_cells
        placeholder.styles.height = self.height_cells
        yield placeholder

    def on_mount(self) -> None:
        # Textual reports ``is_mounted`` as false inside ``on_mount`` in 8.2.x;
        # defer to the next message so the worker actually starts.
        self.call_next(self._request_image)

    def _invalidate(self) -> None:
        self._result = None
        self._render_rows = 0
        self._render_width = 0
        self._token += 1
        self._apply_size_styles()
        self._request_image()

    def watch_url(self) -> None:
        if not self._initializing:
            self._invalidate()

    def watch_width_cells(self) -> None:
        if not self._initializing:
            self._apply_size_styles()
            self._invalidate()

    def watch_height_cells(self) -> None:
        if not self._initializing:
            self._apply_size_styles()
            self._invalidate()

    def watch_renderer(self) -> None:
        if not self._initializing:
            self._invalidate()

    def _resolve(self) -> tuple[str, str]:
        app = cast("AnixoApp", self.app)
        return resolve_renderer(self.renderer, app.capabilities)

    def _request_image(self) -> None:
        app = cast("AnixoApp", self.app)
        effective, note = self._resolve()
        self._effective = effective
        self._fallback_note = note
        if not self.url or effective == "off" or app.config.images == "off":
            self.refresh(recompose=True)
            return
        token = self._token
        url = self.url
        width = self.width_cells
        height = self.height_cells
        cell_size = app.cell_size
        self.run_worker(
            self._load(token, url, width, height, effective, cell_size),
            exclusive=True,
            group=f"poster-{id(self)}",
            name=f"poster-image-{token}",
        )

    async def _load(
        self,
        token: int,
        url: str,
        width: int,
        height: int,
        renderer: str,
        cell_size: tuple[int, int],
    ) -> None:
        app = cast("AnixoApp", self.app)
        result = await app.artwork.processed_image(
            url,
            width,
            height,
            renderer=renderer,
            cell_size=cell_size,
        )
        self.post_message(PosterLoaded(token, result, self.id))

    @on(PosterLoaded)
    def _on_loaded(self, message: PosterLoaded) -> None:
        self._apply_result(message.token, message.result)

    def _apply_result(self, token: int, result: ArtworkResult | None) -> None:
        """Ignore stale results so rapid navigation cannot swap posters."""
        if token != self._token:
            return
        self._result = result
        self._render_rows = result.cell_rows if result else 0
        self._render_width = result.render_width if result else 0
        self._apply_size_styles()
        self.refresh(recompose=True)
