"""Resolve `messages.media_path` values to filesystem paths.

Two storage conventions coexist (see worker/mm.py:_resolve_path):
  - live    → absolute path into WeFlow's cache directory
  - backfill → relative path under settings.data_dir

This module is the single source of truth for that resolution; previously
the same logic was inlined in `worker/mm.py`, `dispatcher._resolve_image_paths`,
and `dispatcher._resolve_quoted_image_path`. The agent's `read_image` tool
also routes through here, and dispatcher's legacy /explain & /ask paths
import these helpers (CLAUDE.md F16 keeps them around).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import settings


def resolve_path(media_path: str) -> Path:
    """Turn a `messages.media_path` value into a Path. Caller checks .exists()."""
    p = Path(media_path)
    if p.is_absolute():
        return p
    return settings.data_dir / media_path


def resolve_media_path_for_msg(
    conn: sqlite3.Connection, msg_id: int, *, expected_type: str | None = None
) -> Path | None:
    """Load row by msg_id, optionally check the type, return resolved path.

    Returns None when:
      - the row doesn't exist
      - the row's type doesn't match `expected_type` (when given)
      - media_path is NULL
      - the file doesn't exist on disk
    """
    row = conn.execute(
        "SELECT type, media_path FROM messages WHERE msg_id=?",
        (msg_id,),
    ).fetchone()
    if row is None or not row["media_path"]:
        return None
    if expected_type is not None and row["type"] != expected_type:
        return None
    p = resolve_path(row["media_path"])
    return p if p.exists() else None


def resolve_image_paths_by_cand(
    conn: sqlite3.Connection, cand_ids: list[str]
) -> list[Path]:
    """Resolve a list of `m:<msg_id>` cand_ids (the legacy /find / chat
    sentinel format) to image paths on disk.

    Skips silently:
      - `f:<...>` forwarded children (their inline images aren't downloaded)
      - cand_ids that aren't `type='image'` or have no `media_path`
      - files that don't exist on disk
    """
    paths: list[Path] = []
    for cid in cand_ids:
        if not cid.startswith("m:"):
            continue
        try:
            msg_id = int(cid[2:])
        except ValueError:
            continue
        p = resolve_media_path_for_msg(conn, msg_id, expected_type="image")
        if p is not None:
            paths.append(p)
    return paths


def resolve_quoted_image_path(
    conn: sqlite3.Connection, wx_msg_id: str | None
) -> Path | None:
    """If `wx_msg_id` (from a quote-reply's `<refermsg><svrid>`) points to
    an image row whose media file is on disk, return its resolved path.
    None for everything else — non-image, no media_path, file missing,
    or no quote at all."""
    if not wx_msg_id:
        return None
    row = conn.execute(
        "SELECT msg_id FROM messages WHERE wx_msg_id=?",
        (wx_msg_id,),
    ).fetchone()
    if row is None:
        return None
    return resolve_media_path_for_msg(conn, int(row["msg_id"]), expected_type="image")
