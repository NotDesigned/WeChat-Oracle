#!/usr/bin/env bash
set -euo pipefail

# Register the WeChat-Oracle MCP server with OpenClaw. Agent/SKILL import is
# left as a UI or version-specific CLI step because OpenClaw's agent config
# commands have changed across releases.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME="${1:-wechat-oracle}"
CONFIG='{"command":"uv","args":["run","wechat-oracle","openclaw","mcp-serve"],"cwd":"'"$ROOT"'"}'

if ! command -v openclaw >/dev/null 2>&1; then
  echo "openclaw CLI not found on PATH"
  echo "Manual MCP config:"
  echo "$CONFIG"
  exit 1
fi

cd "$ROOT"
openclaw mcp unset "$NAME" >/dev/null 2>&1 || true
openclaw mcp set "$NAME" "$CONFIG"

echo "Registered MCP server '$NAME'."
echo "Import examples/openclaw/wechat-bot.SKILL.md into your OpenClaw agent."
echo "Set WO_AGENT_BACKEND=openclaw and WO_OPENCLAW_TOKEN before running dispatcher."
