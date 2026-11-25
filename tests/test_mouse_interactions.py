from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.containers import VerticalScroll
from textual.events import MouseScrollDown
from textual.pilot import _get_mouse_message_arguments
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from app import AnixoApp, HelpScreen
from components.anime_card import AnimeCard
from components.confirm_dialog import ConfirmDialog
from components.poster import Poster
from components.shell import NavRail
from config import Config
from models.anime import Anime, AnimeTitle
from screens.anime_details import AnimeDetailsScreen
from screens.dashboard import DashboardScreen
from screens.discover import DiscoverScreen
from screens.episode_selector import EpisodeCell, EpisodeSelectorScreen
from screens.history import HistoryRow, HistoryScreen
from screens.season_selector import FranchiseCell, SeasonSelectorScreen
from screens.uninstall import UninstallScreen
from screens.watchlist import WatchlistScreen
from services.franchise import Franchise
from services.provider import ProviderAnime
from services.uninstall import UninstallPlan


def _anime(index: int) -> Anime:
    return Anime(
        id=str(index),
        title=AnimeTitle(romaji=f"Anime {index}"),
        episodes=12,
        format="TV",
        genres=("Action",),
    )


def _app(tmp_path: Path, count: int = 4) -> AnixoApp:
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
    for anime in items:
        app.database.upsert_anime(anime)

    async def fake_trending(page: int = 1, per_page: int | None = None) -> list[Anime]:
        return items

    async def fake_browse(**kwargs: object) -> list[Anime]:
        return items

    async def fake_search(
        query: str, page: int = 1, per_page: int | None = None
    ) -> list[Anime]:
        return items

    app.api.trending = fake_trending  # type: ignore[method-assign]
    app.api.browse = fake_browse  # type: ignore[method-assign]
    app.api.search = fake_search  # type: ignore[method-assign]
    return app


def _nav_buttons(app: AnixoApp) -> tuple[Button, Button]:
    return (
        app.screen.query_one("#nav-back", Button),
        app.screen.query_one("#nav-forward", Button),
    )


async def _wait_for_details(app: AnixoApp, pilot) -> None:
    """Yield to the event loop until the details screen finishes loading."""
    for _ in range(30):
        if getattr(app.screen, "anime", None) is not None:
            return
        await pilot.pause()


async def _wheel_down(app: AnixoApp, widget: Widget, times: int = 4) -> None:
    """Forward real wheel events using Textual's own pointer coordinates."""
    arguments = _get_mouse_message_arguments(widget, (2, 2))
    arguments["delta_y"] = 1
    for _ in range(times):
        app.screen._forward_event(MouseScrollDown(**arguments))
        await asyncio.sleep(0)


def test_mouse_back_forward_history_and_nav_rail(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            back, forward = _nav_buttons(app)
            assert back.disabled and forward.disabled

            await pilot.click("#nav-discover")
            await pilot.pause()
            assert isinstance(app.screen, DiscoverScreen)
            assert len(app.screen.query(NavRail)) == 1
            back, forward = _nav_buttons(app)
            assert not back.disabled and forward.disabled

            await pilot.click("#nav-watchlist")
            await pilot.pause()
            assert isinstance(app.screen, WatchlistScreen)
            await pilot.click("#nav-back")
            await pilot.pause()
            assert isinstance(app.screen, DiscoverScreen)
            await pilot.click("#nav-forward")
            await pilot.pause()
            assert isinstance(app.screen, WatchlistScreen)

            await pilot.click("#nav-home")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)

    asyncio.run(run())


def test_nav_click_works_while_search_input_is_focused(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#nav-discover")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DiscoverScreen)

            field = screen.query_one("#search-input", Input)
            await pilot.click(field)
            assert field.has_focus

            await pilot.click("#nav-home")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not isinstance(app.focused, Input)

            await pilot.click("#nav-discover")
            await pilot.pause()
            field = app.screen.query_one("#search-input", Input)
            await pilot.click(field)
            assert field.has_focus

    asyncio.run(run())


def test_cards_posters_and_detail_watch_button_are_clickable(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            cards = app.screen.query("#trending-row AnimeCard")
            assert cards

            # A click anywhere on the card, including its nested cover, opens
            # that exact anime's details.
            for _ in range(5):
                await pilot.click(cards[1])
                await pilot.pause()
                if isinstance(app.screen, AnimeDetailsScreen):
                    break
            await _wait_for_details(app, pilot)
            assert isinstance(app.screen, AnimeDetailsScreen)
            assert app.screen.anime is not None
            assert app.screen.anime.id == "1"

            watched: list[str] = []

            async def fake_watch(anime: Anime) -> None:
                watched.append(anime.id)

            app.watch_anime = fake_watch  # type: ignore[method-assign]
            await pilot.click("#detail-watch")
            await pilot.pause()
            assert watched == ["1"]

    asyncio.run(run())


def test_keyboard_selection_then_poster_click_opens_that_anime(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("G")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DiscoverScreen)

            await pilot.press("right")
            await pilot.pause()
            assert screen.selection.index == 1

            tiles = screen.cards
            assert len(tiles) > 2
            await pilot.click(tiles[2])
            await _wait_for_details(app, pilot)
            assert isinstance(app.screen, AnimeDetailsScreen)
            assert app.screen.anime is not None
            assert app.screen.anime.id == "2"
            assert screen.selection.index == 2

    asyncio.run(run())


def test_discover_cover_click_opens_details(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("G")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DiscoverScreen)

            cover = screen.cards[1].query_one(Poster)
            # Headless hit-testing can miss a nested placeholder while it is
            # recomposing; retry a bounded number of real clicks so the test
            # checks bubbling from the cover rather than pointer timing.
            for _ in range(5):
                await pilot.click(cover)
                await pilot.pause()
                if isinstance(app.screen, AnimeDetailsScreen):
                    break
            await _wait_for_details(app, pilot)
            assert isinstance(app.screen, AnimeDetailsScreen)
            assert app.screen.anime is not None
            assert app.screen.anime.id == "1"

    asyncio.run(run())


def test_season_and_episode_cells_are_clickable(tmp_path: Path) -> None:
    app = _app(tmp_path, count=1)
    anime = app.database.get_anime("0")
    assert anime is not None
    chosen: list[str] = []

    async def fake_select(
        selected: Anime, franchise: Franchise, entry: ProviderAnime
    ) -> None:
        chosen.append(entry.source_id)

    app.select_provider_entry = fake_select  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            entries = (
                ProviderAnime("season-1", "Series S1", "1"),
                ProviderAnime("season-2", "Series S2", "2"),
            )
            app.push_screen(
                SeasonSelectorScreen(
                    anime, (Franchise("series", "Series", entries),)
                )
            )
            await pilot.pause()
            cells = app.screen.query(FranchiseCell)
            assert len(cells) == 2
            await pilot.click(cells[1])
            await pilot.pause()
            assert chosen == ["season-2"]

            await pilot.click("#nav-home")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)

            source_entries = [
                {"number": str(number), "title": f"Episode {number}"}
                for number in range(1, 13)
            ]
            app.push_screen(
                EpisodeSelectorScreen(anime, entries=source_entries)
            )
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, EpisodeSelectorScreen)
            cells = screen.query(EpisodeCell)
            assert len(cells) == 12

            await pilot.click(cells[2])
            await pilot.pause()
            assert screen.selection.index == 2
            info = screen.query_one("#episode-info-number", Static)
            assert "EPISODE 3" in str(info.content)

            await pilot.click(cells[4], button=2)
            await pilot.pause()
            assert screen.selection.index == 2

    asyncio.run(run())


def test_wheel_scroll_does_not_change_episode_selection(tmp_path: Path) -> None:
    app = _app(tmp_path, count=1)
    anime = app.database.get_anime("0")
    assert anime is not None
    source_entries = [
        {"number": str(number), "title": f"Episode {number}"}
        for number in range(1, 25)
    ]

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(EpisodeSelectorScreen(anime, entries=source_entries))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, EpisodeSelectorScreen)
            scroll = screen.query_one("#episode-list-scroll", VerticalScroll)
            assert scroll.scroll_offset.y == 0
            for _ in range(30):
                if scroll.virtual_size.height > scroll.size.height:
                    break
                await pilot.pause()
            assert scroll.virtual_size.height > scroll.size.height

            await _wheel_down(app, scroll)
            # Scroll animation is applied over several frames, not
            # synchronously with the synthetic mouse event.
            for _ in range(30):
                if scroll.scroll_offset.y > 0:
                    break
                await pilot.pause()
            assert scroll.scroll_offset.y > 0
            assert screen.selection.index == 0

    asyncio.run(run())


def test_history_row_confirmation_help_and_uninstall_options_are_clickable(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, count=1)
    anime = app.database.get_anime("0")
    assert anime is not None
    app.database.record_history(anime, "3", entry_id="0")
    played: list[str] = []

    async def fake_play(entry: Any) -> None:
        played.append(entry.id)

    app.play_history_entry = fake_play  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("H")
            await pilot.pause()
            assert isinstance(app.screen, HistoryScreen)
            row = app.screen.query_one(HistoryRow)
            await pilot.click(row)
            await pilot.pause()
            assert played == ["0"]

            answers: list[bool] = []
            app.push_screen(ConfirmDialog("Confirm"), callback=answers.append)
            await pilot.pause()
            await pilot.click("#confirm-yes")
            await pilot.pause()
            assert answers == [True]

            app.action_help()
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.click("#help-close")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)

            plan = UninstallPlan(
                mode="app_only",
                platform="windows",
                manifest_path="",
                app_version="",
                items=(),
                warnings=(),
                notes=(),
            )
            app.push_screen(UninstallScreen(plan))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UninstallScreen)
            assert screen.mode == "app_only"
            await pilot.click("#uninstall-option-complete")
            await pilot.pause()
            assert screen.mode == "complete"
            assert screen.query_one("#uninstall-option-complete").has_class("active")

    asyncio.run(run())
