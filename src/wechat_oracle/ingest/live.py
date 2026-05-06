"""Live capture by subscribing to WeFlow's SSE push.

Replaces the wxauto path. WeFlow (https://github.com/hicccc77/WeFlow) is an Electron app
that decrypts the local WeChat DB and exposes an HTTP API on 127.0.0.1:5031.

Architecture: SSE-as-doorbell + on-demand fetch
  1. Subscribe to GET /api/v1/push/messages (text/event-stream).
  2. Each `message.new` event carries `sessionId` + `rawid` but no full metadata
     (no localType, no mediaLocalPath). Use it as a trigger only.
  3. On each event for a watched session, call `_poll_session` to pull the full
     records from /api/v1/messages and write via `write_messages` (UNIQUE dedupe
     handles the boundary message that may overlap with the prior poll).
  4. SSE drops happen — wrap the stream loop with exponential backoff reconnect.

Why not consume SSE alone:
  Field set is too thin (`content` is "[图片]" placeholder for media; no localType).
  The fetch round-trip is ~10ms on localhost — cheap.

Watermark strategy:
  Per-session `start = max(createTime)` of last fetch. Initialized to subscription
  start, so we ignore history (that's backfill's job).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
from loguru import logger

from ..config import settings
from ..db import get_conn
from ..models import Message, MsgType
from .backfill import _WEFLOW_LOCAL_TYPE_MAP
from .forwarded import (
    FORWARD_LOCAL_TYPE,
    appmsg_subtype,
    base_local_type,
    format_appmsg_content,
    parse_quote_reply_xml,
    parse_record_xml,
)
from .media_store import (
    MEDIA_MISSING_TAGS,
    MEDIA_TYPES,
    materialize_media_ref,
)
from .writer import write_messages


def _api_msg_to_normalized(
    raw: dict[str, Any],
    group_id: str,
    group_name: str | None,
    members: dict[str, str] | None = None,
) -> Message | None:
    """Convert one WeFlow `/api/v1/messages` row to our normalized `Message`.

    WeFlow's HTTP API doesn't pre-parse appmsg-family messages — `content` is
    the raw XML for those. We branch on `appmsg_subtype` and extract the
    actual user-visible text:
      19  → MsgType.FORWARD + child rows in forwarded_records
      57  → MsgType.QUOTE  with reply text in content_text + refermsg in
            quote_text / reply_to_wx_msg_id
      4/5/6/8/51/62/2000/2001 → MsgType.LINK with `[label] title\\nurl` preview
      其他 49.*  → MsgType.LINK, fallback to raw `content`
    See forwarded.py module docstring for the subtype map.

    `members` (wxid -> display name, loaded from /api/v1/group-members at
    startup) supplies the missing display name; falls back to wxid.
    """
    lt = raw.get("localType")
    sub = appmsg_subtype(lt)
    if sub == 19:
        msg_type: MsgType | None = MsgType.FORWARD
    elif sub == 57:
        msg_type = MsgType.QUOTE
    elif sub is not None:
        msg_type = MsgType.LINK   # other appmsg cards
    else:
        msg_type = _WEFLOW_LOCAL_TYPE_MAP.get(base_local_type(lt))
    if msg_type is None:
        return None
    create_time = raw.get("createTime")
    if create_time is None:
        return None

    sender_wxid = raw.get("senderUsername")
    sender_display = (members or {}).get(sender_wxid or "") or sender_wxid

    content_text: str | None = None
    media_path: str | None = None
    forwarded_items: list = []
    quote_text: str | None = None
    reply_to_wx_msg_id: str | None = None

    if msg_type in MEDIA_TYPES:
        media_ref = raw.get("mediaLocalPath") or raw.get("mediaUrl")
        media_path = materialize_media_ref(
            media_ref, msg_type, group_id, settings.data_dir,
        )
        if media_path is None:
            content_text = (
                MEDIA_MISSING_TAGS[msg_type]
                if media_ref and raw.get("mediaLocalPath") else raw.get("content")
            )
    elif msg_type is MsgType.FORWARD:
        forwarded_items = parse_record_xml(raw.get("rawContent"))
        content_text = "[聊天记录]"
    elif msg_type is MsgType.QUOTE:
        parsed = parse_quote_reply_xml(raw.get("rawContent"))
        if parsed:
            content_text = parsed.content or None
            quote_text = parsed.quote_text
            reply_to_wx_msg_id = parsed.quote_msg_id
        else:
            content_text = raw.get("content")
    elif sub is not None:
        # link card / file / video card / etc.
        content_text = format_appmsg_content(raw.get("rawContent"), sub) \
            or raw.get("content")
    else:
        content_text = raw.get("content")

    server_id = raw.get("serverId")
    wx_msg_id = str(server_id) if server_id and str(server_id) != "0" else None

    return Message(
        wx_msg_id=wx_msg_id,
        group_id=group_id,
        group_name=group_name,
        sender_wxid=sender_wxid,
        sender_display=sender_display,
        t=int(create_time),
        type=msg_type,
        content_text=content_text,
        media_path=media_path,
        reply_to_wx_msg_id=reply_to_wx_msg_id,
        quote_text=quote_text,
        source="live",
        forwarded_items=forwarded_items,
    )


def _pick_member_display(m: dict[str, Any]) -> str | None:
    """Display name priority for a group member: per-group nickname first."""
    return (
        m.get("groupNickname")
        or m.get("remark")
        or m.get("nickname")
        or m.get("displayName")
        or None
    )


def _load_group_members(client: httpx.Client, session_id: str) -> dict[str, str]:
    """Returns wxid -> display name. Empty dict for non-group sessions or on error.

    Loaded once at startup and cached in memory; new members joining mid-run will
    fall through to wxid until the next live restart. Acceptable trade-off for v1.
    """
    if "@chatroom" not in session_id:
        return {}
    try:
        resp = client.get(
            "/api/v1/group-members",
            params={"chatroomId": session_id},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("group-members fetch failed for {}: {}", session_id, e)
        return {}
    members = resp.json().get("members") or []
    out: dict[str, str] = {}
    for m in members:
        wxid = m.get("wxid")
        display = _pick_member_display(m)
        if wxid and display:
            out[wxid] = display
    return out


def _build_client() -> httpx.Client:
    if not settings.weflow_token:
        raise RuntimeError(
            "WO_WEFLOW_TOKEN is empty. In WeFlow → 设置 → API 服务, enable HTTP API and "
            "copy the access token into your .env."
        )
    # `read=None` keeps the SSE stream open indefinitely; per-call overrides bound it.
    return httpx.Client(
        base_url=settings.weflow_base_url,
        headers={"Authorization": f"Bearer {settings.weflow_token}"},
        timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
    )


def _contact_display(c: dict[str, Any]) -> str | None:
    return c.get("remark") or c.get("nickname") or c.get("displayName")


def _resolve_sessions(client: httpx.Client, names: list[str]) -> list[tuple[str, str | None]]:
    """For each entry in `names` (session id, display name, or remark), return
    (session_id, display_name).

    Resolution order:
      1. Bulk /api/v1/sessions exact match by username or displayName.
      2. /api/v1/contacts keyword search — covers nickname/remark/displayName, where
         most group remarks actually live. WeFlow's sessions endpoint doesn't join
         contacts, so a saved group remark only shows up here.
      3. /api/v1/sessions keyword search (final fuzzy fallback).
    """
    resp = client.get("/api/v1/sessions", params={"limit": 10000}, timeout=30.0)
    resp.raise_for_status()
    sessions = resp.json().get("sessions", []) or []
    by_username = {s["username"]: s for s in sessions if s.get("username")}
    by_display = {s["displayName"]: s for s in sessions if s.get("displayName")}

    resolved: list[tuple[str, str | None]] = []
    for n in names:
        s = by_username.get(n) or by_display.get(n)
        if s:
            resolved.append((s["username"], s.get("displayName")))
            continue

        # Contacts fallback: groups with remarks/nicknames live here.
        try:
            cresp = client.get("/api/v1/contacts", params={"keyword": n, "limit": 50}, timeout=30.0)
            cresp.raise_for_status()
            contacts = cresp.json().get("contacts", []) or []
        except httpx.HTTPError:
            contacts = []
        groups = [c for c in contacts if "@chatroom" in (c.get("username") or "")]
        exact_groups = [
            c for c in groups
            if n in (c.get("remark"), c.get("nickname"), c.get("displayName"))
        ]
        if len(exact_groups) == 1:
            c = exact_groups[0]
            display = _contact_display(c)
            logger.info("resolved {!r} -> {} ({!r})", n, c["username"], display)
            resolved.append((c["username"], display))
            continue
        if len(exact_groups) > 1:
            logger.warning(
                "{!r} matches multiple groups via contacts; pin one in WO_GROUPS by wxid:", n
            )
            for c in exact_groups:
                logger.warning(
                    "  - nick={!r} remark={!r}  username={}",
                    c.get("nickname"), c.get("remark"), c["username"],
                )
            continue

        # No exact contact hit; show partial matches plus session fuzzy hits.
        try:
            sresp = client.get("/api/v1/sessions", params={"keyword": n, "limit": 50}, timeout=30.0)
            sresp.raise_for_status()
            scands = sresp.json().get("sessions", []) or []
        except httpx.HTTPError:
            scands = []

        if groups or scands:
            logger.warning("no exact match for {!r}; closest candidates:", n)
            for c in groups[:10]:
                logger.warning(
                    "  contact  nick={!r} remark={!r}  username={}",
                    c.get("nickname"), c.get("remark"), c["username"],
                )
            for sc in scands[:10]:
                logger.warning(
                    "  session  display={!r}  username={}",
                    sc.get("displayName"), sc.get("username"),
                )
        else:
            logger.warning("session not found in WeFlow: {!r}", n)
    return resolved


def _resolve_all_group_sessions(client: httpx.Client) -> list[tuple[str, str | None]]:
    """Return every group session WeFlow currently exposes via /api/v1/sessions.

    This is the "watch all groups" mode for an empty WO_GROUPS. WeFlow's
    sessions endpoint is the right source here because SSE events also carry
    `sessionId` values from the same namespace.
    """
    resp = client.get("/api/v1/sessions", params={"limit": 10000}, timeout=30.0)
    resp.raise_for_status()
    sessions = resp.json().get("sessions", []) or []
    groups = [
        (s["username"], s.get("displayName"))
        for s in sessions
        if "@chatroom" in (s.get("username") or "")
    ]
    groups.sort(key=lambda item: item[1] or item[0])
    return groups


def _poll_session(
    client: httpx.Client,
    conn: sqlite3.Connection,
    session_id: str,
    group_name: str | None,
    watermark: int,
    members: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Fetch new messages from one session since `watermark` (unix seconds).

    Returns (new_watermark, inserted_count). Used both as the SSE-triggered fetch and
    (in tests) as a standalone poll.
    """
    inserted = 0
    offset = 0
    new_watermark = watermark
    while True:
        resp = client.get(
            "/api/v1/messages",
            params={
                "talker": session_id,
                "start": str(watermark),
                "limit": 1000,
                "offset": offset,
                "media": "1", "image": "1", "voice": "1", "video": "1",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        api_msgs = data.get("messages") or []
        if not api_msgs:
            break

        normalized: list[Message] = []
        for raw in api_msgs:
            m = _api_msg_to_normalized(raw, session_id, group_name, members)
            if m is None:
                continue
            normalized.append(m)
            if m.t > new_watermark:
                new_watermark = m.t

        if normalized:
            _, new_count = write_messages(conn, normalized)
            inserted += new_count

        if not data.get("hasMore"):
            break
        offset += len(api_msgs)

    return new_watermark, inserted


def _iter_sse_events(client: httpx.Client) -> Iterator[dict[str, Any]]:
    """Yield parsed event payloads from /api/v1/push/messages.

    Implements the minimal SSE bits we need: lines `event:` and `data:`, blank line
    closes one event, lines starting with `:` are comments. The `data` is JSON; its
    `event` field already mirrors the SSE event name so we just trust the payload.

    `read=None` from the client config keeps the connection open; we set a long but
    finite read timeout here so a silently-dead connection eventually surfaces.
    """
    SSE_READ_TIMEOUT = 120.0
    with client.stream(
        "GET",
        "/api/v1/push/messages",
        timeout=httpx.Timeout(connect=10.0, read=SSE_READ_TIMEOUT, write=10.0, pool=10.0),
    ) as resp:
        resp.raise_for_status()
        data_buf: list[str] = []
        for line in resp.iter_lines():
            if not line:
                if data_buf:
                    raw = "".join(data_buf)
                    data_buf = []
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("bad SSE data: {}", raw[:200])
                continue
            if line.startswith(":"):
                continue  # comment / heartbeat
            if line.startswith("data:"):
                data_buf.append(line[5:].lstrip())
            # `event:` and `id:` lines: ignored. The JSON payload already carries `event`.


def _start_mm_worker_thread() -> threading.Thread:
    """Run the OCR/ASR worker in a daemon thread alongside live ingest.

    The mm worker has its own DB connection (sqlite3 connections are per-
    thread; WAL mode handles concurrent writes from live + mm). Daemon=True
    so the thread dies when the main thread exits — no separate shutdown
    plumbing. Engines (rapidocr, faster-whisper) lazy-load on first use,
    so there's no startup cost when the queue is empty.
    """
    from ..worker.mm import run_mm_worker
    t = threading.Thread(target=run_mm_worker, name="mm-worker", daemon=True)
    t.start()
    return t


def run_live() -> None:
    _start_mm_worker_thread()
    logger.info("mm worker thread started (OCR/ASR processing in background)")

    with _build_client() as client:
        if settings.groups:
            sessions = _resolve_sessions(client, settings.groups)
        else:
            sessions = _resolve_all_group_sessions(client)
            logger.info(
                "WO_GROUPS is empty; watching all {} group sessions exposed by WeFlow",
                len(sessions),
            )
        if not sessions:
            raise RuntimeError("no group sessions found in WeFlow; check WeFlow login/API state")

        now = int(time.time())
        watermarks: dict[str, int] = {sid: now for sid, _ in sessions}
        session_names: dict[str, str | None] = dict(sessions)
        session_members: dict[str, dict[str, str]] = {}
        for sid, name in sessions:
            session_members[sid] = _load_group_members(client, sid)
            logger.info(
                "watching: {} ({}) — {} members loaded",
                name or "?", sid, len(session_members[sid]),
            )
        watched = set(session_names)

        with get_conn() as conn:
            _consume_sse(client, conn, watched, session_names, session_members, watermarks)


def _consume_sse(
    client: httpx.Client,
    conn: sqlite3.Connection,
    watched: set[str],
    session_names: dict[str, str | None],
    session_members: dict[str, dict[str, str]],
    watermarks: dict[str, int],
) -> None:
    """Subscribe to SSE; on each `message.new` for a watched session, fetch + write.

    Reconnects with exponential backoff on transport errors. Ctrl+C propagates out.
    """
    backoff = 1.0
    while True:
        try:
            logger.info("SSE: connecting to /api/v1/push/messages")
            for event in _iter_sse_events(client):
                kind = event.get("event")
                sid = event.get("sessionId")
                if sid not in watched:
                    continue
                if kind == "message.new":
                    name = session_names.get(sid)
                    new_wm, inserted = _poll_session(
                        client, conn, sid, name, watermarks[sid], session_members.get(sid)
                    )
                    watermarks[sid] = new_wm
                    if inserted:
                        logger.info("{}: +{} new", name or sid, inserted)
                # `message.revoke`: TODO mark message status, not yet modeled.
            backoff = 1.0
        except KeyboardInterrupt:
            logger.info("live capture stopped by user")
            return
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            logger.warning("SSE stream broke ({}); reconnect in {:.1f}s", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
