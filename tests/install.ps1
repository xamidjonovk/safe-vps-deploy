$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$testHome = Join-Path ([IO.Path]::GetTempPath()) ('sd-installer-test-' + [guid]::NewGuid().ToString('N'))
try {
    & "$root/install.ps1" -Source $root -HomeDir $testHome -Agent codex
    $target = Join-Path $testHome '.codex/skills/server-deploy'
    if (-not (Test-Path "$target/SKILL.md")) { throw 'Missing installed skill' }
    Set-Content "$target/local-marker" 'keep-in-backup'
    & "$root/install.ps1" -Source $root -HomeDir $testHome -Agent codex
    $backups = Get-ChildItem (Join-Path $testHome '.codex/skill-backups') -Directory
    if ($backups.Count -ne 1 -or -not (Test-Path (Join-Path $backups[0].FullName 'local-marker'))) { throw 'Update lost previous installation' }
    Write-Output 'PowerShell install and update passed'
} finally {
    $resolved = [IO.Path]::GetFullPath($testHome)
    $parent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolved.StartsWith($parent,[StringComparison]::OrdinalIgnoreCase) -and (Split-Path $resolved -Leaf) -like 'sd-installer-test-*') { Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue }
}
