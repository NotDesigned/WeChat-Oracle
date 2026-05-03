"""Phase A (read-only) tool implementations.

All tools here construct-bind a `(conn, group_id)` pair via subclassing of
the per-tool factory functions, NOT through tool arguments. The LLM never
sees `group_id` — its calls always operate on the bound group.

Result format: every tool returns plain text the LLM will read in a `tool`
role turn. We prefer human-readable lines over JSON for messages/lists
since the model already speaks the same prose format used elsewhere in
this project; structured fields (notes, run logs) keep their natural shape.

Conventions:
- msg_id is an integer (NOT the legacy `m:N` cand_id string used by /find).
  The agent's initial context formats messages with bare `[123]` markers.
- Timestamps render as `YYYY-MM-DD HH:MM` for compactness.
- Tool errors that the LLM can recover from raise `ToolError`; the runtime
  feeds the message back so the model can retry.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .memory import get_member_notes, list_group_notes
from .tools import Tool, ToolError, ToolSpec, truncate_for_llm


_FMT = "%Y-%m-%d %H:%M"


def _fmt_t(t: int | None) -> str:
    if t is None:
        return "?"
    return datetime.fromtimestamp(t).strftime(_FMT)


def _fmt_msg_row(row: sqlite3.Row) -> str:
    """One line per message. Uses content_text + transcript fallback for
    media; falls back to the type tag when neither has content."""
    sender = row["sender_display"] or row["sender_wxid"] or "?"
    body = row["content_text"]
    if not body:
        body = row["transcript"]
    if not body:
        body = f"[{row['type']}]"
    body = body.replace("\n", " ").strip()
    return f"[{row['msg_id']}] {_fmt_t(row['t'])} {sender}: {body}"


# --- recall_group_history --------------------------------------------------


_RECALL_SPEC = ToolSpec(
    name="recall_group_history",
    description=(
        "Search messages in this group by substring. Returns up to `limit` "
        "matches in chronological order (oldest → newest). Use this to "
        "answer questions like 'did anyone talk about X' or 'what did "
        "<sender> say last week'. Substring matches both content_text and "
        "transcript (OCR/ASR text), so image and voice messages are "
        "included when their text was extracted."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Substring to search for. Pass empty string to skip the substring filter (combine with sender_wxid or since_days for time-only / sender-only queries).",
            },
            "since_days": {
                "type": "integer",
                "description": "Only return messages newer than this many days. Omit for all time.",
                "minimum": 1,
            },
            "sender_wxid": {
                "type": "string",
                "description": "Optional exact-match filter on sender wxid.",
            },
            "limit": {
                "type": "integer",
                "description": "Max rows to return. Defaults to 20, capped at 50.",
                "minimum": 1,
                "maximum": 50,
            },
        },
        "required": ["query"],
    },
)


@dataclass
class RecallGroupHistoryTool(Tool):
    spec = _RECALL_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        query = args.get("query", "")
        if not isinstance(query, str):
            raise ToolError("query must be a string")
        since_days = args.get("since_days")
        sender_wxid = args.get("sender_wxid")
        limit = args.get("limit", 20)
        if not isinstance(limit, int) or limit < 1:
            limit = 20
        limit = min(limit, 50)

        clauses = ["group_id = ?"]
        params: list[Any] = [self.group_id]

        if query.strip():
            clauses.append("(content_text LIKE ? OR transcript LIKE ?)")
            like = f"%{query.strip()}%"
            params.extend([like, like])

        if isinstance(since_days, int) and since_days > 0:
            cutoff = int(datetime.now().timestamp()) - since_days * 86400
            clauses.append("t >= ?")
            params.append(cutoff)

        if isinstance(sender_wxid, str) and sender_wxid.strip():
            clauses.append("sender_wxid = ?")
            params.append(sender_wxid.strip())

        sql = (
            "SELECT msg_id, t, type, sender_wxid, sender_display, "
            "content_text, transcript "
            "FROM messages WHERE " + " AND ".join(clauses)
            + " ORDER BY t DESC LIMIT ?"
        )
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()

        if not rows:
            return "no matches"
        # Reverse to chronological order (oldest first) for the LLM.
        lines = [_fmt_msg_row(r) for r in reversed(rows)]
        header = f"{len(rows)} match(es):"
        return truncate_for_llm("\n".join([header, *lines]))


# --- view_quoted_chain -----------------------------------------------------


_QUOTE_CHAIN_SPEC = ToolSpec(
    name="view_quoted_chain",
    description=(
        "Walk the quote-reply chain backwards from a message. Returns the "
        "given message plus up to 4 ancestors it (transitively) quoted. "
        "Useful when the user is asking about a thread of replies."
    ),
    parameters={
        "type": "object",
        "properties": {
            "msg_id": {
                "type": "integer",
                "description": "Starting message id (an integer from the context, NOT the m:N format).",
            },
        },
        "required": ["msg_id"],
    },
)


@dataclass
class ViewQuotedChainTool(Tool):
    spec = _QUOTE_CHAIN_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        msg_id = args.get("msg_id")
        if not isinstance(msg_id, int):
            raise ToolError("msg_id must be an integer")

        rows: list[sqlite3.Row] = []
        seen: set[int] = set()
        current_id: int | None = msg_id
        for _ in range(5):
            if current_id is None or current_id in seen:
                break
            seen.add(current_id)
            row = self.conn.execute(
                "SELECT msg_id, wx_msg_id, t, type, sender_wxid, sender_display, "
                "content_text, transcript, reply_to_wx_msg_id, quote_text "
                "FROM messages WHERE msg_id=? AND group_id=?",
                (current_id, self.group_id),
            ).fetchone()
            if row is None:
                if not rows:
                    raise ToolError(f"msg_id {msg_id} not found in this group")
                break
            rows.append(row)
            ref = row["reply_to_wx_msg_id"]
            if not ref:
                break
            parent = self.conn.execute(
                "SELECT msg_id FROM messages WHERE wx_msg_id=? AND group_id=?",
                (ref, self.group_id),
            ).fetchone()
            if parent is None:
                # Quote chain points outside our DB (parent never ingested).
                # Surface it as a leaf with the snippet quote_text we have.
                snippet = (row["quote_text"] or "").replace("\n", " ").strip()
                if snippet:
                    rows.append({  # type: ignore[arg-type]
                        "msg_id": "?",
                        "t": None,
                        "type": "?",
                        "sender_wxid": None,
                        "sender_display": "(unresolved parent)",
                        "content_text": snippet,
                        "transcript": None,
                    })
                break
            current_id = int(parent["msg_id"])

        lines: list[str] = []
        for i, r in enumerate(rows):
            indent = "  " * i + ("↳ " if i else "")
            try:
                lines.append(indent + _fmt_msg_row(r))
            except (KeyError, TypeError):
                # the synthetic dict for unresolved parents
                sender = r.get("sender_display") if isinstance(r, dict) else r["sender_display"]
                body = r.get("content_text") if isinstance(r, dict) else r["content_text"]
                lines.append(f"{indent}[?] ? {sender}: {body}")
        return truncate_for_llm("\n".join(lines))


# --- expand_forward_bundle -------------------------------------------------


_EXPAND_FORWARD_SPEC = ToolSpec(
    name="expand_forward_bundle",
    description=(
        "Given a 合并转发 (merged-forward) wrapper message id, list its "
        "children — the actual chat snippets that were packaged inside. "
        "Children carry their original sender + original timestamp from the "
        "source group, which can be older than when this group received "
        "the wrapper."
    ),
    parameters={
        "type": "object",
        "properties": {
            "msg_id": {
                "type": "integer",
                "description": "msg_id of a forward wrapper (type='forward').",
            },
        },
        "required": ["msg_id"],
    },
)


@dataclass
class ExpandForwardBundleTool(Tool):
    spec = _EXPAND_FORWARD_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        msg_id = args.get("msg_id")
        if not isinstance(msg_id, int):
            raise ToolError("msg_id must be an integer")

        wrapper = self.conn.execute(
            "SELECT type, t, sender_display FROM messages "
            "WHERE msg_id=? AND group_id=?",
            (msg_id, self.group_id),
        ).fetchone()
        if wrapper is None:
            raise ToolError(f"msg_id {msg_id} not found in this group")
        if wrapper["type"] != "forward":
            raise ToolError(
                f"msg_id {msg_id} is type {wrapper['type']!r}, not 'forward'; "
                "this tool only expands 合并转发 wrappers"
            )

        children = self.conn.execute(
            """
            SELECT id, seq, sender_display, t, datatype, content
              FROM forwarded_records
             WHERE parent_msg_id=?
             ORDER BY seq
            """,
            (msg_id,),
        ).fetchall()
        if not children:
            return f"forward [{msg_id}] has no children rows"

        header = (
            f"forward [{msg_id}] from {wrapper['sender_display'] or '?'} "
            f"at {_fmt_t(wrapper['t'])}, {len(children)} child(ren):"
        )
        lines = [header]
        for c in children:
            sender = c["sender_display"] or "?"
            body = (c["content"] or "").replace("\n", " ").strip() or f"[datatype={c['datatype']}]"
            lines.append(f"  [{c['seq']}] {_fmt_t(c['t'])} {sender}: {body}")
        return truncate_for_llm("\n".join(lines))


# --- who_is ----------------------------------------------------------------


_WHO_IS_SPEC = ToolSpec(
    name="who_is",
    description=(
        "Look up what we know about a group member: their accumulated "
        "member_notes plus their last 20 messages in this group. Use this "
        "before referring to a person you've not interacted with yet, or "
        "when asked questions about someone's interests / background."
    ),
    parameters={
        "type": "object",
        "properties": {
            "sender_wxid": {
                "type": "string",
                "description": "The member's wxid (from a [N] context line's sender_wxid field). Pass display name only if you don't have a wxid; the tool falls back to display-name match.",
            },
        },
        "required": ["sender_wxid"],
    },
)


@dataclass
class WhoIsTool(Tool):
    spec = _WHO_IS_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        ident = args.get("sender_wxid")
        if not isinstance(ident, str) or not ident.strip():
            raise ToolError("sender_wxid must be a non-empty string")
        ident = ident.strip()

        # Strict wxid match first; fall back to display-name match if nothing.
        rows = self.conn.execute(
            "SELECT msg_id, t, type, sender_wxid, sender_display, "
            "content_text, transcript FROM messages "
            "WHERE group_id=? AND sender_wxid=? "
            "ORDER BY t DESC LIMIT 20",
            (self.group_id, ident),
        ).fetchall()
        matched_by = "wxid"
        if not rows:
            rows = self.conn.execute(
                "SELECT msg_id, t, type, sender_wxid, sender_display, "
                "content_text, transcript FROM messages "
                "WHERE group_id=? AND sender_display=? "
                "ORDER BY t DESC LIMIT 20",
                (self.group_id, ident),
            ).fetchall()
            matched_by = "display_name"
        if not rows:
            return f"no messages from sender {ident!r} in this group"

        # Resolve the canonical wxid (for member_notes lookup) from the rows
        # — display-name mode might still have rows with a real wxid.
        canonical_wxid = next(
            (r["sender_wxid"] for r in rows if r["sender_wxid"]),
            ident if matched_by == "wxid" else "",
        )
        notes = (
            get_member_notes(self.conn, self.group_id, canonical_wxid)
            if canonical_wxid else ""
        )

        out = [
            f"sender: {ident}  (matched_by={matched_by}, canonical_wxid={canonical_wxid or '?'})",
            f"member_notes: {notes if notes else '(none yet)'}",
            f"recent messages ({len(rows)}, newest → oldest):",
        ]
        out.extend(f"  {_fmt_msg_row(r)}" for r in rows)
        return truncate_for_llm("\n".join(out))


# --- read_member_notes -----------------------------------------------------


_READ_MEMBER_NOTES_SPEC = ToolSpec(
    name="read_member_notes",
    description=(
        "Just the accumulated notes about one member, no message history. "
        "Cheaper than `who_is` when you only need the notes (e.g. before "
        "calling `write_member_note` to merge new info into them)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "sender_wxid": {"type": "string"},
        },
        "required": ["sender_wxid"],
    },
)


@dataclass
class ReadMemberNotesTool(Tool):
    spec = _READ_MEMBER_NOTES_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        wxid = args.get("sender_wxid")
        if not isinstance(wxid, str) or not wxid.strip():
            raise ToolError("sender_wxid must be a non-empty string")
        notes = get_member_notes(self.conn, self.group_id, wxid.strip())
        return notes or "(no notes yet)"


# --- read_group_notes ------------------------------------------------------


_READ_GROUP_NOTES_SPEC = ToolSpec(
    name="read_group_notes",
    description=(
        "List recent group-level notes (events, decisions, ongoing topics) "
        "in this group, newest first. Append-only history — you'll see the "
        "evolution rather than a single-line summary."
    ),
    parameters={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Optional exact-match filter on the note's topic field.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "description": "Max notes to return; default 10.",
            },
        },
    },
)


@dataclass
class ReadGroupNotesTool(Tool):
    spec = _READ_GROUP_NOTES_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        topic = args.get("topic")
        if topic is not None and not isinstance(topic, str):
            raise ToolError("topic must be a string")
        limit = args.get("limit", 10)
        if not isinstance(limit, int) or limit < 1:
            limit = 10
        limit = min(limit, 30)

        rows = list_group_notes(
            self.conn, self.group_id,
            topic=topic.strip() if isinstance(topic, str) and topic.strip() else None,
            limit=limit,
        )
        if not rows:
            return "(no group notes yet)"
        lines = [f"{len(rows)} note(s):"]
        for r in rows:
            ts = _fmt_t(int(r["updated_at"])) if r["updated_at"] else "?"
            tag = f"[{r['topic']}] " if r["topic"] else ""
            body = (r["notes_text"] or "").replace("\n", " ").strip()
            lines.append(f"  ({ts}) {tag}{body}")
        return truncate_for_llm("\n".join(lines))


# --- factory ---------------------------------------------------------------


def register_phase_a_tools(tools: "GroupScopedTools") -> None:  # noqa: F821 - circular import in type-only ref
    """Register all Phase A read-only tools (incl. stay_silent) into the
    provided `GroupScopedTools` registry. Caller wires this from dispatcher
    integration in commit 4."""
    from .tools import StaySilentTool  # local to avoid circular hint
    tools.register(RecallGroupHistoryTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ViewQuotedChainTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ExpandForwardBundleTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(WhoIsTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ReadMemberNotesTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ReadGroupNotesTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(StaySilentTool())
