# Start API (:8000), worker and the Vite dev server (:5173). Ctrl+C stops all three.
#   powershell -ExecutionPolicy Bypass -File scripts\run_dev.ps1 [-NoFrontend]
# Settings come from .env / the environment (DATA_DIR, SANDBOX, LLM_CACHE_DIR, ...).
param([switch]$NoFrontend, [int]$ApiPort = 8000)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$env:PYTHONPATH = (Join-Path $Root "backend") + $(if ($env:PYTHONPATH) { ";" + $env:PYTHONPATH } else { "" })
$env:PYTHONUNBUFFERED = "1"
$jobs = if ($env:WORKER_JOBS) { $env:WORKER_JOBS } else { "2" }
$workerId = "dev-$env:COMPUTERNAME-$PID"
$boxes = if ($env:MAX_SANDBOXES) { $env:MAX_SANDBOXES } else { "3" }

& $Py -m autograder.cli init-db
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Py -m autograder.cli bootstrap-admin

$procs = @()
$procs += Start-Process -FilePath $Py -ArgumentList "-m", "uvicorn", "autograder.api.main:app", "--port", $ApiPort `
    -NoNewWindow -PassThru
$procs += Start-Process -FilePath $Py -ArgumentList "-m", "autograder.worker", "--jobs", $jobs, "--sandboxes", $boxes, "--worker-id", $workerId `
    -NoNewWindow -PassThru
if (-not $NoFrontend) {
    $front = Join-Path $Root "frontend"
    if (-not (Test-Path (Join-Path $front "node_modules"))) {
        Push-Location $front; npm install; Pop-Location
    }
    $procs += Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" -WorkingDirectory $front `
        -NoNewWindow -PassThru
}

Write-Host "api http://localhost:$ApiPort/api/health   ui http://localhost:5173   (Ctrl+C to stop)"
try {
    while ($true) {
        if ($procs | Where-Object { $_.HasExited }) { break }
        Start-Sleep -Seconds 1
    }
}
finally {
    foreach ($p in $procs) {
        if (-not $p.HasExited) {
            # taskkill /T also stops the children (node under cmd, uvicorn reloader)
            taskkill /PID $p.Id /T /F | Out-Null
        }
    }
    # the worker removes its own containers on a clean exit; this catches a hard stop
    docker ps -aq --filter "label=autograder.worker=$workerId" 2>$null | ForEach-Object { docker rm -f $_ | Out-Null }
}
