from __future__ import annotations

import json
from pathlib import Path

from data.database import Database
from models.anime import Anime, AnimeTitle


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_legacy_history_and_watchlist_are_migrated(tmp_path: Path) -> None:
    data = tmp_path / "appdata"
    data.mkdir()
    write_json(
        data / "history.json",
        [
            {
                "id": "ani-cli:slime-2866",
                "sourceId": "that-time-i-got-reincarnated-as-a-slime-season-4-2866",
                "episode": "24",
                "lastWatched": "2026-11-20T10:00:00Z",
                "anime": {
                    "title": {
                        "english": "That Time I Got Reincarnated as a Slime Season 4",
                        "romaji": "Tensei shitara Slime Datta Ken 4th Season",
                    },
                    "coverUrl": "https://example.test/slime.jpg",
                    "sourceId": "that-time-i-got-reincarnated-as-a-slime-season-4-2866",
                },
            }
        ],
    )
    write_json(
        data / "watchlist.json",
        [
            {
                "title": {"romaji": "Frieren: Beyond Journey's End"},
                "coverUrl": "https://example.test/frieren.jpg",
            }
        ],
    )

    database = Database(data / "animehub.sqlite3")
    imported = database.migrate_legacy(data)

    assert imported == 2
    history = database.list_history()
    assert len(history) == 1
    assert history[0].episode == "24"
    assert history[0].source_id == "that-time-i-got-reincarnated-as-a-slime-season-4-2866"
    assert len(database.list_watchlist()) == 1
    database.close()


def test_history_preserves_distinct_source_identities(tmp_path: Path) -> None:
    database = Database(tmp_path / "animehub.sqlite3")
    first = Anime(
        id="slime",
        title=AnimeTitle(romaji="Slime Season 4"),
        source_id="slime-2866",
    )
    second = Anime(
        id="slime",
        title=AnimeTitle(romaji="Slime Season 4"),
        source_id="slime-5238",
    )

    database.record_history(first, "24", source_id="slime-2866")
    database.record_history(second, "90", source_id="slime-5238")

    history = {entry.source_id: entry.episode for entry in database.list_history()}
    assert history == {"slime-2866": "24", "slime-5238": "90"}
    database.close()


def test_live_shaped_history_keeps_canonical_source_identity(tmp_path: Path) -> None:
    data = tmp_path / "live"
    data.mkdir()
    write_json(
        data / "history.json",
        {
            "version": 2,
            "ignoredAniCliSourceIds": [],
            "items": [
                {
                    "id": "ani-cli:slime-2866",
                    "episode": "24",
                    "source": "ani-cli",
                    "anime": {
                        "id": "ani-cli:slime-2866",
                        "title": {"romaji": "Slime Season 4"},
                        "coverUrl": "https://example.test/slime.jpg",
                    },
                },
                {
                    "id": "ani-cli:slime-5238",
                    "episode": "90",
                    "source": "ani-cli",
                    "anime": {
                        "id": "ani-cli:slime-5238",
                        "title": {"romaji": "Slime Season 4"},
                    },
                },
            ],
        },
    )
    write_json(
        data / "watchlist.json",
        {"version": 1, "items": [{"anime": {"id": "x", "title": {"romaji": "Frieren"}}}]},
    )
    (data / "cache").mkdir()
    write_json(
        data / "cache" / "metadata.json",
        {
            "version": 2,
            "entries": {
                "e9fe145fe097:trending:1:18": {
                    "timestamp": "2026-11-20T00:00:00Z",
                    "items": [{"id": 1, "title": {"romaji": "One"}}],
                },
                "e9fe145fe097:search:name:with:colons:1:24": {
                    "timestamp": "2026-11-20T00:00:00Z",
                    "items": [],
                },
            },
        },
    )

    database = Database(data / "animehub.sqlite3")
    imported = database.migrate_legacy(data, ttl_hours=12)

    assert imported == 3
    history = database.list_history()
    assert {entry.id for entry in history} == {"ani-cli:slime-2866", "ani-cli:slime-5238"}
    assert {entry.source_id for entry in history} == {"slime-2866", "slime-5238"}
    assert database.meta("legacy_metadata") == "2"
    assert database.meta("legacy_metadata_items") == "1"
    database.close()


def test_migration_is_atomic_when_a_stage_fails(
    tmp_path: Path, monkeypatch
) -> None:
    data = tmp_path / "atomic"
    data.mkdir()
    write_json(
        data / "history.json",
        {
            "items": [
                {
                    "id": "ani-cli:source-1",
                    "episode": "3",
                    "source": "ani-cli",
                    "anime": {"title": {"romaji": "Test Anime"}},
                }
            ]
        },
    )
    (data / "cache").mkdir()
    write_json(data / "cache" / "metadata.json", {"entries": {}})

    def fail_import(self, path, ttl_hours):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated corrupt metadata")

    monkeypatch.setattr(Database, "_import_metadata_cache", fail_import)
    database = Database(data / "animehub.sqlite3")
    try:
        database.migrate_legacy(data)
    except RuntimeError:
        pass
    else:  # pragma: no cover - the monkeypatch always raises
        raise AssertionError("migration did not propagate the failure")

    assert database.list_history() == []
    assert database.meta("legacy_history_rows") == ""
    assert database.connection.execute(
        "SELECT 1 FROM migrations WHERE name = 'legacy-json-v1'"
    ).fetchone() is None
    database.close()


def test_watchlist_rejects_title_equivalents(tmp_path: Path) -> None:
    database = Database(tmp_path / "animehub.sqlite3")
    first = Anime(id="one", title=AnimeTitle(english="Frieren: Beyond Journey's End"))
    second = Anime(
        id="two",
        title=AnimeTitle(romaji="Frieren: Beyond Journey’s End"),
    )

    assert database.add_watchlist(first) is True
    assert database.add_watchlist(second) is False
    assert len(database.list_watchlist()) == 1
    assert database.remove_watchlist_anime(second) is True
    assert len(database.list_watchlist()) == 0
    database.close()


def test_cache_expires_and_can_be_replaced(tmp_path: Path) -> None:
    database = Database(tmp_path / "animehub.sqlite3")
    database.set_cache("key", {"value": 1}, ttl_hours=0)
    assert database.get_cache("key") is None
    assert database.get_stale_cache("key") == {"value": 1}

    database.set_cache("key", {"value": 2}, ttl_hours=1)
    assert database.get_cache("key") == {"value": 2}
    database.close()
