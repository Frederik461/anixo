from __future__ import annotations

from pathlib import Path

from config import Config
from data.database import Database
from models.anime import Anime, AnimeTitle
from services.ani_cli import AniCliService, parse_provider_page_title


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


def test_cmd_wrapper_uses_cmd_exe(tmp_path: Path) -> None:
    wrapper = "C:\\tools\\ani-cli.cmd"
    config = Config(ani_cli_path=wrapper, ani_cli_cmd_wrapper=True)
    resolved = Path(wrapper).resolve()
    assert AniCliService(config)._command() == [
        "cmd.exe",
        "/d",
        "/s",
        "/c",
        f'"{resolved}"',
    ]
