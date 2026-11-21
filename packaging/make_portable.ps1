<#
.SYNOPSIS
    Builds the ANIXO portable ZIP from the one-folder PyInstaller output.
#>
[CmdletBinding()]
param(
    [string] $Version = '0.1.0-beta.1',
    [string] $RepoRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}

$source = Join-Path $RepoRoot 'dist\Anixo'
$releaseDir = Join-Path $RepoRoot 'dist\release'
$portableName = "ANIXO-Portable-v$Version"
$portableRoot = Join-Path $releaseDir "portable\$portableName"
$zipPath = Join-Path $releaseDir "$portableName.zip"

if (-not (Test-Path -LiteralPath (Join-Path $source 'Anixo.exe') -PathType Leaf)) {
    throw "make_portable.ps1: one-folder build missing: $source\Anixo.exe"
}

New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
if (Test-Path -LiteralPath $portableRoot) {
    Remove-Item -LiteralPath $portableRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $portableRoot -Force | Out-Null

Get-ChildItem -LiteralPath $source -Force | Copy-Item -Destination $portableRoot -Recurse -Force

$uninstaller = Join-Path $RepoRoot 'dist\ANIXO-Uninstaller.exe'
if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
    throw "make_portable.ps1: standalone uninstaller missing: $uninstaller"
}
Copy-Item -LiteralPath $uninstaller -Destination $portableRoot -Force
Set-Content -LiteralPath (Join-Path $portableRoot 'PORTABLE.txt') -Value 'ANIXO portable installation' -Encoding ASCII

$launcher = @(
    '@echo off'
    'rem ANIXO portable launcher. No Python, pip, uv or Git is required.'
    'setlocal'
    'if "%ANIXO_DATA_DIR%"=="" set "ANIXO_DATA_DIR=%LOCALAPPDATA%\ANIXO\Portable"'
    'set "ANIXO_CONFIG=%ANIXO_DATA_DIR%\config.json"'
    'if "%ANIXO_CACHE_DIR%"=="" set "ANIXO_CACHE_DIR=%ANIXO_DATA_DIR%\cache"'
    'if "%ANIXO_LOG_DIR%"=="" set "ANIXO_LOG_DIR=%ANIXO_DATA_DIR%\logs"'
    'rem Keep the legacy variable names working for pre-rename builds.'
    'set "ANIME_HUB_DATA_DIR=%ANIXO_DATA_DIR%"'
    'set "ANIME_HUB_CONFIG=%ANIXO_DATA_DIR%\config.json"'
    '"%~dp0Anixo.exe" %*'
    'exit /b %ERRORLEVEL%'
)
Set-Content -LiteralPath (Join-Path $portableRoot 'START ANIXO.cmd') -Value $launcher -Encoding ASCII

$portableDoc = @"
# ANIXO Portable

This folder is a complete, self-contained copy of ANIXO v$Version for Windows.
Nothing is installed: no Python, pip, uv, Git, or administrator rights are
needed. Double-click **START ANIXO.cmd** (or run ``Anixo.exe`` directly).

## Where your data goes

| Item | Installed build | Portable build |
| --- | --- | --- |
| Config, watchlist, history | ``%APPDATA%\ANIXO`` | ``%LOCALAPPDATA%\ANIXO\Portable`` |
| Cache and logs | ``%LOCALAPPDATA%\ANIXO`` | ``%LOCALAPPDATA%\ANIXO\Portable`` |
| Program files | ``%LOCALAPPDATA%\Programs\ANIXO`` | This folder |

`START ANIXO.cmd` sets `ANIXO_DATA_DIR` to
`%LOCALAPPDATA%\ANIXO\Portable` only when you have not set it yourself.
Set `ANIXO_DATA_DIR` before starting to keep data somewhere else (for example
on a USB drive). Running `Anixo.exe` directly uses the installed-build
locations above.

## Differences from the installed build

* No Start Menu entry, desktop shortcut, or Windows uninstall entry.
* Updating means replacing this folder with a newer portable ZIP; your data
  in `%LOCALAPPDATA%\ANIXO\Portable` is left alone.
* Streaming still requires the optional external tools (Git for Windows /
  Git Bash, ani-cli and mpv) if you use playback. The ANIXO app itself and
  its metadata/artwork features do not need them.

## Uninstalling the portable build

Close ANIXO and delete this folder. `ANIXO-Uninstaller.exe` is included for
removing ANIXO-managed dependencies that were installed from the wizard; run
it with `--data-dir "%LOCALAPPDATA%\ANIXO\Portable" --cache-dir
"%LOCALAPPDATA%\ANIXO\Portable\cache" --log-dir
"%LOCALAPPDATA%\ANIXO\Portable\logs" --dry-run` first. Delete
`%LOCALAPPDATA%\ANIXO\Portable` only if you intentionally want to discard
your settings and history.
"@
Set-Content -LiteralPath (Join-Path $portableRoot 'PORTABLE.md') -Value $portableDoc -Encoding UTF8

if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -LiteralPath $portableRoot -DestinationPath $zipPath -Force -CompressionLevel Optimal

Write-Host "make_portable.ps1: created $zipPath"
Write-Output $zipPath
