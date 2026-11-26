<#
.SYNOPSIS
    Builds and validates the complete Windows release of ANIXO v0.1.0-beta.6.

.DESCRIPTION
    Run from the repository root (the script also resolves its own root):

        powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

    Steps: sync dependencies with uv, run pytest, build the one-folder app
    with anixo.spec, build an additional one-file app for validation, validate
    both executables, make the portable ZIP, compile the Inno Setup installer
    when Inno Setup is installed, and write SHA256SUMS.txt plus a build report.

    If Inno Setup is not available the installer step is clearly reported as
    SKIPPED; nothing is faked. User data (%APPDATA%\ANIXO and
    %LOCALAPPDATA%\ANIXO) is never deleted, moved, or rewritten by this script.
#>
[CmdletBinding()]
param(
    [string] $Version = '0.1.0-beta.6',
    [string] $RepoRoot = '',
    [switch] $SkipTests,
    [switch] $SkipInstaller,
    [switch] $SkipPortable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 does not populate $PSScriptRoot while parameter
# defaults are evaluated, so resolve the repository root in the body.
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
# Native command exit codes are checked explicitly with Assert-LastExitCode.
$PSNativeCommandUseErrorActionPreference = $false

$releaseDir = Join-Path $RepoRoot 'dist\release'
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

$script:Report = [System.Collections.Generic.List[string]]::new()

function Write-Log {
    param(
        [string] $Message,
        [ValidateSet('INFO', 'PASS', 'SKIP', 'FAIL')]
        [string] $Level = 'INFO'
    )
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line
    $script:Report.Add($line)
}

function Assert-LastExitCode {
    param([string] $Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

function Get-CommandPath {
    param([string] $Name)
    $command = Get-Command -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) { return $null }
    return $command.Source
}

$originalLocation = Get-Location
try {
    Set-Location -LiteralPath $RepoRoot
    Write-Log "ANIXO build started in $RepoRoot (version $Version)"
    Write-Log 'User data in %APPDATA%\ANIXO and %LOCALAPPDATA%\ANIXO is never touched by the build.'

    $pwsh = Get-CommandPath 'pwsh'
    if (-not $pwsh) { $pwsh = Get-CommandPath 'powershell' }
    if (-not $pwsh) { throw 'PowerShell (pwsh or powershell) is required to run the build.' }

    # --- Locate / create the build Python ---------------------------------
    $venvDir = Join-Path $RepoRoot '.venv'
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Write-Log 'No .venv found; creating one with Python 3.12.'
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            & $py.Source -3.12 -m venv $venvDir
        }
        else {
            & python -m venv $venvDir
        }
        Assert-LastExitCode 'Creating .venv'
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Python virtual environment not found at $venvDir"
    }
    $pythonVersion = (& $venvPython -c 'import sys; print(sys.version.split()[0])').Trim()
    Write-Log "Build Python: $venvPython ($pythonVersion)"
    if (-not $pythonVersion.StartsWith('3.12')) {
        Write-Log "Expected Python 3.12, found $pythonVersion; continuing but results are unverified." 'SKIP'
    }

    # --- Locate / install uv (build-time only) ----------------------------
    $uvExe = Get-CommandPath 'uv'
    $localUv = Join-Path $venvDir 'Scripts\uv.exe'
    if (-not $uvExe -and (Test-Path -LiteralPath $localUv -PathType Leaf)) { $uvExe = $localUv }
    if (-not $uvExe) {
        Write-Log 'uv not found; installing it into the build environment (end users never need uv).'
        & $venvPython -m pip install uv
        Assert-LastExitCode 'Installing uv'
        $uvExe = $localUv
    }
    if (-not $uvExe) { throw 'Unable to locate uv after installation.' }
    Write-Log "uv: $uvExe"

    function Invoke-Uv {
        param([Parameter(ValueFromRemainingArguments = $true)][string[]] $UvArgs)
        if (Test-Path -LiteralPath $uvExe -PathType Leaf) {
            & $uvExe @UvArgs
        }
        else {
            & $venvPython -m uv @UvArgs
        }
    }

    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'uv.lock') -PathType Leaf)) {
        throw 'uv.lock is missing; run `uv lock` once before building.'
    }

    # --- Sync and test ----------------------------------------------------
    Write-Log 'Syncing pinned dependencies with uv (including the build group).'
    Invoke-Uv sync --frozen --inexact --group build
    Assert-LastExitCode 'uv sync --frozen --group build'

    if (-not $SkipTests) {
        Write-Log 'Running pytest before packaging.'
        & $venvPython -m pytest -q
        Assert-LastExitCode 'pytest'
        Write-Log 'pytest passed.' 'PASS'
    }
    else {
        Write-Log 'pytest skipped by request (-SkipTests).' 'SKIP'
    }

    # --- Clean build outputs (never user data) ---------------------------
    foreach ($relative in @('build', 'build-onefile', 'build-uninstaller', 'dist\Anixo', 'dist\onefile', 'dist\ANIXO-Uninstaller.exe',
                            'dist\release\portable')) {
        $path = Join-Path $RepoRoot $relative
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Path (Join-Path $RepoRoot 'dist') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $RepoRoot 'dist\onefile') -Force | Out-Null

    # --- One-folder application build ------------------------------------
    Write-Log 'Building one-folder application with anixo.spec.'
    & $venvPython -m PyInstaller --noconfirm --clean `
        --distpath (Join-Path $RepoRoot 'dist') `
        --workpath (Join-Path $RepoRoot 'build') `
        (Join-Path $RepoRoot 'anixo.spec')
    Assert-LastExitCode 'PyInstaller one-folder build'
    $mainExe = Join-Path $RepoRoot 'dist\Anixo\Anixo.exe'
    if (-not (Test-Path -LiteralPath $mainExe -PathType Leaf)) { throw "Missing $mainExe" }
    Write-Log "One-folder build ready: $mainExe" 'PASS'

    # --- Standalone uninstaller ------------------------------------------
    Write-Log 'Building standalone uninstaller with uninstaller.spec.'
    & $venvPython -m PyInstaller --noconfirm --clean `
        --distpath (Join-Path $RepoRoot 'dist') `
        --workpath (Join-Path $RepoRoot 'build-uninstaller') `
        (Join-Path $RepoRoot 'uninstaller.spec')
    Assert-LastExitCode 'PyInstaller uninstaller build'
    $uninstallerExe = Join-Path $RepoRoot 'dist\ANIXO-Uninstaller.exe'
    if (-not (Test-Path -LiteralPath $uninstallerExe -PathType Leaf)) { throw "Missing $uninstallerExe" }
    & $uninstallerExe --version | Out-Null
    Assert-LastExitCode 'uninstaller --version'
    & $uninstallerExe --help | Out-Null
    Assert-LastExitCode 'uninstaller --help'
    Write-Log "Standalone uninstaller ready: $uninstallerExe" 'PASS'

    # --- One-file validation build ---------------------------------------
    Write-Log 'Building one-file validation executable.'
    # Keep PyInstaller's generated .spec out of the repository root: on Windows
    # "Anixo.spec" and "anixo.spec" are the same file and would clobber the
    # hand-written one-folder spec.
    $oneFileSpecDir = Join-Path $RepoRoot 'build-onefile'
    New-Item -ItemType Directory -Path $oneFileSpecDir -Force | Out-Null
    $cacert = (& $venvPython -c 'import certifi; print(certifi.where())').Trim()
    if (-not (Test-Path -LiteralPath $cacert -PathType Leaf)) { throw "certifi bundle missing: $cacert" }
    $oneFileArgs = @(
        '--noconfirm', '--clean', '--onefile', '--console',
        '--name', 'Anixo',
        '--icon', (Join-Path $RepoRoot 'assets\anixo.ico'),
        '--distpath', (Join-Path $RepoRoot 'dist\onefile'),
        '--workpath', (Join-Path $RepoRoot 'build-onefile'),
        '--specpath', $oneFileSpecDir,
        '--runtime-hook', (Join-Path $RepoRoot 'packaging\anixo_runtime_hook.py'),
        '--add-data', ((Join-Path $RepoRoot 'styles\theme.tcss') + ';styles'),
        '--add-data', ((Join-Path $RepoRoot 'assets\anixo.ico') + ';assets'),
        '--add-data', ($cacert + ';certifi'),
        '--hidden-import', 'textual_image.renderable.halfcell',
        '--hidden-import', 'textual_image.renderable.sixel',
        '--hidden-import', 'textual_image.renderable.tgp',
        '--hidden-import', 'textual_image.renderable.unicode',
        '--hidden-import', 'textual_image.widget.sixel',
        '--collect-submodules', 'textual',
        '--collect-submodules', 'textual_image',
        '--collect-data', 'PIL',
        (Join-Path $RepoRoot 'main.py')
    )
    & $venvPython -m PyInstaller @oneFileArgs
    Assert-LastExitCode 'PyInstaller one-file build'
    $oneFileExe = Join-Path $RepoRoot 'dist\onefile\Anixo.exe'
    if (-not (Test-Path -LiteralPath $oneFileExe -PathType Leaf)) { throw "Missing $oneFileExe" }
    Write-Log "One-file build ready: $oneFileExe" 'PASS'

    # --- Validate both builds --------------------------------------------
    Write-Log 'Validating one-folder build.'
    & $pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'validate_build.ps1') `
        -Exe $mainExe -BundleRoot (Join-Path $RepoRoot 'dist\Anixo') `
        -Version $Version -RequireBundleFiles 1
    Assert-LastExitCode 'validate_build.ps1 (one-folder)'

    Write-Log 'Validating one-file build.'
    & $pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'validate_build.ps1') `
        -Exe $oneFileExe -BundleRoot (Split-Path -Parent $oneFileExe) `
        -Version $Version -RequireBundleFiles 0
    Assert-LastExitCode 'validate_build.ps1 (one-file)'
    Write-Log 'Both builds passed validation.' 'PASS'

    # --- Inno Setup installer (optional) ---------------------------------
    $setupExe = Join-Path $releaseDir "ANIXO-Setup-v$Version.exe"
    if ($SkipInstaller) {
        Write-Log 'Installer skipped by request (-SkipInstaller); no setup.exe was created.' 'SKIP'
    }
    else {
        $isccCommand = Get-CommandPath 'ISCC.exe'
        $iscc = @(
            $isccCommand,
            (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1

        if (-not $iscc) {
            Write-Log 'Inno Setup 6 was not found; installer step SKIPPED (not faked). Install Inno Setup 6 and re-run to produce the setup.exe.' 'SKIP'
            if (Test-Path -LiteralPath $setupExe) { Remove-Item -LiteralPath $setupExe -Force }
        }
        else {
            Write-Log "Compiling installer with $iscc"
            & $iscc /Qp (Join-Path $RepoRoot 'installer\AnixoSetup.iss')
            Assert-LastExitCode 'Inno Setup compilation'
            if (-not (Test-Path -LiteralPath $setupExe -PathType Leaf)) { throw "Inno Setup did not produce $setupExe" }
            Write-Log "Installer ready: $setupExe" 'PASS'
        }
    }

    # --- Portable ZIP -----------------------------------------------------
    if ($SkipPortable) {
        Write-Log 'Portable ZIP skipped by request (-SkipPortable).' 'SKIP'
    }
    else {
        Write-Log 'Building portable ZIP.'
        & $pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'make_portable.ps1') `
            -Version $Version -RepoRoot $RepoRoot
        Assert-LastExitCode 'make_portable.ps1'
        $portableZip = Join-Path $releaseDir "ANIXO-Portable-v$Version.zip"
        if (-not (Test-Path -LiteralPath $portableZip -PathType Leaf)) { throw "Missing $portableZip" }
        Write-Log "Portable build ready: $portableZip" 'PASS'
    }

    # --- Standalone uninstaller release asset ----------------------------
    $releaseUninstaller = Join-Path $releaseDir "ANIXO-Uninstaller-v$Version.exe"
    Copy-Item -LiteralPath $uninstallerExe -Destination $releaseUninstaller -Force
    Write-Log "Standalone uninstaller asset ready: $releaseUninstaller" 'PASS'

    # --- Hashes and report ------------------------------------------------
    Write-Log 'Generating SHA256SUMS.txt.'
    & $pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'hash.ps1') `
        -ReleaseDir $releaseDir
    Assert-LastExitCode 'hash.ps1'
    Write-Log 'Release artifacts hashed.' 'PASS'

    $artifacts = Get-ChildItem -LiteralPath $releaseDir -File |
        Where-Object { $_.Name -ne 'build-report.txt' } |
        Sort-Object Name
    foreach ($artifact in $artifacts) {
        Write-Log ("Artifact: {0} ({1:N0} bytes)" -f $artifact.Name, $artifact.Length)
    }
    Write-Log 'ANIXO build finished successfully.' 'PASS'
}
catch {
    Write-Log "BUILD FAILED: $($_.Exception.Message)" 'FAIL'
    Write-Log $_.ScriptStackTrace 'FAIL'
    throw
}
finally {
    $script:Report.Add('')
    $script:Report.Add('Release assets: ANIXO-Setup-v<version>.exe (when Inno Setup is available), ANIXO-Uninstaller-v<version>.exe, ANIXO-Portable-v<version>.zip, SHA256SUMS.txt')
    $script:Report.Add('End users need none of: Python, pip, uv, Git, Inno Setup.')
    $reportPath = Join-Path $releaseDir 'build-report.txt'
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($reportPath, [string[]] $script:Report.ToArray(), $encoding)
    Write-Host "build_windows.ps1: report written to $reportPath"
    Set-Location -LiteralPath $originalLocation
}
