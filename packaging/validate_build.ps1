<#
.SYNOPSIS
    Smoke-validates a built Anixo.exe (one-folder or one-file).

.DESCRIPTION
    Runs --version, --help and --diagnose, requires exit code 0 from each,
    checks the ANIXO identity/version strings, and (for one-folder builds)
    verifies that styles/theme.tcss and assets/anixo.ico were bundled.
    Exits with a non-zero code when any check fails.
#>
[CmdletBinding()]
param(
    [string] $Exe,
    [string] $BundleRoot,
    [string] $Version = '0.1.0-beta.3',
    # String instead of [bool]: powershell.exe -File passes native arguments as
    # strings and Windows PowerShell 5.1 refuses to bind them to [bool].
    [string] $RequireBundleFiles = 'true'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Capture native exit codes ourselves instead of throwing on stderr output.
$PSNativeCommandUseErrorActionPreference = $false

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
if ([string]::IsNullOrWhiteSpace($Exe)) {
    $Exe = Join-Path $repoRoot 'dist\Anixo\Anixo.exe'
}

$failures = [System.Collections.Generic.List[string]]::new()

$exeItem = Get-Item -LiteralPath $Exe -ErrorAction SilentlyContinue
if (-not $exeItem -or $exeItem.PSIsContainer) {
    Write-Host "validate_build.ps1: FAIL - executable not found: $Exe"
    exit 1
}
if ($exeItem.Name -ne 'Anixo.exe') {
    $failures.Add("Executable must be named Anixo.exe (found: $($exeItem.Name))")
}

if ([string]::IsNullOrWhiteSpace($BundleRoot)) {
    $BundleRoot = Split-Path -Parent $exeItem.FullName
}

$requireBundleFilesResolved = $RequireBundleFiles -match '^(?i:1|true|yes)$'
if ($requireBundleFilesResolved) {
    # PyInstaller 6.x stores bundled data under _internal in one-folder builds.
    foreach ($relative in @('styles\theme.tcss', 'assets\anixo.ico')) {
        $candidates = @(
            (Join-Path $BundleRoot (Join-Path '_internal' $relative)),
            (Join-Path $BundleRoot $relative)
        )
        $found = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (-not $found) {
            $failures.Add("Bundled file missing: $relative (looked in $($candidates -join ', '))")
        }
        else {
            Write-Host "validate_build.ps1: found $relative"
        }
    }
}
else {
    Write-Host 'validate_build.ps1: bundle file check skipped (one-file build embeds resources)'
}

function Invoke-AnixoSmokeCheck {
    param(
        [string] $Path,
        [string] $Flag,
        [string] $Label
    )

    $captured = & $Path $Flag 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    $text = [string] $captured

    Write-Host "validate_build.ps1: $Label -> exit $exitCode"
    if ($exitCode -ne 0) {
        $script:failures.Add("$Label exited with code $exitCode`: $($text.Trim())")
    }
    return $text
}

$versionText = Invoke-AnixoSmokeCheck -Path $exeItem.FullName -Flag '--version' -Label '--version'
$helpText = Invoke-AnixoSmokeCheck -Path $exeItem.FullName -Flag '--help' -Label '--help'
$diagnoseText = Invoke-AnixoSmokeCheck -Path $exeItem.FullName -Flag '--diagnose' -Label '--diagnose'

if ($versionText -notmatch '(?i)anixo') {
    $failures.Add('--version output does not contain the ANIXO identity')
}
if (-not $versionText.Contains($Version)) {
    $failures.Add("--version output does not contain $Version`: $($versionText.Trim())")
}
if ($helpText -notmatch '(?i)anixo') {
    $failures.Add('--help output does not contain the ANIXO identity')
}
if ([string]::IsNullOrWhiteSpace($diagnoseText)) {
    $failures.Add('--diagnose produced no output')
}

if ($failures.Count -gt 0) {
    Write-Host 'validate_build.ps1: VALIDATION FAILED'
    foreach ($failure in $failures) {
        Write-Host "  - $failure"
    }
    exit 1
}

Write-Host "validate_build.ps1: VALIDATION PASSED for $($exeItem.FullName)"
exit 0
