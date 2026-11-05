@echo off
setlocal
cd /d "%~dp0"
docker info >nul 2>&1
if errorlevel 1 (
    echo Start Docker Desktop with Linux containers, then run this file again.
    pause
    exit /b 1
)
docker compose version >nul 2>&1
if errorlevel 1 (
    echo Docker Compose v2 is required. It is included with Docker Desktop.
    pause
    exit /b 1
)
docker image inspect autograder-improved:2.0.0 autograder-improved-sandbox:2.0.0 >nul 2>&1
if errorlevel 1 (
    docker load -i autograder-images.tar
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
docker compose up -d --wait --wait-timeout 120
if errorlevel 1 (
    docker compose logs --tail 60
    pause
    exit /b 1
)
echo.
echo Open http://127.0.0.1:8020
echo On first startup, the Administrator account and password are in the logs below.
echo Stop with: docker compose stop
docker compose logs --tail 60
pause
