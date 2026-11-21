"""Dedicated visual verification screen for the poster image pipeline."""

from __future__ import annotations

import asyncio

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.widgets import Static

from components.poster import Poster, PosterLoaded
from components.shell import ShellScreen
from models.anime import Anime
from services.terminal import resolve_renderer

# Known AniList IDs; all descriptive text comes from the live API response.
_TEST_IDS = (154587, 2866, 120377)


class RenderTestScreen(ShellScreen):
    """Shows three real posters with source, cache, and renderer geometry."""

    CSS = """
    #render-status {
        height: auto;
        padding: 1 2;
        color: $muted;
    }
    #render-scroll {
        height: 1fr;
    }
    #render-grid {
        grid-size: 3 1;
        grid-gutter: 1 2;
        padding: 0 2 1 2;
    }
    .render-slot {
        height: auto;
        background: $panel;
        border: solid $boost;
        padding: 1;
    }
    .render-slot.selected {
        border: solid $accent;
    }
    .render-title {
        width: 100%;
        height: 2;
        color: $text;
        text-style: bold;
    }
    .render-slot Poster {
        width: 100%;
        content-align: center top;
    }
    .render-caption {
        width: 100%;
        height: auto;
        color: $muted;
    }
    .compact #render-grid { grid-size: 1 3; }
    """

    BINDINGS = [
        Binding("left", "move(-1)", "Previous", show=False),
        Binding("right", "move(1)", "Next", show=False),
        Binding("r", "reload", "Reload"),
        Binding("escape", "app.goto_dashboard", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.selected = 0
        self._load_generation = 0

    def compose(self) -> ComposeResult:
        def content() -> ComposeResult:
            yield Static("", id="render-status")
            with VerticalScroll(id="render-scroll"):
                with Grid(id="render-grid"):
                    for index in range(3):
                        with Vertical(classes="render-slot", id=f"render-slot-{index}"):
                            yield Static(
                                "Loading…",
                                id=f"render-title-{index}",
                                classes="render-title",
                            )
                            yield Poster(
                                id=f"render-poster-{index}",
                                width=16,
                                height=12,
                                renderer=self.app.config.artwork_renderer,
                            )
                            yield Static(
                                "Waiting for artwork…",
                                id=f"render-caption-{index}",
                                classes="render-caption",
                            )
            yield Static(
                "←/→ select  •  R reload  •  Esc back",
                classes="meta-text",
            )

        yield from self.compose_shell(
            active="nav-settings",
            title="Image Render Test",
            hint="Sixel • raster cache • geometry",
            content=content(),
        )

    async def on_mount(self) -> None:
        self._update_status()
        self._update_selection()
        self._apply_tier()
        self._load_posters()

    def on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        super().on_resize(event)
        self._apply_tier()

    def _apply_tier(self) -> None:
        if not self.is_mounted:
            return
        grid = self.query_one("#render-grid", Grid)
        grid.styles.grid_size_columns = 1 if self.has_class("compact") else 3

    def _update_status(self) -> None:
        capabilities = self.app.capabilities
        renderer, note = resolve_renderer(
            self.app.config.artwork_renderer, capabilities
        )
        width, height = self.app.cell_size
        if self.app.config.images == "off":
            detail = "Images disabled for this session"
        else:
            detail = note or f"{renderer.upper()} selected"
        self.query_one("#render-status", Static).update(
            f"PROTOCOL {renderer.upper()}  •  {capabilities.summary()}  •  "
            f"CELL {width}x{height}px  •  {detail}"
        )

    @work
    async def _load_posters(self) -> None:
        generation = self._load_generation + 1
        self._load_generation = generation
        results = await asyncio.gather(
            *(self.app.api.details(anilist_id) for anilist_id in _TEST_IDS),
            return_exceptions=True,
        )
        if generation != self._load_generation:
            return
        for index, anime in enumerate(results):
            title = self.query_one(f"#render-title-{index}", Static)
            caption = self.query_one(f"#render-caption-{index}", Static)
            if not isinstance(anime, Anime) or not anime.cover_url:
                title.update(f"Poster {index + 1}")
                caption.update("Artwork unavailable")
                continue
            title.update(anime.display_title())
            caption.update(f"Loading {anime.cover_url.rsplit('/', 1)[-1]}…")
            poster = self.query_one(f"#render-poster-{index}", Poster)
            poster.url = anime.cover_url

    @on(PosterLoaded)
    def on_poster_loaded(self, event: PosterLoaded) -> None:
        if not event.poster_id or not event.poster_id.startswith("render-poster-"):
            return
        index = int(event.poster_id.rsplit("-", 1)[-1])
        caption = self.query_one(f"#render-caption-{index}", Static)
        result = event.result
        if result is None:
            caption.update("No cached raster available")
            return
        renderer, note = resolve_renderer(
            self.app.config.artwork_renderer, self.app.capabilities
        )
        source = f"{result.source_size[0]}x{result.source_size[1]}"
        raster = f"{result.pixel_size[0]}x{result.pixel_size[1]}"
        cells = f"{result.render_width}x{result.cell_rows}"
        origin = "cached" if result.from_cache else "downscaled"
        caption.update(
            f"Original {source} · {result.source_path.name}\n"
            f"Rendered {cells} cells · {raster} px raster\n"
            f"{renderer.upper()} · {origin}"
            + (f"\n{note}" if note else "")
        )

    def action_move(self, delta: int) -> None:
        self.selected = (self.selected + delta) % 3
        self._update_selection()

    def _update_selection(self) -> None:
        for index, slot in enumerate(self.query(".render-slot")):
            slot.set_class(index == self.selected, "selected")

    def action_reload(self) -> None:
        self._update_status()
        self._load_posters()
