# Testing status for ANIXO v0.1.0-beta.1

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
| `python -m pytest -q` | PASS — **198 passed** on real Windows; **197 passed, 1 skipped** under simulated macOS and on both GitHub-hosted macOS runners (the skip is the Windows-only cmd-wrapper integration test). |
| `python -m compileall` / `py_compile` over source, services, platforms, screens, tests | PASS — no syntax errors. |
| `uninstaller.py --version` / `--help` | PASS — standalone CLI starts and lists all options. |
| `assets/make_icons.py` | PASS — ICO files contain 16/32/48/64/128/256 px frames and `anixo.png` source for `.icns`. |
| `packaging/build_windows.ps1` (full pipeline, no skips) | PASS — all stages completed; four artifacts and hashes produced. |
| One-folder build (`anixo.spec`) | PASS — `dist\Anixo\Anixo.exe` produced and validated. |
| One-file validation build | PASS — `dist\onefile\Anixo.exe` produced and validated. |
| Standalone uninstaller build (`uninstaller.spec`) | PASS — `dist\ANIXO-Uninstaller.exe` produced; `--version` and `--help` exit 0. |
| `packaging/validate_build.ps1` on both app executables | PASS — `--version`, `--help`, `--diagnose` all exited 0. |
| Identity in frozen builds | PASS — version/help output contains ANIXO and `0.1.0-beta.1`. |
| Bundled resources | PASS — `styles/theme.tcss` and `assets/anixo.ico` found under `dist\Anixo\_internal`. |
| Full diagnostics report | PASS — `--diagnose` prints version, OS, architecture, install/data/log paths, Python mode, playback tools, image protocol, network status and cache counts. |
| Diagnostics redaction | PASS — automated test confirms tokens, passwords, bearer headers and URL credentials are replaced with `<redacted>` before display/export. |
| Inno Setup compile | PASS — `ANIXO-Setup-v0.1.0-beta.1.exe` built by ISCC 6.7.3. |
| Silent install and OS-level uninstall with redirected user data | PASS — `/VERYSILENT /NOICONS` install into a temp `/DIR`, then silent `unins000.exe`; manifest located, custom paths recorded and replayed, cache and logs removed, sentinel data preserved, empty program folder and `uninstall-paths.ini` removed, real `%APPDATA%\\ANIXO` and `%LOCALAPPDATA%\\ANIXO` unchanged. |
| Portable ZIP | PASS — includes `Anixo.exe`, `ANIXO-Uninstaller.exe`, `START ANIXO.cmd`, `PORTABLE.md`, `PORTABLE.txt` and `_internal\styles\theme.tcss`. |
| Portable data redirection | PASS — launcher sets `ANIXO_DATA_DIR`, `ANIXO_CACHE_DIR` and `ANIXO_LOG_DIR` to `%LOCALAPPDATA%\ANIXO\Portable`. |
| SHA256SUMS.txt | PASS — setup, portable ZIP and standalone uninstaller re-hashed locally and matched. |
| Platform adapters | PASS — automated tests cover Windows/macOS/Linux data, cache, log and state paths, env overrides, shell conversion, installer roots, and portable trust. |
| Persistent manifest | PASS — automated tests cover platform-specific filenames, atomic round trips, ownership fields, missing/corrupt/schema-mismatch quarantine, and separate Windows/macOS records. |
| Dependency manager | PASS — automated tests cover ownership annotation, verified pinned installs, no-overwrite protection, digest-gated backfill, refusal of winget off Windows, and refusal to update unowned ani-cli. |
| ani-cli update safety | PASS — `AniCliService.update()` is a hard refusal; Settings uses the checksum-pinned manager path instead. |
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

> **Intermittent test:** one older build run out of five failed
> `tests/test_rendering.py::test_render_test_screen_reports_geometry_and_survives_interaction`
> while other full-suite runs (including the recent full runs) passed. Treat
> that case as flaky under load and harden its timing assertions before a
> release; it is not evidence of a product regression.

Release artifacts present in `dist\release` after the final Windows rebuild:

```text
ANIXO-Setup-v0.1.0-beta.1.exe          26,862,948 bytes
ANIXO-Uninstaller-v0.1.0-beta.1.exe     7,611,817 bytes
ANIXO-Portable-v0.1.0-beta.1.zip       30,320,093 bytes
SHA256SUMS.txt
build-report.txt   (diagnostic log, not a release asset)
```

Final SHA256 values recorded in `dist\release\SHA256SUMS.txt`:

```text
69fbc56a2572e48f82e828231920914b89f5cf86e9f3a0c02f5ae75eb0c99366  ANIXO-Portable-v0.1.0-beta.1.zip
0a183f7b8b53013e3111742ce68e180ce71afda8a265d077b91380e00bd0ec23  ANIXO-Setup-v0.1.0-beta.1.exe
d7cc83c3eabca6884e5765ae9ea5f061462abb9c95795c79d8a3485896ed6b32  ANIXO-Uninstaller-v0.1.0-beta.1.exe
```

macOS artifacts are **not produced locally on this Windows host**. They were
built on GitHub-hosted macOS runners, downloaded here, and re-hashed, but no DMG
has been opened or installed as part of these local checks.

## GitHub-hosted macOS CI results

The final `macOS CI` run was
[44917520313](https://github.com/Frederik461/anixo/actions/runs/44917520313)
on commit `06b6e5b`. Both jobs completed with `success`:

| Item | macOS ARM64 (`macos-15`) | macOS Intel (`macos-15-intel`) |
| --- | --- | --- |
| Full pytest suite | 197 passed, 1 skipped | 197 passed, 1 skipped |
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
| `ANIXO-macOS-ARM64.dmg` | 32,576,136 | `c2698cfcf5c97b8583f385ea137fe241572be82eb0fe5d6b6d936de4137f5619` |
| `ANIXO-Uninstaller-macOS-ARM64` | 8,292,560 | `f5fdc04bdbf56f2b6de1549e05552bb52af0692d9a097e67f0ef6d78c12459b7` |
| `ANIXO-macOS-Intel.dmg` | 34,646,955 | `448eab8fe82059e9bab58fea9272e5c28f2a1e6b0918949e11886721093a7278` |
| `ANIXO-Uninstaller-macOS-Intel` | 8,823,632 | `a71bf7d1bf5523e8368f7c3aa4c453296daa39043617b78b145d5bedfdc7670a` |

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
| 2 | Pytest suite passes | VERIFIED | 198 passed locally on Windows; 197 passed, 1 skipped on both macOS CI runners. |
| 3 | Icons and `.icns` source generated | VERIFIED | ICO frames and `anixo.png` confirmed locally. |
| 4 | `anixo.spec`, `uninstaller.spec` build | VERIFIED | Windows one-folder and standalone uninstaller built. |
| 5 | Packaging PowerShell scripts parse and execute | VERIFIED | Full pipeline completed with no `SKIP`/`FAIL`. |
| 6 | Inno Setup script compiles | VERIFIED | Real setup.exe produced. |
| 7 | `hash.ps1` writes SHA256SUMS.txt | VERIFIED | Three release binaries re-hashed and matched. |
| 8 | `validate_build.ps1` fails loudly on a missing EXE | VERIFIED | Non-zero exit confirmed during development. |
| 9 | One-folder build produces `dist\Anixo\Anixo.exe` | VERIFIED | Built and validated. |
| 10 | One-file validation build produces `Anixo.exe` | VERIFIED | Validation-only artifact, not shipped. |
| 11 | `--version` prints `ANIXO 0.1.0-beta.1` | VERIFIED | Source and frozen builds. |
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
| 18d | GitHub Actions release workflow syntax and semantics | VERIFIED | PyYAML parse and actionlint 1.7.12 passed; aggregated checksums use bare filenames and all six published assets are checked. |
| 18e | macOS CI matrix definition | VERIFIED | `macos.yml` parses as YAML and passes actionlint; run 44917520313 executed both `macos-15` arm64 and `macos-15-intel` jobs successfully with full tests, build, validators, and artifact upload. |
| 18f | macOS platform-specific tests | VERIFIED | New tests cover macOS adapter paths, app startup/quit, pinned dependency ownership, winget refusal, both uninstall modes, outside sentinels, and pre-existing dependency preservation. |
| 19 | Portable launcher redirects data to `%LOCALAPPDATA%\ANIXO\Portable` | VERIFIED | Launcher variables verified; live first-run write from packaged ZIP not executed locally. |
| 20 | Clean Windows 11 x64 machine with no Python/Git/uv, plus playback tools and terminal rendering | UNKNOWN | **Requires a clean VM.** Includes wizard, winget dependency setup, ani-cli/mpv playback and Sixel/half-cell rendering from the packaged build. |
| 21 | Windows 10 x64 clean install | UNKNOWN | Not run. |
| 22 | macOS 15 ARM64 and Intel `.app`/DMG CI build | VERIFIED IN CI | Both jobs in run 44917520313 passed build, tests, architecture, packaging, runtime/uninstall validation, and artifact upload. Not run on a clean Mac. |
| 23 | macOS Finder launch opens a usable terminal session | UNKNOWN | Bundled launcher writes a Terminal.app `open` command but has not been executed. |
| 24 | macOS Gatekeeper/quarantine, signing and notarization | UNKNOWN | Depends on Apple credentials and a real Mac; unsigned fallback is documented. |
| 25 | Intel Mac build/runtime | UNKNOWN | CI artifacts are uploaded only after the Intel job passes, but no Intel hardware, Finder, Gatekeeper, or playback validation has been performed; no public release asset is attached. |
| 26 | Sixel/TGP terminals beyond the locally confirmed Windows Terminal | UNKNOWN | Requires real terminal testing. |
| 27 | Direct Bash ani-cli playback path on macOS | UNKNOWN | Not executed on macOS. |
| 28 | Real GitHub Releases update download/launch | UNKNOWN | Not run against a published release. |
| 29 | Season/movie/special selection stays inside ANIXO | VERIFIED | Automated flow test plus live `debug` launcher check. |
| 30 | Exact provider ID survives selection → episode fetch → playback | VERIFIED | Provider parser rejects other IDs; spawn test asserts `provider_id`, `-S 1`, and `-e <n>`. |
| 31 | Remembered franchise selection is restored, not Season 1 | VERIFIED | Automated persistence test and selector preselection by exact source ID. |
| 32 | macOS CI: build successful status | VERIFIED IN CI | Both jobs in run 44917520313 reported VERIFIED. |
| 33 | macOS CI: automated tests successful status | VERIFIED IN CI | 197 passed, 1 skipped plus 15 platform tests on each runner. |
| 34 | macOS CI: architecture verified | VERIFIED IN CI | 85 Mach-O files checked per job; ARM64 all `arm64`, Intel all `x86_64`, with no additional architectures. |
| 35 | macOS CI: packaging verified | VERIFIED IN CI | `.app` structure, `Info.plist`, resources, dynamic libraries, DMG mount, uninstaller, and checksums passed on both runners. |
| 36 | macOS CI: code signing / notarization verified | UNVERIFIED | No Apple secrets were configured; both reports say UNVERIFIED rather than faking success. |
| 37 | macOS CI: Finder / Gatekeeper / real playback | NOT TESTED BY DESIGN | Headless GitHub runners cannot establish these; reports always mark them as not tested. |

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
logs are uploaded even on failure. Run 44917520313 executed the workflow on
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

1. Run `ANIXO-Setup-v0.1.0-beta.1.exe`; confirm the default install folder.
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
