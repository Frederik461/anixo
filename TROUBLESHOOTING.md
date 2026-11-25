# Troubleshooting ANIXO

ANIXO v0.1.0-beta.4 is a terminal application for Windows 10/11 x64 and macOS
13+ (Apple Silicon and Intel x86_64). Most problems fall into the categories
below. Start by collecting diagnostics.

Windows:

```text
"%LOCALAPPDATA%\Programs\ANIXO\Anixo.exe" --diagnose
```

macOS:

```sh
/Applications/ANIXO.app/Contents/Resources/Anixo/Anixo --diagnose
```

It prints dependency, terminal, image-protocol and path information and exits
without starting the interface. `--version` prints the product name and
version; include both outputs when reporting a bug.

## Terminal and image rendering

Posters are optional. If they look garbled, are missing, or the interface
flickers:

1. Use a current terminal with the protocol you want. Windows Terminal is
   recommended on Windows; Terminal.app or iTerm2 can be used on macOS. Sixel
   and TGP support depends on the terminal, not just ANIXO.
2. Run `--diagnose` and read the **Image protocol** line. It reports whether
   Sixel, TGP, half-cell, Unicode or off was selected and why.
3. In ANIXO, press **S** for Settings and try:
   * `artwork renderer`: `auto` → `halfcell` → `unicode` → `off`;
   * `images`: `off` to disable downloads entirely for a session.
4. You can also start with `--no-images` to isolate a network/artwork problem
   from a rendering problem.
5. If the text looks misaligned, adjust the terminal cell width/height in
   Settings, or use the **V** key to open the built-in image rendering test.
6. Inside tmux or a remote session, protocols may be unavailable; prefer
   `halfcell`/`unicode` there.

Sixel/TGP behaviour in macOS terminals has not been independently validated.
Treat the diagnostic output as the source of truth.

## Missing playback dependencies

ANIXO is a frontend: it discovers anime and hands playback to external tools.
The installer bundles only ANIXO and its Python runtime. It does not ship mpv,
ani-cli, or other third-party binaries.

If pressing Enter on an episode does nothing, or the app reports a missing
dependency:

1. Open the first-launch setup wizard (Settings → Application → Open setup
   wizard) and review the dependency list.
2. On Windows, the wizard can install Git Bash, the pinned ani-cli script, and
   mpv at user scope through winget. Nothing is installed silently.
3. On macOS, install official mpv and ani-cli builds yourself if you want
   playback, then set their paths in Settings. Homebrew is not required.
4. Run `--diagnose` again; it lists which dependencies were found.

## ani-cli and mpv specifics

* ani-cli is a shell script. On Windows ANIXO looks for Git Bash via
  `GIT_INSTALL_ROOT` and common install locations. On macOS it uses `/bin/bash`
  or the Bash found on PATH; Git is not required.
* If ani-cli lives in a path with spaces on Windows, use the **cmd wrapper**
  option in Settings so ANIXO invokes it through a generated wrapper script.
* Set `ani_cli_path` and `mpv_path` explicitly in Settings if auto-detection
  fails. Paths may include environment variables such as `%LOCALAPPDATA%`.
* A slow or blocked network, a changed anime site domain, or an outdated
  ani-cli version can cause playback to fail even when the files exist.
* Exact playback uses a checksum-verified copy of the official ani-cli script.
  If you replaced that script with an unknown build, ANIXO refuses to patch or
  execute it. Restore the version installed by the setup wizard.
* Never use ani-cli's own `-U` updater: it can replace the verified
  exact-provider patch. Use Settings → Playback → Update ani-cli, which
  re-downloads the checksum-pinned official script and backs up the old copy.
* ANIXO never enters ani-cli's own anime or episode pickers. If a picker
  appears, playback was started outside the ANIXO season and episode screens.
* Title-only exact playback is refused. You must select an exact provider
  result and episode inside ANIXO.

## First-run setup

* Windows data starts at `%APPDATA%\ANIXO` with cache/logs under
  `%LOCALAPPDATA%\ANIXO`. macOS uses `~/Library/Application Support/ANIXO`,
  `~/Library/Caches/ANIXO`, and `~/Library/Logs/ANIXO`. These folders are
  created automatically.
* If a config file becomes corrupt, ANIXO renames it to a `.corrupt-*.bak`
  file and starts with defaults. You can delete the backup once you are happy.
* If the installation manifest becomes corrupt, it is quarantined as a
  `.corrupt-*.bak` file and no uninstall is allowed until the situation is
  reviewed. This prevents accidental removal of unowned files.
* To reset settings deliberately, close ANIXO and rename `config.json` to
  `config.json.old`. Watchlist and history are stored separately and are kept.
* No administrator rights are needed for a normal install.

## macOS Gatekeeper and quarantine

The DMG may be unsigned or unnotarized depending on available Apple
credentials. If macOS refuses to open it:

1. Right-click **ANIXO.app** and choose **Open**, then confirm **Open** in the
   dialog. This is a local Gatekeeper override for that one app.
2. Do not run unknown commands suggested by pop-ups; download only from the
   project's official Releases page.
3. Check `TESTING.md` for the signing/notarization status of the released build.

## Portable mode (Windows)

* Always start the portable copy with **START ANIXO.cmd**. It sets
  `ANIXO_DATA_DIR` to `%LOCALAPPDATA%\ANIXO\Portable` unless you have already
  set that variable, so portable settings stay separate from an installed copy.
* Running `Anixo.exe` directly from the portable folder uses the installed
  locations instead, which can mix settings between copies.
* To relocate portable data, set `ANIXO_DATA_DIR` before starting, for example:

  ```cmd
  set ANIXO_DATA_DIR=E:\AnimeOnTheGo
  "E:\AnimeOnTheGo\START ANIXO.cmd"
  ```

* Automatic updates also work for portable copies: the helper overlays the new
  portable ZIP onto the install folder and leaves the separate data folder
  untouched. Manual extraction of a newer ZIP is still supported.
* `PORTABLE.md` inside the ZIP summarises these differences.

## Update problems

Press `U` to open the Update Center. Buttons and keys do the same thing: `C`
check, `U` update, `X` cancel, `R` restart + install, `N` release notes.
Settings → Updates shows the channel, automatic-check toggle, latest version,
status, last check, feed URL, and optional manifest public key.

* **"No compatible update exists"** means the release has no asset for your
  platform/architecture and preferred install kind. Check the architecture line
  in `--diagnose` and use the manual download for that platform instead.
* **"outside the trusted update source"** means the manifest points at a
  non-HTTPS URL or a host that is neither GitHub nor the configured feed host.
  ANIXO deliberately refuses it. Do not bypass this by disabling verification.
* **"checksum mismatch"** stops the update before any file is opened or run.
  Delete the staged file from the data directory's `updates/staging` folder and
  check again; report the release asset if it repeats.
* **"signature FAILED"** occurs when `update_public_key` is set but the manifest
  is unsigned or its signature does not verify. Confirm the key belongs to the
  release publisher, or clear the key if you intentionally rely on pinned
  checksums only.
* **No beta shown on a stable channel** is correct: stable builds never receive
  pre-releases. Switch the channel in Settings → Updates only if you accept
  beta risk.
* **The helper reports a rollback** means the new build failed its health check;
  the backed-up installation was restored. Check the redacted update log in the
  data directory, then retry or install manually.
* **A manual check appears cached** is normal: checks have a short TTL. Use the
  explicit Check action to refresh; offline checks fall back to the last good
  result so a network outage does not look like an available update.
* Never run an updater helper or installer received outside an official release
  or your configured trusted feed. ANIXO does not execute unverified downloads.

## Uninstalling and leftovers

* The safest path is **Settings → System → Installation & uninstallation →
  Uninstall ANIXO**. Choose **Remove ANIXO only** to keep personal data, or
  **Complete removal** to delete it after a final confirmation.
* The standalone uninstaller waits for ANIXO to close, then removes only files
  and dependencies recorded in the ownership manifest. It never removes
  pre-existing software or other apps' files.
* Windows OS-level uninstall (Start Menu or Settings → Apps) performs an
  app-only removal. It does not delete `%APPDATA%\ANIXO` or
  `%LOCALAPPDATA%\ANIXO`.
* On macOS, quit ANIXO, delete **ANIXO.app** from Applications, and use the
  standalone uninstaller for ANIXO-managed dependencies.
* If removal fails because a file is locked, close all ANIXO/terminal windows
  and retry. The completion report lists failed paths and manual steps; the
  manifest is kept so the uninstall can be retried safely.
* To erase personal data yourself, delete the data folders listed under
  "First-run setup" after uninstalling. This is irreversible without a backup.

## Reporting bugs

Open an issue in the project's issue tracker and include:

1. The exact ANIXO version (`Anixo.exe --version` or the bundled binary's
   `--version`).
2. The full output of `--diagnose`.
3. Operating system version/build and whether you used the installer, DMG, or
   portable ZIP.
4. Terminal name/version and the selected artwork renderer.
5. Steps to reproduce, what you expected, and what happened.
6. Screenshots or short recordings are helpful, but **remove access tokens,
   usernames, and file paths you do not want to share** first.

Because this is a beta, please also say whether Git Bash / Bash, ani-cli and
mpv were installed; that detail changes the meaning of most playback reports.
