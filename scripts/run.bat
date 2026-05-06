@echo off
REM Start live ingest and dispatcher together.
cd /d "%~dp0.."
uv run wechat-oracle run
