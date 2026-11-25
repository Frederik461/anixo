# Installing ANIXO

ANIXO — *Your anime. Your world.* — is a terminal application for **Windows 10/11
x64** and **macOS 13+ Apple Silicon**. Windows builds are validated locally on
Windows 11 x64; Windows 10 support is configured in the installer but has not
been independently tested. The macOS Apple Silicon DMG is built by CI on an
arm64 runner; it has **not** yet been validated on a clean user Mac. A separate
CI matrix also builds and tests Intel on `macos-15-intel`, but that artifact is
not a clean-Mac compatibility claim and is not attached to public releases until
an Intel Mac has been independently tested.

End users do **not** need Python, pip, uv, Git, Homebrew, or administrator
rights to install and run ANIXO itself. Playback is optional and uses external
tools; on Windows the first-launch wizard can install the verified official
ani-cli and mpv at user scope through winget. On macOS, automatic playback-tool
installation is not implemented yet: if you want streaming, provide official
mpv and ani-cli builds yourself. No Homebrew is required.

## 1. Download

Open the project's **Releases** page and download the assets for your platform:

| Asset | Platform | Use it when |
| --- | --- | --- |
| `ANIXO-Setup-v0.1.0-beta.1.exe` | Windows x64 | You want a normal installed application (recommended). |
| `ANIXO-Uninstaller-v0.1.0-beta.1.exe` | Windows x64 | You want the standalone uninstaller on its own. |
| `ANIXO-Portable-v0.1.0-beta.1.zip` | Windows x64 | You want a no-install copy, for example on a USB drive. |
| `ANIXO-v0.1.0-beta.1-macos-arm64.dmg` | macOS 13+ Apple Silicon | You want the drag-and-drop `.app` bundle. |
| `ANIXO-Uninstaller-v0.1.0-beta.1-macos-arm64` | macOS 13+ Apple Silicon | Companion standalone uninstaller. |
| `ANIXO-Updater-windows-x64.exe` / `ANIXO-Updater-macos-*.zip` | All | Standalone helper used by automatic updates. |
| `SHA256SUMS.txt` | All | You want to verify the downloads. |
| `release-manifest.json` | All | Pinned download URLs and digests used by the in-app updater. |

A separate macOS CI matrix also uploads `ANIXO-macOS-ARM64.dmg` and
`ANIXO-macOS-Intel.dmg` build artifacts when their respective jobs pass.
[Run 44919795580](https://github.com/Frederik461/anixo/actions/runs/44919795580)
passed on both `macos-15` ARM64 and `macos-15-intel`, including full tests
(201 passed, 1 skipped), architecture, packaging, and runtime/uninstall
validation. Checksums are in the matching `SHA256SUMS-macOS-*.txt` artifacts.
These are CI artifacts, not public-release assets or clean-machine compatibility
claims; signing/notarization are UNVERIFIED, and Finder, Gatekeeper, and playback
are NOT TESTED by headless CI.

A terminal such as Windows Terminal (Windows) or Terminal.app / iTerm2 (macOS)
is recommended; ANIXO is a text interface.

### Verify the download (optional but recommended)

Windows PowerShell:

```powershell
Get-FileHash .\ANIXO-Setup-v0.1.0-beta.1.exe -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

macOS Terminal:

```sh
shasum -a 256 ANIXO-v0.1.0-beta.1-macos-arm64.dmg
cat SHA256SUMS.txt
```

The hash must match the line for that file name in `SHA256SUMS.txt`.

## 2. Install on Windows

1. Double-click `ANIXO-Setup-v0.1.0-beta.1.exe`.
2. Windows may show a SmartScreen warning for a new publisher; choose **More
   info → Run anyway** only if you trust the source.
3. The installer runs as your normal user — **no administrator rights** are
   required. The default install folder is:

   ```text
   %LOCALAPPDATA%\Programs\ANIXO
   ```

   You may change it during setup.
4. Optional: enable **Create a desktop shortcut** on the shortcut task page.
   A Start Menu folder named **ANIXO** is always created.
5. Click **Install**, then **Finish**.

The installer writes program files, the Start Menu/optional desktop shortcuts,
and a small ownership receipt called `install-manifest-windows.json` in the
user data folder. That receipt is what makes safe uninstall possible. The
installer does **not** change or delete settings, watchlist, or history.

## 3. Install on macOS

1. Download the Apple Silicon DMG and its checksum file.
2. Open the DMG and drag **ANIXO.app** to **Applications**.
3. Eject the disk image. No administrator rights are normally required.
4. Launch ANIXO from Applications or Launchpad. Because ANIXO is a terminal
   application, the bundle opens a Terminal window and runs there.
5. If macOS Gatekeeper blocks an unsigned or unnotarized build, right-click
   **ANIXO.app** and choose **Open** once. The current signing/notarization
   status is recorded in `TESTING.md`.

The app bundle installs into `/Applications/ANIXO.app`. User data is kept
separately under your home Library folders (see the table below).

## 4. First launch

* On first launch ANIXO creates its data folders and a default `config.json`.
* Press **S** for Settings to choose your theme, artwork mode, and optional
  playback tool paths. Settings → System → Installation & uninstallation shows
  the install kind, manifest location, and managed component count.
* Anime metadata and artwork work out of the box. Playback requires optional
  external tools; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
* To check your setup from a terminal, run:

  ```text
  "%LOCALAPPDATA%\Programs\ANIXO\Anixo.exe" --diagnose      (Windows)
  /Applications/ANIXO.app/Contents/Resources/Anixo/Anixo --diagnose   (macOS)
  ```

  It prints dependency and terminal information and exits without opening the
  interface. `--version` prints the product name and version; `--help` lists
  all command-line options.

## 5. Dependency management

All dependency installs go through one manager that records, for each item:

* component name and version;
* install location;
* installation date;
* download/source URL and install method;
* expected and observed SHA256 where applicable;
* whether ANIXO installed it, whether it is managed, and whether it is safe to
  remove.

Downloads come from official sources and are verified before use. ANIXO never
overwrites a pre-existing tool and never removes a component it did not install.
ani-cli's own `-U` updater is never used because it can replace the verified
exact-provider patch; updates re-download the checksum-pinned script instead.

On Windows the wizard can install ani-cli and mpv at user scope through winget.
On macOS, provide official copies yourself and set their paths in Settings if
auto-detection does not find them.

## 6. Data locations

| Item | Windows | macOS |
| --- | --- | --- |
| Config, database, ownership manifest | `%APPDATA%\ANIXO` | `~/Library/Application Support/ANIXO` |
| Cache and generated runtime files | `%LOCALAPPDATA%\ANIXO\cache` | `~/Library/Caches/ANIXO` |
| Logs | `%LOCALAPPDATA%\ANIXO\logs` | `~/Library/Logs/ANIXO` |
| Portable data | `%LOCALAPPDATA%\ANIXO\Portable` | Not applicable |
| Installed program | `%LOCALAPPDATA%\Programs\ANIXO` | `/Applications/ANIXO.app` |

Legacy environment variables `ANIME_HUB_DATA_DIR` and `ANIME_HUB_CONFIG` still
work as aliases. New overrides are `ANIXO_DATA_DIR`, `ANIXO_CONFIG`,
`ANIXO_CACHE_DIR`, `ANIXO_LOG_DIR`, `ANIXO_UPDATE_REPO`, and
`ANIXO_UPDATER_HELPER` (path to a trusted standalone helper). Repository URLs,
feed URLs, and optional public keys are stored as Settings values, never as
credentials.

Legacy AnimeHub data is only ever read for migration. Migration creates a
backup and never deletes the old folder or overwrites existing ANIXO data.

## 7. Update

### Automatic (recommended)

Press `U`, or go to Settings → Updates → **Open update center**. Choose your
channel in Settings → Updates (`beta` is the default for `0.1.0-beta.1`; stable
builds default to `stable` and never receive betas). **Check** uses GitHub
Releases or a configured HTTPS feed; **Update ANIXO** downloads only after you
approve. **Cancel** stays available until installation begins. Once the staged
package is verified, choose **Restart + install** (or `R`): ANIXO closes, a
separate helper backs up the old program files, applies the update, checks the
new build, and restores the backup if anything fails. **Release notes** (`N`)
shows the manifest notes. The update center works fully with the mouse.

Downloads must be HTTPS, are pinned to SHA-256 digests, and are checked for a
valid package structure before use. An optional manifest signature can be
required by setting `update_public_key` to a PEM public key file; if configured,
a missing or invalid signature blocks the update. No GitHub token or other
credential is stored by ANIXO. Redacted update events are written to the data
directory.

Your settings, history, watchlist, progress, playback preferences,
user-installed dependencies, shortcuts, and ownership manifest are kept.

### Manual

1. Download the newer installer or DMG for your platform and verify it against
   `SHA256SUMS.txt`.
2. Run the installer / replace `ANIXO.app` in Applications / extract the portable
   ZIP over the existing portable folder (portable data lives outside the ZIP).
3. Your personal data and ownership manifest are kept.

Back up your data folder before updating between beta versions if you rely on
its contents.

## 8. Uninstall

### From inside ANIXO

Open **Settings → System → Installation & uninstallation → Uninstall ANIXO**.
You are asked to choose:

* **Option A — Remove ANIXO only**: removes program files, launchers,
  ANIXO-managed dependencies, caches and logs, but keeps settings, history,
  watchlist, progress, and API configuration.
* **Option B — Complete removal**: also deletes settings and personal data.
  This requires a final explicit confirmation before anything is deleted.

The confirmation screen shows selectable categories, calculated sizes where
available, warnings, and preservation notes. ANIXO writes a
`pending-uninstall.json` request and then starts the standalone uninstaller,
which waits for ANIXO to exit before removing locked program files. The
console shows live progress per component; its completion report lists removed
items, failures, and manual cleanup steps.

### From the operating system

Windows: use **Start Menu → ANIXO → Uninstall ANIXO** or
**Settings → Apps → Installed apps → ANIXO → Uninstall**. This performs an
app-only removal and keeps settings and personal data. The installer records
any custom ANIXO data/cache/log paths beside the app, and the bundled uninstall
wrapper replays them, so a custom install location or `ANIXO_*` override is
honoured during OS-level uninstall.

macOS: quit ANIXO, delete **ANIXO.app** from Applications, then run the
standalone uninstaller if you also want ANIXO-managed dependencies removed.
Never delete a dependency you installed yourself.

To erase personal data without the in-app flow, delete the data folders listed
above manually after uninstalling. Recovery requires a backup; personal data is
never deleted silently.

## 9. Portable mode (Windows)

1. Extract `ANIXO-Portable-v0.1.0-beta.1.zip` to any folder.
2. Double-click **START ANIXO.cmd**. You can also run `Anixo.exe` directly.

Portable mode has no installer, no Start Menu entry, no desktop shortcut, and
no Windows uninstall entry. `START ANIXO.cmd` stores data in
`%LOCALAPPDATA%\ANIXO\Portable` by default so settings do not mix with an
installed copy. Override that by setting `ANIXO_DATA_DIR` before starting. See
`PORTABLE.md` inside the ZIP for details.

To update a portable copy, extract a newer ZIP over the folder (or replace the
folder). The portable data folder is left in place.
