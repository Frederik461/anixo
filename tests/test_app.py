from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import nullcontext
from pathlib import Path

from app import AnixoApp
from components.confirm_dialog import ConfirmDialog
from components.poster import Poster
from config import Config
from models.anime import Anime, AnimeTitle
from screens.dashboard import DashboardScreen
from screens.discover import DiscoverScreen
from screens.history import HistoryScreen
from screens.settings import SettingsScreen
from screens.watchlist import WatchlistScreen


def test_update_action_is_awaitable(tmp_path: Path) -> None:
    """Settings and Update screens await this action, so it must be a coroutine."""
    app = AnixoApp(
        config=Config(images="off", artwork_renderer="off"),
        data_dir=tmp_path / "data",
    )
    assert inspect.iscoroutinefunction(app.action_update_anixo)


def test_provider_url_is_synced_at_startup(tmp_path: Path) -> None:
    """Settings reads config directly, so the installed script must win at startup."""
    script = tmp_path / "declared-ani-cli"
    script.write_text(
        '#!/usr/bin/env bash\nmiruro_base="https://miruro.invalid"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    config = Config(
        images="off",
        artwork_renderer="off",
        ani_cli_path=str(script),
        ani_cli_base_url="https://hianime.invalid",
    )
    app = AnixoApp(config=config, data_dir=tmp_path / "data")

    assert config.ani_cli_base_url == "https://miruro.invalid"
    assert app.provider.base_url == "https://miruro.invalid"
    assert app.provider.kind == "miruro"


def test_dashboard_startup_and_rapid_navigation(tmp_path: Path) -> None:
    history = tmp_path / "empty-ani-hsts"
    history.write_text("", encoding="utf-8")
    config = Config(
        images="off",
        artwork_renderer="off",
        ani_cli_history_path=str(history),
    )
    app = AnixoApp(config=config, data_dir=tmp_path / "data")

    trending = [
        Anime(id=str(index), title=AnimeTitle(romaji=f"Anime {index}"))
        for index in range(3)
    ]

    async def fake_trending(page: int = 1, per_page: int | None = None) -> list[Anime]:
        return trending

    app.api.trending = fake_trending  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DashboardScreen)
            assert len(screen.continue_entries) == 0
            assert len(screen.active_cards) == 0

            await pilot.press("down")
            await pilot.pause()
            assert len(screen.active_cards) == 3
            posters_before = [id(card.query_one(Poster)) for card in screen.active_cards]

            for _ in range(12):
                await pilot.press("right")
                await pilot.press("left")
            await pilot.press("up")
            await pilot.press("down")
            await pilot.pause()

            assert isinstance(app.screen, DashboardScreen)
            assert len(app.screen.active_cards) == 3
            assert sum(1 for card in app.screen.active_cards if card.selected) == 1
            assert [
                id(card.query_one(Poster)) for card in app.screen.active_cards
            ] == posters_before

    asyncio.run(run())


def _test_app(tmp_path: Path) -> AnixoApp:
    history = tmp_path / "empty-ani-hsts"
    history.write_text("", encoding="utf-8")
    return AnixoApp(
        config=Config(
            images="off",
            artwork_renderer="off",
            ani_cli_history_path=str(history),
        ),
        data_dir=tmp_path / "data",
    )


def test_global_route_shortcuts_and_tab_cycle(tmp_path: Path) -> None:
    app = _test_app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for key, expected in (
                ("G", DiscoverScreen),
                ("W", WatchlistScreen),
                ("H", HistoryScreen),
                ("S", SettingsScreen),
                ("D", DashboardScreen),
            ):
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, expected), key
            await pilot.press("tab")
            await pilot.pause()
            assert isinstance(app.screen, DiscoverScreen)

    asyncio.run(run())


def test_history_screen_filters_and_confirms_remove(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    app.database.record_history(
        Anime(
            id="ani-cli:slime-2866",
            title=AnimeTitle(romaji="Slime Season 4"),
            source_id="slime-2866",
        ),
        "24",
        entry_id="ani-cli:slime-2866",
        source_id="slime-2866",
        source="ani-cli",
    )

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("H")
            await pilot.pause()
            assert isinstance(app.screen, HistoryScreen)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.press("y")
            await pilot.pause()
            assert app.database.list_history() == []

    asyncio.run(run())


def test_playback_releases_terminal_and_records_history(
    tmp_path: Path, monkeypatch
) -> None:
    app = _test_app(tmp_path)

    class FakeProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def fake_spawn(**kwargs) -> FakeProcess:
        assert kwargs["source_id"] == "source-1"
        assert kwargs["episode"] == "4"
        return FakeProcess()

    app.suspend = lambda: nullcontext()  # type: ignore[method-assign]
    app.ani_cli.spawn = fake_spawn  # type: ignore[method-assign]
    app.ani_cli.import_history = lambda database: 0  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            success = await app.play_anime(
                Anime(id="99", title=AnimeTitle(romaji="Example Anime")),
                source_id="source-1",
                episode="4",
            )
            assert success is True
            entry = app.database.get_history_entry("ani-cli:source-1")
            assert entry is not None
            assert entry.episode == "4"

    asyncio.run(run())


def test_playback_checks_dependencies_after_setup(tmp_path: Path, monkeypatch) -> None:
    """Completed setup must not crash on the dependency preflight check."""
    import services.setup as setup_module

    monkeypatch.setattr(
        setup_module, "_program_version", lambda *args, **kwargs: (True, "test")
    )
    history = tmp_path / "empty-ani-hsts"
    history.write_text("", encoding="utf-8")
    script = tmp_path / "ani-cli"
    script.write_text("", encoding="utf-8")
    config = Config(
        images="off",
        artwork_renderer="off",
        ani_cli_history_path=str(history),
        ani_cli_path=str(script),
    )
    config.setup_completed = True
    app = AnixoApp(config=config, data_dir=tmp_path / "data")

    class FakeProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def fake_spawn(**kwargs) -> FakeProcess:
        return FakeProcess()

    app.suspend = lambda: nullcontext()  # type: ignore[method-assign]
    app.ani_cli.spawn = fake_spawn  # type: ignore[method-assign]
    app.ani_cli.import_history = lambda database: 0  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            success = await app.play_anime(
                Anime(id="99", title=AnimeTitle(romaji="Example Anime")),
                source_id="source-1",
                episode="4",
            )
            assert success is True
            assert isinstance(app.screen, DashboardScreen)

    asyncio.run(run())


def test_save_config_uses_explicit_config_path(tmp_path: Path) -> None:
    history = tmp_path / "empty-ani-hsts"
    history.write_text("", encoding="utf-8")
    config_path = tmp_path / "custom-config.json"
    app = AnixoApp(
        config=Config(ani_cli_history_path=str(history)),
        data_dir=tmp_path / "data",
        config_path=config_path,
    )
    app.config.theme = "aurora"

    app.save_config()
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    assert raw["theme"] == "aurora"
    assert not (tmp_path / "data" / "config.json").exists()


def test_default_config_store_follows_data_dir(tmp_path: Path) -> None:
    history = tmp_path / "empty-ani-hsts"
    history.write_text("", encoding="utf-8")
    app = AnixoApp(
        config=Config(ani_cli_history_path=str(history)),
        data_dir=tmp_path / "custom-data",
    )

    assert app.config_store.path == tmp_path / "custom-data" / "config.json"
