# ANIXO

**A modern anime discovery and streaming application.**  
*Your anime. Your world.*

ANIXO is a terminal application with packaged builds for **Windows x64** and
**macOS 13+ Apple Silicon**. Windows builds are validated locally. GitHub
Actions builds and tests separate Apple Silicon (`macos-15`) and Intel
(`macos-15-intel`) targets, uploading each architecture's DMG only when that
job passes. This is headless CI, not clean-Mac, Finder, Gatekeeper, or real
playback validation. Public release assets remain Apple Silicon only until an
Intel Mac has been independently tested. The deep-charcoal, purple, and
electric-blue interface, Sixel-first poster pipeline, and exact playback flow
are unchanged from the original design.

The product name and executable are ANIXO / `Anixo.exe` (Windows) or
`Anixo` inside the macOS app bundle.

---

## Highlights

- Dashboard with hero, continue-watching, trending cards, and responsive layout
- Discover with search, filters, genres, sorting, and grid/list views
- Anime details with source-derived episode counts only — no invented episode metadata
- Conservative franchise grouping with a custom season/movie/special selector and remembered choices
- Custom episode selector and exact `-e <n>` playback by exact provider ID, never ani-cli's interactive picker
- SUB/DUB preference (`U`) persisted as `preferred_audio`; `--dub` is used only for dub
- Per-episode state: unwatched / watching / completed, with explicit unwatch support
- Watchlist, history, resume prompts, and metadata caching
- Sixel-first artwork with TGP and labeled fallbacks, exact-size LANCZOS processing, original preservation, and stale-result rejection
- First-launch setup wizard, legacy data migration, diagnostics screen, and GitHub Releases update checker
- Centralized dependency manager with verified official sources, ownership records, and no silent overwrites
- Persistent, platform-specific installation manifest with safe app-only or complete uninstall
- Windows per-user installer, portable bundle, standalone uninstaller, and per-architecture macOS CI/DMG pipeline

## Verified status (2026-11-21)

- Windows: full local pipeline passed with **198 tests passed**; after the episode-release, mouse-accessibility, and performance work the full suite is **217 passed** (2026-11-25). Silent install/uninstall validation passed.
- macOS: [CI run 44919795580](https://github.com/Frederik461/anixo/actions/runs/44919795580) passed on both `macos-15` (ARM64) and `macos-15-intel` (Intel): build, **201 passed / 1 skipped** tests, architecture, packaging, and runtime/uninstall validation. The newer mouse-interaction tests have not been run on a Mac yet.
- Signing and notarization are **UNVERIFIED** because no Apple secrets were configured. Real Finder launch, Gatekeeper installation, and playback are **NOT TESTED** by headless CI. Intel DMGs are CI artifacts only, not public-release assets.

---

## Quick start

### Windows installed build

1. Download `ANIXO-Setup-v<version>.exe` from GitHub Releases.
2. Run it. No Python, pip, uv, Git, Visual Studio, or manual dependency install
   is needed for the app itself. No administrator rights are required.
3. Start ANIXO from the Start Menu or optional desktop shortcut.
4. Complete the first-launch setup. The wizard can install the pinned ani-cli
   and mpv through winget at user scope if you want playback.
5. Search, open details, choose the exact season/movie/special and episode, then watch.

### macOS Apple Silicon build

1. Download `ANIXO-v<version>-macos-arm64.dmg` and verify its checksum.
2. Open the DMG and drag **ANIXO.app** to Applications.
3. Launch ANIXO; it opens in a Terminal window because it is a text interface.
4. Complete the setup wizard and provide official mpv/ani-cli builds yourself if
   you want playback. Homebrew is not required.

### Windows portable build

Extract `ANIXO-Portable-v<version>.zip` and run `START ANIXO.cmd`. Portable mode
stores data under `%LOCALAPPDATA%\ANIXO\Portable` by default. See
`INSTALLATION.md` for differences.

### Source checkout

For development with Python 3.12 and uv:

```sh
uv sync --frozen --group build
uv run python main.py --diagnose
uv run python main.py
```

`run.bat` provides a Windows pip-based bootstrap for a local checkout, and
`launch.bat` skips the install step on normal starts.

---

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `D` / `G` / `W` / `H` / `S` | Home / Discover / Watchlist / History / Settings |
| `Tab` / `Shift+Tab` | Next / previous screen |
| `Alt+←` / `Alt+→` | Back / forward through primary screens (also the top-bar arrows) |
| `←` / `→` | Move selection (switch franchise group in the season browser) |
| `↑` / `↓` | Section, page, or row navigation |
| `Enter` | Watch, resume, or open |
| `E` | Exact episode selector |
| `A` | Add/remove watchlist |
| `M` | Mark watched / unwatched |
| `Shift+M` | Explicitly unwatch |
| `U` | Toggle SUB/DUB in the episode browser |
| `/` | Search or filter |
| `C` / `[` / `]` / `S` | Discover: cycle category / previous-next genre / sort (the filter chips are also clickable) |
| In Settings: `←/→`, `↑/↓`, `Enter`, `Esc` | Change category, select setting, edit/toggle, cancel edit or return from a subcategory |
| `V` | Image rendering test |
| `X` | Diagnostics |
| `?` / `F1` | Help |
| `Q` | Quit |

## Mouse and pointer access

Cards, posters, nav items, filter chips, settings/history rows, season and
episode cells, action buttons, dialog buttons, and the top-bar back/forward
arrows are all clickable. Left-click activates; right-click never changes a
selection. Hover, focus, selected, and disabled states are styled, and mouse
wheel scrolling never selects an episode. Keyboard shortcuts remain authoritative
and can be mixed with clicks. See `MOUSE_ACCESSIBILITY.md` for the manual
checklist and `tests/test_mouse_interactions.py` for automated coverage.

---

## Data and migration

ANIXO keeps install files and user data separate:

| Item | Windows | macOS |
| --- | --- | --- |
| Config / database / ownership manifest | `%APPDATA%\ANIXO` | `~/Library/Application Support/ANIXO` |
| Cache | `%LOCALAPPDATA%\ANIXO\cache` | `~/Library/Caches/ANIXO` |
| Logs | `%LOCALAPPDATA%\ANIXO\logs` | `~/Library/Logs/ANIXO` |
| Portable data | `%LOCALAPPDATA%\ANIXO\Portable` | Not applicable |
| Installed build | `%LOCALAPPDATA%\Programs\ANIXO` | `/Applications/ANIXO.app` |

Legacy environment variables `ANIME_HUB_DATA_DIR` and `ANIME_HUB_CONFIG` remain
supported as aliases. New variables are `ANIXO_DATA_DIR`, `ANIXO_CONFIG`,
`ANIXO_CACHE_DIR`, `ANIXO_LOG_DIR`, and `ANIXO_UPDATE_REPO`.

The setup wizard detects legacy AnimeHub data and can offer a backed-up
migration. It never deletes the old folder and never overwrites existing ANIXO
history or watchlist data.

---

## Playback model

ANIXO controls discovery → details → season/movie/special selection → episode
selection → playback:

- Search results are grouped conservatively into franchises; original titles and provider order are preserved.
- The season/movie/special browser shows the exact provider result ID, format, episode count, and available synopsis. Your last choice is remembered per franchise and is preselected on return.
- Episodes are read from the exact selected provider ID and displayed with their real titles when the provider supplies them. Provider rows are authoritative and are never capped by AniList's planned total; when the provider list is unavailable, a `RELEASING` series with a known `nextAiringEpisode` shows only episodes through `episode - 1`, and the manual episode jump is always retained.
- A checksum-verified copy of the official ani-cli script is patched in ANIXO's runtime cache to accept that exact provider ID. `ANI_CLI_MENU=false` makes any attempt to open ani-cli's picker fail instead of prompting.
- `AniCliService.spawn(..., episode="<n>", resume=False)` sends `-e <n>` together with `-S 1` for the single pinned provider result.
- `-c` is never combined with an exact episode.
- `--dub` is sent only when dub is selected.
- ani-cli's own interactive anime and episode pickers are never entered, and ani-cli's `-U` updater is never used.

The setup wizard can install the official pinned ani-cli script after SHA256
verification and install mpv through winget at user scope on Windows. On
macOS, provide official playback tools yourself. Nothing is installed silently.

---

## Diagnostics, updates, and uninstalling

Press `X` for a redacted environment report showing version, OS, architecture,
install/data paths, runtime status, playback status, image protocol, network
status, and cache counts.

Press `U` (or Tab to **Updates**, or Settings → Updates → **Open update
center**) for the Update Center. It supports both buttons and keyboard shortcuts
(`C` check, `U` update, `X` cancel, `R` restart + install, `N` release notes).

Settings → Updates holds the stable/beta channel, automatic-check toggle,
current/latest versions, status, check/update/cancel/restart actions, release
notes, and optional feed/manifest fields. Checks are cached and channel-aware:
stable users never receive beta releases. Default `update_repo` is
`Frederik461/anixo`; a custom HTTPS feed can be set with `update_feed_url` and
optionally signed with `update_public_key` (OpenSSL). No credentials are stored.

Downloads are accepted only over HTTPS from GitHub Releases assets or the
configured feed, are pinned to SHA-256 digests, and their package structure is
validated before use. A separate updater helper waits for ANIXO to exit, backs
up the old installation, applies the staged package, health-checks the new
build, and rolls back automatically on failure. Cancellation works until
installation begins. Settings, history, watchlist, progress, config,
user-installed dependencies, shortcuts, and uninstall registration are never
deleted. Redacted update events are written to the data directory.

### Releases from the CLI

Maintainers publish from the repository with the integrated release manager:

```text
Anixo release beta               # 0.1.0-beta.4 → 0.1.0-beta.4
Anixo release stable             # 0.1.0-beta.4 → 0.1.0 (requires typing "stable")
Anixo release check [tag]        # resume workflow/asset verification for an existing tag
Anixo release history [limit]    # list local tags and GitHub Releases
```

The manager prints the current/target version, channel, branch, tests, workflow
status, and final updater-test readiness. Before changing anything it verifies
the repository/remote, release branch, clean work tree, pushed HEAD, available
tag, `gh` authentication, workflow, required tools, full test suite, Python
compilation, manifest schema, dependency lock, and packaging references. It
never stashes, resets, discards changes, creates a GitHub Release itself,
bypasses the workflow, or overwrites a tag. Version references are discovered
from tracked files and updated together. Beta publication needs an explicit
yes; beta→stable additionally requires typing `stable`.

`.github/workflows/release.yml` remains the only publisher: the CLI commits
`Release v<version>`, pushes the configured branch, pushes the `v<version>` tag,
then monitors the exact tagged workflow run. Configure `release_repo`
(optional; derived from the Git remote when empty), `release_branch`,
`release_remote`, `release_workflow`, and `release_timeout_minutes` in
`config.json` if the defaults (`main`, `origin`, that workflow, 45 minutes) are
not appropriate. The updater's `update_repo` is deliberately separate and may
point at a tester's fork without affecting publication. Use
`--timeout-minutes MINUTES` on `beta`, `stable`, or `check` to override the wait
once.

Settings → Dependencies shows the detected ani-cli revision and ownership. A
verified ani-cli update re-downloads the official checksum-pinned script and is
refused for user-managed or unknown copies.

Settings → System → Installation & uninstallation shows the installation kind,
manifest path, and owned-component count. The **Uninstall ANIXO** action offers:

* **Remove ANIXO only** — keeps settings, history, watchlist, and progress;
* **Complete removal** — also deletes personal data after a final confirmation.

A standalone uninstaller receives a pending request containing the effective
data paths, waits for ANIXO to exit, and removes only components recorded as
ANIXO-owned. Its console shows live progress and a completion report with
failures and manual cleanup steps instead of deleting unknown files.

---

## Repository layout

- `app.py`, `main.py`, `config.py`, `version.py` — application shell and identity
- `platforms/` — Windows/macOS path, shell, launcher, and install-root adapters
- `screens/`, `components/`, `styles/` — Textual UI and ANIXO design system
- `services/` — AniList, artwork, ani-cli, dependency manager, manifest, uninstall, updater, diagnostics
- `data/` — SQLite persistence and migration
- `uninstaller.py`, `uninstaller*.spec` — standalone uninstaller
- `updater_helper.py` — separate staged-update/rollback process
- `anixo.spec`, `anixo_macos.spec`, `packaging/`, `installer/` — build and release scripts
- `packaging/make_release_manifest.py` — deterministic HTTPS asset manifest with digests
- `.github/workflows/release.yml` — separate Windows and macOS release jobs
- `.github/workflows/macos.yml` — macOS ARM64/Intel build, test, validation, and artifact matrix
- `packaging/validate_macos_bundle.py`, `packaging/validate_macos_runtime.py` — macOS CI validators
- `INSTALLATION.md`, `TESTING.md`, `RELEASES.md`, `TROUBLESHOOTING.md` — user and release documentation
- `MOUSE_ACCESSIBILITY.md`, `benchmarks/` — manual pointer checklist and repeatable offline performance harness

---

## Validation

Local validation commands:

```sh
uv lock --check
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q app.py main.py config.py version.py platforms services screens components data models tests
.venv\Scripts\python.exe main.py --version
.venv\Scripts\python.exe main.py --diagnose
.venv\Scripts\python.exe uninstaller.py --help
.\.venv\Scripts\python.exe benchmarks\perf_bench.py after
```

See `benchmarks/README.md` for the measured before/after report and its
caveats. UI-level timings are noisy; the deterministic request/cache/database
hotspots are the measured improvements.

`macos.yml` adds a separate macOS 15 ARM64 and Intel job. Each job runs the
full suite, builds the standalone app/`.app`/DMG, checks Mach-O architecture and
dynamic libraries, exercises the packaged uninstaller in both modes with
redirected user data and outside sentinels, and uploads a status report. Finder
launch, Gatekeeper installation, and real playback are explicitly reported as
not tested by CI.

See `TESTING.md` for the exact verified/unknown status, including the remaining
clean-machine, macOS runtime, signing, and terminal-protocol checks.
