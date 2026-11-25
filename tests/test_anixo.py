from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from textual.widgets import Button

from app import AnixoApp
from config import Config
from data.database import Database
from screens.setup import SetupScreen
from services import setup as setup_service
from services.diagnostics import _redact, build_report, export_report
from services.terminal import detect_capabilities
from services.updater import (
    ReleaseAsset,
    ReleaseInfo,
    UpdateService,
    _version_key,
    parse_version,
)
from version import APP_DESCRIPTION, APP_NAME, APP_TAGLINE, APP_VERSION, VERSION, __version__

from services.dependencies import diagnostics as dependency_diagnostics


def test_product_identity_is_consistent() -> None:
    assert APP_NAME == "ANIXO"
    assert VERSION == __version__ == APP_VERSION
    assert APP_TAGLINE == "Your anime. Your world."
    assert APP_DESCRIPTION == "A modern anime discovery and streaming application."
    assert "ANIXO" in " ".join(dependency_diagnostics(Config()))


def test_legacy_environment_aliases_still_supported(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ANIME_HUB_DATA_DIR", str(tmp_path / "legacy-data"))
    from config import default_data_dir

    assert default_data_dir() == tmp_path / "legacy-data"

    monkeypatch.setenv("ANIXO_DATA_DIR", str(tmp_path / "anixo-data"))
    assert default_data_dir() == tmp_path / "anixo-data"


def test_parse_version_orders_beta_and_stable() -> None:
    assert parse_version("9.8.7-beta.4") == (9, 8, 7, 4, "beta")
    assert parse_version("v1.2.3") == (1, 2, 3, 0, "")

    service = UpdateService(Config())
    releases = [
        ReleaseInfo("9.8.7-beta.4", "v9.8.7-beta.4", "", "", True, ()),
        ReleaseInfo("9.8.7", "v9.8.7", "", "", False, ()),
    ]
    # Stable releases outrank prereleases with the same numeric base.
    assert max(
        releases,
        key=lambda release: _version_key(parse_version(release.version)),
    ).version == "9.8.7"


def test_update_installer_requires_checksum_file() -> None:
    service = UpdateService(Config())
    release = ReleaseInfo(
        "9.9.9",
        "v9.9.9",
        "notes",
        "2026-01-01",
        False,
        (ReleaseAsset("ANIXO-Setup-v9.9.9.exe", "https://example.test/setup.exe", 1),),
    )

    async def run() -> None:
        try:
            await service.download_installer(release, Path("downloads"))
        except ValueError as error:
            assert "SHA256SUMS.txt" in str(error)
        else:  # pragma: no cover - defensive
            raise AssertionError("missing checksum accepted")

    asyncio.run(run())


def test_diagnostics_redacts_secrets_and_exports(tmp_path: Path) -> None:
    report = build_report(
        app_version=VERSION,
        install_dir=tmp_path / "install",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        config=Config(),
        ani_cli=None,
        capabilities=detect_capabilities(),
        cache_counts={"originals": 2, "processed": 4},
    )
    assert "ANIXO DIAGNOSTIC REPORT" in report
    assert "ANIXO version:" in report

    secret_text = "api_key=supersecret Bearer abc123 https://user:pass@example.test"
    redacted = _redact(secret_text)
    assert "supersecret" not in redacted
    assert "abc123" not in redacted
    assert "user:pass" not in redacted
    assert "<redacted>" in redacted

    path = export_report(redacted, tmp_path / "logs")
    assert path.exists()
    assert path.name.startswith("anixo-diagnostic-")
    assert "supersecret" not in path.read_text(encoding="utf-8")


def test_setup_wizard_opens_when_forced(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(setup_service, "check_network", lambda *args, **kwargs: True)
    app = AnixoApp(
        config=Config(images="off", artwork_renderer="off"),
        data_dir=tmp_path / "data",
        force_setup=True,
    )

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, SetupScreen)
            assert app.screen.current_step == 0
            await pilot.press("right")
            await pilot.pause()
            assert app.screen.current_step == 1
            await pilot.press("left")
            await pilot.pause()
            assert app.screen.current_step == 0

    asyncio.run(run())


def test_setup_finish_button_closes_wizard_and_marks_complete(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(setup_service, "check_network", lambda *args, **kwargs: True)
    app = AnixoApp(
        config=Config(images="off", artwork_renderer="off"),
        data_dir=tmp_path / "data",
        force_setup=True,
    )

    async def confirm(*args, **kwargs) -> bool:
        return True

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SetupScreen)
            app.confirm = confirm  # type: ignore[method-assign]
            screen.current_step = 5
            screen.render_step()
            await pilot.pause()
            button = screen.query_one("#setup-finish", Button)
            button.post_message(Button.Pressed(button))
            for _ in range(10):
                await pilot.pause()
            assert app.config.setup_completed is True
            assert not isinstance(app.screen, SetupScreen)

    asyncio.run(run())


def test_setup_install_button_awaits_app_action(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(setup_service, "check_network", lambda *args, **kwargs: True)
    app = AnixoApp(
        config=Config(images="off", artwork_renderer="off"),
        data_dir=tmp_path / "data",
        force_setup=True,
    )
    called: list[str] = []

    async def fake_install(key: str) -> None:
        called.append(key)

    app.action_setup_install = fake_install  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SetupScreen)
            status = replace(
                screen.snapshot.dependencies[0],
                installed=False,
                installable=True,
            )
            screen.snapshot = replace(
                screen.snapshot, dependencies=(status,)
            )
            key = status.key
            screen.selected_dependency = key
            screen.current_step = 2
            screen.render_step()
            await pilot.pause()
            button = screen.query_one("#setup-install", Button)
            button.post_message(Button.Pressed(button))
            for _ in range(5):
                await pilot.pause()
            assert called == [key]

    asyncio.run(run())


def test_legacy_migration_keeps_old_data_and_makes_backup(
    tmp_path: Path, monkeypatch
) -> None:
    legacy = tmp_path / "AnimeHub"
    legacy.mkdir()
    history = [
        {
            "id": "legacy-1",
            "anime": {"id": "1", "title": {"romaji": "Legacy Anime"}},
            "episode": "3",
            "lastWatched": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "source": "ani-cli",
            "sourceId": "legacy-source",
        }
    ]
    (legacy / "history.json").write_text(
        json.dumps(history), encoding="utf-8"
    )
    (legacy / "watchlist.json").write_text("[]", encoding="utf-8")
    legacy_db = legacy / "animehub.sqlite3"
    Database(legacy_db).close()

    app = AnixoApp(
        config=Config(images="off", artwork_renderer="off"),
        data_dir=tmp_path / "ANIXO",
        legacy_data_dir=legacy,
    )

    async def confirm(*args, **kwargs) -> bool:
        return True

    app.confirm = confirm  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert await app.migrate_legacy_data() is True
            assert app.config.migration_completed is True

    asyncio.run(run())

    assert legacy.exists()
    assert (legacy / "history.json").exists()
    assert legacy_db.exists()
    backups = list((tmp_path / "ANIXO" / "backups").glob("animehub-*"))
    assert len(backups) == 1
    assert (backups[0] / "history.json").exists()
    assert (backups[0] / "animehub.sqlite3").exists()
