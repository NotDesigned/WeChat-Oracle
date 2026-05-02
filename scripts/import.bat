@echo off
REM Import a WeFlow JSON export. Usage: scripts\import.bat <path-to-export.json>
if "%~1"=="" (
    echo usage: %~nx0 ^<weflow-export.json^>
    exit /b 1
)
cd /d "%~dp0.."
uv run wechat-oracle init-db || exit /b 1
uv run wechat-oracle ingest backfill "%~1" --format weflow || exit /b 1
uv run wechat-oracle status
