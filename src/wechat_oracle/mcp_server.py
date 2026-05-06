"""MCP server exposing WeChat-Oracle's per-group tools to OpenClaw.

Stdio-based MCP server. OpenClaw launches it as a subprocess and the
wechat-bot agent calls these tools while the SQLite truth source stays local.

Native tools are bound to a hidden `group_id` by `GroupScopedTools`. OpenClaw
serves multiple groups through one agent, so every MCP tool takes `group_id`
explicitly and enforces group isolation in SQL.

Every tool call is audited to `data/mcp.log` (JSONL): when openclaw mode is
the only thing running the agent, this is the sole record of what the
wechat-bot actually did — `agent_run_log.phase_b_trace` stays empty in
openclaw mode because tool use happens out-of-process from the dispatcher.
"""
from __future__ import annotations

import functools
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterator

from mcp.server.fastmcp import FastMCP, Image

from .agent.media_paths import resolve_path
from .agent.tools import ToolError
from .agent.tools_read import (
    ExpandForwardBundleTool,
    GetMessageContextTool,
    ReadImageTool,
    ReadVoiceTool,
    SearchGroupMessagesTool,
    ViewQuotedChainTool,
)
from .agent.tools_write import (
    MemoryWriteSession,
    ReadGroupMemoryForWriteTool,
    ReadPersonaDriftTool,
    UpdateGroupMemoryTool,
    UpdatePersonaDriftTool,
)
from .config import settings
from .db import get_conn, transaction
from .llm import _sniff_image_mime, build_vision_client
from .log_utils import append_mcp_audit


_mcp = FastMCP(
    "wechat-oracle",
    instructions=(
        "WeChat group history and per-group memory for the wechat-bot agent. "
        "Every tool requires `group_id` (a wxid like `12345@chatroom`). "
        "Operate ONLY on that group_id; never default to a different group."
    ),
)

_sessions: dict[str, MemoryWriteSession] = {}
_sessions_lock = Lock()
@contextmanager
def _open_conn() -> Iterator[sqlite3.Connection]:
    """Open a per-call SQLite connection with the project's WAL settings."""
    with get_conn() as conn:
        yield conn


def _write_session(group_id: str) -> MemoryWriteSession:
    """Process-local read-before-write snapshots for MCP memory writes.

    We clear a snapshot after every successful write. That forces any later
    update, including a concurrent stale one, to read the current value again
    before it can replace the blob.
    """
    with _sessions_lock:
        session = _sessions.get(group_id)
        if session is None:
            session = MemoryWriteSession()
            _sessions[group_id] = session
        return session


def _tool_result(tool: Any, args: dict[str, Any]) -> str:
    try:
        return tool.call(args)
    except ToolError as e:
        return f"tool_error: {e}"


def _mcp_audit_path():
    """Resolved at call time so a CWD change before run_mcp_server doesn't
    silently strand the audit file in the wrong dir."""
    settings.ensure_dirs()
    return settings.data_dir / "mcp.log"


def _audit(*audit_args_keys: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Decorate an MCP tool function to write one line to data/mcp.log per call.

    `audit_args_keys` names which kwargs to record verbatim. Use this to drop
    multi-KB writer payloads (notes_text / drift_text) — the per-call result
    line still captures their effect via prev_len/new_len in the result text.
    """

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        tool_name = fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            group_id = kwargs.get("group_id")
            if group_id is None and args:
                group_id = args[0]
            audit_args = {k: kwargs[k] for k in audit_args_keys if k in kwargs}
            started = time.time()
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                append_mcp_audit(
                    _mcp_audit_path(),
                    tool=tool_name, group_id=group_id, args=audit_args,
                    duration_s=time.time() - started, ok=False,
                    error=f"{type(e).__name__}: {e}",
                )
                raise
            is_tool_error = isinstance(result, str) and result.startswith("tool_error:")
            if is_tool_error:
                result_for_log: str | None = None
                error_for_log: str | None = result[len("tool_error:"):].strip()
            elif isinstance(result, str):
                result_for_log = result
                error_for_log = None
            else:
                # Non-string success: e.g. load_image returns FastMCP Image. Log
                # a short summary instead of dumping bytes into mcp.log.
                data = getattr(result, "data", None)
                fmt = getattr(result, "format", None) or "?"
                length = len(data) if isinstance(data, (bytes, bytearray)) else "?"
                result_for_log = f"<{type(result).__name__} format={fmt} bytes={length}>"
                error_for_log = None
            append_mcp_audit(
                _mcp_audit_path(),
                tool=tool_name, group_id=group_id, args=audit_args,
                duration_s=time.time() - started,
                ok=not is_tool_error,
                result=result_for_log,
                error=error_for_log,
            )
            return result

        return wrapper

    return decorator


@_mcp.tool(
    name="search_group_messages",
    description=(
        "Search a specific WeChat group's archive with optional substring, "
        "absolute date range, sender filter, message types, and nearby context. "
        "Use start_date/end_date for month/day questions such as 2024-04."
    ),
)
@_audit(
    "query", "sender", "sender_wxid", "start_date", "end_date", "types",
    "limit", "context_before", "context_after", "mode",
)
def search_group_messages(
    group_id: str,
    query: str | None = None,
    sender: str | None = None,
    sender_wxid: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    types: list[str] | None = None,
    limit: int = 30,
    context_before: int = 0,
    context_after: int = 0,
    mode: str = "compact",
) -> str:
    """Search this group's message history with structured filters."""
    args: dict[str, Any] = {
        "query": query or "",
        "limit": limit,
        "context_before": context_before,
        "context_after": context_after,
        "mode": mode,
    }
    if sender:
        args["sender"] = sender
    if sender_wxid:
        args["sender_wxid"] = sender_wxid
    if start_date:
        args["start_date"] = start_date
    if end_date:
        args["end_date"] = end_date
    if types:
        args["types"] = types
    with _open_conn() as conn:
        tool = SearchGroupMessagesTool(conn=conn, group_id=group_id)
        return _tool_result(tool, args)


@_mcp.tool(
    name="get_message_context",
    description=(
        "Read nearby messages around one msg_id in chronological order. Use "
        "after search_group_messages finds a key message."
    ),
)
@_audit("msg_id", "before", "after", "mode")
def get_message_context(
    group_id: str,
    msg_id: int,
    before: int = 10,
    after: int = 10,
    mode: str = "compact",
) -> str:
    """Return nearby messages around one direct message row."""
    with _open_conn() as conn:
        tool = GetMessageContextTool(conn=conn, group_id=group_id)
        return _tool_result(
            tool,
            {"msg_id": msg_id, "before": before, "after": after, "mode": mode},
        )


@_mcp.tool(
    name="read_group_memory",
    description=(
        "Read this group's freeform memory document. Also records a write "
        "snapshot, so call this before update_group_memory."
    ),
)
@_audit()
def read_group_memory(group_id: str) -> str:
    """Read group memory and record a write snapshot."""
    with _open_conn() as conn:
        tool = ReadGroupMemoryForWriteTool(
            conn=conn, group_id=group_id, session=_write_session(group_id),
        )
        return _tool_result(tool, {})


@_mcp.tool(
    name="view_quoted_chain",
    description=(
        "Walk the quote-reply chain backwards from a message in the given "
        "group. Use when quoted context matters."
    ),
)
@_audit("msg_id")
def view_quoted_chain(group_id: str, msg_id: int) -> str:
    """Return a message plus up to 4 quoted ancestors."""
    with _open_conn() as conn:
        tool = ViewQuotedChainTool(conn=conn, group_id=group_id)
        return _tool_result(tool, {"msg_id": msg_id})


@_mcp.tool(
    name="expand_forward_bundle",
    description=(
        "Expand a merged-forward wrapper message in the given group into its "
        "child records with original sender and timestamp."
    ),
)
@_audit("msg_id")
def expand_forward_bundle(group_id: str, msg_id: int) -> str:
    """List children of a type='forward' wrapper message."""
    with _open_conn() as conn:
        tool = ExpandForwardBundleTool(conn=conn, group_id=group_id)
        return _tool_result(tool, {"msg_id": msg_id})


@_mcp.tool(
    name="load_image",
    description=(
        "Return the actual image bytes of an image message in this group as "
        "an MCP image content block. The vision-capable agent (e.g. Claude "
        "via OpenClaw) can then look at the original pixels directly — no "
        "intermediate OCR / external vision API. Use this whenever the OCR "
        "transcript is missing, partial, or insufficient, or when the image "
        "itself (chart, meme, screenshot) is the answer's substance."
    ),
)
@_audit("msg_id")
def load_image(group_id: str, msg_id: int) -> Image:
    """Return image bytes for direct vision-model viewing.

    Returns an MCP `Image` content block; the agent's next turn sees the
    actual image. On error raises a `ToolError` — FastMCP serializes it as
    a tool-call error and the agent can recover (e.g. fall back to recall).
    `Image | str` return is unfriendly to FastMCP's pydantic schema gen, so
    success/error are split across return vs. raise.
    """
    with _open_conn() as conn:
        row = conn.execute(
            "SELECT type, media_path FROM messages WHERE msg_id=? AND group_id=?",
            (msg_id, group_id),
        ).fetchone()
    if row is None:
        raise ToolError(f"msg_id {msg_id} not found in this group")
    if row["type"] != "image":
        raise ToolError(f"msg_id {msg_id} is type {row['type']!r}, not 'image'")
    if not row["media_path"]:
        raise ToolError(f"msg_id {msg_id} has empty media_path")
    path = resolve_path(row["media_path"])
    if not path.exists():
        raise ToolError(f"msg_id {msg_id} image file missing: {path}")
    data = path.read_bytes()
    return Image(data=data, format=_sniff_image_mime(data).split("/", 1)[1])


@_mcp.tool(
    name="read_image",
    description=(
        "Read an image message through the configured WO_VISION_* model and "
        "return a textual description/OCR result. Use when you need a stable "
        "text summary for reasoning, logging, or memory. If WO_VISION_API_KEY "
        "is not configured and your model can see images, use load_image instead."
    ),
)
@_audit("msg_id", "prompt")
def read_image(group_id: str, msg_id: int, prompt: str | None = None) -> str:
    """Return a textual vision-model reading of one image message."""
    vision = build_vision_client(
        provider=settings.vision_provider,
        api_key=settings.vision_api_key,
        endpoint=settings.vision_endpoint,
    )
    with _open_conn() as conn:
        tool = ReadImageTool(
            conn=conn,
            group_id=group_id,
            vision=vision,
            vision_model=settings.vision_model,
            vision_max_tokens=settings.vision_max_tokens,
        )
        args: dict[str, Any] = {"msg_id": msg_id}
        if prompt:
            args["prompt"] = prompt
        return _tool_result(tool, args)


@_mcp.tool(
    name="read_voice",
    description=(
        "Read or produce the ASR transcript of a voice message in the given "
        "group. The tool enforces group_id isolation before touching media."
    ),
)
@_audit("msg_id")
def read_voice(group_id: str, msg_id: int) -> str:
    """Return transcript for a voice message."""
    with _open_conn() as conn:
        tool = ReadVoiceTool(conn=conn, group_id=group_id)
        return _tool_result(tool, {"msg_id": msg_id})


@_mcp.tool(
    name="read_persona_drift",
    description=(
        "Read this group's editable persona supplement. Must be called before "
        "update_persona_drift so the write can merge with current text."
    ),
)
@_audit()
def read_persona_drift(group_id: str) -> str:
    """Read persona drift and record a write snapshot."""
    with _open_conn() as conn:
        tool = ReadPersonaDriftTool(
            conn=conn, group_id=group_id, session=_write_session(group_id),
        )
        return _tool_result(tool, {})


@_mcp.tool(
    name="update_group_memory",
    description=(
        "Replace this group's memory document with a full merged version. "
        "Call read_group_memory first; stale writes are rejected."
    ),
)
@_audit()  # notes_text dropped from audit (multi-KB); result text records prev/new lengths
def update_group_memory(group_id: str, notes_text: str) -> str:
    """Replace group_memory after a read-before-write snapshot."""
    session = _write_session(group_id)
    with _open_conn() as conn:
        with transaction(conn):
            tool = UpdateGroupMemoryTool(
                conn=conn, group_id=group_id, session=session,
            )
            result = _tool_result(tool, {"notes_text": notes_text})
            if not result.startswith("tool_error:"):
                session.group_memory_hash = None
            return result


@_mcp.tool(
    name="update_persona_drift",
    description=(
        "Replace this group's persona drift with a full merged version. "
        "Call read_persona_drift first; stale writes are rejected."
    ),
)
@_audit()  # drift_text dropped from audit; result text records prev/new lengths
def update_persona_drift(group_id: str, drift_text: str) -> str:
    """Replace persona_drift after a read-before-write snapshot."""
    session = _write_session(group_id)
    with _open_conn() as conn:
        with transaction(conn):
            tool = UpdatePersonaDriftTool(
                conn=conn, group_id=group_id, session=session,
            )
            result = _tool_result(tool, {"drift_text": drift_text})
            if not result.startswith("tool_error:"):
                session.persona_hash = None
            return result


def run_mcp_server() -> None:
    """Run the MCP server over stdio. Blocks until stdin closes / signal.

    Prints a one-line startup banner to STDERR with cwd + resolved DB path.
    Caught by OpenClaw / Claude Code / Codex when they spawn this server,
    visible in their logs. Critical for debugging "unable to open database
    file" errors when a runtime spawns us with a different cwd than we
    expected (e.g. codex changes cwd to the agent's workspaceDir, breaking
    relative paths in settings).
    """
    import os
    import sys
    from pathlib import Path

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    db_abs = settings.db_path if settings.db_path.is_absolute() else Path.cwd() / settings.db_path
    sys.stderr.write(
        f"[wechat-oracle mcp-serve] cwd={os.getcwd()!r} "
        f"db_path={str(settings.db_path)!r} "
        f"resolved={str(db_abs)!r} "
        f"exists={db_abs.exists()}\n"
    )
    sys.stderr.flush()
    _mcp.run("stdio")
