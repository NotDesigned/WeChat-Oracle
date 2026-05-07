"""Proactive continuation outbox.

Agent turns can register a delayed follow-up by intent. The system never
stores pre-generated reply text; when a job becomes due, dispatcher reruns the
agent with fresh recent context and the saved intent.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Any

from ..config import settings
from ..log_utils import append_event


CANCELABLE_STATUSES = ("planned", "pending", "running")
BLOCKING_STATUSES = ("pending", "running")
TERMINAL_STATUSES = ("sent", "cancelled", "expired", "failed")


def new_continuation_token() -> str:
    return uuid.uuid4().hex


def clamp_delay(value: Any) -> int:
    if not isinstance(value, int) or value <= 0:
        value = settings.agent_continuation_delay_seconds
    ttl = max(1, settings.agent_continuation_ttl_seconds)
    return max(5, min(value, ttl))


def clamp_max_followups(value: Any, *, inherited: int | None = None) -> int:
    cap = max(0, settings.agent_continuation_max_followups)
    if inherited is not None and inherited > 0:
        cap = min(cap, inherited)
    if not isinstance(value, int) or value <= 0:
        value = cap
    return max(0, min(value, cap))


def progress_for_token(conn: sqlite3.Connection, continuation_token: str) -> tuple[int, int | None]:
    row = conn.execute(
        """
        SELECT MAX(sequence) AS sequence, MAX(max_sequence) AS max_sequence
          FROM agent_proactive_outbox
         WHERE continuation_token=?
        """,
        (continuation_token,),
    ).fetchone()
    if row is None or row["sequence"] is None:
        return (0, None)
    return (int(row["sequence"]), int(row["max_sequence"]) if row["max_sequence"] is not None else None)


def plan_followup(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    group_name: str | None,
    continuation_token: str,
    kind: str,
    delay_seconds: int,
    intent: str,
    reason: str,
    source_trigger_msg_id: int | None,
    source_trigger_kind: str | None,
    source_job_id: int | None,
    current_sequence: int,
    max_sequence: int,
    anchor_msg_id: int | None,
) -> str:
    """Insert a planned follow-up row and return a tool-facing summary."""
    if not settings.agent_continuation_enabled:
        return "follow-up not scheduled: continuation is disabled"
    continuation_token = (continuation_token or "").strip()
    if not continuation_token:
        return "follow-up not scheduled: continuation_token is required"
    group_id = (group_id or "").strip()
    if not group_id:
        return "follow-up not scheduled: group_id is required"
    kind = (kind or "").strip().lower()
    if kind not in {"committed", "thread"}:
        return "follow-up not scheduled: kind must be committed or thread"
    intent = (intent or "").strip()
    if not intent:
        return "follow-up not scheduled: intent is required"
    reason = (reason or "").strip()
    if current_sequence >= max_sequence:
        return (
            "follow-up not scheduled: continuation limit already reached "
            f"({current_sequence}/{max_sequence})"
        )
    existing_planned = conn.execute(
        """
        SELECT job_id FROM agent_proactive_outbox
         WHERE continuation_token=?
           AND status='planned'
         ORDER BY job_id DESC
         LIMIT 1
        """,
        (continuation_token,),
    ).fetchone()
    if existing_planned is not None:
        return (
            "follow-up already planned for this turn: "
            f"job_id={existing_planned['job_id']}"
        )

    now = time.time()
    delay = clamp_delay(delay_seconds)
    sequence = current_sequence + 1
    conn.execute(
        """
        INSERT INTO agent_proactive_outbox (
            group_id, group_name, kind, status, continuation_token,
            source_trigger_msg_id, source_trigger_kind, source_job_id,
            sequence, max_sequence, intent, reason, delay_seconds,
            scheduled_at, expires_at, anchor_msg_id, latest_msg_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            group_id,
            group_name,
            kind,
            continuation_token,
            source_trigger_msg_id,
            source_trigger_kind,
            source_job_id,
            sequence,
            max_sequence,
            intent,
            reason,
            delay,
            now + delay,
            now + max(1, settings.agent_continuation_ttl_seconds),
            anchor_msg_id,
            anchor_msg_id,
            now,
            now,
        ),
    )
    job_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(
        "continuation.plan",
        job_id=job_id,
        group_id=group_id,
        group_name=group_name,
        kind=kind,
        sequence=sequence,
        max_sequence=max_sequence,
        delay_seconds=delay,
        source_trigger_msg_id=source_trigger_msg_id,
        source_trigger_kind=source_trigger_kind,
        source_job_id=source_job_id,
    )
    return (
        f"follow-up planned: job_id={job_id} kind={kind} "
        f"sequence={sequence}/{max_sequence} delay={delay}s"
    )


def arm_planned_followups(
    conn: sqlite3.Connection,
    *,
    continuation_token: str | None,
    source_run_id: int | None,
    source_trigger_msg_id: int | None,
    source_trigger_kind: str | None,
    group_name: str | None = None,
    source_job_id: int | None = None,
) -> int:
    if not continuation_token:
        return 0
    now = time.time()
    ttl = max(1, settings.agent_continuation_ttl_seconds)
    cur = conn.execute(
        """
        UPDATE agent_proactive_outbox
           SET status='pending',
               source_run_id=COALESCE(source_run_id, ?),
               group_name=COALESCE(group_name, ?),
               source_trigger_msg_id=COALESCE(source_trigger_msg_id, ?),
               source_trigger_kind=COALESCE(source_trigger_kind, ?),
               source_job_id=COALESCE(source_job_id, ?),
               scheduled_at=? + delay_seconds,
               expires_at=? + ?,
               anchor_msg_id=COALESCE(anchor_msg_id, ?),
               latest_msg_id=COALESCE(latest_msg_id, ?),
               updated_at=?,
               result='armed'
         WHERE continuation_token=?
           AND status='planned'
        """,
        (
            source_run_id,
            group_name,
            source_trigger_msg_id,
            source_trigger_kind,
            source_job_id,
            now,
            now,
            ttl,
            source_trigger_msg_id,
            source_trigger_msg_id,
            now,
            continuation_token,
        ),
    )
    count = cur.rowcount or 0
    if count:
        append_event(
            "continuation.arm",
            continuation_token=continuation_token,
            count=count,
            source_run_id=source_run_id,
            source_trigger_msg_id=source_trigger_msg_id,
            source_trigger_kind=source_trigger_kind,
            source_job_id=source_job_id,
            group_name=group_name,
        )
    return count


def cancel_planned_followups(
    conn: sqlite3.Connection,
    *,
    continuation_token: str | None,
    reason: str,
) -> int:
    if not continuation_token:
        return 0
    return _cancel_where(
        conn,
        "continuation_token=? AND status='planned'",
        (continuation_token,),
        reason,
    )


def cancel_active_followups_for_group(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    reason: str,
) -> int:
    placeholders = ",".join("?" for _ in CANCELABLE_STATUSES)
    return _cancel_where(
        conn,
        f"group_id=? AND status IN ({placeholders})",
        (group_id, *CANCELABLE_STATUSES),
        reason,
    )


def _cancel_where(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[Any, ...],
    reason: str,
) -> int:
    now = time.time()
    cur = conn.execute(
        f"""
        UPDATE agent_proactive_outbox
           SET status='cancelled',
               result=?,
               updated_at=?
         WHERE {where_sql}
        """,
        (reason, now, *params),
    )
    count = cur.rowcount or 0
    if count:
        append_event("continuation.cancel", count=count, reason=reason)
    return count


def has_active_followups_for_group(conn: sqlite3.Connection, group_id: str) -> bool:
    placeholders = ",".join("?" for _ in BLOCKING_STATUSES)
    row = conn.execute(
        f"""
        SELECT 1 FROM agent_proactive_outbox
         WHERE group_id=?
           AND status IN ({placeholders})
         LIMIT 1
        """,
        (group_id, *BLOCKING_STATUSES),
    ).fetchone()
    return row is not None


def due_followups(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    now = time.time()
    conn.execute(
        """
        UPDATE agent_proactive_outbox
           SET status='expired',
               result='expired before dispatcher picked it up',
               updated_at=?
         WHERE status IN ('planned', 'pending')
           AND expires_at < ?
        """,
        (now, now),
    )
    return conn.execute(
        """
        SELECT * FROM agent_proactive_outbox
         WHERE status='pending'
           AND scheduled_at <= ?
           AND expires_at >= ?
         ORDER BY scheduled_at, job_id
         LIMIT ?
        """,
        (now, now, max(1, limit)),
    ).fetchall()


def claim_followup(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    now = time.time()
    row = conn.execute(
        "SELECT * FROM agent_proactive_outbox WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None or row["status"] != "pending":
        return None
    if row["expires_at"] < now:
        complete_followup(conn, job_id, status="expired", result="expired before run")
        return None
    cur = conn.execute(
        """
        UPDATE agent_proactive_outbox
           SET status='running',
               updated_at=?,
               result='running'
         WHERE job_id=?
           AND status='pending'
        """,
        (now, job_id),
    )
    if (cur.rowcount or 0) != 1:
        return None
    return conn.execute(
        "SELECT * FROM agent_proactive_outbox WHERE job_id=?",
        (job_id,),
    ).fetchone()


def complete_followup(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    result: str,
) -> None:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"invalid terminal continuation status: {status}")
    conn.execute(
        """
        UPDATE agent_proactive_outbox
           SET status=?,
               result=?,
               updated_at=?
         WHERE job_id=?
        """,
        (status, result, time.time(), job_id),
    )
    append_event("continuation.end", job_id=job_id, status=status, result=result[:200])


def latest_non_bot_message_after(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    after_msg_id: int | None,
    bot_wxid: str | None,
    bot_name: str,
) -> int | None:
    clauses = ["group_id=?", "type != 'system'"]
    params: list[Any] = [group_id]
    if after_msg_id is not None:
        clauses.append("msg_id > ?")
        params.append(after_msg_id)
    if bot_wxid:
        clauses.append("(sender_wxid IS NULL OR sender_wxid != ?)")
        params.append(bot_wxid)
    if bot_name:
        clauses.append("(sender_display IS NULL OR sender_display != ?)")
        params.append(bot_name)
    row = conn.execute(
        "SELECT MAX(msg_id) AS msg_id FROM messages WHERE " + " AND ".join(clauses),
        params,
    ).fetchone()
    value = row["msg_id"] if row else None
    return int(value) if value is not None else None
