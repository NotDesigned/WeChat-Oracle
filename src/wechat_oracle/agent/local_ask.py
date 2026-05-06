"""Local operator asks against one group's archive.

This is the same agent capability as a direct @ trigger, but it is initiated
from the CLI / TUI and never sends a message back to WeChat. The caller chooses
one group, asks a question, and gets the answer locally.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..db import get_conn, init_db
from ..log_utils import append_event


@dataclass(frozen=True)
class LocalAskGroup:
    group_id: str
    group_name: str | None
    msg_count: int
    last_t: int | None

    @property
    def label(self) -> str:
        name = self.group_name or "(unnamed group)"
        return f"{name} ({self.group_id})"

    @property
    def short_label(self) -> str:
        return self.group_name or self.group_id

    @property
    def last_seen(self) -> str:
        if not self.last_t:
            return "?"
        return datetime.fromtimestamp(self.last_t).strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class LocalAskResult:
    group: LocalAskGroup
    question: str
    reply_text: str | None
    trace_block: str
    allow_writes: bool
    duration_s: float


def list_local_ask_groups(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[LocalAskGroup]:
    rows = conn.execute(
        """
        SELECT m.group_id,
               (
                   SELECT m2.group_name
                     FROM messages m2
                    WHERE m2.group_id = m.group_id
                      AND m2.group_name IS NOT NULL
                      AND m2.group_name != ''
                    ORDER BY m2.msg_id DESC
                    LIMIT 1
               ) AS group_name,
               COUNT(*) AS msg_count,
               MAX(m.t) AS last_t
          FROM messages m
         WHERE m.group_id IS NOT NULL
           AND m.group_id != ''
         GROUP BY m.group_id
         ORDER BY last_t DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        LocalAskGroup(
            group_id=r["group_id"],
            group_name=r["group_name"],
            msg_count=int(r["msg_count"] or 0),
            last_t=int(r["last_t"]) if r["last_t"] is not None else None,
        )
        for r in rows
    ]


def resolve_local_ask_group(
    conn: sqlite3.Connection,
    selector: str | None,
) -> LocalAskGroup:
    groups = list_local_ask_groups(conn, limit=200)
    if not groups:
        raise ValueError("no groups found in messages; run ingest live or backfill first")

    selector = (selector or "").strip()
    if not selector:
        if len(settings.groups) == 1:
            return resolve_local_ask_group(conn, settings.groups[0])
        return groups[0]

    exact = [
        g for g in groups
        if g.group_id == selector or (g.group_name and g.group_name == selector)
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return _ambiguous(selector, exact)

    needle = selector.casefold()
    fuzzy = [
        g for g in groups
        if needle in g.group_id.casefold()
        or (g.group_name and needle in g.group_name.casefold())
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        return _ambiguous(selector, fuzzy)
    raise ValueError(f"no group matches {selector!r}")


def run_local_ask(
    *,
    group_selector: str | None,
    question: str,
    allow_writes: bool = False,
    log_path: Path | None = None,
    llm_log_path: Path | None = None,
) -> LocalAskResult:
    """Run one local ask turn. Never touches the WeChat reply path."""
    from ..dispatcher import (
        CommandContext,
        _build_llm_client,
        _build_vision_client,
        _resolve_bot_wxid,
    )
    from .backend import get_agent_backend

    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    if not settings.bot_name:
        raise ValueError("WO_BOT_NAME is empty; set it before using local ask")

    init_db()
    settings.ensure_dirs()
    started = time.time()
    llm = _build_llm_client()
    vision = _build_vision_client()
    local_instruction = (
        "\n\n[local_ask]\n"
        "This question comes from the local operator console, not from a WeChat "
        "message. Answer normally for the operator; do not stay silent just "
        "because it was not a live group trigger. Do not @ anyone and do not "
        "assume the answer will be sent to WeChat.\n"
    )
    if allow_writes:
        local_instruction += (
            "The operator explicitly enabled write mode. You may update "
            "group_memory / persona_drift only when the request asks for it "
            "or the run clearly discovers stable long-term information.\n"
        )
    else:
        local_instruction += (
            "This run is read-only. You may read group memory and search "
            "history, but do not update group_memory or persona_drift.\n"
        )

    with get_conn() as conn:
        group = resolve_local_ask_group(conn, group_selector)
        bot_wxid = _resolve_bot_wxid(conn, settings.bot_name)
        ctx = CommandContext(
            conn=conn,
            llm=llm,
            model=settings.llm_model,
            bot_name=settings.bot_name,
            group_id=group.group_id,
            group_name=group.group_name,
            requester="local operator",
            candidate_limit=settings.dispatcher_candidate_limit,
            candidate_limit_chat=settings.dispatcher_context_chat,
            llm_log_path=llm_log_path or settings.data_dir / "llm_debug.log",
            quoted_text=None,
            quoted_msg_id=None,
            vision=vision,
            vision_model=settings.vision_model,
            vision_max_images=settings.vision_max_images,
            vision_max_tokens=settings.vision_max_tokens,
            trigger_msg_id=None,
            trigger_t=None,
            bot_wxid=bot_wxid,
        )
        trigger_kind = "local_task" if allow_writes else "local_ask"
        append_event(
            "agent.local_ask.start",
            group_id=group.group_id,
            group_name=group.group_name,
            trigger_kind=trigger_kind,
            question=question[:200],
            allow_writes=allow_writes,
        )
        try:
            outcome = get_agent_backend().chat(
                ctx=ctx,
                user_question=question + local_instruction,
                trigger_kind=trigger_kind,
                reflection_enabled=allow_writes,
            )
            reply_text = outcome.reply_text
            trace_block = outcome.trace_block
        except Exception as e:
            append_event(
                "agent.local_ask.end",
                group_id=group.group_id,
                group_name=group.group_name,
                trigger_kind=trigger_kind,
                status="error",
                duration_ms=round((time.time() - started) * 1000, 3),
                error=f"{type(e).__name__}: {e}",
            )
            raise

    duration_s = time.time() - started
    append_event(
        "agent.local_ask.end",
        group_id=group.group_id,
        group_name=group.group_name,
        trigger_kind="local_task" if allow_writes else "local_ask",
        status="ok",
        duration_ms=round(duration_s * 1000, 3),
        reply_chars=len(reply_text or ""),
        allow_writes=allow_writes,
    )
    if log_path:
        from ..log_utils import append_log

        append_log(
            log_path,
            int(started),
            (
                f"local ask group={group.short_label} writes={'on' if allow_writes else 'off'} "
                f"dur={duration_s:.1f}s\n"
                f"question: {question}\n"
                f"reply: {reply_text or '(silent)'}\n"
                f"{trace_block}"
            ),
        )
    return LocalAskResult(
        group=group,
        question=question,
        reply_text=reply_text,
        trace_block=trace_block,
        allow_writes=allow_writes,
        duration_s=duration_s,
    )


def _ambiguous(selector: str, groups: list[LocalAskGroup]) -> LocalAskGroup:
    preview = "; ".join(g.label for g in groups[:8])
    more = "" if len(groups) <= 8 else f"; ... {len(groups) - 8} more"
    raise ValueError(f"group selector {selector!r} is ambiguous: {preview}{more}")
