# Register the WeChat-Oracle MCP server with OpenClaw so the wechat-bot
# agent can call our group-scoped tools (recall_group_history /
# read_group_memory / etc).
#
# Idempotent: if a server with the same name is already registered, replaces
# it. If `openclaw` CLI isn't on PATH, prints the JSON snippet + instructions
# for manual registration via the Control UI.
#
# Usage (from any dir):
#   pwsh scripts\register_mcp.ps1
#   pwsh scripts\register_mcp.ps1 -Unset      # remove the registration

param(
    [switch]$Unset,
    [string]$Name = "wechat-oracle"
)

$ErrorActionPreference = "Stop"
$projectDir = (Resolve-Path "$PSScriptRoot/..").Path -replace '\\', '/'

$cli = Get-Command openclaw -ErrorAction SilentlyContinue
if (-not $cli) {
    Write-Host "openclaw CLI not found on PATH." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Either:"
    Write-Host "  1) install OpenClaw + ensure 'openclaw' is on PATH, then re-run this script"
    Write-Host "  2) register manually via the Control UI (http://127.0.0.1:18789):"
    Write-Host "     name: $Name"
    Write-Host "     config (paste verbatim):"
    Write-Host ""
    Write-Host "       {`"command`":`"uv`",`"args`":[`"run`",`"wechat-oracle`",`"openclaw`",`"mcp-serve`"],`"cwd`":`"$projectDir`"}"
    Write-Host ""
    exit 1
}

if ($Unset) {
    Write-Host "Removing MCP registration '$Name'..."
    & openclaw mcp unset $Name
    Write-Host "Done."
    exit 0
}

$config = '{"command":"uv","args":["run","wechat-oracle","openclaw","mcp-serve"],"cwd":"' + $projectDir + '"}'
Write-Host "Registering '$Name' MCP server with OpenClaw"
Write-Host "  config: $config"

$listing = & openclaw mcp list 2>$null
if ($listing -match "(?m)^$Name`$") {
    Write-Host "  (replacing existing registration)"
    & openclaw mcp unset $Name | Out-Null
}
& openclaw mcp set $Name $config

Write-Host ""
Write-Host "Done. Verify:"
Write-Host "  openclaw mcp show $Name"
Write-Host "  openclaw mcp list"
Write-Host ""
Write-Host "Then in OpenClaw Control, ensure the wechat-bot agent has '$Name' enabled."
