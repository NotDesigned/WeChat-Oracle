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

from .. import prompts
from ..llm import VisionLLM
from .media_paths import resolve_media_path_for_msg
from .memory import get_group_memory
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
                "cannot read images directly. Try recall_group_history "
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
    tools.register(RecallGroupHistoryTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ViewQuotedChainTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ExpandForwardBundleTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ReadGroupMemoryTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ReadImageTool(
        conn=tools.conn, group_id=tools.group_id,
        vision=vision, vision_model=vision_model,
        vision_max_tokens=vision_max_tokens,
    ))
    tools.register(ReadVoiceTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(StaySilentTool())
