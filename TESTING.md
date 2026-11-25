# Testing status for ANIXO v0.1.0-beta.5

This file records what was **actually run locally** and what is still
**unknown**. It is deliberately conservative: clean-machine compatibility is
**not** claimed until a fresh Windows/macOS system without developer tools has
been tested. Intel Mac builds are not published until independently tested.

## Local environment used for these checks

* Windows 11 x64, Python 3.12.10 (project `.venv`).
* `uv` 0.13.9 and PyInstaller 6.22.3 in the **build environment only**; end
  users receive neither.
* Inno Setup 6.7.3 installed per-user for the Windows installer build.
* Pillow 13.0.0 for icon generation.
* Full Windows pipeline rerun on 2026-11-21 after the cross-platform and
  uninstall integration; report in `dist/release/build-report.txt`.

## Actually verified locally

| Check | Result |
| --- | --- |
| `uv lock --check` | PASS — frozen lock resolves. |
| `python -m pytest -q` | PASS — **282 passed** on real Windows on 2026-11-25 after the updater and settings-action fixes; **280 passed** on both GitHub-hosted macOS runners during the successful `v0.1.0-beta.5` release workflow. Earlier macOS checks were `202 passed, 1 skipped` (Windows-only cmd-wrapper test). |
| `python -m compileall` / `py_compile` over source, services, platforms, screens, tests | PASS — no syntax errors. |
| `uninstaller.py --version` / `--help` | PASS — standalone CLI starts and lists all options. |
| `assets/make_icons.py` | PASS — ICO files contain 16/32/48/64/128/256 px frames and `anixo.png` source for `.icns`. |
| `packaging/build_windows.ps1` (full pipeline, no skips) | PASS — all stages completed; four artifacts and hashes produced. |
| One-folder build (`anixo.spec`) | PASS — `dist\Anixo\Anixo.exe` produced and validated. |
| One-file validation build | PASS — `dist\onefile\Anixo.exe` produced and validated. |
| Standalone uninstaller build (`uninstaller.spec`) | PASS — `dist\ANIXO-Uninstaller.exe` produced; `--version` and `--help` exit 0. |
| `packaging/validate_build.ps1` on both app executables | PASS — `--version`, `--help`, `--diagnose` all exited 0. |
| Identity in frozen builds | PASS — version/help output contains ANIXO and `0.1.0-beta.5`. |
| Bundled resources | PASS — `styles/theme.tcss` and `assets/anixo.ico` found under `dist\Anixo\_internal`. |
| Full diagnostics report | PASS — `--diagnose` prints version, OS, architecture, install/data/log paths, Python mode, playback tools, image protocol, network status and cache counts. |
| Diagnostics redaction | PASS — automated test confirms tokens, passwords, bearer headers and URL credentials are replaced with `<redacted>` before display/export. |
| Inno Setup compile | PASS — `ANIXO-Setup-v0.1.0-beta.5.exe` built by ISCC 6.7.3. |
| Silent install and OS-level uninstall with redirected user data | PASS — `/VERYSILENT /NOICONS` install into a temp `/DIR`, then silent `unins000.exe`; manifest located, custom paths recorded and replayed, cache and logs removed, sentinel data preserved, empty program folder and `uninstall-paths.ini` removed, real `%APPDATA%\\ANIXO` and `%LOCALAPPDATA%\\ANIXO` unchanged. |
| Portable ZIP | PASS — includes `Anixo.exe`, `ANIXO-Uninstaller.exe`, `START ANIXO.cmd`, `PORTABLE.md`, `PORTABLE.txt` and `_internal\styles\theme.tcss`. |
| Portable data redirection | PASS — launcher sets `ANIXO_DATA_DIR`, `ANIXO_CACHE_DIR` and `ANIXO_LOG_DIR` to `%LOCALAPPDATA%\ANIXO\Portable`. |
| SHA256SUMS.txt | PASS — setup, portable ZIP and standalone uninstaller re-hashed locally and matched. |
| Platform adapters | PASS — automated tests cover Windows/macOS/Linux data, cache, log and state paths, env overrides, shell conversion, installer roots, and portable trust. |
| Persistent manifest | PASS — automated tests cover platform-specific filenames, atomic round trips, ownership fields, missing/corrupt/schema-mismatch quarantine, and separate Windows/macOS records. |
| Dependency manager | PASS — automated tests cover ownership annotation, verified pinned installs, no-overwrite protection, digest-gated backfill, refusal of winget off Windows, and refusal to update unowned ani-cli. |
| ani-cli update safety | PASS — `AniCliService.update()` is a hard refusal; Settings uses the checksum-pinned ownership/manifest path or the new verified `AniCliUpdateService`, both restricted to ANIXO-owned copies. |
| Uninstall planner | PASS — automated tests cover app-only vs complete plans, no filesystem mutation during planning, unmanaged-dependency preservation, outside-root refusal, missing/corrupt manifest refusal, command planning, failure reporting, and config-pointer cleanup. |
| Uninstall CLI | PASS — automated tests cover all flags, dry-run JSON/text output, safety exit code 2, app-only data preservation, complete removal, and pending-request round trips. |
| `--record-install` | PASS — installer CLI writes a platform manifest with install kind, version, path and ownership metadata. |
| Settings → System → Installation & uninstallation | PASS — automated Textual test opens the System category, enters the subsection, shows the Uninstall row, and returns with Esc. |
| Legacy migration safety | PASS — automated test: old AnimeHub folder remains intact, backup created, ANIXO data not overwritten. |
| First-launch wizard | PASS — automated Textual test opens the wizard with `force_setup=True`, navigates steps, and confirms Finish closes it and marks setup complete. |
| Discover filters | PASS — automated tests confirm Category, Genre, and Sort chips work by click and keyboard, reload results with the selected values, and pass category/genre/sort variables to AniList. |
| Settings Enter/focus | PASS — automated tests confirm settings rows render after route changes, Enter activates choice/bool/text rows without leaving Settings, text edits save/cancel correctly, and category/row clicks select the expected entries. |
| Settings confirmation actions | PASS — confirmation dialogs run in Textual workers, so destructive/setup actions show their modal and respond to Y/N/Esc instead of hanging or raising `NoActiveWorker`. |
| Multi-season selector | PASS — automated test confirms the franchise screen, exact provider ID, provider episode titles, and exact `-e` playback with `provider_id` and `resume=False`. |
| Conservative franchise grouping | PASS — related Overlord-style entries group without merging unrelated `One Piece` / `One Punch Man` titles; original titles and provider order are preserved. |
| Live provider parsing | PASS — live `Overlord` search returned 17 ordered results; `overlord-iv-1502` yielded 13 episode titles and the Sacred Kingdom movie yielded 1 episode. |
| Verified exact-ID launcher | PASS — the installed official v5.1.8 script's SHA256 matched a known digest, the patch applied cleanly, and `bash -n` accepted the generated script. |
| Pinned v5.1 exact-ID launcher | PASS — the pinned official v5.1 download matched `F99A1DAC…CD46`, accepted the same patch, passed `bash -n`, and resolved Overlord IV episode 1 in `debug` mode with no menu. |
| Live exact-ID playback resolution | PASS — patched launcher with `ANI_CLI_PROVIDER_ID=overlord-iv-1502`, `-S 1 -e 1` and `ANI_CLI_MENU=false` resolved the fourth-season episode in `debug` mode with no interactive menu. |
| Invalid provider ID | PASS — the same launcher exited with `Selected provider result is no longer available` and did not open a picker. |
| No title-only exact playback | PASS — `spawn(exact=True)` without a provider ID and a history entry without a provider ID both refuse to launch instead of opening ani-cli's picker. |
| Playback dependency preflight after setup | PASS — regression test runs `play_anime()` with `setup_completed=True`; real local config reports no missing required dependencies. |
| Update release parsing and channels | PASS — deterministic tests cover GitHub and manifest feeds, stable/beta filtering, TTL caching, offline fallback, platform/arch/kind asset selection, and rejection of malformed manifests, non-HTTPS hosts, and uninstaller/helper assets. |
| Staged download and package validation | PASS — deterministic tests cover streamed SHA-256 verification, size limits, ZIP path-traversal rejection, PE/DMG architecture checks, cancellation before download, helper selection, and fail-closed signature verification. |
| Helper staging, rollback, and data preservation | PASS — deterministic tests cover plan validation, checksum re-verification, parent-process wait, backup/overlay for installer/portable/`.app` modes, health check, rollback on failure, and preservation of data/config/dependency files. |
| Update center UI | PASS — automated Textual test checks all buttons, keyboard bindings, disabled states, release-notes and back actions, and route integration. |
| ani-cli automatic update | PASS — deterministic tests cover known/owned revision detection, upstream digest lookup with offline cache, owned-only staged replacement, backup/rollback, and refusal for user-managed copies. |
| In-CLI release manager | PASS — deterministic tests cover version discovery/increment, beta→stable phrase, preflight refusal paths, commit/push/tag order, workflow monitoring/failure resume, published manifest/checksum/URL verification, history, CLI dispatch, and release-repository derivation from the Git remote; no test contacts GitHub or mutates a real repository. |

## Release-manager verification (2026-11-25)

| Check | Result |
| --- | --- |
| `pytest -q tests/test_release_manager.py` | PASS — 22 passed. |
| `pytest -q tests/test_cli.py tests/test_release_manager.py` | PASS — 30 passed; existing CLI parsing/install/version tests unchanged. |
| `pytest -q` | PASS — **280 passed in 250.94s** on Windows 11, Python 3.12.10 after the episode-loading fix. |
| `compileall -q main.py config.py version.py services updater_helper.py screens packaging/make_release_manifest.py` | PASS — no syntax errors. |
| Git/gh/HTTP isolation | PASS — injected runner, confirmer, fetcher, head fetcher, clock, and sleep fakes; no token value is printed. |
| Release repository resolution with maintainer config | PASS — with `update_repo=fred8/anixo`, the manager resolved `origin` → `frederik461/anixo` and `gh` authentication passed. |
| Failed beta.2 workflow | ANALYZED — Windows failed because the workflow's `version.py` regex did not match `__version__ = "..."`. Both macOS jobs failed because release-version bumping rewrote hardcoded test constants (including a beta counter expected as a tuple integer), so version-sensitive tests now derive from `version.VERSION` or use fixed non-product fixtures. No tag was deleted or recreated. |
| Workflow-run discovery | FIXED — tagged-run lookup retries for GitHub's short registration delay instead of failing immediately after pushing the tag. |
| Release workflow version check | FIXED — `.github/workflows/release.yml` now matches the real `__version__ = "..."` declaration, with a test that reads both real files. |
| Piped-console release output | FIXED — release output is ASCII-only with a cp1252 fallback, so automated `yes`-piped runs cannot fail on Unicode before preflight. |
| Local packaging preflight | FIXED — stale/partial `dist/release/SHA256SUMS.txt` is now `NOT TESTED`, not a mandatory failure; CI rebuilds and verifies the published assets from the new tag. |
| Update-screen initial button state flake | FIXED — `test_update_screen_buttons_are_mouse_and_keyboard_accessible` now waits for the async mount state instead of asserting after one `pilot.pause()`. |
| Existing Textual timing flake | FIXED — `test_watch_without_history_opens_selector` now waits for the exclusive watch worker's screen state instead of relying on one `pilot.pause()` under full-suite load. |
| Real GitHub tag, workflow run, and published release | PASS — the manager pushed `v0.1.0-beta.5`, found run `45679602164`, and all four jobs succeeded (Windows x64, macOS arm64, macOS x86_64, aggregated publish). Verification passed for release metadata, 12 assets, HTTPS asset URLs, manifest schema/channel/7 pinned assets, 10 checksum entries, and agreement between GitHub, the manifest, and `SHA256SUMS.txt`. The configured beta updater also found beta.4 with 12 assets. Real-terminal install/update behaviour remains a manual test. |

## Episode-loading fix (2026-11-25)

| Check | Result |
| --- | --- |
| Full regression suite | PASS — **280 passed in 250.94s** on Windows 11, Python 3.12.10 after the fix. |
| Stale `RELEASING` metadata | FIXED — AniList details are refreshed when airing progress is unknown; a newly finished series now copies `FINISHED` status even when `nextAiringEpisode` is absent, so its planned episodes are no longer capped at zero. |
| Details → Select Episode | FIXED — Select Episode now resolves provider results and the exact season before opening the browser, and skips the resume prompt. Provider episode rows remain authoritative over AniList's planned count. |
| Provider lookup failures | FIXED — failures are shown in the episode list instead of being silently converted to an empty page. The list shows a visible loading state, an actionable error message, and an `R` retry path; direct numeric playback still works. |
| Automated coverage | PASS — new tests cover stale-to-finished metadata, resolved provider browsing without a resume prompt, provider failure messaging, and retry recovery. |
| macOS compatibility of this fix | PASS — the `v0.1.0-beta.5` tagged workflow ran the same 280-test suite on both GitHub-hosted macOS runners and all build/publish jobs succeeded. |

## In-app updater fix (2026-11-25)

| Check | Result |
| --- | --- |
| Root cause | FOUND — the Windows installer is built by Inno Setup, whose launcher PE reports `x86` even for an x64 application. The updater rejected the verified setup executable with `Windows asset architecture x86 does not match x86_64` after the download had already passed checksum verification. |
| Installer validation | FIXED — setup/installer executables are still checked as valid PE files and still constrained to the correct platform, but their 32-bit launcher architecture is no longer mistaken for the installed app architecture. Portable and updater-helper executables remain strict about exact architecture. |
| Error visibility | FIXED — prepare and helper-launch failures are written to `update.log` and shown for 10 seconds instead of a brief 3-second notification. HTTP 403 is reported as a rate-limit with a retry suggestion instead of a misleading private-repository message. |
| Settings update button | FIXED — `action_update_anixo` was synchronous while both Settings and Updates awaited it, causing `TypeError: object NoneType can't be used in 'await' expression`. It is now a coroutine, with an automated regression check. |
| Preflight rendering test | FIXED — the render-test caption assertion now waits for `PosterLoaded` text instead of assuming image widgets and captions update in the same frame. The test passed five consecutive isolated runs. |
| Reproduced failure | PASS — replaying the cached public release through the old validation produced the exact `x86 does not match x86_64` error; after the fix the same release prepared successfully with a verified plan and helper path. |
| Automated coverage | PASS — `pytest -q tests/test_updates.py tests/test_app.py tests/test_rendering.py` → **54 passed**; full suite → **282 passed in 260.46s**. New tests cover a 32-bit Inno Setup launcher, strict helper architecture checking, HTTP 403 messaging, and the awaitable update action. |
| Installed beta.3 self-update | BLOCKED until a new build is installed manually — the validation bug lives in the already installed client, so a release published afterward cannot repair that client through its own update button. Future in-app updates work from the fixed build onward. |

## Episode-release, mouse, and performance work (2026-11-24)

| Check | Result |
| --- | --- |
| Full regression suite after all changes | PASS — **251 passed** on Windows 11, Python 3.12.10. |
| Released-episode behavior | PASS — provider rows are authoritative and uncapped; AniList `nextAiringEpisode` is used only as a fallback cap for generated lists; `RELEASING` with no airing info shows locally known progress only; manual episode jump remains. Live check: planned 25, next airing 9, provider released 8 — both paths showed 1–8. |
| Mouse interaction tests | PASS — 8 new tests cover nav/back/forward history, nav clicks while typing, card/poster/Watch clicks, a dedicated Discover cover click, keyboard-then-mouse mixing, season/episode cells, right-click rejection, wheel scrolling without selection changes, history rows, confirm/help buttons, and uninstall mode options. |
| Pointer bug found by the new tests | FIXED — `DiscoverScreen`, `DashboardScreen`, and `WatchlistScreen` referenced `Message.sender`, which does not exist in Textual 8.2.8. Custom `Chosen` messages now carry an explicit `source` widget. |
| Unsupported CSS pseudo-class | FIXED — Textual 8.2.8 rejects `:active`; hover/focus/disabled states are used instead and the full suite loads the stylesheet. |
| Performance profiling | PASS — `benchmarks/perf_bench.py` stubs network services and records import, warm/cold startup, details, search, episodes, duplicate requests, cached posters, history lookup, and fixed-workload Python heap/CPU. |
| Deterministic hotspot improvements | PASS — identical concurrent searches went from 6 posts / 1982.08 ms to 1 post / ~30 ms; 200 cached-poster lookups from 27.20 ms to ~5 ms; 20 history lookups from 175.43 ms to ~0.5 ms. |
| UI-level speed claims | NOT CLAIMED — details/navigation/search/startup medians varied widely between runs and were dominated by fixed pilot pauses. Full table and caveats are in `benchmarks/README.md`. |
| Manual pointer checklist | WRITTEN, NOT MANUALLY RUN — `MOUSE_ACCESSIBILITY.md` must be completed on real terminals on Windows 10/11 and Apple Silicon/Intel macOS before release. |
| macOS compatibility of the new work | NOT TESTED — earlier macOS CI results predate these changes. |
| Cold/warm startup, interactive time, memory/CPU | PARTIAL — measured on Windows only; no macOS comparison and no OS-level RSS/CPU profiler was used. |

> **Intermittent test:** one older build run out of five failed
> `tests/test_rendering.py::test_render_test_screen_reports_geometry_and_survives_interaction`
> while other full-suite runs (including the recent full runs) passed. Treat
> that case as flaky under load and harden its timing assertions before a
> release; it is not evidence of a product regression.

Release artifacts present in `dist\release` after the final Windows rebuild:

```text
ANIXO-Setup-v0.1.0-beta.5.exe          26,862,948 bytes
ANIXO-Uninstaller-v0.1.0-beta.5.exe     7,611,817 bytes
ANIXO-Portable-v0.1.0-beta.5.zip       30,320,093 bytes
SHA256SUMS.txt
build-report.txt   (diagnostic log, not a release asset)
```

Final SHA256 values recorded in `dist\release\SHA256SUMS.txt`:

```text
69fbc56a2572e48f82e828231920914b89f5cf86e9f3a0c02f5ae75eb0c99366  ANIXO-Portable-v0.1.0-beta.5.zip
0a183f7b8b53013e3111742ce68e180ce71afda8a265d077b91380e00bd0ec23  ANIXO-Setup-v0.1.0-beta.5.exe
d7cc83c3eabca6884e5765ae9ea5f061462abb9c95795c79d8a3485896ed6b32  ANIXO-Uninstaller-v0.1.0-beta.5.exe
```

macOS artifacts are **not produced locally on this Windows host**. They were
built on GitHub-hosted macOS runners, downloaded here, and re-hashed, but no DMG
has been opened or installed as part of these local checks.

## GitHub-hosted macOS CI results

The final `macOS CI` run was
[44929838262](https://github.com/Frederik461/anixo/actions/runs/44929838262)
on commit `f68b824`. Both jobs completed with `success`:

| Item | macOS ARM64 (`macos-15`) | macOS Intel (`macos-15-intel`) |
| --- | --- | --- |
| Full pytest suite | 202 passed, 1 skipped | 202 passed, 1 skipped |
| Platform tests | 15 passed | 15 passed |
| Build successful | VERIFIED | VERIFIED |
| Architecture verified | VERIFIED — 85 Mach-O files, all `arm64`, no extra architectures | VERIFIED — 85 Mach-O files, all `x86_64`, no extra architectures |
| Packaging verified | VERIFIED | VERIFIED |
| Runtime/uninstall checks | VERIFIED | VERIFIED |
| Code signing verified | UNVERIFIED — no Apple certificate secret | UNVERIFIED — no Apple certificate secret |
| Notarization verified | UNVERIFIED — no notary credentials | UNVERIFIED — no notary credentials |
| Real Finder launch | NOT TESTED (headless CI) | NOT TESTED (headless CI) |
| Real Gatekeeper installation | NOT TESTED (headless CI) | NOT TESTED (headless CI) |
| Real playback | NOT TESTED (headless CI) | NOT TESTED (headless CI) |

Packaging verification covered the `.app` structure, `Info.plist` identifier
`io.anixo.app`, minimum macOS 13.0, bundled theme/icon/CA resources under
`Contents/Resources/Anixo/_internal`, 147 dynamic dependencies across 84
binaries, no Windows-only artifacts, DMG mount/contents, checksums, and the
standalone uninstaller's `--version`/`--help` checks. Seven absolute OS library
paths were accepted as macOS dyld shared-cache dependencies; this is expected on
macOS 13+ and is recorded as a warning, not as a missing bundled library.

Runtime validation on both architectures verified packaged startup/shutdown,
custom data/cache/log paths, installation ownership, dependency detection,
app-only uninstall preserving user data, complete uninstall removing personal
data, outside sentinels surviving, and a pre-existing dependency surviving.

Downloaded CI artifacts were re-hashed locally and matched their staged
checksum files:

| Artifact | Size | SHA256 |
| --- | ---: | --- |
| `ANIXO-macOS-ARM64.dmg` | 32,579,806 | `02971ff27102dff4dcc6c6efa558acb96cbf184fffad707d61be4449f8427528` |
| `ANIXO-Uninstaller-macOS-ARM64` | 8,292,704 | `23fc872b164b5a19bfb1eac17ef8de5dbc2d4c243699eb3c8803d8bc5935f075` |
| `ANIXO-macOS-Intel.dmg` | 34,648,159 | `455e042b4942ceeef945dfe6bee8650e5223a97c7cc7d9c3b772dc3fc385bade` |
| `ANIXO-Uninstaller-macOS-Intel` | 8,823,552 | `4845e31e448ebf0e53b37c56781f3fd9764c3cd6974658d5cb74380d25487272` |

These are CI artifacts, not public-release assets. The Intel DMG remains
CI-validated only; no public Intel asset is attached and no clean Intel Mac,
Finder, Gatekeeper, or playback validation has been performed.

## Release checklist

Status legend: **VERIFIED** (ran locally), **UNKNOWN** (not run — do not
assume it works), **CI-ONLY** (script exists and runs on a macOS runner, not
validated on a user Mac).

| # | Item | Status | Notes |
| --- | --- | --- | --- |
| 1 | Dependency lock is reproducible (`uv lock --check`) | VERIFIED | Frozen lock used by the pipeline. |
| 2 | Pytest suite passes | VERIFIED | 251 passed locally on Windows on 2026-11-25; 202 passed, 1 skipped on the earlier macOS CI runners. |
| 3 | Icons and `.icns` source generated | VERIFIED | ICO frames and `anixo.png` confirmed locally. |
| 4 | `anixo.spec`, `uninstaller.spec` build | VERIFIED | Windows one-folder and standalone uninstaller built. |
| 5 | Packaging PowerShell scripts parse and execute | VERIFIED | Full pipeline completed with no `SKIP`/`FAIL`. |
| 6 | Inno Setup script compiles | VERIFIED | Real setup.exe produced. |
| 7 | `hash.ps1` writes SHA256SUMS.txt | VERIFIED | Three release binaries re-hashed and matched. |
| 8 | `validate_build.ps1` fails loudly on a missing EXE | VERIFIED | Non-zero exit confirmed during development. |
| 9 | One-folder build produces `dist\Anixo\Anixo.exe` | VERIFIED | Built and validated. |
| 10 | One-file validation build produces `Anixo.exe` | VERIFIED | Validation-only artifact, not shipped. |
| 11 | `--version` prints `ANIXO 0.1.0-beta.5` | VERIFIED | Source and frozen builds. |
| 12 | `--help` shows the ANIXO identity | VERIFIED | Source and frozen builds. |
| 13 | `--diagnose` exits 0 with a useful report | VERIFIED | Full redacted report in both app builds. |
| 14 | `styles/theme.tcss` and `assets/anixo.ico` are inside the bundle | VERIFIED | Found under `_internal`. |
| 15 | HTTPS metadata works using the bundled `certifi` CA file from a packaged build | UNKNOWN | Source-level network checks passed; clean-machine packaged fetch not yet run. |
| 16 | Installer uses `%LOCALAPPDATA%\Programs\ANIXO`, Start Menu, optional desktop icon | VERIFIED | Default path in script; `/NOICONS` suppressed both real and virtual Start Menu folders in the silent validation. |
| 17 | Install/uninstall preserves settings, history, watchlist and progress | VERIFIED | Silent install/uninstall kept a sentinel in the redirected data folder; automated app-only uninstall tests also pass. Clean-VM re-test pending. |
| 18 | Ownership manifest created by installer and consumed by standalone uninstaller | VERIFIED | `--record-install` test plus observed Inno `[Run]`/`[UninstallRun]` execution in the silent validation. Clean-VM re-test pending. |
| 18a | Custom install-time data paths replayed by OS uninstall | VERIFIED | Silent validation: `uninstall-paths.ini` recorded custom data/cache/log dirs, the wrapper set `ANIXO_*`, removed the manifest/cache/logs, and Inno removed the INI and empty app folder. |
| 18b | In-app handoff preserves custom data/cache/log/config paths | VERIFIED | Pending request records all paths and the standalone CLI restores them when `--execute-pending` is used. |
| 18c | Uninstall progress and completion report | VERIFIED | Executor callback reports every selected item; CLI prints live progress plus explicit removed/kept/failed/manual sections. |
| 18d | GitHub Actions release workflow syntax and semantics | PARTIAL | PyYAML parse passed for the updated `release.yml` (3 jobs) and `macos.yml` (1 job); actionlint 1.7.12 passed on an earlier revision. The new helper/manifest/Intel steps and actionlint on the updated file have not been re-run in CI. |
| 18e | macOS CI matrix definition | VERIFIED | `macos.yml` parses as YAML and passes actionlint; run 44929838262 executed both `macos-15` arm64 and `macos-15-intel` jobs successfully with full tests, build, validators, and artifact upload. |
| 18f | macOS platform-specific tests | VERIFIED | New tests cover macOS adapter paths, app startup/quit, pinned dependency ownership, winget refusal, both uninstall modes, outside sentinels, and pre-existing dependency preservation. |
| 19 | Portable launcher redirects data to `%LOCALAPPDATA%\ANIXO\Portable` | VERIFIED | Launcher variables verified; live first-run write from packaged ZIP not executed locally. |
| 20 | Clean Windows 11 x64 machine with no Python/Git/uv, plus playback tools and terminal rendering | UNKNOWN | **Requires a clean VM.** Includes wizard, winget dependency setup, ani-cli/mpv playback and Sixel/half-cell rendering from the packaged build. |
| 21 | Windows 10 x64 clean install | UNKNOWN | Not run. |
| 22 | macOS 15 ARM64 and Intel `.app`/DMG CI build | VERIFIED IN CI | Both jobs in run 44929838262 passed build, tests, architecture, packaging, runtime/uninstall validation, and artifact upload. Not run on a clean Mac. |
| 23 | macOS Finder launch opens a usable terminal session | UNKNOWN | Bundled launcher writes a Terminal.app `open` command but has not been executed. |
| 24 | macOS Gatekeeper/quarantine, signing and notarization | UNKNOWN | Depends on Apple credentials and a real Mac; unsigned fallback is documented. |
| 25 | Intel Mac build/runtime | UNKNOWN | CI artifacts and the release workflow now produce an x86_64 DMG, but no Intel hardware, Finder, Gatekeeper, or playback validation has been performed. |
| 26 | Sixel/TGP terminals beyond the locally confirmed Windows Terminal | UNKNOWN | Requires real terminal testing. |
| 27 | Direct Bash ani-cli playback path on macOS | UNKNOWN | Not executed on macOS. |
| 28 | Real GitHub Releases update download/launch against a published release | UNKNOWN | Parsing, selection, digest checks, staging, helper launch, and rollback are covered by deterministic tests, but no published release has been downloaded or applied end-to-end. The update feed URL must be supplied/published before testers can use it. |
| 29 | Season/movie/special selection stays inside ANIXO | VERIFIED | Automated flow test plus live `debug` launcher check. |
| 30 | Exact provider ID survives selection → episode fetch → playback | VERIFIED | Provider parser rejects other IDs; spawn test asserts `provider_id`, `-S 1`, and `-e <n>`. |
| 31 | Remembered franchise selection is restored, not Season 1 | VERIFIED | Automated persistence test and selector preselection by exact source ID. |
| 32 | macOS CI: build successful status | VERIFIED IN CI | Both jobs in run 44929838262 reported VERIFIED. |
| 33 | macOS CI: automated tests successful status | VERIFIED IN CI | 202 passed, 1 skipped plus 15 platform tests on each runner. |
| 34 | macOS CI: architecture verified | VERIFIED IN CI | 85 Mach-O files checked per job; ARM64 all `arm64`, Intel all `x86_64`, with no additional architectures. |
| 35 | macOS CI: packaging verified | VERIFIED IN CI | `.app` structure, `Info.plist`, resources, dynamic libraries, DMG mount, uninstaller, and checksums passed on both runners. |
| 36 | macOS CI: code signing / notarization verified | UNVERIFIED | No Apple secrets were configured; both reports say UNVERIFIED rather than faking success. |
| 37 | macOS CI: Finder / Gatekeeper / real playback | NOT TESTED BY DESIGN | Headless GitHub runners cannot establish these; reports always mark them as not tested. |
| 38 | Manual mouse accessibility checklist | UNKNOWN | `MOUSE_ACCESSIBILITY.md` is written and automated event tests pass; no real-terminal run has been recorded on any platform. |
| 39 | Post-change Windows benchmark comparison | VERIFIED | Deterministic hotspot improvements recorded in `benchmarks/README.md`; UI-level timing noise is documented, not claimed as a speedup. |
| 40 | Post-change macOS benchmark comparison | UNKNOWN | Harness is portable but has not been run on Apple Silicon or Intel macOS. |

### macOS CI status reporting

`.github/workflows/macos.yml` uses separate `macos-15` (ARM64) and
`macos-15-intel` (x86_64) jobs. Each job runs `uv sync --locked`, installs the
build group, runs the complete pytest suite, then runs the platform-specific
tests. Only after those pass does it build the standalone app, `.app`, DMG, and
standalone uninstaller. `validate_macos_runtime.py` exercises the packaged CLI
with redirected data/cache/log paths, verifies installation ownership and
dependency detection, and tests both uninstall modes with outside sentinels and
a pre-existing dependency. `validate_macos_bundle.py` checks every bundled
Mach-O file for the target architecture, dynamic libraries, Windows-only
artifacts, bundle resources, DMG contents, checksums, and signing/notarization
status.

Each generated report distinguishes build, automated tests, architecture,
packaging, code signing, notarization, Finder launch, Gatekeeper installation,
and real playback. The last three are always reported as not tested. DMG and
checksum artifacts are uploaded only from a fully passing job; build reports and
logs are uploaded even on failure. Run 44929838262 executed the workflow on
both runners and completed successfully; the generated reports and checksums
are summarised above.

Additional unknowns: SmartScreen reputation of an unsigned installer; real
network-dependent playback after upstream ani-cli/AniList changes; and Finder
launch behaviour when no terminal application is available.

## What to run before publishing a release

From the repository root, on a machine with Python 3.12, uv and Inno Setup 6:

```powershell
uv lock --check
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

Then, on a **clean Windows VM** with no developer tools installed:

1. Run `ANIXO-Setup-v0.1.0-beta.5.exe`; confirm the default install folder.
2. Launch ANIXO from the Start Menu and the optional desktop shortcut.
3. Complete the setup wizard and confirm `setup_completed` is stored.
4. Run `--version`, `--help`, and `--diagnose` from the install folder.
5. Confirm artwork loads and exact-episode playback works after the wizard
   installs/validates Git Bash, ani-cli and mpv.
6. Open Settings → System → Installation & uninstallation and confirm the
   manifest path and owned-component count.
7. Choose **Remove ANIXO only**, let the standalone uninstaller finish, and
   confirm settings/history/watchlist/progress remain.
8. Reinstall, choose **Complete removal**, confirm the final warning, and check
   that program files, managed dependencies, caches and personal data are gone.
9. Extract the portable ZIP, start it with `START ANIXO.cmd`, and confirm its
   data goes to `%LOCALAPPDATA%\ANIXO\Portable`.
10. Verify the SHA256 hashes of all downloaded assets.

On a **clean macOS 13+ arm64 Mac**:

1. Verify the DMG checksum, open it, and drag ANIXO.app to Applications.
2. Resolve any Gatekeeper prompt and launch from Applications.
3. Run `--version`, `--help`, and `--diagnose` from the bundled binary.
4. Confirm metadata/artwork load and, if you supplied playback tools, that
   exact-episode playback works.
5. Test both uninstall modes and confirm that Apple Silicon paths under
   `~/Library` are handled correctly.
6. Record the signing/notarization result before publishing release notes.
