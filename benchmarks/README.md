# Performance benchmarks

`perf_bench.py` is a repeatable, offline benchmark harness. It stubs AniList,
provider responses, and playback so before/after runs exercise the same code
path without network variance.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe benchmarks\perf_bench.py baseline2
.\.venv\Scripts\python.exe benchmarks\perf_bench.py after
.\.venv\Scripts\python.exe benchmarks\perf_bench.py after2
.\.venv\Scripts\python.exe benchmarks\perf_bench.py final-untraced1
.\.venv\Scripts\python.exe benchmarks\perf_bench.py final-untraced2
```

Each command writes `<label>.json` beside the script and prints the same JSON.
`RUNS = 3` with one warmup. Timings are **medians of three runs**, except where
a range across separate process runs is shown below. All reported timings are
untraced; resource usage is collected in a separate `tracemalloc` pass whose
internal timings are discarded.

## What changed

The profiling pass identified deterministic hotspots before code changes:

1. Six concurrent identical AniList searches issued six HTTP posts.
2. Every processed-poster cache hit reopened and PNG-verified the file.
3. `get_history_entry()` scanned up to 1,000 history rows for one ID.
4. Setup/diagnostics probes blocked the UI thread with subprocess/network work.
5. Several Textual screens used raw `asyncio.create_task()` instead of
   cancellable workers.

Changes made for those findings:

- In-flight AniList requests are deduplicated and shielded; cache DB work moved
  to `asyncio.to_thread`.
- Processed cache hits reuse an LRU of `(mtime_ns, size)` signatures (512 max).
- History lookup and `find_history_for_anime()` use indexed SQL queries.
- Setup probes run concurrently in a thread pool; diagnostics builds its report
  in a Textual worker.
- Discover search uses a cancellable Textual worker and only assigns results
  from the newest generation.
- Dashboard/Discover batch watchlist membership into one query; Dashboard
  enriches only rows it actually renders.
- Artwork caches are bounded, PIL work is off the event loop, and concurrent
  downloads/processing are capped at six.
- Legacy artwork import and ani-cli history import run concurrently.

## Before / after (measured on Windows 11, Python 3.12.10)

`baseline2.json` was captured before the performance changes. `after.json` and
`after2.json` came from the first post-change harness. `final-untraced1.json`
and `final-untraced2.json` were captured after the update work with the
corrected, always-untraced harness. Only those four untraced post-change runs
are compared here. Earlier `final.json`, `release1.json`, `release2.json`, and
`release3.json` were captured while `tracemalloc` was active during timing and
are **not** comparable for cache/history timings.

| Metric | Before (`baseline2`) | After (four untraced runs) | Result |
| --- | ---: | ---: | --- |
| Module import median | 671.25 ms | 409.11–748.60 ms | Highly variable; no reliable claim |
| Details open median | 731.35 ms | 746.09–817.04 ms | No reliable improvement |
| Search typing-to-settled | 930.00 ms | 868.34–1158.28 ms | Highly variable; API calls stayed at 1 per 9 chars |
| Episodes open median | 430.03 ms | 308.71–627.74 ms | Highly variable |
| 6 identical concurrent searches | 1982.08 ms, 6 posts | 30.48–31.55 ms, **1 post** | Large deterministic improvement |
| 200 cached poster lookups | 27.20 ms | 4.61–6.83 ms | Large deterministic improvement |
| 20 history entry lookups | 175.43 ms | 0.49–0.65 ms | Large deterministic improvement |

The UI-level timings are intentionally conservative: fixed `pilot.pause()`
delays dominate several measurements. Do not claim a startup, search, details,
or navigation speedup from these numbers. The deterministic hotspot benchmarks
are the ones that demonstrate measurable improvement.

## Resource usage (separate traced pass)

| Metric | `final-untraced1` | `final-untraced2` | Meaning |
| --- | ---: | ---: | --- |
| Cold startup estimate | 828.95 ms | 820.39 ms | Import cost plus warm startup |
| Warm startup median | 419.84 ms | 410.03 ms | Modules already imported in the benchmark process |
| Details open median | 750.66 ms | 746.09 ms | Same operation as `navigation` |
| Peak traced Python heap | 0.09 MiB | 0.09 MiB | Fixed hot-path workload, Python allocations only, not RSS |
| CPU time for hot-path workload | 250.00 ms | 250.00 ms | Six duplicate API bursts, 600 poster lookups, 60 history lookups |

`resource_usage` uses `tracemalloc` and `time.process_time()`. It is not a
full-process RSS or OS-level CPU measurement; no external profiler was invented
or implied. Exact values are in the JSON files.

## Interpreting compatibility

These numbers were recorded on Windows 11 x64 only. The same harness can be run
on Apple Silicon and Intel macOS, but no macOS timing results have been
collected yet. Do not copy these figures into macOS release notes as if they
were measured there.
