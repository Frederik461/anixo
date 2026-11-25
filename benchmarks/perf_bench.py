"""Repeatable, offline ANIXO performance benchmarks.

Run from the project root::

    .venv\\Scripts\\python.exe benchmarks\\perf_bench.py baseline
    .venv\\Scripts\\python.exe benchmarks\\perf_bench.py after

The benchmarks stub all network services so measurements stay deterministic and
can be compared across machines. Results are written as JSON next to this file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image  # noqa: E402

from app import AnixoApp  # noqa: E402
from config import Config  # noqa: E402
from data.database import Database  # noqa: E402
from models.anime import Anime, AnimeTitle  # noqa: E402
from services.anime_api import AnimeAPI  # noqa: E402
from services.image_cache import CacheKey, ImageCache  # noqa: E402
from services.provider import ProviderEpisode  # noqa: E402

RUNS = 3
WARMUP = 1


def sample_anime(index: int) -> Anime:
    return Anime(
        id=str(1000 + index),
        title=AnimeTitle(romaji=f"Benchmark Anime {index}", english=f"Benchmark {index}"),
        cover_url=None,
        episodes=12,
        status="FINISHED",
        average_score=80,
        genres=("Action", "Drama"),
    )


def make_app(directory: Path, *, search_calls: list[str] | None = None) -> AnixoApp:
    history = directory / "ani-hsts"
    history.write_text("", encoding="utf-8")
    config = Config(
        images="off",
        artwork_renderer="off",
        ani_cli_history_path=str(history),
    )
    app = AnixoApp(config=config, data_dir=directory / "data")
    anime = [sample_anime(index) for index in range(48)]

    async def trending(page: int = 1, per_page: int | None = None) -> list[Anime]:
        return anime[: per_page or 12]

    async def browse(**_: Any) -> list[Anime]:
        return anime

    async def search(query: str, **__: Any) -> list[Anime]:
        if search_calls is not None:
            search_calls.append(query)
        return [item for item in anime if query.casefold() in item.display_title().casefold()]

    app.api.trending = trending  # type: ignore[method-assign]
    app.api.browse = browse  # type: ignore[method-assign]
    app.api.search = search  # type: ignore[method-assign]
    app.provider.search = lambda query: []  # type: ignore[method-assign]
    app.provider.episodes = lambda source_id: [  # type: ignore[method-assign]
        ProviderEpisode(str(number), str(number), f"Episode {number}")
        for number in range(1, 13)
    ]
    return app


def measure_import() -> dict[str, float]:
    command = [
        sys.executable,
        "-c",
        "import time; t=time.perf_counter(); import app; "
        "print((time.perf_counter()-t)*1000)",
    ]
    values: list[float] = []
    for _ in range(RUNS):
        output = subprocess.run(
            command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        values.append(float(output.splitlines()[-1]))
    return {"median_ms": round(statistics.median(values), 2), "runs_ms": values}


async def measure_startup() -> dict[str, Any]:
    values: list[float] = []
    widget_counts: list[int] = []
    for run in range(WARMUP + RUNS):
        with tempfile.TemporaryDirectory() as tmp:
            started = time.perf_counter()
            app = make_app(Path(tmp))
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                elapsed = (time.perf_counter() - started) * 1000
                if run >= WARMUP:
                    values.append(elapsed)
                    widget_counts.append(len(list(app.query("*"))))
    return {
        "median_ms": round(statistics.median(values), 2),
        "runs_ms": [round(value, 2) for value in values],
        "median_widgets": statistics.median(widget_counts),
    }


async def measure_navigation() -> dict[str, Any]:
    values: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(Path(tmp))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for index in range(WARMUP + RUNS):
                started = time.perf_counter()
                app.open_details(str(1000 + index))
                await pilot.pause()
                await pilot.pause()
                elapsed = (time.perf_counter() - started) * 1000
                if index >= WARMUP:
                    values.append(elapsed)
                app.pop_screen()
                await pilot.pause()
    return {
        "median_ms": round(statistics.median(values), 2),
        "runs_ms": [round(value, 2) for value in values],
    }


async def measure_search() -> dict[str, Any]:
    calls: list[str] = []
    values: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(Path(tmp), search_calls=calls)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_goto_discover()
            await pilot.pause()
            from textual.widgets import Input

            for run in range(WARMUP + RUNS):
                field = app.screen.query_one("#search-input", Input)
                field.value = ""
                calls.clear()
                started = time.perf_counter()
                for character in "benchmark":
                    field.value += character
                    await pilot.pause()
                await asyncio.sleep(0.45)
                await pilot.pause()
                elapsed = (time.perf_counter() - started) * 1000
                if run >= WARMUP:
                    values.append(elapsed)
    return {
        "typing_to_settled_ms": round(statistics.median(values), 2),
        "runs_ms": [round(value, 2) for value in values],
        "api_calls_for_9_characters": len(calls),
        "api_calls": calls,
    }


async def measure_episodes() -> dict[str, Any]:
    values: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(Path(tmp))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            anime = sample_anime(0)
            anime.source_id = "benchmark-anime-1000"
            app.database.upsert_anime(anime)
            for index in range(WARMUP + RUNS):
                started = time.perf_counter()
                app.open_episode_selector(anime)
                await pilot.pause()
                await pilot.pause()
                elapsed = (time.perf_counter() - started) * 1000
                if index >= WARMUP:
                    values.append(elapsed)
                app.pop_screen()
                await pilot.pause()
    return {
        "median_ms": round(statistics.median(values), 2),
        "runs_ms": [round(value, 2) for value in values],
    }


class _EmptyCacheDB:
    def get_cache(self, *_: Any, **__: Any) -> None:
        return None

    def get_stale_cache(self, *_: Any, **__: Any) -> None:
        return None

    def set_cache(self, *_: Any, **__: Any) -> None:
        return None


async def measure_duplicate_api_calls() -> dict[str, Any]:
    """Six concurrent identical searches: count posts and measure wall time."""
    values: list[float] = []
    posts: list[int] = []
    for run in range(WARMUP + RUNS):
        api = AnimeAPI(Config(), _EmptyCacheDB())  # type: ignore[arg-type]
        count = 0

        async def fake_post(query: str, variables: dict) -> dict:
            nonlocal count
            count += 1
            await asyncio.sleep(0.02)
            return {}

        api._post = fake_post  # type: ignore[method-assign]
        started = time.perf_counter()
        await asyncio.gather(*(api.search("same query") for _ in range(6)))
        elapsed = (time.perf_counter() - started) * 1000
        if run >= WARMUP:
            values.append(elapsed)
            posts.append(count)
    return {
        "median_ms": round(statistics.median(values), 2),
        "posts_per_burst": posts,
    }


def measure_cache_hit() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        cache = ImageCache(Path(tmp) / "artwork")
        key = CacheKey("https://example.invalid/poster.jpg", 20, 15, "halfcell:10x20")
        image = Image.new("RGB", (40, 30), (32, 64, 128))
        cache.store_processed(key, image)
        values: list[float] = []
        for run in range(WARMUP + RUNS):
            started = time.perf_counter()
            for _ in range(200):
                assert cache.get_processed(key) is not None
            elapsed = (time.perf_counter() - started) * 1000
            if run >= WARMUP:
                values.append(elapsed)
    return {
        "median_ms_per_200_lookups": round(statistics.median(values), 2),
        "runs_ms": [round(value, 2) for value in values],
    }


def measure_history_lookup() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "history.db")
        for index in range(400):
            anime = sample_anime(index)
            anime.id = f"history-{index}"
            database.record_history(anime, "1", entry_id=f"history-{index}")
        values: list[float] = []
        for run in range(WARMUP + RUNS):
            started = time.perf_counter()
            for _ in range(20):
                assert database.get_history_entry("history-399") is not None
            elapsed = (time.perf_counter() - started) * 1000
            if run >= WARMUP:
                values.append(elapsed)
        database.close()
    return {
        "median_ms_per_20_lookups": round(statistics.median(values), 2),
        "runs_ms": [round(value, 2) for value in values],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("label", nargs="?", default="run")
    args = parser.parse_args()

    import_cost = measure_import()
    startup = await measure_startup()
    navigation = await measure_navigation()
    search = await measure_search()
    episodes = await measure_episodes()
    # Every reported timing is deliberately untraced: tracemalloc changes the
    # scheduler enough to make wall times misleading. Resource usage is
    # measured in a separate traced pass over the same fixed workload, and its
    # internal timings are discarded.
    duplicate = await measure_duplicate_api_calls()
    cache_hits = measure_cache_hit()
    history = measure_history_lookup()

    tracemalloc.start()
    cpu_started = time.process_time()
    await measure_duplicate_api_calls()
    measure_cache_hit()
    measure_history_lookup()
    cpu_ms = (time.process_time() - cpu_started) * 1000
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    report = {
        "label": args.label,
        "python": sys.version.split()[0],
        "import": import_cost,
        # The benchmark process has already imported every module, so this is a
        # warm start. Cold start is import cost plus that warm start.
        "cold_startup_estimate_ms": round(
            import_cost["median_ms"] + startup["median_ms"], 2
        ),
        "warm_startup": startup,
        # Opening anime details is the measured navigation operation.
        "details": navigation,
        "navigation": navigation,
        "search": search,
        "episodes": episodes,
        "duplicate_api_calls": duplicate,
        "image_cache_hit": cache_hits,
        "history_lookup": history,
        "resource_usage": {
            "peak_traced_mib": round(peak_bytes / (1024 * 1024), 2),
            "cpu_ms_for_hot_path_workload": round(cpu_ms, 2),
            "note": "Fixed workload: 6 duplicate API bursts, 600 cached-poster "
                    "lookups, 60 history lookups. Python heap only, not RSS.",
        },
    }
    destination = PROJECT_ROOT / "benchmarks" / f"{args.label}.json"
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
