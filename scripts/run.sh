#!/usr/bin/env bash
# Start live ingest and dispatcher together.
set -e
cd "$(dirname "$0")/.."
uv run wechat-oracle run
