"""Resolve `messages.media_path` values to filesystem paths.

New live/backfill rows store data_dir-relative paths under `data/media`.
Absolute paths are still accepted so older DB rows can be read before running
`scripts/normalize_media_paths.py`.
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


def resolve_quoted_msg_meta(
    conn: sqlite3.Connection, wx_msg_id: str | None
) -> tuple[int | None, str | None]:
    """Resolve a quote-reply's wx_msg_id (from `<refermsg><svrid>`) to the
    referenced message's integer `msg_id` and `type`. Returns `(None, None)`
    when the quote target isn't in our DB (parent never ingested, or no quote).
    """
    if not wx_msg_id:
        return None, None
    row = conn.execute(
        "SELECT msg_id, type FROM messages WHERE wx_msg_id=?",
        (wx_msg_id,),
    ).fetchone()
    if row is None:
        return None, None
    return int(row["msg_id"]), row["type"]


def openclaw_quoted_hint(
    *, group_id: str, msg_id: int, msg_type: str | None
) -> str:
    """Single-line hint for OpenClaw mode telling wechat-bot which MCP tool
    to call for the quoted message's content. Empty string when there's no
    matching rich-content tool — text/link/etc. quotes are already inlined as
    `quoted_text` so the bot doesn't need a tool for them.

    The hint exists because the dispatcher only inlines `quoted_text` (which
    can be a placeholder like `[图片]` or `[卡片消息]`); without this hint the
    bot has no way to know it can expand the quote via MCP.
    """
    if msg_type == "image":
        return (
            f"OpenClaw MCP hint: the user quoted an image message. "
            f"To see the image, call read_image(group_id={group_id!r}, msg_id={msg_id})."
        )
    if msg_type == "voice":
        return (
            f"OpenClaw MCP hint: the user quoted a voice message. "
            f"For the transcript, call read_voice(group_id={group_id!r}, msg_id={msg_id})."
        )
    if msg_type == "forward":
        return (
            f"OpenClaw MCP hint: the user quoted a merged-forward bundle (聊天记录 / [卡片消息]). "
            f"To list its children, call expand_forward(group_id={group_id!r}, msg_id={msg_id})."
        )
    return ""
