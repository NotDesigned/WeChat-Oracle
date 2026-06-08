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
from datetime import datetime, timedelta
from typing import Any

from .. import prompts
from ..llm import VisionLLM
from ..message_render import format_time, one_line, render_message_line
from .media_paths import resolve_media_path_for_msg
from .memory import get_group_memory
from .tools import Tool, ToolError, ToolSpec, truncate_for_llm
from .url_reader import read_public_url


def _fmt_t(t: int | None) -> str:
    return format_time(t)


def _fmt_msg_row(row: sqlite3.Row) -> str:
    """One line per message using the shared LLM-visible body renderer."""
    return render_message_line(row, style="tool", id_style="bare")


# --- shared search/context helpers -----------------------------------------


def _clip_line(text: str, limit: int) -> str:
    text = one_line(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _parse_date_bound(value: Any, *, end: bool) -> int | None:
    """Parse YYYY-MM-DD or YYYY-MM into an inclusive unix-second bound."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        if len(raw) == 7:
            dt = datetime.strptime(raw, "%Y-%m")
            if end:
                if dt.month == 12:
                    next_month = datetime(dt.year + 1, 1, 1)
                else:
                    next_month = datetime(dt.year, dt.month + 1, 1)
                dt = next_month - timedelta(seconds=1)
            return int(dt.timestamp())
        dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as e:
        raise ToolError("date must be YYYY-MM-DD or YYYY-MM") from e
    if end:
        dt = dt + timedelta(days=1) - timedelta(seconds=1)
    return int(dt.timestamp())


def _coerce_types(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _render_search_line(row: sqlite3.Row | dict[str, Any], *, mode: str, target: bool = False) -> str:
    if isinstance(row, dict) and row.get("kind") == "forward_child":
        prefix = ">" if target else " "
        body_limit = 420 if mode == "full" else 160
        return (
            f"{prefix}[f:{row['id']}] {_fmt_t(row['t'])} "
            f"{row.get('sender_display') or '?'}: "
            f"{_clip_line(row.get('content') or '', body_limit)} "
            f"[来自合并转发 m:{row.get('parent_msg_id')}]"
        )

    line = _fmt_msg_row(row)  # type: ignore[arg-type]
    body_limit = 520 if mode == "full" else 190
    prefix = ">" if target else " "
    return prefix + _clip_line(line, body_limit)


def _fetch_message_context_rows(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    row: sqlite3.Row,
    before: int,
    after: int,
) -> list[tuple[sqlite3.Row, bool]]:
    before_rows = conn.execute(
        """
        SELECT m.msg_id, m.t, m.type, m.sender_wxid, m.sender_display,
               m.content_text, m.transcript, m.quote_text,
               p.msg_id AS parent_msg_id, p.type AS parent_type,
               p.sender_display AS parent_sender,
               p.sender_wxid AS parent_sender_wxid
          FROM messages m
     LEFT JOIN messages p
            ON m.reply_to_wx_msg_id IS NOT NULL
           AND p.wx_msg_id = m.reply_to_wx_msg_id
           AND p.group_id = m.group_id
         WHERE m.group_id=?
           AND (m.t < ? OR (m.t = ? AND m.msg_id < ?))
         ORDER BY m.t DESC, m.msg_id DESC
         LIMIT ?
        """,
        (group_id, row["t"], row["t"], row["msg_id"], before),
    ).fetchall()
    after_rows = conn.execute(
        """
        SELECT m.msg_id, m.t, m.type, m.sender_wxid, m.sender_display,
               m.content_text, m.transcript, m.quote_text,
               p.msg_id AS parent_msg_id, p.type AS parent_type,
               p.sender_display AS parent_sender,
               p.sender_wxid AS parent_sender_wxid
          FROM messages m
     LEFT JOIN messages p
            ON m.reply_to_wx_msg_id IS NOT NULL
           AND p.wx_msg_id = m.reply_to_wx_msg_id
           AND p.group_id = m.group_id
         WHERE m.group_id=?
           AND (m.t > ? OR (m.t = ? AND m.msg_id > ?))
         ORDER BY m.t ASC, m.msg_id ASC
         LIMIT ?
        """,
        (group_id, row["t"], row["t"], row["msg_id"], after),
    ).fetchall()
    return (
        [(r, False) for r in reversed(before_rows)]
        + [(row, True)]
        + [(r, False) for r in after_rows]
    )


# --- search_group_messages -------------------------------------------------


_SEARCH_GROUP_MESSAGES_SPEC = ToolSpec(
    name="search_group_messages",
    description=(
        "Search this group's message archive with optional absolute date range, "
        "sender filter, message types, and nearby context. Prefer this over "
        "guessing many separate substring searches. Use start_date/end_date "
        "for month or day questions, e.g. 2024-04 or 2024-04-01."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional substring searched in message text, OCR/ASR transcript, quote snippet, and forwarded child content. Empty means no text filter.",
            },
            "sender": {
                "type": "string",
                "description": "Optional fuzzy sender filter matched against sender display name or wxid.",
            },
            "sender_wxid": {
                "type": "string",
                "description": "Optional exact sender wxid filter. Forwarded child rows have no wxid and are skipped when this is set.",
            },
            "start_date": {
                "type": "string",
                "description": "Inclusive lower bound as YYYY-MM-DD or YYYY-MM.",
            },
            "end_date": {
                "type": "string",
                "description": "Inclusive upper bound as YYYY-MM-DD or YYYY-MM.",
            },
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional message types such as text, quote, image, voice, forward, forward_child.",
            },
            "limit": {
                "type": "integer",
                "description": "Max matched rows before context expansion. Defaults to 30, capped at 80.",
                "minimum": 1,
                "maximum": 80,
            },
            "context_before": {
                "type": "integer",
                "description": "For direct message matches, include this many preceding group messages. Defaults to 0, capped at 5.",
                "minimum": 0,
                "maximum": 5,
            },
            "context_after": {
                "type": "integer",
                "description": "For direct message matches, include this many following group messages. Defaults to 0, capped at 5.",
                "minimum": 0,
                "maximum": 5,
            },
            "mode": {
                "type": "string",
                "enum": ["compact", "full"],
                "description": "compact returns short one-line rows; full allows longer row text. Defaults to compact.",
            },
        },
    },
)


@dataclass
class SearchGroupMessagesTool(Tool):
    spec = _SEARCH_GROUP_MESSAGES_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        query = args.get("query", "")
        if query is not None and not isinstance(query, str):
            raise ToolError("query must be a string")
        query = (query or "").strip()
        sender = args.get("sender")
        if sender is not None and not isinstance(sender, str):
            raise ToolError("sender must be a string")
        sender = (sender or "").strip()
        sender_wxid = args.get("sender_wxid")
        if sender_wxid is not None and not isinstance(sender_wxid, str):
            raise ToolError("sender_wxid must be a string")
        sender_wxid = (sender_wxid or "").strip()
        start_t = _parse_date_bound(args.get("start_date"), end=False)
        end_t = _parse_date_bound(args.get("end_date"), end=True)
        if start_t is not None and end_t is not None and start_t > end_t:
            raise ToolError("start_date must be before or equal to end_date")

        limit = args.get("limit", 30)
        if not isinstance(limit, int) or limit < 1:
            limit = 30
        limit = min(limit, 80)
        before = args.get("context_before", 0)
        after = args.get("context_after", 0)
        before = min(before if isinstance(before, int) and before > 0 else 0, 5)
        after = min(after if isinstance(after, int) and after > 0 else 0, 5)
        mode = args.get("mode", "compact")
        mode = mode if mode in ("compact", "full") else "compact"
        types = _coerce_types(args.get("types"))

        clauses = ["m.group_id = ?"]
        params: list[Any] = [self.group_id]
        if query:
            like = f"%{query}%"
            clauses.append(
                "(m.content_text LIKE ? OR m.transcript LIKE ? OR m.quote_text LIKE ?)"
            )
            params.extend([like, like, like])
        if sender:
            like = f"%{sender}%"
            clauses.append("(m.sender_display LIKE ? OR m.sender_wxid LIKE ?)")
            params.extend([like, like])
        if sender_wxid:
            clauses.append("m.sender_wxid = ?")
            params.append(sender_wxid)
        if start_t is not None:
            clauses.append("m.t >= ?")
            params.append(start_t)
        if end_t is not None:
            clauses.append("m.t <= ?")
            params.append(end_t)
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"m.type IN ({placeholders})")
            params.extend(types)

        sql = (
            "SELECT m.msg_id, m.t, m.type, m.sender_wxid, m.sender_display, "
            "m.content_text, m.transcript, m.quote_text, "
            "p.msg_id AS parent_msg_id, p.type AS parent_type, "
            "p.sender_display AS parent_sender, p.sender_wxid AS parent_sender_wxid "
            "FROM messages m "
            "LEFT JOIN messages p "
            "ON m.reply_to_wx_msg_id IS NOT NULL "
            "AND p.wx_msg_id = m.reply_to_wx_msg_id "
            "AND p.group_id = m.group_id "
            "WHERE " + " AND ".join(clauses)
            + " ORDER BY m.t DESC, m.msg_id DESC LIMIT ?"
        )
        msg_rows = self.conn.execute(sql, [*params, limit]).fetchall()

        fwd_rows: list[dict[str, Any]] = []
        include_forwarded = not types or any(t in {"forward", "forward_child"} for t in types)
        if include_forwarded and not sender_wxid:
            fwd_clauses = ["m.group_id = ?", "f.content IS NOT NULL", "f.content <> ''"]
            fwd_params: list[Any] = [self.group_id]
            if query:
                fwd_clauses.append("f.content LIKE ?")
                fwd_params.append(f"%{query}%")
            if sender:
                fwd_clauses.append("f.sender_display LIKE ?")
                fwd_params.append(f"%{sender}%")
            if start_t is not None:
                fwd_clauses.append("f.t >= ?")
                fwd_params.append(start_t)
            if end_t is not None:
                fwd_clauses.append("f.t <= ?")
                fwd_params.append(end_t)
            fwd_sql = (
                "SELECT f.id, f.t, f.sender_display, f.content, "
                "m.msg_id AS parent_msg_id "
                "FROM forwarded_records f "
                "JOIN messages m ON m.msg_id = f.parent_msg_id "
                "WHERE " + " AND ".join(fwd_clauses)
                + " ORDER BY f.t DESC, f.id DESC LIMIT ?"
            )
            for r in self.conn.execute(fwd_sql, [*fwd_params, limit]).fetchall():
                fwd_rows.append({
                    "kind": "forward_child",
                    "id": r["id"],
                    "t": r["t"],
                    "sender_display": r["sender_display"],
                    "content": r["content"],
                    "parent_msg_id": r["parent_msg_id"],
                })

        merged: list[tuple[int, int, sqlite3.Row | dict[str, Any]]] = [
            (int(r["t"]), int(r["msg_id"]), r) for r in msg_rows
        ]
        merged.extend((int(r["t"]), -int(r["id"]), r) for r in fwd_rows)
        merged.sort(key=lambda item: (item[0], item[1]))
        if len(merged) > limit:
            merged = merged[-limit:]
        if not merged:
            return "no matches"

        lines = [f"{len(merged)} match(es):"]
        for _, _, item in merged:
            if isinstance(item, sqlite3.Row) and (before or after):
                lines.append(f"context for [{item['msg_id']}]:")
                for ctx_row, is_target in _fetch_message_context_rows(
                    self.conn,
                    group_id=self.group_id,
                    row=item,
                    before=before,
                    after=after,
                ):
                    lines.append(_render_search_line(ctx_row, mode=mode, target=is_target))
            else:
                lines.append(_render_search_line(item, mode=mode, target=False))
        return truncate_for_llm("\n".join(lines), limit=6000 if mode == "full" else 3500)


# --- get_message_context ---------------------------------------------------


_GET_MESSAGE_CONTEXT_SPEC = ToolSpec(
    name="get_message_context",
    description=(
        "Read nearby messages around one msg_id in chronological order. Use "
        "after search_group_messages finds a key message, or when the user "
        "points at a specific recent msg_id and the surrounding conversation matters."
    ),
    parameters={
        "type": "object",
        "properties": {
            "msg_id": {
                "type": "integer",
                "description": "Target message id from context/search results.",
            },
            "before": {
                "type": "integer",
                "description": "Messages before the target. Defaults to 10, capped at 30.",
                "minimum": 0,
                "maximum": 30,
            },
            "after": {
                "type": "integer",
                "description": "Messages after the target. Defaults to 10, capped at 30.",
                "minimum": 0,
                "maximum": 30,
            },
            "mode": {
                "type": "string",
                "enum": ["compact", "full"],
                "description": "compact returns short lines; full allows longer text. Defaults to compact.",
            },
        },
        "required": ["msg_id"],
    },
)


@dataclass
class GetMessageContextTool(Tool):
    spec = _GET_MESSAGE_CONTEXT_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        msg_id = args.get("msg_id")
        if not isinstance(msg_id, int):
            raise ToolError("msg_id must be an integer")
        before = args.get("before", 10)
        after = args.get("after", 10)
        before = min(before if isinstance(before, int) and before > 0 else 0, 30)
        after = min(after if isinstance(after, int) and after > 0 else 0, 30)
        mode = args.get("mode", "compact")
        mode = mode if mode in ("compact", "full") else "compact"
        row = self.conn.execute(
            """
            SELECT m.msg_id, m.t, m.type, m.sender_wxid, m.sender_display,
                   m.content_text, m.transcript, m.quote_text,
                   p.msg_id AS parent_msg_id, p.type AS parent_type,
                   p.sender_display AS parent_sender,
                   p.sender_wxid AS parent_sender_wxid
              FROM messages m
         LEFT JOIN messages p
                ON m.reply_to_wx_msg_id IS NOT NULL
               AND p.wx_msg_id = m.reply_to_wx_msg_id
               AND p.group_id = m.group_id
             WHERE m.msg_id=? AND m.group_id=?
            """,
            (msg_id, self.group_id),
        ).fetchone()
        if row is None:
            raise ToolError(f"msg_id {msg_id} not found in this group")
        lines = [f"context around [{msg_id}]:"]
        for ctx_row, is_target in _fetch_message_context_rows(
            self.conn,
            group_id=self.group_id,
            row=row,
            before=before,
            after=after,
        ):
            lines.append(_render_search_line(ctx_row, mode=mode, target=is_target))
        return truncate_for_llm("\n".join(lines), limit=7000 if mode == "full" else 4000)


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
                """
                SELECT m.msg_id, m.wx_msg_id, m.t, m.type, m.sender_wxid,
                       m.sender_display, m.content_text, m.transcript,
                       m.reply_to_wx_msg_id, m.quote_text,
                       p.msg_id AS parent_msg_id, p.type AS parent_type,
                       p.sender_display AS parent_sender,
                       p.sender_wxid AS parent_sender_wxid
                  FROM messages m
             LEFT JOIN messages p
                    ON m.reply_to_wx_msg_id IS NOT NULL
                   AND p.wx_msg_id = m.reply_to_wx_msg_id
                   AND p.group_id = m.group_id
                 WHERE m.msg_id=? AND m.group_id=?
                """,
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
            SELECT id, seq, sender_display, t, datatype, content, src_msg_id, media_path
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
            if int(c["datatype"]) == 2:
                body += (
                    f" (call read_forward_child_image(parent_msg_id={msg_id}, "
                    f"seq={c['seq']}) to inspect)"
                )
            lines.append(f"  [{c['seq']}] {_fmt_t(c['t'])} {sender}: {body}")
        return truncate_for_llm("\n".join(lines))


# --- read_group_memory ------------------------------------------------------


_READ_GROUP_MEMORY_SPEC = ToolSpec(
    name="read_group_memory",
    description=(
        "Read this group's freeform memory document — everything the agent "
        "has accumulated about who's who, group culture, and recurring "
        "topics. One document, not per-person; you organize it however you "
        "want when writing back. Returns empty string when nothing's been "
        "written yet."
    ),
    parameters={"type": "object", "properties": {}},
)


@dataclass
class ReadGroupMemoryTool(Tool):
    spec = _READ_GROUP_MEMORY_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        text = get_group_memory(self.conn, self.group_id)
        if not text:
            return "(empty — nothing learned about this group yet)"
        return text  # NOT truncated: the agent owns this data, gets it whole


# --- read_url --------------------------------------------------------------


_READ_URL_SPEC = ToolSpec(
    name="read_url",
    description=(
        "Fetch a public http(s) URL, including WeChat public-account article "
        "links when accessible, and return extracted readable text. Use this "
        "after a link/card message exposes a URL in context or search results."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http(s) URL to read.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters returned to the model. Defaults to 12000, capped at 20000.",
                "minimum": 1000,
                "maximum": 20000,
            },
        },
        "required": ["url"],
    },
)


@dataclass
class ReadUrlTool(Tool):
    spec = _READ_URL_SPEC

    def call(self, args: dict[str, Any]) -> str:
        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ToolError("url must be a non-empty string")
        max_chars = args.get("max_chars", 12000)
        if not isinstance(max_chars, int):
            max_chars = 12000
        max_chars = max(1000, min(max_chars, 20000))
        return read_public_url(url.strip(), max_chars=max_chars)


# --- read_image ------------------------------------------------------------


_READ_IMAGE_SPEC = ToolSpec(
    name="read_image",
    description=(
        "Look at an image message directly with the vision model. Use this "
        "when the OCR text is missing, partial, gibberish, or when the image "
        "itself (not its text) is the point — memes, screenshots of charts, "
        "photos. Optional `prompt` focuses what to extract; without one, "
        "the model returns a faithful description plus any visible text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "msg_id": {
                "type": "integer",
                "description": "msg_id of an image message in this group.",
            },
            "prompt": {
                "type": "string",
                "description": "Optional steering — e.g. 'what does this meme mean?', 'extract any handwriting'.",
            },
        },
        "required": ["msg_id"],
    },
)


@dataclass
class ReadImageTool(Tool):
    """Vision-LLM read-through. The vision client is a hard requirement —
    if `vision is None`, the tool raises ToolError so the agent can
    redirect (e.g. ask the user to wait, or fall back to recall)."""
    spec = _READ_IMAGE_SPEC
    conn: sqlite3.Connection
    group_id: str
    vision: VisionLLM | None
    vision_model: str
    vision_max_tokens: int | None

    def call(self, args: dict[str, Any]) -> str:
        if self.vision is None:
            raise ToolError(
                "vision model not configured (WO_VISION_API_KEY empty); "
                "cannot read images directly. Try search_group_messages "
                "with keywords from the OCR transcript instead."
            )
        msg_id = args.get("msg_id")
        if not isinstance(msg_id, int):
            raise ToolError("msg_id must be an integer")
        prompt = args.get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            raise ToolError("prompt must be a string")

        path = resolve_media_path_for_msg(self.conn, msg_id, expected_type="image")
        if path is None:
            # Disambiguate the failure for the LLM so it can decide whether to retry.
            row = self.conn.execute(
                "SELECT type, media_path FROM messages WHERE msg_id=? AND group_id=?",
                (msg_id, self.group_id),
            ).fetchone()
            if row is None:
                raise ToolError(f"msg_id {msg_id} not found in this group")
            if row["type"] != "image":
                raise ToolError(
                    f"msg_id {msg_id} is type {row['type']!r}, not 'image'"
                )
            raise ToolError(
                f"msg_id {msg_id} has no usable image file on disk (live cache "
                "may have been cleared, or backfill ran without media)"
            )

        user_prompt = (prompt or "").strip() or prompts.READ_IMAGE_USER_DEFAULT
        try:
            reply = self.vision.complete_with_images(
                model=self.vision_model,
                system=prompts.READ_IMAGE_SYSTEM,
                user=user_prompt,
                images=[path.read_bytes()],
                temperature=0.2,
                max_tokens=self.vision_max_tokens,
            )
        except Exception as e:
            raise ToolError(f"vision call failed: {e}")
        return truncate_for_llm((reply or "").strip() or "(vision model returned empty)")


_READ_FORWARD_CHILD_IMAGE_SPEC = ToolSpec(
    name="read_forward_child_image",
    description=(
        "Read one image child inside a merged-forward wrapper. Use this when "
        "expand_forward_bundle shows child rows like `[0] ...: [图片]`; pass the "
        "forward wrapper's msg_id as parent_msg_id and the child index as seq. "
        "Do not call read_image on the forward wrapper itself."
    ),
    parameters={
        "type": "object",
        "properties": {
            "parent_msg_id": {
                "type": "integer",
                "description": "msg_id of the merged-forward wrapper in this group.",
            },
            "seq": {
                "type": "integer",
                "description": "Child index shown by expand_forward_bundle, starting at 0.",
                "minimum": 0,
            },
            "prompt": {
                "type": "string",
                "description": "Optional steering for the vision model.",
            },
        },
        "required": ["parent_msg_id", "seq"],
    },
)


@dataclass
class ReadForwardChildImageTool(Tool):
    spec = _READ_FORWARD_CHILD_IMAGE_SPEC
    conn: sqlite3.Connection
    group_id: str
    vision: VisionLLM | None
    vision_model: str
    vision_max_tokens: int | None

    def call(self, args: dict[str, Any]) -> str:
        if self.vision is None:
            raise ToolError(
                "vision model not configured (WO_VISION_API_KEY empty); "
                "cannot read forwarded child images directly"
            )
        parent_msg_id = args.get("parent_msg_id")
        seq = args.get("seq")
        if not isinstance(parent_msg_id, int):
            raise ToolError("parent_msg_id must be an integer")
        if not isinstance(seq, int) or seq < 0:
            raise ToolError("seq must be a non-negative integer")
        prompt = args.get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            raise ToolError("prompt must be a string")

        child = self.conn.execute(
            """
            SELECT f.datatype, f.src_msg_id, f.content, f.media_path
              FROM forwarded_records f
              JOIN messages parent ON parent.msg_id = f.parent_msg_id
             WHERE f.parent_msg_id=? AND f.seq=? AND parent.group_id=?
            """,
            (parent_msg_id, seq, self.group_id),
        ).fetchone()
        if child is None:
            raise ToolError(
                f"forward child parent_msg_id={parent_msg_id} seq={seq} "
                "not found in this group"
            )
        if int(child["datatype"]) != 2:
            raise ToolError(
                f"forward child parent_msg_id={parent_msg_id} seq={seq} "
                f"is datatype={child['datatype']}, not image"
            )
        path = None
        if child["media_path"]:
            from .media_paths import resolve_path
            candidate = resolve_path(child["media_path"])
            if candidate.exists():
                path = candidate
        if path is None:
            src_msg_id = (child["src_msg_id"] or "").strip()
            if not src_msg_id:
                raise ToolError(
                    f"forward child parent_msg_id={parent_msg_id} seq={seq} "
                    "has no media_path or source image id"
                )
            row = self.conn.execute(
                """
                SELECT msg_id, media_path
                  FROM messages
                 WHERE wx_msg_id=? AND type='image' AND media_path IS NOT NULL
                 ORDER BY msg_id DESC
                 LIMIT 1
                """,
                (src_msg_id,),
            ).fetchone()
            if row is None:
                raise ToolError(
                    f"source image for forward child parent_msg_id={parent_msg_id} "
                    f"seq={seq} is not available in the local archive"
                )
            path = resolve_media_path_for_msg(
                self.conn, int(row["msg_id"]), expected_type="image"
            )
            if path is None:
                raise ToolError(
                    f"source image msg_id {row['msg_id']} has no usable image file on disk"
                )

        user_prompt = (prompt or "").strip() or prompts.READ_IMAGE_USER_DEFAULT
        try:
            reply = self.vision.complete_with_images(
                model=self.vision_model,
                system=prompts.READ_IMAGE_SYSTEM,
                user=user_prompt,
                images=[path.read_bytes()],
                temperature=0.2,
                max_tokens=self.vision_max_tokens,
            )
        except Exception as e:
            raise ToolError(f"vision call failed: {e}")
        return truncate_for_llm((reply or "").strip() or "(vision model returned empty)")


# --- read_voice ------------------------------------------------------------


_READ_VOICE_SPEC = ToolSpec(
    name="read_voice",
    description=(
        "Get the ASR transcript of a voice message. If we've already "
        "transcribed it (the worker runs in the background), this is "
        "instant; otherwise the model waits for ASR (typically 1–3s)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "msg_id": {
                "type": "integer",
                "description": "msg_id of a voice message in this group.",
            },
        },
        "required": ["msg_id"],
    },
)


@dataclass
class ReadVoiceTool(Tool):
    spec = _READ_VOICE_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        msg_id = args.get("msg_id")
        if not isinstance(msg_id, int):
            raise ToolError("msg_id must be an integer")

        # Group-scope check first — read_voice must not pull voice from
        # another group via a leaked msg_id.
        row = self.conn.execute(
            "SELECT type FROM messages WHERE msg_id=? AND group_id=?",
            (msg_id, self.group_id),
        ).fetchone()
        if row is None:
            raise ToolError(f"msg_id {msg_id} not found in this group")
        if row["type"] != "voice":
            raise ToolError(
                f"msg_id {msg_id} is type {row['type']!r}, not 'voice'"
            )

        from ..worker.mm import transcribe_voice_for_msg
        text = transcribe_voice_for_msg(self.conn, msg_id)
        if not text:
            return "(empty transcript — silent or unrecognizable)"
        return truncate_for_llm(text)


# --- factory ---------------------------------------------------------------


def register_phase_a_tools(
    tools: "GroupScopedTools",  # noqa: F821 - structural-only reference
    *,
    vision: VisionLLM | None = None,
    vision_model: str = "",
    vision_max_tokens: int | None = None,
) -> None:
    """Register all Phase A read-only tools into the provided
    `GroupScopedTools` registry. Vision-related kwargs may be left at
    their defaults — `read_image` will then raise a clean ToolError
    when called instead of crashing."""
    from .tools import StaySilentTool  # local to keep import cycle light
    tools.register(SearchGroupMessagesTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(GetMessageContextTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ViewQuotedChainTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ExpandForwardBundleTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ReadGroupMemoryTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ReadUrlTool())
    tools.register(ReadImageTool(
        conn=tools.conn, group_id=tools.group_id,
        vision=vision, vision_model=vision_model,
        vision_max_tokens=vision_max_tokens,
    ))
    tools.register(ReadForwardChildImageTool(
        conn=tools.conn, group_id=tools.group_id,
        vision=vision, vision_model=vision_model,
        vision_max_tokens=vision_max_tokens,
    ))
    tools.register(ReadVoiceTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(StaySilentTool())
