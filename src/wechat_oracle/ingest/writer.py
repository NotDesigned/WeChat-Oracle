"""Persists normalized Message objects to SQLite.

Every importer (live, backfill, ...) funnels through `write_messages`. Dedupe is enforced by
the (source, dedupe_key) UNIQUE index in schema, so re-running an importer is safe.
"""

import sqlite3
from collections.abc import Iterable

from loguru import logger

from ..db import transaction
from ..models import Message

INSERT_SQL = """
INSERT OR IGNORE INTO messages (
    wx_msg_id, group_id, group_name, sender_wxid, sender_display,
    t, type, content_text, media_path, reply_to_wx_msg_id, quote_text,
    source, status, dedupe_key
) VALUES (
    :wx_msg_id, :group_id, :group_name, :sender_wxid, :sender_display,
    :t, :type, :content_text, :media_path, :reply_to_wx_msg_id, :quote_text,
    :source, :status, :dedupe_key
)
"""


def _row(msg: Message) -> dict:
    return {
        "wx_msg_id": msg.wx_msg_id,
        "group_id": msg.group_id,
        "group_name": msg.group_name,
        "sender_wxid": msg.sender_wxid,
        "sender_display": msg.sender_display,
        "t": msg.t,
        "type": msg.type.value,
        "content_text": msg.content_text,
        "media_path": msg.media_path,
        "reply_to_wx_msg_id": msg.reply_to_wx_msg_id,
        "quote_text": msg.quote_text,
        "source": msg.source,
        "status": msg.status.value,
        "dedupe_key": msg.compute_dedupe_key(),
    }


def write_messages(
    conn: sqlite3.Connection,
    messages: Iterable[Message],
    batch_size: int = 500,
) -> tuple[int, int]:
    """Insert messages, batched in a single transaction per batch.

    Returns (attempted, inserted). `inserted < attempted` indicates dedupe hits.
    """
    attempted = 0
    inserted = 0
    batch: list[dict] = []

    def flush() -> None:
        nonlocal inserted
        if not batch:
            return
        with transaction(conn):
            cur = conn.executemany(INSERT_SQL, batch)
            inserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        batch.clear()

    for msg in messages:
        batch.append(_row(msg))
        attempted += 1
        if len(batch) >= batch_size:
            flush()
    flush()

    skipped = attempted - inserted
    logger.info(
        "wrote {} messages ({} new, {} duplicates skipped)",
        attempted, inserted, skipped,
    )
    return attempted, inserted
