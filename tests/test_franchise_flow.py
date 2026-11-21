"""Tests for ANIXO-owned franchise, episode, and exact-ID playback flow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app import AnixoApp
from config import Config, get_franchise_selection, remember_franchise_selection
from models.anime import Anime, AnimeTitle, HistoryEntry
from screens.episode_selector import EpisodeSelectorScreen
from screens.season_selector import SeasonSelectorScreen
from services.ani_cli import (
    AniCliInvocation,
    AniCliService,
    patch_exact_provider_selection,
)
from services.franchise import franchise_key, group_franchises
from services.provider import (
    ProviderAnime,
    ProviderEpisode,
    ProviderService,
    parse_search_page,
)


SEARCH_HTML = """
<div class="flw-item flw-item-big">
  <h3 class="film-name"><a href="https://hianime.fm/overlord-464" title="Overlord">Overlord</a></h3>
  <div class="description">First season.</div>
  <div class="fd-infor"><span class="fdi-item">TV</span><span class="fdi-item fdi-duration">24m</span></div>
  <div class="tick-item tick-eps">13</div>
  <img src="https://img/one.jpg" class="film-poster-img">
</div>
<div class="flw-item flw-item-big">
  <h3 class="film-name"><a href="https://hianime.fm/overlord-iv-1502" title="Overlord IV">Overlord IV</a></h3>
  <div class="description">Fourth season.</div>
  <div class="fd-infor"><span class="fdi-item">TV</span></div>
  <div class="tick-item tick-eps">13</div>
</div>
<div class="flw-item flw-item-big">
  <h3 class="film-name"><a href="https://hianime.fm/one-piece-21" title="One Piece">One Piece</a></h3>
  <div class="fd-infor"><span class="fdi-item">TV</span></div>
</div>
<div id="main-sidebar">
  <div class="flw-item"><h3 class="film-name"><a href="/duplicate-1" title="Duplicate">Duplicate</a></h3></div>
</div>
"""

EPISODE_PAYLOAD = {
    "html": """
    <a title="Sorcerous Nation" class="ssl-item ep-item" data-number="1" data-id="25839"
       href="https://hianime.fm/watch/overlord-iv-1502?ep=25839"></a>
    <a title="Ainz" class="ssl-item ep-item" data-number="2" data-id="25840"
       href="https://hianime.fm/watch/overlord-iv-1502?ep=25840"></a>
    <a title="Wrong" class="ssl-item ep-item" data-number="1" data-id="9999"
       href="https://hianime.fm/watch/overlord-464?ep=9999"></a>
    """
}


@dataclass
class FakeResponse:
    text: str = ""
    payload: Any = None
    status_code: int = 200

    def json(self) -> Any:
        return self.payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.urls: list[str] = []

    def get(self, url: str, **_: Any) -> FakeResponse:
        self.urls.append(url)
        return self.response


def _sample_anime() -> Anime:
    return Anime(
        id="123",
        title=AnimeTitle(english="Overlord", romaji="Overlord"),
        episodes=13,
        cover_url="https://img/anilist.jpg",
    )


def _test_app(tmp_path: Path) -> AnixoApp:
    history = tmp_path / "ani-hsts"
    history.write_text("", encoding="utf-8")
    config = Config(
        images="off",
        artwork_renderer="off",
        ani_cli_history_path=str(history),
    )
    app = AnixoApp(config=config, data_dir=tmp_path / "data")
    app.api.trending = lambda *args, **kwargs: []  # type: ignore[method-assign]
    return app


def test_parse_search_preserves_provider_order_metadata_and_titles() -> None:
    results = parse_search_page(SEARCH_HTML)
    assert [item.source_id for item in results] == [
        "overlord-464",
        "overlord-iv-1502",
        "one-piece-21",
    ]
    assert [item.title for item in results] == ["Overlord", "Overlord IV", "One Piece"]
    assert results[0].format == "TV"
    assert results[0].episode_count == "13"
    assert results[0].description == "First season"
    assert results[0].poster_url == "https://img/one.jpg"


def test_grouping_is_conservative_and_preserves_original_entries() -> None:
    results = parse_search_page(SEARCH_HTML)
    extra = [
        ProviderAnime("overlord-ii-856", "Overlord II", "856"),
        ProviderAnime("sacred-2701", "Overlord: The Sacred Kingdom", "2701"),
        ProviderAnime("piece-movie-9", "One Piece Movie: Gold", "9"),
        ProviderAnime("punch-1", "One Punch Man", "1"),
    ]
    groups = group_franchises(results + extra)
    overlord = next(group for group in groups if group.key == "overlord")
    assert [entry.source_id for entry in overlord.entries] == [
        "overlord-464",
        "overlord-iv-1502",
        "overlord-ii-856",
        "sacred-2701",
    ]
    piece = next(group for group in groups if group.key == "one piece")
    assert [entry.source_id for entry in piece.entries] == [
        "one-piece-21",
        "piece-movie-9",
    ]
    punch = next(group for group in groups if group.key == "one punch man")
    assert [entry.source_id for entry in punch.entries] == ["punch-1"]
    assert franchise_key("Overlord IV") == franchise_key("Overlord: The Sacred Kingdom")


def test_episodes_are_rejected_when_they_belong_to_another_result() -> None:
    service = ProviderService(client=FakeClient(FakeResponse(payload=EPISODE_PAYLOAD)))  # type: ignore[arg-type]
    episodes = service.episodes("overlord-iv-1502")
    assert [(item.number, item.provider_episode_id, item.title) for item in episodes] == [
        ("1", "25839", "Sorcerous Nation"),
        ("2", "25840", "Ainz"),
    ]


def test_exact_patch_filters_by_id_and_disables_menu_dependency() -> None:
    source = (
        '#!/bin/sh\n'
        '        anime_list=$(hianime_search "$query") || exit 1\n'
        '        [ -z "$anime_list" ] && die "No results found!"\n'
        'dep_ch "$menu_program" # TODO: use dep_ch_failover to iterate through options\n'
    )
    patched = patch_exact_provider_selection(source)
    assert "ANI_CLI_PROVIDER_ID" in patched
    assert "ANI_CLI_MENU" not in patched  # supplied via environment, not script text
    assert "if [ -z \"$ANI_CLI_PROVIDER_ID\" ] || [ -z \"$ep_no\" ]" in patched
    assert "episode_maps" not in patched  # patch does not touch episode resolution


def test_spawn_uses_exact_id_without_continuing_or_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AniCliService(Config())
    script = tmp_path / "ani-cli-exact"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        service,
        "resolve_invocation",
        lambda: AniCliInvocation("direct", "ani-cli", (), "ani-cli"),
    )
    monkeypatch.setattr(service, "exact_script", lambda: script)
    monkeypatch.setattr(
        service,
        "exact_invocation",
        lambda _: AniCliInvocation("direct", "ani-cli", (), "ani-cli"),
    )
    monkeypatch.setattr(service, "supports_exit_after_play", lambda: True)
    captured: dict[str, Any] = {}

    async def fake_exec(command: str, *args: Any, **kwargs: Any) -> Any:
        captured["argv"] = [command, *args]
        captured["env"] = kwargs["env"]

        class Process:
            returncode = 0

            async def wait(self) -> int:
                return 0

        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    process = asyncio.run(
        service.spawn(
            title="Overlord IV",
            source_id="overlord-iv-1502",
            episode="3",
            exact=True,
            provider_id="overlord-iv-1502",
        )
    )
    assert process is not None
    assert captured["argv"] == [
        "ani-cli",
        "Overlord IV",
        "-S",
        "1",
        "-e",
        "3",
        "--exit-after-play",
    ]
    assert captured["env"]["ANI_CLI_PROVIDER_ID"] == "overlord-iv-1502"
    assert captured["env"]["ANI_CLI_MENU"] == "false"
    assert "-c" not in captured["argv"]


def test_exact_spawn_refuses_title_only_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AniCliService(Config())
    monkeypatch.setattr(
        service,
        "resolve_invocation",
        lambda: AniCliInvocation("direct", "ani-cli", (), "ani-cli"),
    )
    captured: dict[str, Any] = {}

    async def fake_exec(command: str, *args: Any, **kwargs: Any) -> Any:
        captured["argv"] = [command, *args]
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    process = asyncio.run(
        service.spawn(title="Overlord", episode="1", exact=True)
    )
    assert process is None
    assert "argv" not in captured


def test_history_without_provider_id_refuses_interactive_playback(
    tmp_path: Path,
) -> None:
    app = _test_app(tmp_path)
    anime = _sample_anime()
    anime.source_id = None
    entry = HistoryEntry(id="legacy", anime=anime, episode="1")
    result = asyncio.run(app.play_history_entry(entry))
    assert result is False


def test_franchise_selection_is_persisted(tmp_path: Path) -> None:
    config = Config()
    remember_franchise_selection(
        config, "overlord", "overlord-iv-1502", "Overlord IV", "7"
    )
    assert get_franchise_selection(config, "overlord") == {
        "source_id": "overlord-iv-1502",
        "title": "Overlord IV",
        "episode": "7",
    }


def test_watch_flow_uses_own_selectors_and_exact_provider_id(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    results = parse_search_page(SEARCH_HTML)
    app.provider.search = lambda query: results  # type: ignore[method-assign]
    app.provider.episodes = lambda source_id: [  # type: ignore[method-assign]
        ProviderEpisode("1", "25839", "Sorcerous Nation"),
        ProviderEpisode("2", "25840", "Ainz"),
    ]
    captured: dict[str, Any] = {}

    async def fake_spawn(**kwargs: Any) -> Any:
        captured.update(kwargs)

        class Process:
            returncode = 0

            async def wait(self) -> int:
                return 0

        return Process()

    app.ani_cli.spawn = fake_spawn  # type: ignore[method-assign]
    app.ani_cli.import_history = lambda database: 0  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await app.watch_anime(_sample_anime())
            await pilot.pause()
            season = app.screen
            assert isinstance(season, SeasonSelectorScreen)
            await season.action_choose()
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)
            assert selector.anime.source_id == "overlord-464"
            selector.selection.index = 1
            assert await app.play_episode(selector.anime, "2") is True
            assert captured["episode"] == "2"
            assert captured["provider_id"] == "overlord-464"
            assert captured["resume"] is False
            assert get_franchise_selection(app.config, "overlord") is not None

    asyncio.run(run())
