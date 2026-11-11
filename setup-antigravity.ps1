# Locate the Google Antigravity CLI ('agy') on this PC and register it for AI DEV DIRECTOR (AGY_BIN, user scope).
$ErrorActionPreference = 'SilentlyContinue'
$found = $null
$cmd = Get-Command agy -ErrorAction SilentlyContinue
if ($cmd) { $found = $cmd.Source }
if (-not $found) {
    $roots = @("$env:LOCALAPPDATA\agy", "$env:LOCALAPPDATA\Programs", "$env:ProgramFiles", "${env:ProgramFiles(x86)}", "$env:APPDATA\npm", "$HOME\.agy", "$HOME\.local\bin")
    foreach ($r in $roots) {
        if (-not (Test-Path $r)) { continue }
        $hit = Get-ChildItem $r -Recurse -Depth 4 -Include agy.exe,agy.cmd,agy.ps1 -ErrorAction SilentlyContinue |
               Where-Object { $_.FullName -match 'agy|antigravity' } | Select-Object -First 1
        if ($hit) { $found = $hit.FullName; break }
    }
}
if (-not $found) {
    Write-Output '[MISSING] agy (Antigravity CLI) was not found on this PC.'
    Write-Output 'Install the Antigravity CLI (from the Antigravity app/docs), sign in with your Google account, then run this script again.'
    Write-Output 'Until then the Director fails over: REVIEWER -> claude (config.yaml stays as is).'
    exit 1
}
[Environment]::SetEnvironmentVariable('AGY_BIN', $found, 'User')
$env:AGY_BIN = $found
Write-Output "[OK] agy -> $found  (AGY_BIN registered for your user; reopen PowerShell)"
& $found --version
