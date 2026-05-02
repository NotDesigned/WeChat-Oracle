#!/usr/bin/env bash
# Import a WeFlow JSON export into the same database.
# Usage: ./scripts/import.sh <path-to-weflow-export.json>
set -e
if [ -z "$1" ]; then
  echo "usage: $0 <weflow-export.json>" >&2
  exit 1
fi
cd "$(dirname "$0")/.."
uv run wechat-oracle init-db
uv run wechat-oracle ingest backfill "$1" --format weflow
uv run wechat-oracle status
