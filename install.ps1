# safe-vps-deploy — one-line installer for Windows (PowerShell)
#
#   irm https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.ps1 | iex
#
# Installs the skill for:
#   Claude Code  ->  %USERPROFILE%\.claude\skills\safe-vps-deploy
#   Codex        ->  %USERPROFILE%\.agents\skills\safe-vps-deploy
# Run the same line again to update.
# To uninstall:  $env:SVD_UNINSTALL=1; irm https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.ps1 | iex

$ErrorActionPreference = 'Stop'
$Repo = if ($env:SVD_REPO) { $env:SVD_REPO } else { 'xamidjonovk/safe-vps-deploy' }
$Ref  = if ($env:SVD_REF)  { $env:SVD_REF }  else { 'main' }
$Name = 'safe-vps-deploy'
$Targets = @(
    (Join-Path $HOME ".claude\skills\$Name"),
    (Join-Path $HOME ".agents\skills\$Name")
)

if ($env:SVD_UNINSTALL) {
    foreach ($t in $Targets) {
        if (Test-Path (Join-Path $t 'SKILL.md')) {
            Remove-Item -Recurse -Force $t
            Write-Host "removed $t" -ForegroundColor Green
        }
    }
    Remove-Item Env:\SVD_UNINSTALL -ErrorAction SilentlyContinue
    return
}

try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("svd-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $zip = Join-Path $tmp 'src.zip'
    Write-Host "Downloading $Repo ($Ref) ..."
    Invoke-WebRequest -UseBasicParsing -Uri "https://codeload.github.com/$Repo/zip/refs/heads/$Ref" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $tmp -Force

    $src = Get-ChildItem -Path $tmp -Directory |
        ForEach-Object { Join-Path $_.FullName "plugins\$Name\skills\$Name" } |
        Where-Object { Test-Path (Join-Path $_ 'SKILL.md') } |
        Select-Object -First 1
    if (-not $src) { throw "SKILL.md not found in the downloaded archive" }

    $verFile = Get-ChildItem -Path $tmp -Directory | ForEach-Object { Join-Path $_.FullName 'VERSION' } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    $version = if ($verFile) { (Get-Content $verFile -Raw).Trim() } else { '?' }

    foreach ($t in $Targets) {
        $parent = Split-Path $t -Parent
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        if ((Test-Path $t) -and -not (Test-Path (Join-Path $t 'SKILL.md'))) {
            throw "$t exists but is not this skill - move it away and re-run"
        }
        if (Test-Path $t) { Remove-Item -Recurse -Force $t }
        Copy-Item -Recurse -Path $src -Destination $t
        Write-Host "OK  installed v$version -> $t" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "Ready. Open your project and ask the agent, e.g.:"
    Write-Host '   "Loyihamni VPS ga deploy qil. IP: 1.2.3.4, domen: app.misol.uz"'
    Write-Host "Claude Code: start a NEW session (or run /reload-plugins) so it picks up the skill."
    Write-Host "Update later: run the same install command again."
}
finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
