from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from config import Config
from data.database import Database
from services.anime_api import AnimeAPI


_MEDIA = {
    "id": 154587,
    "title": {"romaji": "Sousou no Frieren", "english": "Frieren"},
    "coverImage": {"extraLarge": "https://example.test/cover.jpg"},
    "genres": ["Adventure", "Fantasy"],
}


def _api(tmp_path: Path) -> AnimeAPI:
    return AnimeAPI(Config(), Database(tmp_path / "animehub.sqlite3"))


def test_search_reads_graphql_data_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path)

    async def fake_post(self: AnimeAPI, query: str, variables: dict) -> dict:
        return {"data": {"Page": {"media": [_MEDIA]}}}

    monkeypatch.setattr(AnimeAPI, "_post", fake_post)
    results = asyncio.run(api.search("Frieren"))

    assert len(results) == 1
    assert results[0].display_title() == "Frieren"
    assert results[0].id == "154587"


def test_browse_applies_category_genre_and_sort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path)
    captured: dict[str, object] = {}

    async def fake_post(self: AnimeAPI, query: str, variables: dict) -> dict:
        captured["query"] = query
        captured["variables"] = variables
        return {"data": {"Page": {"media": [_MEDIA]}}}

    monkeypatch.setattr(AnimeAPI, "_post", fake_post)
    results = asyncio.run(
        api.browse(category="Airing", genre="Action", sort="Score", per_page=9)
    )

    assert len(results) == 1
    variables = captured["variables"]
    assert isinstance(variables, dict)
    assert variables["genre_in"] == ["Action"]
    assert variables["sort"] == ["SCORE_DESC"]
    assert variables["status"] == "RELEASING"
    assert "format" not in variables
    assert "genre_in" in str(captured["query"])
    key = Database._browse_key("Airing", "Action", "Score", 1, 9)
    assert api.database.get_cache(key) is not None


def test_browse_default_delegates_to_trending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path)
    calls: list[tuple[int, int | None]] = []

    async def fake_trending(page: int = 1, per_page: int | None = None) -> list:
        calls.append((page, per_page))
        return []

    monkeypatch.setattr(api, "trending", fake_trending)
    assert asyncio.run(api.browse()) == []
    assert calls == [(1, 48)]


def test_details_reads_graphql_data_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path)

    async def fake_post(self: AnimeAPI, query: str, variables: dict) -> dict:
        return {"data": {"Media": _MEDIA}}

    monkeypatch.setattr(AnimeAPI, "_post", fake_post)
    anime = asyncio.run(api.details(154587))

    assert anime is not None
    assert anime.display_title() == "Frieren"


def test_details_tracks_next_airing_episode_and_can_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path)
    calls: list[str] = []
    media = {
        **_MEDIA,
        "status": "RELEASING",
        "nextAiringEpisode": {"episode": 9, "airingAt": 1796137200},
    }

    async def fake_post(self: AnimeAPI, query: str, variables: dict) -> dict:
        calls.append(query)
        return {"data": {"Media": media}}

    monkeypatch.setattr(AnimeAPI, "_post", fake_post)
    anime = asyncio.run(api.details(154587))

    assert anime is not None and anime.next_airing_episode == 9
    assert "nextAiringEpisode" in calls[0]

    asyncio.run(api.details(154587))
    assert len(calls) == 1  # cached

    asyncio.run(api.details(154587, refresh=True))
    assert len(calls) == 2  # forced refresh


def test_post_normalizes_envelope_before_caching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path)

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"Page": {"media": [_MEDIA]}}}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: object) -> Response:
        return Response()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    payload = asyncio.run(api._post("query", {}))

    assert payload == {"Page": {"media": [_MEDIA]}}
