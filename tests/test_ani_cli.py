from __future__ import annotations

import platform
from pathlib import Path

import pytest

from config import Config
from data.database import Database
from models.anime import Anime, AnimeTitle
from platforms import MACOS, WINDOWS, PlatformAdapter
import services.ani_cli as ani_cli_module
from services.ani_cli import AniCliInvocation, AniCliService, parse_provider_page_title


def test_parse_ani_cli_history(tmp_path: Path) -> None:
    history = tmp_path / "ani-hsts"
    history.write_text(
        "5\twatch-example-anime-123\tExample Anime\n"
        "90\twatch-other-anime-456\tOther Anime\n",
        encoding="utf-8",
    )
    config = Config(ani_cli_history_path=str(history))
    records = AniCliService(config).parse_history()

    assert [record.episode for record in records] == ["5", "90"]
    assert records[0].title == "Example Anime"
    assert records[1].source_id == "watch-other-anime-456"


def test_provider_import_does_not_duplicate_migrated_history(tmp_path: Path) -> None:
    history = tmp_path / "ani-hsts"
    history.write_text("90\tslime-5238\tSlime Season 4\n", encoding="utf-8")
    config = Config(ani_cli_history_path=str(history))
    database = Database(tmp_path / "animehub.sqlite3")
    database.record_history(
        Anime(
            id="ani-cli:slime-5238",
            title=AnimeTitle(romaji="Slime Season 4"),
            source_id="slime-5238",
        ),
        "90",
        entry_id="ani-cli:slime-5238",
        source_id="slime-5238",
        source="ani-cli",
    )

    assert AniCliService(config).import_history(database) == 0
    entries = database.list_history()
    assert len(entries) == 1
    assert entries[0].id == "ani-cli:slime-5238"
    database.close()


def test_provider_import_updates_episode_without_replacing_artwork(tmp_path: Path) -> None:
    history = tmp_path / "ani-hsts"
    history.write_text("91\tslime-5238\tSlime Season 4\n", encoding="utf-8")
    config = Config(ani_cli_history_path=str(history))
    database = Database(tmp_path / "animehub.sqlite3")
    database.record_history(
        Anime(
            id="ani-cli:slime-5238",
            title=AnimeTitle(romaji="Correct Title"),
            cover_url="https://example.test/correct.jpg",
            source_id="slime-5238",
        ),
        "90",
        entry_id="ani-cli:slime-5238",
        source_id="slime-5238",
        source="ani-cli",
    )

    AniCliService(config).import_history(database)
    entry = database.get_history_entry("ani-cli:slime-5238")
    assert entry is not None
    assert entry.episode == "91"
    assert entry.anime.display_title() == "Correct Title"
    assert entry.anime.cover_url == "https://example.test/correct.jpg"
    database.close()


def test_provider_title_parser_strips_wrapper_and_tagline() -> None:
    assert (
        parse_provider_page_title(
            "Watch Kimi ni Todoke - From Me To You Season 2 – A Crush | HiAnime"
        )
        == "Kimi ni Todoke - From Me To You Season 2"
    )


def test_cmd_wrapper_uses_cmd_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows command construction is host-independent.

    Platform detection is mocked rather than inferred from the runner OS, and
    the wrapper is a real file so its resolved path is valid on every host.
    Nothing here invokes ``cmd.exe``; only the argument list is asserted.
    """
    monkeypatch.setattr(
        ani_cli_module, "get_platform_adapter", lambda: PlatformAdapter(WINDOWS)
    )
    wrapper = tmp_path / "ani-cli.cmd"
    wrapper.write_text("@echo off\r\n", encoding="utf-8")
    config = Config(ani_cli_path=str(wrapper), ani_cli_cmd_wrapper=True)
    resolved = wrapper.resolve()
    assert AniCliService(config)._command() == [
        "cmd.exe",
        "/d",
        "/s",
        "/c",
        f'"{resolved}"',
    ]


@pytest.mark.skipif(
    platform.system() != "Windows",
    reason="Real Windows path integration runs in the Windows CI job.",
)
def test_windows_cmd_wrapper_integration(tmp_path: Path) -> None:
    """Exercise the unmocked Windows adapter on an actual Windows host."""
    wrapper = tmp_path / "ani-cli.cmd"
    wrapper.write_text("@echo off\r\n", encoding="utf-8")
    service = AniCliService(
        Config(ani_cli_path=str(wrapper), ani_cli_cmd_wrapper=True)
    )
    resolved = wrapper.resolve()
    assert service._command() == [
        "cmd.exe",
        "/d",
        "/s",
        "/c",
        f'"{resolved}"',
    ]
    assert service.command_arguments(
        AniCliInvocation("cmd", "cmd.exe", (), str(resolved)),
        ["search", "-e", "1"],
    ) == ["/d", "/s", "/c", f'"{resolved}" "search" "-e" "1"']


def test_macos_uses_posix_ani_cli_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS rejects .cmd wrappers and uses its POSIX ani-cli path."""
    monkeypatch.setattr(
        ani_cli_module, "get_platform_adapter", lambda: PlatformAdapter(MACOS)
    )
    wrapper = tmp_path / "ani-cli.cmd"
    wrapper.write_text("@echo off\r\n", encoding="utf-8")
    assert AniCliService(Config(ani_cli_path=str(wrapper))).resolve_invocation() is None

    script = tmp_path / "ani-cli"
    script.write_text("#!/bin/bash\necho ani-cli\n", encoding="utf-8")
    script.chmod(0o700)
    invocation = AniCliService(Config(ani_cli_path=str(script))).resolve_invocation()
    assert invocation is not None
    assert invocation.kind in {"bash", "direct"}
    assert invocation.command != "cmd.exe"
    assert str(script.resolve()) in (
        *invocation.base_args,
        invocation.display_path,
    )
