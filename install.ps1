[CmdletBinding()]
param(
    [ValidateSet('both','codex','claude')][string]$Agent = 'both',
    [string]$Source,
    [string]$HomeDir = [Environment]::GetFolderPath('UserProfile')
)
$ErrorActionPreference = 'Stop'
$Repo = 'xamidjonovk/safe-vps-deploy'
$Name = 'server-deploy'
$tempDir = Join-Path ([IO.Path]::GetTempPath()) ('server-deploy-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null
try {
    if ($Source) {
        $sourceRoot = (Resolve-Path -LiteralPath $Source).Path
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri "https://codeload.github.com/$Repo/zip/refs/heads/main" -OutFile (Join-Path $tempDir 'source.zip')
        Expand-Archive -LiteralPath (Join-Path $tempDir 'source.zip') -DestinationPath $tempDir
        $sourceRoot = (Get-ChildItem -LiteralPath $tempDir -Directory | Select-Object -First 1).FullName
    }
    $skillSource = Join-Path $sourceRoot 'plugins\server-deploy\skills\server-deploy'
    if (-not (Test-Path -LiteralPath (Join-Path $skillSource 'SKILL.md'))) { throw 'Download is missing SKILL.md' }
    $targets = @()
    if ($Agent -in @('both','codex')) { $targets += Join-Path $HomeDir '.codex\skills\server-deploy' }
    if ($Agent -in @('both','claude')) { $targets += Join-Path $HomeDir '.claude\skills\server-deploy' }
    foreach ($target in $targets) {
        $resolvedTarget = [IO.Path]::GetFullPath($target)
        $resolvedHome = [IO.Path]::GetFullPath($HomeDir).TrimEnd('\') + '\'
        if (-not $resolvedTarget.StartsWith($resolvedHome, [StringComparison]::OrdinalIgnoreCase)) { throw 'Target outside requested home' }
        $parent = Split-Path $resolvedTarget -Parent
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        $staged = Join-Path $tempDir ('stage-' + [guid]::NewGuid().ToString('N'))
        Copy-Item -LiteralPath $skillSource -Destination $staged -Recurse
        $backup = $null
        if (Test-Path -LiteralPath $resolvedTarget) {
            $manifest = Join-Path $resolvedTarget 'SKILL.md'
            if (-not (Test-Path -LiteralPath $manifest) -or (Get-Content -LiteralPath $manifest -Raw) -notmatch '(?m)^name:\s*server-deploy\s*$') { throw "Refusing to replace unrelated directory: $resolvedTarget" }
            $backupRoot = Join-Path (Split-Path $parent -Parent) 'skill-backups'
            New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
            $backup = Join-Path $backupRoot ('server-deploy-' + [guid]::NewGuid().ToString('N'))
            Move-Item -LiteralPath $resolvedTarget -Destination $backup
        }
        try { Move-Item -LiteralPath $staged -Destination $resolvedTarget }
        catch {
            if ($backup -and -not (Test-Path -LiteralPath $resolvedTarget)) { Move-Item -LiteralPath $backup -Destination $resolvedTarget }
            throw
        }
        Write-Host "Installed: $resolvedTarget"
        if ($backup) { Write-Host "Backup: $backup" }
    }
    Write-Host 'Ready. Open a new chat and invoke $server-deploy. Requires Python 3.11+, uv and OpenSSH.'
} finally {
    $resolvedTemp = [IO.Path]::GetFullPath($tempDir)
    $expectedTempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolvedTemp.StartsWith($expectedTempParent, [StringComparison]::OrdinalIgnoreCase) -and (Split-Path $resolvedTemp -Leaf) -like 'server-deploy-*') {
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
