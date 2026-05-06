#!/usr/bin/env bash
# Register the WeChat-Oracle MCP server with OpenClaw so the wechat-bot
# agent can call our group-scoped tools.
#
# Idempotent: replaces any existing registration with the same name. If
# openclaw isn't on PATH, prints the JSON snippet + manual instructions.
#
# Usage:
#   scripts/register_mcp.sh
#   scripts/register_mcp.sh --unset

set -euo pipefail

NAME="wechat-oracle"
UNSET=0

for arg in "$@"; do
    case "$arg" in
        --unset) UNSET=1 ;;
        --name=*) NAME="${arg#*=}" ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v openclaw >/dev/null 2>&1; then
    cat >&2 <<EOF
openclaw CLI not found on PATH.

Either:
  1) install OpenClaw + ensure 'openclaw' is on PATH, then re-run this script
  2) register manually via the Control UI (http://127.0.0.1:18789):
     name: $NAME
     config (paste verbatim):

       {"command":"uv","args":["run","wechat-oracle","openclaw","mcp-serve"],"cwd":"$PROJECT_DIR"}

EOF
    exit 1
fi

if [ "$UNSET" = "1" ]; then
    echo "Removing MCP registration '$NAME'..."
    openclaw mcp unset "$NAME"
    echo "Done."
    exit 0
fi

CONFIG='{"command":"uv","args":["run","wechat-oracle","openclaw","mcp-serve"],"cwd":"'"$PROJECT_DIR"'"}'
echo "Registering '$NAME' MCP server with OpenClaw"
echo "  config: $CONFIG"

if openclaw mcp list 2>/dev/null | grep -qx "$NAME"; then
    echo "  (replacing existing registration)"
    openclaw mcp unset "$NAME" >/dev/null
fi
openclaw mcp set "$NAME" "$CONFIG"

echo ""
echo "Done. Verify:"
echo "  openclaw mcp show $NAME"
echo "  openclaw mcp list"
echo ""
echo "Then in OpenClaw Control, ensure the wechat-bot agent has '$NAME' enabled."
