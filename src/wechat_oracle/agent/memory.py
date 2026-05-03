"""DAO functions for the agent's evolvable memory tables.

Tables (defined in `schema.sql`):
  - persona_drift   — per-group, replace-on-write
  - member_notes    — per-(group, member), replace-on-write
  - group_notes     — per-group append-only event log
  - agent_run_log   — full audit trace of every agent run

All functions take an explicit `conn` (the dispatcher's connection); none
own a connection or transaction. Writers use `db.transaction(conn)` from
the caller side when they need to be atomic — for now agent runs are
serial within one dispatcher process so single-statement UPSERTs are fine.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


# --- persona_drift ---------------------------------------------------------


def get_persona_drift(conn: sqlite3.Connection, group_id: str) -> str:
    row = conn.execute(
        "SELECT drift_text FROM persona_drift WHERE group_id=?",
        (group_id,),
    ).fetchone()
    return (row["drift_text"] if row else "") or ""


def upsert_persona_drift(
    conn: sqlite3.Connection, group_id: str, drift_text: str
) -> None:
    """Replace-on-write: the agent reads the current value, decides what the
    new full text should be, and overwrites. No appending here."""
    conn.execute(
        """
        INSERT INTO persona_drift (group_id, drift_text, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            drift_text = excluded.drift_text,
            updated_at = excluded.updated_at
        """,
        (group_id, drift_text, time.time()),
    )


# --- member_notes ----------------------------------------------------------


def get_member_notes(
    conn: sqlite3.Connection, group_id: str, sender_wxid: str
) -> str:
    row = conn.execute(
        "SELECT notes_text FROM member_notes WHERE group_id=? AND sender_wxid=?",
        (group_id, sender_wxid),
    ).fetchone()
    return (row["notes_text"] if row else "") or ""


def upsert_member_note(
    conn: sqlite3.Connection,
    group_id: str,
    sender_wxid: str,
    notes_text: str,
) -> None:
    """Replace-on-write per (group, sender). Agent is expected to have
    called `read_member_notes` first and decided on the merged text."""
    conn.execute(
        """
        INSERT INTO member_notes (group_id, sender_wxid, notes_text, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_id, sender_wxid) DO UPDATE SET
            notes_text = excluded.notes_text,
            updated_at = excluded.updated_at
        """,
        (group_id, sender_wxid, notes_text, time.time()),
    )


# --- group_notes -----------------------------------------------------------


def list_group_notes(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    topic: str | None = None,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Newest first. `topic` is an exact-match filter; pass None for all."""
    if topic is None:
        return conn.execute(
            """
            SELECT note_id, topic, notes_text, updated_at
              FROM group_notes
             WHERE group_id=?
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (group_id, limit),
        ).fetchall()
    return conn.execute(
        """
        SELECT note_id, topic, notes_text, updated_at
          FROM group_notes
         WHERE group_id=? AND topic=?
         ORDER BY updated_at DESC
         LIMIT ?
        """,
        (group_id, topic, limit),
    ).fetchall()


def insert_group_note(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    topic: str | None,
    notes_text: str,
) -> int:
    """Append-only. Returns the new `note_id`."""
    cur = conn.execute(
        """
        INSERT INTO group_notes (group_id, topic, notes_text, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (group_id, topic, notes_text, time.time()),
    )
    return int(cur.lastrowid)


# --- agent_run_log ---------------------------------------------------------


def insert_run_log(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    trigger_msg_id: int | None,
    trigger_kind: str,
    phase_a_trace: list[dict[str, Any]],
    phase_b_trace: list[dict[str, Any]] | None,
    reply_text: str | None,
    started_at: float,
    finished_at: float,
) -> int:
    """One row per agent run. Trace lists are serialized as JSON; downstream
    inspection happens via raw SQL + json_extract / Python parse."""
    cur = conn.execute(
        """
        INSERT INTO agent_run_log
            (group_id, trigger_msg_id, trigger_kind,
             phase_a_trace, phase_b_trace, reply_text,
             started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            group_id,
            trigger_msg_id,
            trigger_kind,
            json.dumps(phase_a_trace, ensure_ascii=False),
            json.dumps(phase_b_trace, ensure_ascii=False) if phase_b_trace is not None else None,
            reply_text,
            started_at,
            finished_at,
        ),
    )
    return int(cur.lastrowid)
