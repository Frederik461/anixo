"""Offline tests for provider URL following after an ani-cli update.

The fixtures are synthetic Next.js-style provider responses.  No live network,
installed application directory, or version literal is used.
"""

from __future__ import annotations

from typing import Any

from services.provider import (
    ProviderService,
    parse_miruro_episode_page,
    parse_miruro_search_page,
)


class _Response:
    def __init__(self, text: str = "") -> None:
        self.status_code = 200
        self.text = text

    def json(self) -> Any:
        raise AssertionError("Miruro provider reads use response text only")


class _Client:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.urls.append(url)
        return _Response(self.responses[url])


def _search_page() -> str:
    return """<script>items:[{id:"siteA",external_ids:{anilist:["21"],mal:["50410"]},title:{romaji:"First",english:"First English",native:"原生"},format:"TV",episode_counts:{raw:12,sub:12,dub:11},description:"A test show.",cover_url:"https://example.invalid/poster.jpg"},{id:"siteB",external_ids:{anilist:["22"]},title:{romaji:"Second",english:"Second English"},format:"MOVIE",episode_count:1}],entityItems:[]</script>"""


def test_parse_miruro_search_page_keeps_exact_composite_ids() -> None:
    rows = parse_miruro_search_page(_search_page())

    # ani-cli 5.2 keys are AniListID:siteID:MALID; the MAL ID must be present
    # even when it differs from the AniList ID, and empty when unadvertised.
    assert [(row.source_id, row.title, row.numeric_id) for row in rows] == [
        ("21:siteA:50410", "First English", "21"),
        ("22:siteB:", "Second English", "22"),
    ]
    assert rows[0].format == "TV"
    assert rows[0].episode_count == "12"
    assert rows[0].description == "A test show"
    assert rows[0].poster_url == "https://example.invalid/poster.jpg"


def test_parse_miruro_episode_page_uses_only_provider_numbers() -> None:
    page = "tracks:[{number:2},{number:1}]; .number=3; value:99; number:2"

    episodes = parse_miruro_episode_page(page, "siteA")

    assert [item.number for item in episodes] == ["1", "2", "3"]
    assert [item.provider_episode_id for item in episodes] == [
        "siteA:1",
        "siteA:2",
        "siteA:3",
    ]


def test_provider_service_follows_updated_miruro_url() -> None:
    search_url = "https://miruro.example/search?q=one%20piece"
    watch_url = "https://miruro.example/watch/siteA/x?ep=1"
    client = _Client(
        {search_url: _search_page(), watch_url: "episodes:[{number:1},{number:2}]"}
    )
    service = ProviderService(
        "https://miruro.example", client=client, kind="miruro"
    )

    rows = service.search("one piece")
    episodes = service.episodes("21:siteA:50410")

    assert client.urls == [search_url, watch_url]
    assert rows[0].source_id == "21:siteA:50410"
    assert [item.number for item in episodes] == ["1", "2"]
