from __future__ import annotations

import asyncio
import io
import os
from dataclasses import replace
from pathlib import Path

from PIL import Image
from textual.containers import VerticalScroll
from textual_image.widget import HalfcellImage, SixelImage

from app import AnixoApp
from components.poster import Poster
from config import THEME_PALETTES, Config
from models.anime import Anime, AnimeTitle
from screens.dashboard import DashboardScreen
from screens.render_test import RenderTestScreen
from services.image_cache import CacheKey
from services.terminal import TerminalCapabilities


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (600, 900), color).save(buffer, format="PNG")
    return buffer.getvalue()


_COLORS = {
    "https://example.test/cover-0.jpg": (210, 70, 70),
    "https://example.test/cover-1.jpg": (70, 180, 110),
    "https://example.test/cover-2.jpg": (80, 110, 220),
}


def _app(tmp_path: Path, monkeypatch, *, cache_max_entries: int = 128) -> AnixoApp:
    async def fake_download(self, url: str) -> bytes | None:  # type: ignore[no-untyped-def]
        return _png(_COLORS.get(url, (160, 160, 160)))

    monkeypatch.setattr("services.artwork.ArtworkService._download", fake_download)
    history = tmp_path / "empty-ani-hsts"
    history.write_text("", encoding="utf-8")
    app = AnixoApp(
        config=Config(
            images="on",
            artwork_renderer="halfcell",
            ani_cli_history_path=str(history),
            terminal_cell_width=10,
            terminal_cell_height=20,
            extra={"cache_max_entries": cache_max_entries},
        ),
        data_dir=tmp_path / "data",
    )
    trending = [
        Anime(
            id=str(index),
            title=AnimeTitle(romaji=f"Anime {index}"),
            cover_url=url,
        )
        for index, url in enumerate(_COLORS)
    ]

    async def fake_trending(page: int = 1, per_page: int | None = None) -> list[Anime]:
        return trending

    app.api.trending = fake_trending  # type: ignore[method-assign]
    return app


def test_halfcell_posters_render_and_survive_rapid_navigation(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app(tmp_path, monkeypatch)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            for _ in range(20):
                await pilot.pause()
                if len(list(app.screen.query(".poster-image"))) == 4:
                    break
            screen = app.screen
            assert isinstance(screen, DashboardScreen)
            # One large hero poster plus three cards in the visible section.
            images = list(screen.query(".poster-image"))
            assert len(images) == 4
            assert all(isinstance(image, HalfcellImage) for image in images)

            posters_before = [id(card.query_one(Poster)) for card in screen.active_cards]
            images_before = [id(card.query_one(".poster-image")) for card in screen.active_cards]
            assert len(set(images_before)) == 3

            for _ in range(12):
                await pilot.press("right")
                await pilot.press("left")
            await pilot.press("up")
            await pilot.press("down")
            await pilot.pause()

            cards = app.screen.active_cards
            assert [id(card.query_one(Poster)) for card in cards] == posters_before
            assert [id(card.query_one(".poster-image")) for card in cards] == images_before
            # The hero uses a larger cell size, so it occupies a separate
            # processed cache entry while still reusing the same download.
            assert app.image_cache.counts()["processed"] == 4
            assert app.artwork.download_count == 3

    asyncio.run(run())


def test_mounted_posters_survive_lru_cleanup(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path, monkeypatch, cache_max_entries=8)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("down")
            for _ in range(20):
                await pilot.pause()
                if len(list(app.screen.query(".poster-image"))) == 4:
                    break
            posters = list(app.screen.query(Poster))
            active_paths = [
                poster._result.path for poster in posters if poster._result is not None
            ]
            assert len(active_paths) == 4
            # Make the displayed entries the oldest so cleanup would evict them.
            for path in active_paths:
                os.utime(path, (1, 1))

            for index in range(20):
                key = CacheKey(
                    f"https://example.test/evict-{index}.jpg",
                    14,
                    11,
                    "halfcell:10x20",
                )
                app.image_cache.store_processed(key, Image.new("RGB", (14, 22)))

            assert app.image_cache.cleanup() > 0
            assert all(path.is_file() for path in active_paths)
            await pilot.pause()
            assert len(list(app.screen.query(".poster-image"))) == 4

    asyncio.run(run())


def test_missing_cached_poster_falls_back_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app(tmp_path, monkeypatch)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("down")
            for _ in range(20):
                await pilot.pause()
                if len(list(app.screen.query(".poster-image"))) == 4:
                    break
            poster = next(iter(app.screen.query(Poster)))
            result = poster._result
            assert result is not None
            poster._unpin_result()

            requests: list[int] = []

            def fake_request() -> None:
                requests.append(1)

            monkeypatch.setattr(poster, "_request_image", fake_request)
            poster._result = replace(result, path=tmp_path / "gone.png")
            poster.refresh(recompose=True)
            await pilot.pause()

            assert requests
            assert poster.query(".poster-placeholder")
            assert poster.query_one(".poster-image").query(".poster-placeholder")

    asyncio.run(run())


def test_stale_generation_result_is_not_applied(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path, monkeypatch)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("down")
            for _ in range(20):
                await pilot.pause()
                if len(list(app.screen.query(".poster-image"))) == 4:
                    break
            poster = next(iter(app.screen.query(Poster)))
            result = poster._result
            assert result is not None
            requests: list[int] = []

            def fake_request() -> None:
                requests.append(1)

            monkeypatch.setattr(poster, "_request_image", fake_request)
            poster._apply_result(poster._token, replace(result, generation=-1))

            assert requests
            assert poster._result is result

    asyncio.run(run())


def test_poster_widget_height_matches_aspect_correct_cache(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app(tmp_path, monkeypatch)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("down")
            for _ in range(20):
                await pilot.pause()
                if len(list(app.screen.query(".poster-image"))) == 4:
                    break

            posters = list(app.screen.query(Poster))
            assert len(posters) == 4
            for poster in posters:
                result = poster._result
                assert result is not None
                assert result.cell_rows > 0
                assert poster.size == (poster.width_cells, result.cell_rows)
                assert poster.query_one(".poster-image").size == poster.size
                with Image.open(result.path) as image:
                    # Half-cell consumes two source rows per terminal row.
                    assert image.size == (poster.width_cells, result.cell_rows * 2)

    asyncio.run(run())


def test_discover_pagination_reuses_cached_posters(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path, monkeypatch)
    app.config.page_size = 1

    async def wait_for_images(pilot, expected: int) -> list:
        for _ in range(20):
            await pilot.pause()
            images = list(app.screen.query(".poster-image"))
            if len(images) == expected:
                return images
        raise AssertionError(
            f"expected {expected} poster images, got "
            f"{len(list(app.screen.query('.poster-image')))}"
        )

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("G")
            images = await wait_for_images(pilot, 1)
            assert all(isinstance(image, HalfcellImage) for image in images)

            # Prime all three cached page sizes before measuring re-use.
            for _ in range(2):
                await pilot.press("down")
                images = await wait_for_images(pilot, 1)
                assert all(isinstance(image, HalfcellImage) for image in images)

            downloads = app.artwork.download_count
            processed = app.image_cache.counts()["processed"]
            assert downloads == 3

            for _ in range(4):
                await pilot.press("up")
                images = await wait_for_images(pilot, 1)
                assert all(isinstance(image, HalfcellImage) for image in images)
                await pilot.press("down")
                images = await wait_for_images(pilot, 1)
                assert all(isinstance(image, HalfcellImage) for image in images)

            assert app.artwork.download_count == downloads
            assert app.image_cache.counts()["processed"] == processed

    asyncio.run(run())


def test_images_off_prevents_poster_downloads(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path, monkeypatch)
    app.config.images = "off"

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("down")
            for _ in range(10):
                await pilot.pause()
            assert list(app.screen.query(".poster-image")) == []
            assert app.artwork.download_count == 0
            assert app.image_cache.counts() == {"original": 0, "processed": 0}

    asyncio.run(run())


def test_ansi_renderer_uses_halfcell_pixel_cache(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_download(self, url: str) -> bytes | None:  # type: ignore[no-untyped-def]
        return _png((120, 140, 200))

    monkeypatch.setattr("services.artwork.ArtworkService._download", fake_download)
    app = _app(tmp_path, monkeypatch)
    result = asyncio.run(
        app.artwork.processed_image(
            "https://example.test/ansi.jpg",
            13,
            19,
            renderer="ansi",
            cell_size=(10, 20),
        )
    )
    assert result is not None
    with Image.open(result.path) as image:
        # 13 columns at 10x20 cells yields 10 half-cell rows, so the cached
        # raster is 13x20 and preserves the 2:3 source aspect ratio.
        assert image.size == (13, 20)


def _sixel_app(tmp_path: Path, monkeypatch) -> AnixoApp:
    app = _app(tmp_path, monkeypatch)
    app.config.artwork_renderer = "auto"
    app.capabilities = TerminalCapabilities(
        is_tty=True,
        sixel=True,
        tgp=False,
        cell_width=10,
        cell_height=20,
    )

    urls = tuple(_COLORS)

    async def fake_details(anilist_id: int) -> Anime | None:
        return Anime(
            id=str(anilist_id),
            title=AnimeTitle(romaji=f"Render test {anilist_id}"),
            cover_url=urls[anilist_id % len(urls)],
        )

    app.api.details = fake_details  # type: ignore[method-assign]
    return app


def test_auto_selects_sixel_widget_when_terminal_supports_it(
    tmp_path: Path, monkeypatch
) -> None:
    app = _sixel_app(tmp_path, monkeypatch)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("down")
            for _ in range(20):
                await pilot.pause()
                if len(list(app.screen.query(".poster-image"))) == 4:
                    break
            images = list(app.screen.query(".poster-image"))
            assert len(images) == 4
            assert all(isinstance(image, SixelImage) for image in images)
            assert not any(isinstance(image, HalfcellImage) for image in images)

    asyncio.run(run())


def test_render_test_screen_reports_geometry_and_survives_interaction(
    tmp_path: Path, monkeypatch
) -> None:
    app = _sixel_app(tmp_path, monkeypatch)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            # Let the dashboard's in-flight posters finish before snapshotting
            # cache counts; otherwise their results land during the modal and
            # inflate the "processed" count after the baseline is captured.
            for _ in range(40):
                await pilot.pause()
                posters = list(app.screen.query(Poster))
                if len(posters) == 4 and all(poster._result for poster in posters):
                    break
            await pilot.press("V")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RenderTestScreen)
            assert "SIXEL" in screen.query_one("#render-status").content

            for _ in range(60):
                await pilot.pause()
                images = list(screen.query(".poster-image"))
                captions = [
                    screen.query_one(f"#render-caption-{index}").content
                    for index in range(3)
                ]
                if len(images) == 3 and all(
                    "Original 600x900" in caption for caption in captions
                ):
                    break
            assert len(images) == 3
            assert all(isinstance(image, SixelImage) for image in images)
            for index in range(3):
                caption = screen.query_one(f"#render-caption-{index}").content
                assert "Original 600x900" in caption
                assert "Rendered 16x12 cells" in caption
                assert "160x240 px raster" in caption
                assert "SIXEL" in caption

            poster_ids = [id(item) for item in screen.query(Poster)]
            image_ids = [id(item) for item in screen.query(".poster-image")]
            downloads = app.artwork.download_count
            processed = app.image_cache.counts()["processed"]
            assert downloads == 3
            assert processed >= 3

            for _ in range(12):
                await pilot.press("right")
                await pilot.press("left")
            await pilot.pause()
            assert [id(item) for item in app.screen.query(Poster)] == poster_ids
            assert [id(item) for item in app.screen.query(".poster-image")] == image_ids
            assert app.artwork.download_count == downloads
            assert app.image_cache.counts()["processed"] == processed

            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.screen.has_class("compact")
            scroll = app.screen.query_one("#render-scroll", VerticalScroll)
            scroll.scroll_down()
            await pilot.pause()
            assert len(list(app.screen.query(".poster-image"))) == 3
            assert [id(item) for item in app.screen.query(Poster)] == poster_ids
            assert app.artwork.download_count == downloads
            assert app.image_cache.counts()["processed"] == processed

            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert len(list(app.screen.query(".poster-image"))) == 3
            assert app.artwork.download_count == downloads
            assert app.image_cache.counts()["processed"] == processed

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            await pilot.press("V")
            await pilot.pause()
            for _ in range(30):
                await pilot.pause()
                if len(list(app.screen.query(".poster-image"))) == 3:
                    break
            reopened = list(app.screen.query(Poster))
            assert len(reopened) == 3
            assert all(poster._result and poster._result.from_cache for poster in reopened)
            assert app.artwork.download_count == downloads
            assert app.image_cache.counts()["processed"] == processed

    asyncio.run(run())


def test_theme_palette_is_applied_to_css_variables(tmp_path: Path) -> None:
    history = tmp_path / "empty-ani-hsts"
    history.write_text("", encoding="utf-8")
    app = AnixoApp(
        config=Config(ani_cli_history_path=str(history)),
        data_dir=tmp_path / "data",
    )
    app.config.theme = "aurora"
    app.config.accent = "#5EEAD4"
    variables = app.get_theme_variable_defaults()

    assert variables["surface"] == THEME_PALETTES["aurora"]["surface"]
    assert variables["panel"] == THEME_PALETTES["aurora"]["panel"]
    assert variables["accent"] == "#5EEAD4"
    assert variables["accent-dim"].startswith("#")
