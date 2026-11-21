from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Static

from app import AnixoApp
from config import Config
from models.anime import Anime, AnimeTitle
from screens.discover import DiscoverScreen


def _anime(index: int) -> Anime:
    return Anime(
        id=str(index),
        title=AnimeTitle(romaji=f"Anime {index}"),
        genres=("Action", "Fantasy"),
        average_score=70 + index,
    )


def _app(tmp_path: Path, calls: list[dict[str, object]]) -> AnixoApp:
    history = tmp_path / "empty-ani-hsts"
    history.write_text("", encoding="utf-8")
    app = AnixoApp(
        config=Config(
            images="off",
            artwork_renderer="off",
            ani_cli_history_path=str(history),
        ),
        data_dir=tmp_path / "data",
    )

    async def fake_browse(**kwargs: object) -> list[Anime]:
        calls.append(kwargs)
        return [_anime(index) for index in range(3)]

    app.api.browse = fake_browse  # type: ignore[method-assign]
    return app


def test_discover_keyboard_filters_reload_results(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    app = _app(tmp_path, calls)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("G")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DiscoverScreen)
            assert calls[-1] == {
                "category": "Trending",
                "genre": "All",
                "sort": "Default",
                "per_page": 48,
            }
            calls.clear()

            await pilot.press("s")
            await pilot.pause()
            assert calls[-1]["sort"] == "Score"

            await pilot.press("]")
            await pilot.pause()
            assert calls[-1]["genre"] == "Action"

            await pilot.press("c")
            await pilot.pause()
            assert calls[-1]["category"] == "Popular"
            assert str(screen.query_one("#filter-category", Static).content) == "Popular"

    asyncio.run(run())


def test_discover_filter_chips_are_clickable(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    app = _app(tmp_path, calls)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("G")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DiscoverScreen)
            calls.clear()

            await pilot.click("#filter-sort")
            await pilot.pause()
            assert calls[-1]["sort"] == "Score"
            assert str(screen.query_one("#filter-sort", Static).content) == "Score"

            await pilot.click("#filter-genre")
            await pilot.pause()
            assert calls[-1]["genre"] == "Action"
            assert str(screen.query_one("#filter-genre", Static).content) == "Action"

            await pilot.click("#filter-category")
            await pilot.pause()
            assert calls[-1]["category"] == "Popular"
            assert str(screen.query_one("#filter-category", Static).content) == "Popular"

    asyncio.run(run())
