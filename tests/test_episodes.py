from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from app import AnixoApp
from components.resume_dialog import ResumeDialog
from config import Config
from data.database import Database
from models.anime import Anime, AnimeTitle
from models.episode import (
    COMPLETED,
    UNWATCHED,
    WATCHING,
    build_episodes,
    episode_number,
    find_episode,
    grouped_episodes,
    has_groups,
    is_series_completed,
)
from screens.anime_details import AnimeDetailsScreen
from screens.episode_selector import EpisodeCell, EpisodeSelectorScreen
from services.ani_cli import AniCliInvocation, AniCliService
from services.provider import ProviderEpisode


# ---------------------------------------------------------------------------
# Helpers


def make_app(tmp_path: Path) -> AnixoApp:
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

    async def no_trending(page: int = 1, per_page: int | None = None) -> list[Anime]:
        return []

    app.api.trending = no_trending  # type: ignore[method-assign]
    app.provider.search = lambda query: []  # type: ignore[method-assign]
    app.provider.episodes = lambda source_id: []  # type: ignore[method-assign]
    return app


def sample_anime(**overrides) -> Anime:
    values = dict(
        id="123",
        title=AnimeTitle(romaji="Example Anime", english="Example Anime EN"),
        episodes=12,
        format="TV",
        season_year=2024,
        average_score=80,
        genres=("Action", "Drama"),
        description="<b>Hello</b> world",
        studios=("Studio X",),
        source_id="example-anime-123",
    )
    values.update(overrides)
    return Anime(**values)


class FakeProcess:
    returncode = 0

    async def wait(self) -> int:
        return 0


def install_fake_playback(app: AnixoApp, captured: dict) -> None:
    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return FakeProcess()

    app.suspend = lambda: nullcontext()  # type: ignore[method-assign]
    app.ani_cli.spawn = fake_spawn  # type: ignore[method-assign]
    app.ani_cli.import_history = lambda database: 0  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Episode model


def test_build_episodes_derives_states_from_history() -> None:
    episodes = build_episodes(sample_anime(), last_episode="5")

    assert [episode.number for episode in episodes] == [str(n) for n in range(1, 13)]
    assert [episode.state for episode in episodes[:4]] == [COMPLETED] * 4
    assert episodes[4].state == WATCHING
    assert all(episode.state == UNWATCHED for episode in episodes[5:])


def test_build_episodes_prefers_explicit_states() -> None:
    episodes = build_episodes(
        sample_anime(),
        states={"2": COMPLETED, "3": WATCHING, "9": COMPLETED},
        last_episode="3",
    )

    by_number = {episode.number: episode.state for episode in episodes}
    assert by_number["2"] == COMPLETED
    assert by_number["3"] == WATCHING
    assert by_number["9"] == COMPLETED


def test_build_episodes_without_source_metadata_is_empty() -> None:
    assert build_episodes(sample_anime(episodes=None)) == []
    assert build_episodes(sample_anime(episodes=0)) == []


def test_build_episodes_uses_supplied_entries_without_inventing() -> None:
    entries = [
        {"number": "1", "title": "Start", "duration": "24m", "group": "Part 1"},
        {"number": "2", "description": "Second", "group": "Part 1"},
        {"episode": "3", "title": "  "},
    ]
    episodes = build_episodes(sample_anime(), entries=entries)

    assert [episode.number for episode in episodes] == ["1", "2", "3"]
    assert episodes[0].title == "Start"
    assert episodes[0].duration == "24m"
    assert episodes[1].description == "Second"
    assert episodes[1].title is None
    assert episodes[2].title is None


def test_build_episodes_stops_before_next_airing_episode() -> None:
    anime = sample_anime(
        episodes=25, status="RELEASING", next_airing_episode=9
    )

    episodes = build_episodes(anime)

    assert [episode.number for episode in episodes] == [str(n) for n in range(1, 9)]


def test_build_episodes_with_unknown_airing_progress_shows_only_known_episodes() -> None:
    anime = sample_anime(episodes=25, status="RELEASING")

    assert build_episodes(anime) == []

    episodes = build_episodes(anime, states={"3": COMPLETED}, last_episode="4")
    assert [episode.number for episode in episodes] == ["1", "2", "3", "4"]


def test_provider_episode_list_is_not_capped_by_anilist() -> None:
    anime = sample_anime(episodes=25, status="RELEASING", next_airing_episode=3)
    entries = [{"number": str(number)} for number in range(1, 6)]

    episodes = build_episodes(anime, entries=entries)

    assert [episode.number for episode in episodes] == ["1", "2", "3", "4", "5"]


def test_find_episode_matches_exact_then_numeric() -> None:
    episodes = build_episodes(sample_anime())

    assert find_episode(episodes, "7").number == "7"
    assert find_episode(episodes, 7).number == "7"
    assert find_episode(episodes, "99") is None
    assert find_episode(episodes, None) is None
    assert episode_number("EP 04") == 4


def test_grouping_preserves_source_order() -> None:
    entries = [
        {"number": "1", "group": "Season 1"},
        {"number": "2", "group": "Season 1"},
        {"number": "3", "group": "Season 2"},
    ]
    episodes = build_episodes(sample_anime(), entries=entries)
    groups = grouped_episodes(episodes)

    assert has_groups(episodes) is True
    assert [(label, [item.number for item in items]) for label, items in groups] == [
        ("Season 1", ["1", "2"]),
        ("Season 2", ["3"]),
    ]
    assert has_groups(build_episodes(sample_anime())) is False


def test_series_completion_requires_final_episode_completed() -> None:
    anime = sample_anime(episodes=12)

    assert is_series_completed(anime, "12", {}) is False
    assert is_series_completed(anime, "12", {"12": COMPLETED}) is True
    assert is_series_completed(anime, "11", {"11": COMPLETED}) is False


# ---------------------------------------------------------------------------
# Persistence


def test_episode_progress_roundtrip(tmp_path: Path) -> None:
    database = Database(tmp_path / "episodes.sqlite3")
    anime = sample_anime()

    database.set_episode_state(
        anime.id, "3", WATCHING, progress_seconds=60, duration_seconds=120
    )
    database.mark_episode_completed(anime.id, "2")
    assert database.get_episode_states(anime.id) == {"2": COMPLETED, "3": WATCHING}
    records = database.get_episode_records(anime.id)
    assert records["3"]["progress_seconds"] == 60
    assert records["3"]["duration_seconds"] == 120

    database.sync_episode_states(anime.id, "5")
    states = database.get_episode_states(anime.id)
    assert states["1"] == COMPLETED
    assert states["2"] == COMPLETED
    assert states["4"] == COMPLETED
    assert states["5"] == WATCHING

    assert database.clear_episode_progress(anime.id) == 5
    assert database.get_episode_states(anime.id) == {}
    database.close()


def test_mark_episode_unwatched_clears_state_and_progress(tmp_path: Path) -> None:
    database = Database(tmp_path / "episodes.sqlite3")
    anime = sample_anime()

    database.set_episode_state(
        anime.id, "3", WATCHING, progress_seconds=60, duration_seconds=120
    )
    database.mark_episode_unwatched(anime.id, "3")

    assert database.get_episode_states(anime.id) == {"3": UNWATCHED}
    record = database.get_episode_records(anime.id)["3"]
    assert record["state"] == UNWATCHED
    assert record["progress_seconds"] is None
    assert record["duration_seconds"] is None
    database.close()


def test_launch_does_not_downgrade_completed_episode(tmp_path: Path) -> None:
    database = Database(tmp_path / "episodes.sqlite3")
    anime = sample_anime()

    database.mark_episode_completed(anime.id, "3")
    database.mark_episode_watching(anime.id, "3")

    assert database.get_episode_states(anime.id)["3"] == COMPLETED
    database.close()


def test_sync_never_downgrades_completed_episode(tmp_path: Path) -> None:
    database = Database(tmp_path / "episodes.sqlite3")
    anime = sample_anime()

    database.mark_episode_completed(anime.id, "3")
    database.sync_episode_states(anime.id, "3")

    assert database.get_episode_states(anime.id)["3"] == COMPLETED
    database.close()


def test_removing_history_clears_episode_progress(tmp_path: Path) -> None:
    database = Database(tmp_path / "episodes.sqlite3")
    anime = sample_anime()
    database.record_history(anime, "5", entry_id=anime.id)
    database.mark_episode_completed(anime.id, "1")

    assert database.remove_history(anime.id) is True
    assert database.get_episode_states(anime.id) == {}
    database.close()


def test_clearing_history_clears_all_episode_progress(tmp_path: Path) -> None:
    database = Database(tmp_path / "episodes.sqlite3")
    first = sample_anime(id="1")
    second = sample_anime(id="2")
    database.record_history(first, "1", entry_id=first.id)
    database.record_history(second, "2", entry_id=second.id)
    database.mark_episode_completed(first.id, "1")
    database.mark_episode_completed(second.id, "2")

    database.clear_history()

    assert database.get_episode_states(first.id) == {}
    assert database.get_episode_states(second.id) == {}
    database.close()


# ---------------------------------------------------------------------------
# ani-cli command generation


def _service_with_invocation(monkeypatch, exit_after_play: bool) -> tuple[AniCliService, dict]:
    service = AniCliService(Config(ani_cli_path="ani-cli"))
    service.resolve_invocation = lambda: AniCliInvocation(  # type: ignore[method-assign]
        "direct", "ani-cli", (), "ani-cli"
    )
    service._exit_after_play = exit_after_play
    service.exact_script = lambda: Path("ani-cli-exact")  # type: ignore[method-assign]
    service.exact_invocation = lambda _: AniCliInvocation(  # type: ignore[method-assign]
        "direct", "ani-cli", (), "ani-cli"
    )
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return service, captured


def test_exact_episode_argv_skips_picker(monkeypatch) -> None:
    service, captured = _service_with_invocation(monkeypatch, exit_after_play=True)

    asyncio.run(
        service.spawn(
            title="Example",
            source_id="example-1",
            episode="7",
            exact=True,
            provider_id="example-1",
        )
    )

    args = captured["args"]
    assert args[:2] == ("ani-cli", "Example")
    assert args[args.index("-e") + 1] == "7"
    assert "--exit-after-play" in args
    # Continue mode would overwrite the requested episode with ani-cli history.
    assert "-c" not in args


def test_exact_episode_omits_unsupported_flag(monkeypatch) -> None:
    service, captured = _service_with_invocation(monkeypatch, exit_after_play=False)

    asyncio.run(
        service.spawn(
            title="Example",
            source_id="example-1",
            episode="7",
            exact=True,
            provider_id="example-1",
        )
    )

    args = captured["args"]
    assert args[args.index("-e") + 1] == "7"
    assert "--exit-after-play" not in args


def test_exact_episode_dub_argv_uses_existing_flag(monkeypatch) -> None:
    service, captured = _service_with_invocation(monkeypatch, exit_after_play=True)

    asyncio.run(
        service.spawn(
            title="Example",
            source_id="example-1",
            episode="7",
            exact=True,
            dub=True,
            provider_id="example-1",
        )
    )

    args = captured["args"]
    assert args[args.index("-e") + 1] == "7"
    assert "--dub" in args
    assert "-c" not in args


def test_exact_episode_sub_argv_omits_dub(monkeypatch) -> None:
    service, captured = _service_with_invocation(monkeypatch, exit_after_play=True)

    asyncio.run(
        service.spawn(
            title="Example",
            source_id="example-1",
            episode="7",
            exact=True,
            dub=False,
            provider_id="example-1",
        )
    )

    args = captured["args"]
    assert args[args.index("-e") + 1] == "7"
    assert "--dub" not in args


def test_exit_after_play_detection_reads_installed_script(tmp_path: Path) -> None:
    script = tmp_path / "ani-cli"
    script.write_text("while true; do mpv --exit-after-play; done", encoding="utf-8")
    service = AniCliService(Config(ani_cli_path=str(script)))
    service.installed_script = lambda: script  # type: ignore[method-assign]
    assert service.supports_exit_after_play() is True

    other = tmp_path / "ani-cli-old"
    other.write_text("mpv", encoding="utf-8")
    old = AniCliService(Config(ani_cli_path=str(other)))
    old.installed_script = lambda: other  # type: ignore[method-assign]
    assert old.supports_exit_after_play() is False


# ---------------------------------------------------------------------------
# Details screen


def test_details_renders_metadata_and_actions(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.database.upsert_anime(sample_anime())

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_details("123")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, AnimeDetailsScreen)

            summary = str(screen.query_one("#detail-summary", Static).content)
            assert "Example Anime EN" in summary
            assert "TV" in summary and "2024" in summary and "12 eps" in summary
            assert "★ 80%" in summary
            assert "Action" in summary and "Studio: Studio X" in summary
            assert "Progress: Not started" in summary

            description = str(screen.query_one("#detail-description", Static).content)
            assert "Hello world" in description
            assert "<b>" not in description

            assert screen.query_one("#detail-watch").label == "▶  Watch"
            assert screen.query_one("#detail-watchlist").label == "+  Add Watchlist"
            assert screen.query_one("#detail-mark").disabled is True

    asyncio.run(run())


@pytest.mark.parametrize("size", [(160, 40), (120, 40), (100, 30), (80, 24)])
def test_details_sections_do_not_cover_action_buttons(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app = make_app(tmp_path)
    app.database.upsert_anime(
        sample_anime(
            title=AnimeTitle(
                romaji=(
                    "A Very Long Anime Title That Wraps Across Several Lines "
                    "In The Details Summary Panel"
                ),
                english="An Equally Long Alternative English Title For Wrapping",
            ),
            genres=("Action", "Adventure", "Drama", "Fantasy", "Supernatural"),
            description=("A long synopsis that wraps. " * 20),
        )
    )

    async def run() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            app.open_details("123")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, AnimeDetailsScreen)

            head = screen.query_one("#detail-head").region
            summary = screen.query_one("#detail-summary").region
            actions = screen.query_one("#detail-actions").region
            scroll = screen.query_one("#detail-scroll").region

            assert summary.y + summary.height <= head.y + head.height
            assert head.y + head.height <= actions.y
            assert summary.y + summary.height <= actions.y
            assert actions.y + actions.height <= scroll.y
            for button in screen.query("#detail-actions Button"):
                region = button.region
                assert region.y >= actions.y
                assert region.y + region.height <= actions.y + actions.height

    asyncio.run(run())


def test_watch_without_history_opens_selector(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.database.upsert_anime(sample_anime())

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_details("123")
            await pilot.pause()
            await pilot.press("w")
            # Watch resolution runs in an exclusive worker.  Under a loaded
            # full-suite run one pause can observe the screen before that worker
            # publishes the selector, so wait for the state instead of relying
            # on a single timing window.
            for _ in range(20):
                if isinstance(app.screen, EpisodeSelectorScreen):
                    break
                await pilot.pause()
            assert isinstance(app.screen, EpisodeSelectorScreen)

    asyncio.run(run())


def test_watch_with_progress_offers_resume_then_select(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.record_history(anime, "5", entry_id=anime.id)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_details("123")
            await pilot.pause()
            screen = app.screen
            assert screen.query_one("#detail-watch").label == "▶  Resume EP 5"

            await pilot.press("w")
            await pilot.pause()
            assert isinstance(app.screen, ResumeDialog)

            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, EpisodeSelectorScreen)
            selector = app.screen
            assert selector.current_episode.number == "5"
            assert selector.current_episode.state == WATCHING

    asyncio.run(run())


def test_resume_plays_exact_episode_without_advancing(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.record_history(anime, "5", entry_id=anime.id)
    captured: dict = {}
    install_fake_playback(app, captured)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_details("123")
            await pilot.pause()
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            await pilot.pause()

            assert captured["episode"] == "5"
            assert captured["resume"] is False
            assert captured["exact"] is True

            entry = app.database.get_history_entry(anime.id)
            assert entry is not None
            assert entry.episode == "5"
            states = app.database.get_episode_states(anime.id)
            assert "4" not in states
            assert states["5"] == WATCHING

    asyncio.run(run())


def test_completed_series_opens_episode_browser(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.record_history(anime, "12", entry_id=anime.id)
    app.database.mark_episode_completed(anime.id, "12")

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.watch_anime(anime)
            await pilot.pause()
            assert isinstance(app.screen, EpisodeSelectorScreen)

    asyncio.run(run())


def test_discover_and_history_route_through_shared_watch(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.record_history(anime, "5", entry_id=anime.id)
    calls: list[str] = []

    async def spy(value: Any) -> None:
        calls.append(getattr(value, "anime", value).id)

    async def history_spy(entry: Any) -> bool:
        await spy(entry)
        return True

    app.watch_anime = spy  # type: ignore[method-assign]
    app.play_history_entry = history_spy  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_goto_discover()
            await pilot.pause()
            discover = app.screen
            discover.results = [anime]
            await discover.action_open()
            assert calls == ["123"]

            app.action_goto_history()
            await pilot.pause()
            history = app.screen
            assert len(history.entries) == 1
            await history.action_watch()
            assert calls == ["123", "123"]

    asyncio.run(run())


def test_details_mark_watched_advances_resume(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.record_history(anime, "5", entry_id=anime.id)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_details("123")
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()

            states = app.database.get_episode_states(anime.id)
            assert states["5"] == COMPLETED
            entry = app.database.get_history_entry(anime.id)
            assert entry is not None and entry.episode == "6"
            assert app.screen.query_one("#detail-watch").label == "▶  Resume EP 6"

    asyncio.run(run())


def test_details_mark_toggles_watched_and_unwatched(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.record_history(anime, "5", entry_id=anime.id)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_details("123")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, AnimeDetailsScreen)
            mark = screen.query_one("#detail-mark")
            watch = screen.query_one("#detail-watch")

            assert mark.label == "✓  Mark Watched"
            await pilot.press("m")
            await pilot.pause()
            assert app.database.get_episode_states(anime.id)["5"] == COMPLETED
            entry = app.database.get_history_entry(anime.id)
            assert entry is not None and entry.episode == "6"
            assert mark.label == "↺  Mark Unwatched"

            await pilot.press("M")
            await pilot.pause()
            assert app.database.get_episode_states(anime.id)["5"] == UNWATCHED
            entry = app.database.get_history_entry(anime.id)
            assert entry is not None and entry.episode == "5"
            assert mark.label == "✓  Mark Watched"
            assert watch.label == "▶  Resume EP 5"

    asyncio.run(run())


def test_details_watchlist_toggle(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.database.upsert_anime(sample_anime())

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_details("123")
            await pilot.pause()
            screen = app.screen

            await pilot.press("a")
            await pilot.pause()
            assert app.database.is_watchlisted("123") is True
            assert screen.query_one("#detail-watchlist").label == "−  Remove Watchlist"

            await pilot.press("a")
            await pilot.pause()
            assert app.database.is_watchlisted("123") is False
            assert screen.query_one("#detail-watchlist").label == "+  Add Watchlist"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Episode selector


def test_selector_navigation_pagination_and_back(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.database.upsert_anime(sample_anime(episodes=60))

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_details("123")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)
            assert len(selector.episodes) == 60

            await pilot.press("right")
            await pilot.pause()
            assert selector.current_episode.number == "2"

            await pilot.press("end")
            await pilot.pause()
            assert selector.current_episode.number == "60"

            await pilot.press("home")
            await pilot.pause()
            assert selector.current_episode.number == "1"

            first_page = selector.selection.page_start
            await pilot.press("pagedown")
            await pilot.pause()
            assert selector.selection.page_start > first_page

            await pilot.press("pageup")
            await pilot.pause()
            assert selector.selection.page_start == first_page

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, AnimeDetailsScreen)

    asyncio.run(run())


def test_refresh_airing_progress_recovers_missing_next_airing(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime(anilist_id=154587, episodes=25, status="RELEASING")

    async def fake_details(anilist_id: int, *, refresh: bool = False):
        assert anilist_id == 154587 and refresh is True
        return sample_anime(
            id=str(anilist_id),
            anilist_id=anilist_id,
            episodes=25,
            status="RELEASING",
            next_airing_episode=9,
        )

    app.api.details = fake_details  # type: ignore[method-assign]
    refreshed = asyncio.run(app.refresh_airing_progress(anime))

    assert refreshed.next_airing_episode == 9
    assert [episode.number for episode in build_episodes(refreshed)] == [
        str(number) for number in range(1, 9)
    ]


def test_selector_renders_source_groups_and_updates_info_panel(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime(episodes=3)
    app.database.upsert_anime(anime)
    entries = [
        {
            "number": "1",
            "title": "Arrival",
            "description": "First episode",
            "duration": "24m",
            "group": "Part 1",
        },
        {
            "number": "2",
            "title": "Departure",
            "description": "Second episode",
            "group": "Part 1",
        },
        {"number": "3", "title": "Return", "group": "Part 2"},
    ]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_episode_selector(anime, entries=entries)
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)
            assert [episode.title for episode in selector.episodes] == [
                "Arrival",
                "Departure",
                "Return",
            ]
            assert len(selector.query(".episode-group")) == 2
            assert len(selector.query(".episode-cell.selected")) == 1

            def info(widget_id: str) -> str:
                return str(selector.query_one(widget_id, Static).content)

            assert "Arrival" in info("#episode-info-title")
            assert "First episode" in info("#episode-info-description")
            assert "24m" in info("#episode-info-duration")
            assert "Part 1" in info("#episode-info-status")

            await pilot.press("right")
            await pilot.pause()
            assert "Departure" in info("#episode-info-title")
            assert "Second episode" in info("#episode-info-description")
            assert "Part 1" in info("#episode-info-status")
            assert selector.query_one("#episode-prev").disabled is False
            assert selector.query_one("#episode-next").disabled is False

    asyncio.run(run())


def test_selector_uses_provider_released_episodes_without_explicit_entries(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    anime = sample_anime(episodes=25, status="FINISHED")
    app.database.upsert_anime(anime)
    app.provider.episodes = lambda source_id: [  # type: ignore[method-assign]
        ProviderEpisode("1", "1001", "Arrival"),
        ProviderEpisode("2", "1002", "Departure"),
    ]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_episode_selector(anime)
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)
            assert [episode.number for episode in selector.episodes] == ["1", "2"]
            assert [episode.title for episode in selector.episodes] == [
                "Arrival",
                "Departure",
            ]

    asyncio.run(run())


def test_selector_plays_exact_episode(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.upsert_anime(anime)
    captured: dict = {}
    install_fake_playback(app, captured)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_episode_selector(anime)
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)

            for _ in range(6):
                await pilot.press("right")
            await pilot.pause()
            assert selector.current_episode.number == "7"

            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            assert captured["episode"] == "7"
            assert captured["exact"] is True
            assert captured["dub"] is False
            entry = app.database.find_history_for_anime(anime)
            assert entry is not None and entry.episode == "7"

    asyncio.run(run())


def test_selector_sub_dub_toggle_persists_and_controls_playback(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.upsert_anime(anime)
    captured: dict = {}
    install_fake_playback(app, captured)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_episode_selector(anime)
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)

            assert "active" in selector.query_one("#episode-audio-sub").classes
            assert "active" not in selector.query_one("#episode-audio-dub").classes

            await pilot.press("u")
            await pilot.pause()
            assert selector.audio == "dub"
            assert app.config.preferred_audio == "dub"
            assert app.config_store.load().preferred_audio == "dub"
            assert "active" in selector.query_one("#episode-audio-dub").classes

            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert captured["episode"] == "1"
            assert captured["exact"] is True
            assert captured["dub"] is True

            selector.action_set_audio("sub")
            assert selector.audio == "sub"
            assert app.config_store.load().preferred_audio == "sub"
            assert "active" in selector.query_one("#episode-audio-sub").classes

    asyncio.run(run())


def test_selector_mark_watched_updates_state(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.upsert_anime(anime)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_episode_selector(anime)
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)

            await pilot.press("m")
            await pilot.pause()

            assert app.database.get_episode_states(anime.id)["1"] == COMPLETED
            assert selector.current_episode.state == COMPLETED

    asyncio.run(run())


def test_selector_mark_toggles_watched_and_unwatched(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.upsert_anime(anime)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_episode_selector(anime)
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)
            mark = selector.query_one("#episode-mark")

            assert mark.label == "✓  Mark Watched"
            await pilot.press("m")
            await pilot.pause()
            assert app.database.get_episode_states(anime.id)["1"] == COMPLETED
            assert selector.current_episode.state == COMPLETED
            assert mark.label == "↺  Mark Unwatched"
            entry = app.database.find_history_for_anime(anime)
            assert entry is not None and entry.episode == "2"

            await pilot.press("M")
            await pilot.pause()
            assert app.database.get_episode_states(anime.id)["1"] == UNWATCHED
            assert selector.current_episode.state == UNWATCHED
            assert mark.label == "✓  Mark Watched"
            status = str(selector.query_one("#episode-info-status", Static).content)
            assert "Unwatched" in status
            entry = app.database.find_history_for_anime(anime)
            assert entry is not None and entry.episode == "1"

    asyncio.run(run())


def test_selector_shows_persisted_progress(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.upsert_anime(anime)
    app.database.set_episode_state(
        anime.id,
        "2",
        WATCHING,
        progress_seconds=60,
        duration_seconds=120,
    )

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_episode_selector(anime)
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)

            await pilot.press("right")
            await pilot.pause()
            duration = str(selector.query_one("#episode-info-duration", Static).content)
            assert "50% watched" in duration

    asyncio.run(run())


def test_selector_handles_missing_episode_metadata(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime(episodes=None)
    app.database.upsert_anime(anime)
    captured: dict = {}
    install_fake_playback(app, captured)

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_episode_selector(anime)
            await pilot.pause()
            selector = app.screen
            assert isinstance(selector, EpisodeSelectorScreen)
            assert selector.episodes == []
            assert selector.current_episode is None
            assert len(selector.query(EpisodeCell)) == 0

            await pilot.press("/")
            await pilot.pause()
            field = selector.query_one("#episode-jump-input", Input)
            field.value = "3"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            assert captured["episode"] == "3"
            assert captured["exact"] is True

    asyncio.run(run())


def test_playback_failure_is_reported_without_crashing(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    anime = sample_anime()
    app.database.upsert_anime(anime)

    async def failing_spawn(**kwargs):
        return None

    app.suspend = lambda: nullcontext()  # type: ignore[method-assign]
    app.ani_cli.spawn = failing_spawn  # type: ignore[method-assign]

    async def run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert await app.play_episode(anime, "4") is False
            assert app.database.get_episode_states(anime.id) == {}
            assert app.database.list_history() == []

    asyncio.run(run())
