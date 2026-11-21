<#
.SYNOPSIS
    Generates SHA256SUMS.txt for the ANIXO release artifacts.

.DESCRIPTION
    By default every file in dist\release is hashed except SHA256SUMS.txt
    itself and build-report.txt (a diagnostic log, not a release asset).
    Pass -Files to hash an explicit list instead.
#>
[CmdletBinding()]
param(
    [string] $ReleaseDir = '',
    [string] $Output,
    [string[]] $Files = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ReleaseDir)) {
    $ReleaseDir = Join-Path (Split-Path -Parent (Split-Path -Parent $PSCommandPath)) 'dist\release'
}

if (-not (Test-Path -LiteralPath $ReleaseDir -PathType Container)) {
    New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
}

if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $ReleaseDir 'SHA256SUMS.txt'
}

if ($Files.Count -gt 0) {
    $items = @(
        foreach ($file in $Files) {
            if (Test-Path -LiteralPath $file -PathType Leaf) {
                Get-Item -LiteralPath $file
            }
            else {
                throw "hash.ps1: artifact not found: $file"
            }
        }
    )
}
else {
    $items = @(
        Get-ChildItem -LiteralPath $ReleaseDir -File |
            Where-Object { $_.Name -ne 'SHA256SUMS.txt' -and $_.Name -ne 'build-report.txt' }
    )
}

if ($items.Count -eq 0) {
    throw "hash.ps1: no release artifacts found in $ReleaseDir"
}

$lines = @(
    $items |
        Sort-Object -Property Name |
        ForEach-Object {
            $digest = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            '{0}  {1}' -f $digest, $_.Name
        }
)

$encoding = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($Output, [string[]] $lines, $encoding)
Write-Host "hash.ps1: wrote $($lines.Count) hash(es) to $Output"
