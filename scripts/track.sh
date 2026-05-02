#!/usr/bin/env bash
# Start live capture for the group(s) in WO_GROUPS.
# WeFlow must be running with HTTP API enabled.
set -e
cd "$(dirname "$0")/.."
uv run wechat-oracle init-db
uv run wechat-oracle ingest live
