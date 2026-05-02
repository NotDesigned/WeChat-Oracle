@echo off
REM Start live capture. WeFlow must be running with HTTP API enabled.
cd /d "%~dp0.."
uv run wechat-oracle init-db || exit /b 1
uv run wechat-oracle ingest live
