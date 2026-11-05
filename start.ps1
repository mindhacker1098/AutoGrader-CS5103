param([int]$Port = 8020)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$studioPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$studioData = Join-Path $PSScriptRoot 'data'
if (-not (Test-Path -LiteralPath $studioPython)) { throw 'Create .venv and install dependencies first; see README.md.' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\dist\index.html'))) { throw 'Run npm run build in frontend first.' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port is in use. Choose another with -Port." }
New-Item -ItemType Directory -Path $studioData -Force | Out-Null
$env:DATA_DIR = $studioData
$env:DATABASE_URL = ''
$env:LLM_CACHE_DIR = Join-Path $studioData 'cache\llm'
$env:PYTHONPATH = Join-Path $PSScriptRoot 'backend'
$env:SANDBOX = 'docker'
$env:PYTHONUNBUFFERED = '1'
& $studioPython -m autograder.cli init-db
if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed.' }
& $studioPython -m autograder.cli bootstrap-admin
if ($LASTEXITCODE -ne 0) { throw 'Administrator setup failed.' }
$studioProcesses = @()
try {
    $studioProcesses += Start-Process -FilePath $studioPython -WindowStyle Hidden -PassThru -WorkingDirectory $PSScriptRoot `
        -ArgumentList '-m','uvicorn','autograder.api.main:app','--host','127.0.0.1','--port',"$Port" `
        -RedirectStandardOutput (Join-Path $studioData 'server.log') -RedirectStandardError (Join-Path $studioData 'server.err.log')
    $studioProcesses += Start-Process -FilePath $studioPython -WindowStyle Hidden -PassThru -WorkingDirectory $PSScriptRoot `
        -ArgumentList '-m','autograder.worker','--jobs','2','--sandboxes','3' `
        -RedirectStandardOutput (Join-Path $studioData 'worker.log') -RedirectStandardError (Join-Path $studioData 'worker.err.log')
    Write-Host "Open http://127.0.0.1:$Port (Ctrl+C stops this copy)."
    while (-not ($studioProcesses | Where-Object { $_.HasExited })) { Start-Sleep -Seconds 1 }
    throw 'A server process exited. See data/*.log.'
}
finally {
    foreach ($studioProcess in $studioProcesses) {
        if (-not $studioProcess.HasExited) { Stop-Process -Id $studioProcess.Id -ErrorAction SilentlyContinue }
    }
}
