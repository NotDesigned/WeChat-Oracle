@echo off
setlocal

REM Register the WeChat-Oracle MCP server with OpenClaw.

where openclaw >nul 2>nul
if errorlevel 1 (
  echo openclaw CLI not found on PATH
  exit /b 1
)

set NAME=%1
if "%NAME%"=="" set NAME=wechat-oracle
for %%I in ("%~dp0..\..") do set ROOT=%%~fI
set CONFIG={"command":"uv","args":["run","wechat-oracle","openclaw","mcp-serve"],"cwd":"%ROOT:\=/%"}

openclaw mcp unset "%NAME%" >nul 2>nul
openclaw mcp set "%NAME%" "%CONFIG%"

echo Registered MCP server "%NAME%".
echo Import examples\openclaw\wechat-bot.SKILL.md into your OpenClaw agent.
echo Set WO_AGENT_BACKEND=openclaw and WO_OPENCLAW_TOKEN before running dispatcher.
