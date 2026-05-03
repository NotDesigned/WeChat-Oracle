"""DAO functions for the agent's evolvable memory tables.

Tables (defined in `schema.sql`):
  - persona_drift   — per-group, replace-on-write. Agent's behavior补充.
  - group_memory    — per-group single freeform blob bounded at
                      WO_AGENT_MEMORY_MAX_CHARS. Holds everything the agent
                      has learned about members + culture + recurring topics.
                      Agent organizes internal structure freely.
  - agent_run_log   — full audit trace of every agent run.

Both writable rows carry a `last_run_id` foreign key into `agent_run_log` so
any state can be traced back to the run that produced it (combats summary
drift; fix-up happens in dispatcher.chat_via_agent after insert_run_log
returns the run_id).

Two legacy tables (`member_notes`, `group_notes`) still exist as inert
CREATE statements in schema.sql for old installs but are no longer touched
by code — see schema.sql for the manual DROP recipe.
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


# --- group_memory (consolidated member + group notes) ----------------------


def get_group_memory(conn: sqlite3.Connection, group_id: str) -> str:
    row = conn.execute(
        "SELECT notes_text FROM group_memory WHERE group_id=?",
        (group_id,),
    ).fetchone()
    return (row["notes_text"] if row else "") or ""


def upsert_group_memory(
    conn: sqlite3.Connection, group_id: str, notes_text: str
) -> None:
    """Replace-on-write per group. Caller is responsible for the size cap
    (raised as a ToolError to the LLM at the tool boundary, not here — DAO
    stays dumb)."""
    conn.execute(
        """
        INSERT INTO group_memory (group_id, notes_text, size_chars, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            notes_text = excluded.notes_text,
            size_chars = excluded.size_chars,
            updated_at = excluded.updated_at
        """,
        (group_id, notes_text, len(notes_text), time.time()),
    )


# --- last_run_id linking (for raw↔summary tracing) ------------------------


def link_last_run_id(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    run_id: int,
    touched_persona: bool,
    touched_memory: bool,
) -> None:
    """Patch `last_run_id` on whichever memory row(s) the agent just wrote.
    Called from dispatcher.chat_via_agent after `insert_run_log` returns
    the run_id, since auto-incremented IDs aren't known until the row exists.
    """
    if touched_persona:
        conn.execute(
            "UPDATE persona_drift SET last_run_id=? WHERE group_id=?",
            (run_id, group_id),
        )
    if touched_memory:
        conn.execute(
            "UPDATE group_memory SET last_run_id=? WHERE group_id=?",
            (run_id, group_id),
        )


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


def list_recent_runs(
    conn: sqlite3.Connection, group_id: str, limit: int = 10
) -> list[sqlite3.Row]:
    """Newest first. Used by `wechat-oracle agent show-runs`. Returns the
    full phase_a_trace + phase_b_trace JSON so the caller can inspect tool
    calls and silent reasons without a second query."""
    return conn.execute(
        """
        SELECT run_id, trigger_msg_id, trigger_kind, reply_text,
               started_at, finished_at, phase_a_trace, phase_b_trace
          FROM agent_run_log
         WHERE group_id=?
         ORDER BY run_id DESC
         LIMIT ?
        """,
        (group_id, limit),
    ).fetchall()
