from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Input

from app import AnixoApp
from components.confirm_dialog import ConfirmDialog
from components.shell import FocusPane
from config import Config
from screens.settings import SettingsScreen


def _app(tmp_path: Path) -> AnixoApp:
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


async def _settle(pilot, count: int = 10) -> None:
    for _ in range(count):
        await pilot.pause()


def test_settings_rows_render_and_enter_activates_them(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("S")
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, SettingsScreen)
            assert len(list(screen.query(".settings-row"))) == 3
            assert not screen.query_one("#setting-input", Input).display
            assert isinstance(app.focused, FocusPane)

            assert app.config.theme == "midnight"
            await pilot.press("enter")
            await _settle(pilot, 5)
            assert isinstance(app.screen, SettingsScreen)
            assert app.config.theme == "aurora"

            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await _settle(pilot, 5)
            assert app.config.accent == "#5EEAD4"

            await pilot.press("down")
            await pilot.pause()
            assert app.config.animations is True
            await pilot.press("enter")
            await _settle(pilot, 5)
            assert app.config.animations is False

    asyncio.run(run())


def test_settings_text_row_enter_saves_value(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("S")
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, SettingsScreen)

            await pilot.press("right")
            await pilot.press("right")
            await pilot.pause()
            assert screen.section_name == "Playback"

            await pilot.press("down")
            await pilot.pause()
            assert screen.current_row is not None
            assert screen.current_row.key == "ani_cli_path"

            await pilot.press("enter")
            await pilot.pause()
            field = screen.query_one("#setting-input", Input)
            assert screen._editing_key == "ani_cli_path"
            assert field.display is True
            assert app.focused is field

            field.value = "C:\\tools\\ani-cli"
            await pilot.pause()
            await pilot.press("enter")
            await _settle(pilot, 5)

            assert app.config.ani_cli_path == "C:\\tools\\ani-cli"
            assert screen._editing_key is None
            assert field.display is False
            assert isinstance(app.screen, SettingsScreen)

    asyncio.run(run())


def test_settings_categories_and_rows_are_clickable(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("S")
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, SettingsScreen)

            categories = list(screen.query(".settings-category"))
            await pilot.click(categories[3])
            await _settle(pilot, 5)
            assert screen.section_name == "Data & cache"
            assert screen.row_index == 0

            rows = list(screen.query(".settings-row"))
            assert len(rows) > 2
            await pilot.click(rows[2])
            await _settle(pilot, 5)
            assert screen.row_index == 2
            assert screen.current_row is not None
            assert screen.current_row.key == "ani_cli_base_url"

    asyncio.run(run())


def test_system_category_opens_installation_subsection(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("S")
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, SettingsScreen)

            for _ in range(5):
                await pilot.press("right")
                await pilot.pause()
            assert screen.section_name == "System"
            await pilot.press("down")
            await pilot.pause()
            assert screen.current_row is not None
            assert screen.current_row.key == "open_installation"

            await pilot.press("enter")
            await _settle(pilot, 5)
            assert screen.section_name == "Installation & uninstallation"
            assert any(row.key == "uninstall" for row in screen.rows)

            await pilot.press("escape")
            await _settle(pilot, 5)
            assert screen.section_name == "System"
            assert isinstance(app.screen, SettingsScreen)

    asyncio.run(run())


def test_settings_confirm_action_works_from_normal_action(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("S")
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, SettingsScreen)

            await pilot.press("right")
            await pilot.press("right")
            await pilot.pause()
            for _ in range(6):
                await pilot.press("down")
                await pilot.pause()
            assert screen.current_row is not None
            assert screen.current_row.key == "update_ani_cli"

            await pilot.press("enter")
            await _settle(pilot, 5)
            assert isinstance(app.screen, ConfirmDialog)

            await pilot.press("escape")
            await _settle(pilot, 5)
            assert isinstance(app.screen, SettingsScreen)

    asyncio.run(run())


def test_settings_escape_cancels_text_edit(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("S")
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, SettingsScreen)

            await pilot.press("right")
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            original = app.config.ani_cli_path

            await pilot.press("enter")
            await pilot.pause()
            screen.query_one("#setting-input", Input).value = "discard-me"
            await pilot.pause()
            await pilot.press("escape")
            await _settle(pilot, 5)

            assert app.config.ani_cli_path == original
            assert screen._editing_key is None
            assert isinstance(app.screen, SettingsScreen)

    asyncio.run(run())
