# ANIXO agent rules

Re-read this file at the start of every phase. Work one approved phase at a time, make exactly one LOCAL commit per phase, and never push, tag, publish, or trigger a workflow.

## Firm rules

1. `.github/workflows/release.yml` is the only publisher of GitHub Releases. Never create a release, upload assets, delete a release or tag, overwrite or re-push a tag, or touch `v0.1.0-beta.2`. Edit workflow/packaging files only where a phase explicitly allows it; never trigger them.
2. Never expose tokens, passwords, bearer headers, URL credentials, or signed URL query parameters (`sig`, `se`, `X-Amz-*`) in UI text, logs, caches, crash reports, or commit messages.
3. Provider-released episode rows are authoritative. Never cap them with AniList's planned episode count. Never invent unaired episodes. Health or error data must never replace, filter, or synthesize episode rows.
4. Playback uses the exact provider ID and exact episode number. `ani-cli` is a REQUIRED dependency and stays. Never enter ani-cli's interactive picker, never use `ani-cli -U`, use `--dub` only when DUB is explicitly selected, never combine `-c` with an exact episode.
5. `update_repo` and `release_repo` are independent. Stable users never receive beta releases. Stable requires `manifest.channel == "stable"` explicitly.
6. Never weaken production update validation: HTTPS only, config-driven trusted hosts with exact or dot-boundary matching, per-hop redirect validation, userinfo rejection, SHA-256 verification, package validation, backup and rollback. Cached data must never bypass any of these.
7. Test hooks may redirect paths or endpoints but must never skip a check, and must not let a production install run an arbitrary helper or package. State exactly how each hook is gated.
8. Never run tests, the updater, or any update flow against `C:\ANIXO`. Use temporary install and data directories only. Never launch `ANIXO-Portable-v0.1.0-beta.1`.
9. Statuses are only `PASSED` / `FAILED` / `UNKNOWN` / `NOT TESTED`. Never claim signing, notarization, clean-machine behavior, or macOS behavior is verified. Windows-only runs are reported as `PASSED on Windows; macOS NOT TESTED`.
10. Never delete user data. Never touch anything outside the install root, data dir, or temp dirs created for the work.

## Test rules

- Never remove or weaken an existing assertion. Never delete or skip an existing test to get green.
- Before EVERY commit run `git diff --cached -- tests/` and put either every modified/deleted assertion in the commit message body or the exact line `Existing assertions modified: none`.
- New tests must be cross-platform: they also run in two GitHub-hosted macOS jobs. Simulate Windows behavior with monkeypatch instead of depending on Windows. Use `tmp_path` for all files.
- No hardcoded version strings in tests. Derive versions from `version.py` or build clearly synthetic versions from it.
- No live network in the default suite. Use fakes and fixtures. Keep the default suite offline and deterministic.
- Keep the full suite under about 167 seconds plus 20%. Flag any new test over 10 seconds. Use fakes rather than sleeps; where timing is unavoidable, wait on a condition with a timeout.
- For each phase run targeted tests, then the full suite, then compileall, then `git diff --cached --check`.

## Key paths

| Purpose | Path |
| --- | --- |
| Project | `C:\Users\fred8\AnimeHub` |
| Venv Python | `C:\Users\fred8\AnimeHub\.venv\Scripts\python.exe` |
| Source version | `version.py` (`APP_VERSION`) |
| Public repo | `Frederik461/anixo` |
| Installed app (never test/target) | `C:\ANIXO` |
| Local config | `C:\Users\fred8\AppData\Roaming\ANIXO\config.json` |
| Local app data | `C:\Users\fred8\AppData\Roaming\ANIXO` |
| Update log | `C:\Users\fred8\AppData\Roaming\ANIXO\updates\update.log` |
| Install manifest | `<data dir>\install-manifest-<platform>.json` via `services/install_manifest.py` |
| Release publisher | `.github/workflows/release.yml` |
| Tests | `tests/` |

## Common commands

```powershell
# Run the app
.\.venv\Scripts\python.exe main.py

# Version and diagnostics
.\.venv\Scripts\python.exe main.py --version
.\.venv\Scripts\python.exe main.py --diagnose

# Verification
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app.py config.py version.py services updater_helper.py screens
git diff --cached --check
git diff --cached -- tests/

# Read-only release inspection (terminal only)
.\.venv\Scripts\python.exe main.py release check
.\.venv\Scripts\python.exe main.py release history
```

Never run `main.py release beta` or `main.py release stable`.

## Things never to recommend

- Launching `ANIXO-Portable-v0.1.0-beta.1`
- Deleting or re-tagging `v0.1.0-beta.2`
- Manual GitHub Releases or asset uploads
- `fred8/anixo` as the update repo
- Switching the beta user to stable
- Treating `beta.3` as current
- Capping provider episodes
- Inventing unaired episodes
- Using ani-cli's picker or `-U`
- Claiming signing or clean-machine behavior is verified
