# ANIXO releases

ANIXO release assets are packaged for **Windows x64** and **macOS 13+ Apple
Silicon**. A separate macOS CI matrix builds and tests Intel targets:
[run 44919795580](https://github.com/Frederik461/anixo/actions/runs/44919795580)
passed on both Apple Silicon and Intel runners, including architecture,
packaging, and uninstall validation. Intel DMGs are available only as CI
artifacts and are not attached to public releases until real Intel Mac, Finder,
Gatekeeper, and playback validation has been performed.

Each release is named **ANIXO v<version>** and tagged `v<version>`. A release
contains these downloadable assets:

```text
Windows:
  ANIXO-Setup-v<version>.exe                    per-user installer (Inno Setup 6)
  ANIXO-Uninstaller-v<version>.exe              standalone guarded uninstaller
  ANIXO-Portable-v<version>.zip                 no-install portable copy

macOS 13+ Apple Silicon:
  ANIXO-v<version>-macos-arm64.dmg               drag-and-drop ANIXO.app
  ANIXO-Uninstaller-v<version>-macos-arm64       standalone guarded uninstaller

All platforms:
  SHA256SUMS.txt                                 SHA-256 checksums for every asset
```

`build-report.txt` may be present in the build output as a diagnostic log; it is
not a release asset.

## Channels and versioning

| Channel | Version pattern | Meaning |
| --- | --- | --- |
| Beta | `0.1.0-beta.1`, `0.1.0-beta.2`, … | Early release. Settings and data layout may still change; back up your data folder before updating. GitHub release is marked as a pre-release. |
| Stable | `0.1.0`, `0.2.0`, … | Recommended for everyday use. Marked as a full release. |

Versions follow `major.minor.patch` plus an optional `-beta.N` suffix. Asset
file names always use the exact version string after the `v`, for example
`ANIXO-Setup-v0.1.0-beta.1.exe`.

There is no auto-updater in this beta. Download the new asset from the Releases
page and run the installer or replace the portable folder / `.app` bundle.

## Verify a download

`SHA256SUMS.txt` contains one line per release asset in the form
`<sha256>  <file name>`.

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

The hashes must match exactly. Do not run an unverified download.

## Building a release

Prerequisites on the build machines:

* Python 3.12 and uv for both jobs;
* Inno Setup 6 for the Windows installer;
* a macOS 15 arm64 runner for the Apple Silicon `.app` and DMG;
* optional Apple Developer credentials for signing/notarization.

None of these are needed by end users.

1. **Prepare the release.** Update the version string consistently in:
   * `pyproject.toml` (`project.version` and the pinned dependency set),
   * `packaging/build_windows.ps1` (`$Version` default),
   * `installer/AnixoSetup.iss` (`MyAppVersion`, `OutputBaseFilename`,
     `VersionInfoProductVersion` / `VersionInfoVersion`),
   * `.github/workflows/release.yml` (`APP_VERSION` default),
   * `INSTALLATION.md`, `TESTING.md`, and this document.
   `anixo.spec` always builds `Anixo.exe`, so its name does not change.
2. **Refresh the lock file** if dependencies changed:

   ```powershell
   uv lock
   uv lock --check
   ```

3. **Build Windows locally** from the repository root:

   ```powershell
   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
   ```

   This syncs dependencies, runs pytest, builds the one-folder app, the
   standalone uninstaller, and a one-file validation executable, validates the
   builds, compiles the installer, builds the portable ZIP, and writes hashes
   plus `build-report.txt`. If Inno Setup is unavailable the step is reported
   as `SKIP`, not faked; never publish from a report containing `SKIP` or
   `FAIL`.

4. **Build macOS on an arm64 Mac or macOS 15 runner**:

   ```sh
   bash packaging/build_macos.sh 0.1.0-beta.1
   ```

   This runs tests, builds the console binary and standalone uninstaller,
   generates `anixo.icns`, assembles `ANIXO.app`, creates the DMG, and writes
   per-architecture checksums. If Apple credentials are present it signs and
   notarizes the distributed DMG; otherwise it produces an unsigned build and
   reports that clearly.

   The `macOS CI` workflow runs the same build on separate `macos-15` and
   `macos-15-intel` runners, validates architecture/resources/dynamic libraries
   and both uninstall modes, and uploads per-architecture reports and checksums.
   Only a passing job uploads its DMG; run 44919795580 passed on both runners.
   This does not replace real Finder, Gatekeeper, clean-machine, or playback
   testing, and no Apple signing/notarization secrets were configured in that
   run.

5. **Review the artifacts** and `build-report.txt`. Confirm the Windows and
   macOS hashes, then follow [TESTING.md](TESTING.md). Do not claim clean-machine
   or Intel Mac compatibility until those checks have actually been run.

6. **Publish**: push a tag matching the version, for example:

   ```powershell
   git tag v0.1.0-beta.1
   git push origin v0.1.0-beta.1
   ```

   The `Release ANIXO` workflow runs separate Windows and macOS jobs, uploads
   per-OS artifacts, aggregates them, verifies that every expected asset is
   present, writes a final `SHA256SUMS.txt`, and publishes the GitHub release.
   A manual `workflow_dispatch` run is also available; it uses the version
   configured in the workflow.

## Signing and notarization

macOS signing is conditional on these repository secrets:

* `MACOS_SIGNING_IDENTITY`
* `APPLE_ID`
* `APPLE_APP_SPECIFIC_PASSWORD`
* `APPLE_TEAM_ID`

When only the signing identity is present, the app and DMG are signed but not
notarized. When all four are present, the distributed DMG is submitted to
notarytool and stapled. When none are present, the DMG is unsigned;
`TESTING.md` records the status of the current release. Windows builds are
currently unsigned apart from any publisher reputation SmartScreen may assign.

## Data safety

The Windows installer writes only program files, shortcuts, a small
`uninstall-paths.ini` beside the app (so custom path overrides can be replayed),
and the ownership manifest (`install-manifest-windows.json`) in the user data
folder. Settings, history, watchlist, progress, and API configuration are never
touched on install or update.

The standalone and OS-level uninstallers remove only components positively
identified as ANIXO-owned. Pre-existing software, shared system components,
other apps' files, and personal data are never removed silently. **Complete
removal** is available only through the in-app Settings flow and requires a
separate final confirmation before personal data is deleted.
