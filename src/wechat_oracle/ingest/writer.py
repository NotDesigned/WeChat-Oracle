"""Persists normalized Message objects to SQLite.

Every importer (live, backfill, ...) funnels through `write_messages`. Dedupe is enforced by
the UNIQUE(dedupe_key) constraint in schema, so re-running an importer is safe.

Messages of type='forward' may carry `forwarded_items` — children of a 合并转发
bundle. After the parent row is inserted, those children are written into
`forwarded_records` keyed by the parent's msg_id. The link is resolved by
re-querying the dedupe_key, since `executemany` doesn't expose per-row
`lastrowid`. Children are also dedup-protected via UNIQUE(parent_msg_id, seq),
so re-running an import is idempotent end-to-end.
"""

import sqlite3
from collections import Counter
from collections.abc import Iterable

from loguru import logger

from ..db import transaction
from ..log_utils import append_event
from ..models import Message

INSERT_SQL = """
INSERT OR IGNORE INTO messages (
    wx_msg_id, group_id, group_name, sender_wxid, sender_display,
    t, type, content_text, media_path, reply_to_wx_msg_id, quote_text,
    transcript, source, status, dedupe_key
) VALUES (
    :wx_msg_id, :group_id, :group_name, :sender_wxid, :sender_display,
    :t, :type, :content_text, :media_path, :reply_to_wx_msg_id, :quote_text,
    :transcript, :source, :status, :dedupe_key
)
"""

INSERT_FWD_SQL = """
INSERT OR IGNORE INTO forwarded_records (
    parent_msg_id, seq, sender_display, t, datatype, content, src_msg_id, media_path
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        "transcript": msg.transcript,
        "source": msg.source,
        "status": msg.status.value,
        "dedupe_key": msg.compute_dedupe_key(),
    }


def _write_forwarded_for_batch(
    conn: sqlite3.Connection, parents: list[Message]
) -> int:
    """Persist children of forward-type messages in `parents`. Resolves each
    parent's msg_id via its dedupe_key (set whether or not the row was newly
    inserted in this batch — covers re-run idempotency too).
    """
    has_items = [m for m in parents if m.forwarded_items]
    if not has_items:
        return 0
    keys = [m.compute_dedupe_key() for m in has_items]
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT msg_id, dedupe_key FROM messages WHERE dedupe_key IN ({placeholders})",
        keys,
    ).fetchall()
    id_by_key = {r["dedupe_key"]: r["msg_id"] for r in rows}

    fwd_rows: list[tuple] = []
    for m in has_items:
        pid = id_by_key.get(m.compute_dedupe_key())
        if pid is None:
            # parent insert failed AND no prior row matched → unreachable in
            # practice (dedupe_key is deterministic), but be defensive.
            continue
        for it in m.forwarded_items:
            fwd_rows.append((
                pid, it.seq, it.sender_display, it.t,
                it.datatype, it.content, it.src_msg_id, it.media_path,
            ))
    if not fwd_rows:
        return 0
    cur = conn.executemany(INSERT_FWD_SQL, fwd_rows)
    conn.execute(
        """
        UPDATE forwarded_records
           SET media_path = (
               SELECT m.media_path
                 FROM messages m
                WHERE m.wx_msg_id = forwarded_records.src_msg_id
                  AND m.type = 'image'
                  AND m.media_path IS NOT NULL
                ORDER BY m.msg_id DESC
                LIMIT 1
           )
         WHERE media_path IS NULL
           AND datatype = 2
           AND src_msg_id IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM messages m
                WHERE m.wx_msg_id = forwarded_records.src_msg_id
                  AND m.type = 'image'
                  AND m.media_path IS NOT NULL
           )
        """
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def write_messages(
    conn: sqlite3.Connection,
    messages: Iterable[Message],
    batch_size: int = 500,
) -> tuple[int, int]:
    """Insert messages, batched in a single transaction per batch.

    Returns (attempted, inserted). `inserted < attempted` indicates dedupe hits.
    Forwarded children are written within the same transaction as their parents
    (and counted separately in the log).
    """
    attempted = 0
    inserted = 0
    fwd_inserted = 0
    batch_msgs: list[Message] = []

    def flush() -> None:
        nonlocal inserted, fwd_inserted
        if not batch_msgs:
            return
        batch_attempted = len(batch_msgs)
        by_source = Counter(m.source for m in batch_msgs)
        by_type = Counter(m.type.value for m in batch_msgs)
        by_group = Counter(m.group_id for m in batch_msgs)
        with transaction(conn):
            cur = conn.executemany(INSERT_SQL, [_row(m) for m in batch_msgs])
            batch_inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            batch_fwd_inserted = _write_forwarded_for_batch(conn, batch_msgs)
            inserted += batch_inserted
            fwd_inserted += batch_fwd_inserted
        append_event(
            "ingest.write_batch",
            attempted=batch_attempted,
            inserted=batch_inserted,
            duplicates=batch_attempted - batch_inserted,
            forwarded_items=batch_fwd_inserted,
            source=dict(by_source),
            type=dict(by_type),
            groups=len(by_group),
            top_groups=dict(by_group.most_common(5)),
        )
        batch_msgs.clear()

    for msg in messages:
        batch_msgs.append(msg)
        attempted += 1
        if len(batch_msgs) >= batch_size:
            flush()
    flush()

    skipped = attempted - inserted
    if fwd_inserted:
        logger.info(
            "wrote {} messages ({} new, {} duplicates skipped); +{} forwarded items",
            attempted, inserted, skipped, fwd_inserted,
        )
    else:
        logger.info(
            "wrote {} messages ({} new, {} duplicates skipped)",
            attempted, inserted, skipped,
        )
    return attempted, inserted
