# Windows environment check for AI DEV DIRECTOR: HEAD=claude, BUILDER=codex, REVIEWER=antigravity (+ optional local LLM).
$env:Path += ";C:\Program Files\Git\cmd;C:\Program Files\nodejs;$env:APPDATA\npm;$env:LOCALAPPDATA\Programs\Ollama"
python -m pip install -q -r requirements.txt
foreach ($c in 'git','python','claude','codex','agy','ollama') {
    $x = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $x -and $c -eq 'agy' -and $env:AGY_BIN -and (Test-Path $env:AGY_BIN)) { $x = @{Source=$env:AGY_BIN} }
    if ($x) { Write-Output ("[OK]      {0,-7} {1}" -f $c, $x.Source) } else { Write-Output ("[MISSING] {0}" -f $c) }
}
Write-Output 'Roles (config.yaml): head=claude  builder=codex  reviewer=antigravity  | optional: reviewer/head -> local'
Write-Output 'agy missing? run .\setup-antigravity.ps1'
Write-Output 'Console: .\start-companion.ps1  ->  http://127.0.0.1:8081'
