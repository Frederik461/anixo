from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.containers import Grid

from app import AnixoApp
from components.poster import Poster
from components.shell import NavItem, NavRail
from components.ui import FeaturedPanel, PosterTile
from config import Config
from models.anime import Anime, AnimeTitle
from screens.dashboard import DashboardScreen
from screens.discover import DiscoverScreen
from screens.history import HistoryScreen
from screens.settings import SettingsScreen
from screens.watchlist import WatchlistScreen


def _anime(index: int) -> Anime:
    return Anime(
        id=str(index),
        title=AnimeTitle(romaji=f"Anime {index}"),
        episodes=12,
        genres=("Action",),
    )


def _app(tmp_path: Path, *, count: int = 4) -> AnixoApp:
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
    items = [_anime(index) for index in range(count)]

    async def fake_trending(page: int = 1, per_page: int | None = None) -> list[Anime]:
        return items

    async def fake_search(
        query: str, page: int = 1, per_page: int | None = None
    ) -> list[Anime]:
        return items

    app.api.trending = fake_trending  # type: ignore[method-assign]
    app.api.search = fake_search  # type: ignore[method-assign]
    return app


def test_shell_is_consistent_across_routes_and_responsive_tiers(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert app.screen.has_class("compact")
            assert len(app.screen.query(NavRail)) == 1
            assert len(app.screen.query(NavItem)) == 5
            assert app.screen.query_one("#nav-home").has_class("active")

            for key, expected, nav_id in (
                ("G", DiscoverScreen, "#nav-discover"),
                ("W", WatchlistScreen, "#nav-watchlist"),
                ("H", HistoryScreen, "#nav-history"),
                ("S", SettingsScreen, "#nav-settings"),
                ("D", DashboardScreen, "#nav-home"),
            ):
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, expected), key
                assert len(app.screen.query(NavRail)) == 1
                active = [
                    item.id for item in app.screen.query(NavItem) if item.has_class("active")
                ]
                assert active == [nav_id[1:]], key

            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert app.screen.has_class("medium")
            await pilot.resize_terminal(160, 52)
            await pilot.pause()
            assert app.screen.has_class("wide")

    asyncio.run(run())


def test_featured_panel_prefers_available_history_data(tmp_path: Path) -> None:
    app = _app(tmp_path)
    watched = _anime(99)
    app.database.record_history(watched, "7", entry_id="ani-cli:featured", source_id="featured")
    app.database.upsert_anime(watched)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            panel = app.screen.query_one(FeaturedPanel)
            assert panel.anime.id == watched.id
            assert panel.kicker == "CONTINUE WATCHING"
            assert "Episode 7" in "\n".join(
                str(child.content) for child in panel.query(".featured-meta")
            )

    asyncio.run(run())


@pytest.mark.parametrize("size", [(160, 40), (120, 40), (80, 24)])
def test_dashboard_featured_text_stays_clear_of_continue_watching(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app = _app(tmp_path)
    watched = Anime(
        id="98",
        title=AnimeTitle(
            romaji=" ".join(["ExtraordinaryLongWrappingTitleWord"] * 12)
        ),
        episodes=24,
        description=(
            "In the 2nd STAGE, a desert crossing spanning roughly 750 miles, "
            "the land that Johnny Joestar and Gyro Zeppeli must challenge, is "
            "home to the area that is feared as the Devil's Palm. As ominous "
            "incidents begin to unfold, they are forced to realize that this "
            "race is far more than a mere competition." * 3
        ),
    )
    app.database.upsert_anime(watched)
    app.database.record_history(watched, "9", entry_id=watched.id)

    async def run() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DashboardScreen)

            panel = screen.query_one(FeaturedPanel).region
            copy = screen.query_one(".featured-copy").region
            button = screen.query_one("#featured-watch").region
            heading = screen.query(".section-heading")[0].region

            assert "Devil's Palm" in str(
                screen.query_one(".featured-description").content
            )
            assert copy.y + copy.height <= panel.y + panel.height
            assert button.y + button.height <= panel.y + panel.height
            assert button.y + button.height <= heading.y

    asyncio.run(run())


def test_watchlist_grid_selection_does_not_recompose_posters(tmp_path: Path) -> None:
    app = _app(tmp_path)
    for anime in (_anime(0), _anime(1), _anime(2)):
        app.database.add_watchlist(anime)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("W")
            await pilot.pause()
            grid = app.screen.query_one("#watchlist-grid", Grid)
            tiles = list(grid.query(PosterTile))
            assert len(tiles) == 3
            posters_before = [id(tile.query_one(Poster)) for tile in tiles]

            await pilot.press("right")
            await pilot.pause()
            tiles = list(app.screen.query_one("#watchlist-grid", Grid).query(PosterTile))
            assert [id(tile.query_one(Poster)) for tile in tiles] == posters_before
            assert sum(1 for tile in tiles if tile.selected) == 1

            await pilot.press("/")
            await pilot.pause()
            field = app.screen.query_one("#watchlist-filter")
            field.value = "Anime 2"
            filtered: list[PosterTile] = []
            for _ in range(20):
                await pilot.pause()
                try:
                    filtered = list(
                        app.screen.query_one("#watchlist-grid", Grid).query(PosterTile)
                    )
                except Exception:
                    continue
                if len(filtered) == 1:
                    break
            assert len(filtered) == 1

    asyncio.run(run())


def test_settings_groups_are_reorganized(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("S")
            await pilot.pause()
            labels = [
                str(item.content) for item in app.screen.query(".settings-category")
            ]
            assert labels == [
                "Appearance",
                "Image rendering",
                "Playback",
                "Data & cache",
                "Dependencies",
                "Updates",
                "Application",
                "System",
            ]

    asyncio.run(run())
