"""Bridge between the dispatcher's per-message context and the agent runtime.

`dispatcher.py` stays focused on parsing slash commands, classifying triggers,
and managing schedulers + replier; everything that knows about the agent's
multi-phase loop, persona assembly, memory tables, or trace serialization
lives here.

Two top-level entry points:

  `chat_via_agent(ctx, user_question, trigger_kind)`
      One full agent turn for an inbound user message. Returns
      `(reply_text_or_None, trace_block)` so the caller can write to chat
      and to dispatcher.log respectively.

  `chat_via_lurk(...)`
      Silent background-learning pass over new messages since the lurk
      cursor. Never produces a chat reply, only updates `group_memory` /
      `persona_drift`. Returns the trace_block (also appended to
      dispatcher.log when `log_path` is given). When
      `WO_AGENT_BACKEND=openclaw` it delegates to `_chat_via_lurk_openclaw`
      which forwards the same reflection task through the OpenClaw gateway
      so memory writes happen via MCP. Native lurk path stays the default.

`lurk_due_groups()` is exposed for the dispatcher main loop's eligibility
scan.

Trace rendering (`_trace_step_line`, `_format_trace_for_log`) and the
recent-message + cursor SQL helpers all live here too — they are only
useful in agent contexts and have no callers outside this module.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from .. import prompts
from ..config import settings
from ..db import transaction
from ..llm import LLMClient, OpenClawChatCompletions
from ..log_utils import append_log, dump_llm_call
from .memory import get_group_memory, insert_run_log, link_last_run_id
from .persona import assemble_system_prompts
from .runtime import ToolBudget, run_agent, run_lurk_reflection
from .tools import GroupScopedTools
from .tools_read import (
    ExpandForwardBundleTool,
    RecallGroupHistoryTool,
    ViewQuotedChainTool,
    register_phase_a_tools,
)
from .tools_write import (
    phase_b_system_prompt,
    register_phase_b_tools,
    trace_touched_tables,
)

if TYPE_CHECKING:
    from ..dispatcher import CommandContext


# --- recent-message rendering ---------------------------------------------


def _format_recent_for_agent(
    rows: list[sqlite3.Row], bot_wxid: str | None = None
) -> str:
    """One line per message, oldest first. Bare integer msg_ids in [...] so
    the agent can pass them straight to its tools (which take int, not the
    legacy `m:N` cand_id format used by /find).

    When `bot_wxid` is known, mark the bot's own rows with `[自己]` after the
    timestamp — without this the LLM tends to read its prior replies as
    just-another-user and may parrot them or argue with itself.
    """
    out: list[str] = []
    for r in rows:
        sender = r["sender_display"] or r["sender_wxid"] or "?"
        wxid = r["sender_wxid"] or "?"
        ts = datetime.fromtimestamp(int(r["t"])).strftime("%Y-%m-%d %H:%M")
        body = r["content_text"] or r["transcript"] or f"[{r['type']}]"
        body = body.replace("\n", " ").strip()
        self_tag = " [自己]" if bot_wxid and wxid == bot_wxid else ""
        out.append(f"[{r['msg_id']}] {ts}{self_tag} {sender} ({wxid}): {body}")
    return "\n".join(out) if out else prompts.CHAT_RECENT_EMPTY


def _trace_step_line(s: dict[str, Any]) -> str:
    """Render one trace step as a one-liner for dispatcher.log. Compact,
    grep-friendly, but readable enough that you don't need to dig into the
    raw JSON for normal debugging."""
    step = s.get("step", "?")
    kind = s.get("kind")
    if kind == "tool_call":
        tool = s.get("tool") or "?"
        try:
            args_str = json.dumps(s.get("args") or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = repr(s.get("args"))
        if len(args_str) > 120:
            args_str = args_str[:117] + "..."
        result = (s.get("result") or "")
        if isinstance(result, str):
            result = result.replace("\n", " ").strip()
            if len(result) > 200:
                result = result[:197] + "..."
        return f"  step{step} → {tool}({args_str})  ⇒ {result}"
    if kind == "final":
        content = s.get("content")
        if content:
            text = str(content).replace("\n", " ").strip()
            if len(text) > 200:
                text = text[:197] + "..."
            return f"  step{step} ← FINAL: {text}"
        return f"  step{step} ← FINAL: (empty)"
    if kind == "terminate":
        return f"  step{step} ← TERMINATE: {s.get('reason') or '?'}"
    if kind == "tool_error":
        return f"  step{step} ✗ {s.get('tool') or '?'} ERROR: {(s.get('error') or '?')[:120]}"
    if kind == "tool_crash":
        return f"  step{step} ✗ {s.get('tool') or '?'} CRASHED: {(s.get('error') or '?')[:120]}"
    if kind == "tool_budget_exceeded":
        return f"  step{step} ✗ BUDGET: {(s.get('error') or '?')[:120]}"
    if kind == "empty_final_retry":
        return f"  step{step} ↻ empty final → nudge → retry"
    if kind == "max_steps_hit":
        return f"  step{step} ⚠ MAX STEPS HIT"
    if kind == "lurk_observation":
        args = s.get("args") or {}
        return (
            f"  step{step} ◌ lurk_observation: {args.get('recent_msgs', '?')} msgs "
            f"range={args.get('oldest_msg_id', '?')}..{args.get('newest_msg_id', '?')} "
            f"after={args.get('after_msg_id', None)}"
        )
    return f"  step{step} ?{kind}: {json.dumps(s, ensure_ascii=False, default=str)[:160]}"


def _format_trace_for_log(
    phase_a_trace: list[dict[str, Any]] | None,
    phase_b_trace: list[dict[str, Any]] | None,
) -> str:
    """Render Phase A + Phase B traces as block of compact lines suitable for
    dispatcher.log. Empty / None traces collapse to nothing."""
    parts: list[str] = []
    if phase_a_trace:
        parts.append("[Phase A]")
        parts.extend(_trace_step_line(s) for s in phase_a_trace)
    if phase_b_trace:
        parts.append("[Phase B]")
        parts.extend(_trace_step_line(s) for s in phase_b_trace)
    return "\n".join(parts)


# --- recent / lurk SQL helpers --------------------------------------------


def _fetch_recent_for_agent(
    conn: sqlite3.Connection, group_id: str, limit: int
) -> list[sqlite3.Row]:
    """Newest `limit` messages in this group, returned oldest-first."""
    rows = conn.execute(
        """
        SELECT msg_id, t, type, sender_wxid, sender_display,
               content_text, transcript
          FROM messages
         WHERE group_id=?
         ORDER BY t DESC
         LIMIT ?
        """,
        (group_id, limit),
    ).fetchall()
    return list(reversed(rows))


def _fetch_lurk_window(
    conn: sqlite3.Connection, group_id: str, *, after_msg_id: int | None, limit: int
) -> list[sqlite3.Row]:
    """Messages for one lurk pass, oldest-first.

    First run has no cursor, so it bootstraps from the latest `limit` rows.
    Later runs only process rows whose autoincrement msg_id is newer than the
    stored cursor. The agent can still use recall_group_history to inspect
    older material when the new batch points to it.
    """
    if after_msg_id is None:
        return _fetch_recent_for_agent(conn, group_id, limit)
    rows = conn.execute(
        """
        SELECT msg_id, t, type, sender_wxid, sender_display,
               content_text, transcript
          FROM messages
         WHERE group_id=?
           AND msg_id > ?
         ORDER BY msg_id ASC
         LIMIT ?
        """,
        (group_id, after_msg_id, limit),
    ).fetchall()
    return list(rows)


def _get_lurk_cursor(conn: sqlite3.Connection, group_id: str) -> int | None:
    row = conn.execute(
        "SELECT last_msg_id FROM agent_lurk_state WHERE group_id=?",
        (group_id,),
    ).fetchone()
    if row is None or row["last_msg_id"] is None:
        return None
    return int(row["last_msg_id"])


def _upsert_lurk_cursor(
    conn: sqlite3.Connection, *, group_id: str, last_msg_id: int, run_id: int
) -> None:
    conn.execute(
        """
        INSERT INTO agent_lurk_state (group_id, last_msg_id, last_run_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            last_msg_id = excluded.last_msg_id,
            last_run_id = excluded.last_run_id,
            updated_at = excluded.updated_at
        """,
        (group_id, last_msg_id, run_id, time.time()),
    )


def lurk_due_groups(
    conn: sqlite3.Connection, *, min_new_messages: int, limit: int
) -> list[sqlite3.Row]:
    """Groups with enough messages newer than their lurk cursor. Public so
    the dispatcher main loop can drive the auto-scheduler."""
    min_new = max(1, min_new_messages)
    return conn.execute(
        """
        SELECT m.group_id,
               (
                   SELECT m2.group_name
                     FROM messages m2
                    WHERE m2.group_id = m.group_id
                      AND m2.group_name IS NOT NULL
                    ORDER BY m2.msg_id DESC
                    LIMIT 1
               ) AS group_name,
               COUNT(*) AS new_count,
               MAX(m.msg_id) AS newest_msg_id
          FROM messages m
     LEFT JOIN agent_lurk_state s ON s.group_id = m.group_id
         WHERE m.type != 'system'
           AND (s.last_msg_id IS NULL OR m.msg_id > s.last_msg_id)
         GROUP BY m.group_id
        HAVING new_count >= ?
         ORDER BY newest_msg_id ASC
         LIMIT ?
        """,
        (min_new, limit),
    ).fetchall()


# --- chat path ------------------------------------------------------------


def chat_via_agent(
    *,
    ctx: "CommandContext",
    user_question: str,
    trigger_kind: str = "mention",
) -> tuple[str | None, str]:
    """Run the multi-turn agent loop for an @<bot> chat trigger.

    Returns `(reply_text, trace_block)` where:
      - `reply_text` is the text to send to the group (None when the agent
        chose stay_silent, returned empty, etc.).
      - `trace_block` is a multi-line human-readable rendering of the
        Phase A + Phase B traces, suitable for appending to dispatcher.log.

    Always writes one row to `agent_run_log` regardless of outcome (audit).
    """
    started_at = time.time()
    recent_rows = _fetch_recent_for_agent(
        ctx.conn, ctx.group_id, settings.agent_recent_context_chat
    )
    recent_block = _format_recent_for_agent(recent_rows, bot_wxid=ctx.bot_wxid)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    requester_line = (
        prompts.CHAT_REQUESTER_LINE.format(
            requester=ctx.requester, requester_repr=repr(ctx.requester),
        )
        if ctx.requester else ""
    )
    quoted_line = (
        prompts.CHAT_QUOTED_LINE.format(quoted=ctx.quoted_text.strip())
        if ctx.quoted_text and ctx.quoted_text.strip() else ""
    )
    trigger_line = prompts.CHAT_TRIGGER_LINE.format(
        trigger_kind=trigger_kind, trigger_msg_id=ctx.trigger_msg_id,
    )
    self_hint = (
        prompts.CHAT_SELF_HINT.format(bot_wxid=ctx.bot_wxid)
        if ctx.bot_wxid else ""
    )
    user_msg = prompts.CHAT_USER.format(
        now=now_str,
        trigger_line=trigger_line,
        requester_line=requester_line,
        quoted_line=quoted_line,
        self_hint=self_hint,
        recent_block=recent_block,
        user_question=user_question,
    )

    read_tools = GroupScopedTools(
        conn=ctx.conn,
        group_id=ctx.group_id,
        group_name=ctx.group_name,
        bot_name=ctx.bot_name,
    )
    register_phase_a_tools(
        read_tools,
        vision=ctx.vision,
        vision_model=ctx.vision_model,
        vision_max_tokens=ctx.vision_max_tokens,
    )

    # Persona: yaml core + persona_drift table → both system prompts. Yaml
    # missing → built-in defaults; see agent/persona.py.
    system_prompt, phase_b_system_full = assemble_system_prompts(
        conn=ctx.conn,
        group_id=ctx.group_id,
        group_name=ctx.group_name,
        bot_name=ctx.bot_name,
        personas_dir=settings.agent_personas_dir,
        base_phase_b_prompt=phase_b_system_prompt(),
    )

    write_tools: GroupScopedTools | None = None
    phase_b_system: str | None = None
    if settings.agent_reflection_enabled:
        write_tools = GroupScopedTools(
            conn=ctx.conn, group_id=ctx.group_id,
            group_name=ctx.group_name, bot_name=ctx.bot_name,
        )
        register_phase_b_tools(write_tools)
        phase_b_system = phase_b_system_full

    result = run_agent(
        llm=ctx.llm,  # type: ignore[arg-type]  # OpenAICompatLLM satisfies ToolingLLM structurally
        model=ctx.model,
        phase_a_system=system_prompt,
        phase_a_user=user_msg,
        read_tools=read_tools,
        write_tools=write_tools,
        phase_b_system=phase_b_system,
        max_steps=settings.agent_max_steps,
        reflect_max_steps=settings.agent_reflect_max_steps,
        reflection_enabled=settings.agent_reflection_enabled,
        temperature=0.5,
        max_tokens=settings.chat_max_tokens,
        write_max_tokens=settings.write_max_tokens,
        tool_budget=ToolBudget(
            max_per_run=settings.agent_max_tool_calls_per_run,
            max_per_step=settings.agent_max_tool_calls_per_step,
            max_image_reads=settings.agent_max_image_reads_per_run,
            max_voice_reads=settings.agent_max_voice_reads_per_run,
        ),
    )
    finished_at = time.time()

    try:
        with transaction(ctx.conn):
            run_id = insert_run_log(
                ctx.conn,
                group_id=ctx.group_id,
                trigger_msg_id=ctx.trigger_msg_id,
                trigger_kind=trigger_kind,
                phase_a_trace=result.phase_a_trace,
                phase_b_trace=result.phase_b_trace,
                reply_text=result.reply_text,
                started_at=started_at,
                finished_at=finished_at,
            )
            # Raw↔summary link: any memory rows the agent wrote in Phase B
            # get last_run_id pointing back to this run, so future debugging
            # can trace any state to the run that produced it. Skipped when
            # Phase B was disabled or didn't write.
            touched_persona, touched_memory = trace_touched_tables(result.phase_b_trace)
            if touched_persona or touched_memory:
                link_last_run_id(
                    ctx.conn,
                    group_id=ctx.group_id,
                    run_id=run_id,
                    touched_persona=touched_persona,
                    touched_memory=touched_memory,
                )
    except Exception:
        logger.exception("failed to write agent_run_log; agent reply still returned")

    if ctx.llm_log_path:
        # Full trace dump (not just count) so llm_debug.log is self-sufficient
        # for post-mortem; dispatcher.log gets a compact one-liner version.
        dump_llm_call(
            ctx.llm_log_path,
            label=f"agent  ::  {user_question[:60]}",
            system=system_prompt,
            user=user_msg,
            raw=result.reply_text or "(stay_silent)",
            parsed={
                "phase_a_trace": result.phase_a_trace,
                "phase_b_trace": result.phase_b_trace,
            },
        )
    logger.info(
        "agent :: trigger={} msg_id={} steps={} reply_len={}",
        trigger_kind, ctx.trigger_msg_id, len(result.phase_a_trace),
        len(result.reply_text or ""),
    )
    trace_block = _format_trace_for_log(result.phase_a_trace, result.phase_b_trace)
    return result.reply_text, trace_block


# --- lurk path ------------------------------------------------------------


def chat_via_lurk(
    *,
    conn: sqlite3.Connection,
    llm: LLMClient,
    model: str,
    bot_name: str,
    bot_wxid: str | None,
    group_id: str,
    group_name: str | None,
    log_path: Path | None = None,
    llm_log_path: Path | None = None,
) -> str:
    """One synchronous lurk pass: read recent messages + current memory →
    decide whether to update group_memory / persona_drift, no chat reply.

    Behaves like a Phase-B-only agent run: no Phase A tool exploration,
    no `stay_silent` (lurk silence is the default — model just emits empty
    text to end). Caller passes the dispatcher conn explicitly so this can
    be invoked from CLI (one-shot) and later from the dispatcher idle loop.

    Returns a human-readable trace block (also appended to dispatcher.log
    when `log_path` is provided). Writes one `agent_run_log` row with
    `trigger_kind='lurk'` and links `last_run_id` on any memory rows the
    model rewrote.
    """
    started_at = time.time()
    after_msg_id = _get_lurk_cursor(conn, group_id)
    rows = _fetch_lurk_window(
        conn,
        group_id,
        after_msg_id=after_msg_id,
        limit=settings.agent_lurk_recent_msgs,
    )
    if not rows:
        logger.info(
            "lurk: no new messages for group_id={!r} after_msg_id={}",
            group_id, after_msg_id,
        )
        return "(lurk: no new messages)"
    recent_block = _format_recent_for_agent(rows, bot_wxid=bot_wxid)
    msg_ids = [int(r["msg_id"]) for r in rows]
    oldest_msg_id = min(msg_ids)
    newest_msg_id = max(msg_ids)
    window_label = (
        prompts.LURK_WINDOW_LABEL_INCREMENTAL.format(after_msg_id=after_msg_id)
        if after_msg_id is not None
        else prompts.LURK_WINDOW_LABEL_FIRST_RUN
    )

    lurk_rules = phase_b_system_prompt() + prompts.LURK_SYSTEM_ADDENDUM

    _, lurk_system = assemble_system_prompts(
        conn=conn,
        group_id=group_id,
        group_name=group_name,
        bot_name=bot_name,
        personas_dir=settings.agent_personas_dir,
        base_phase_b_prompt=lurk_rules,
    )

    user_msg = prompts.LURK_USER.format(
        window_label=window_label,
        oldest_msg_id=oldest_msg_id,
        newest_msg_id=newest_msg_id,
        n_msgs=len(rows),
        recent_block=recent_block,
    )

    audit_observation_trace: list[dict[str, Any]] = [
        {
            "step": 0,
            "kind": "lurk_observation",
            "tool": "_lurk",
            "args": {
                "group_id": group_id,
                "after_msg_id": after_msg_id,
                "oldest_msg_id": oldest_msg_id,
                "newest_msg_id": newest_msg_id,
                "recent_msgs": len(rows),
            },
            "result": prompts.LURK_OBSERVATION_AUDIT_RESULT,
        }
    ]

    if (settings.agent_backend or "native").lower() == "openclaw":
        return _chat_via_lurk_openclaw(
            conn=conn,
            group_id=group_id,
            group_name=group_name,
            bot_name=bot_name,
            lurk_system=lurk_system,
            user_msg=user_msg,
            audit_observation_trace=audit_observation_trace,
            oldest_msg_id=oldest_msg_id,
            newest_msg_id=newest_msg_id,
            n_msgs=len(rows),
            started_at=started_at,
            log_path=log_path,
            llm_log_path=llm_log_path,
        )

    lurk_tools = GroupScopedTools(
        conn=conn, group_id=group_id, group_name=group_name, bot_name=bot_name,
    )
    lurk_tools.register(RecallGroupHistoryTool(conn=conn, group_id=group_id))
    lurk_tools.register(ViewQuotedChainTool(conn=conn, group_id=group_id))
    lurk_tools.register(ExpandForwardBundleTool(conn=conn, group_id=group_id))
    register_phase_b_tools(lurk_tools)

    phase_b_trace = run_lurk_reflection(
        llm=llm,  # type: ignore[arg-type]
        model=model,
        system_prompt=lurk_system,
        user_message=user_msg,
        tools=lurk_tools,
        max_steps=settings.agent_lurk_max_steps,
        temperature=0.2,
        max_tokens=settings.write_max_tokens,
        tool_budget=ToolBudget(max_per_run=settings.agent_lurk_max_steps * 3, max_per_step=3),
    )
    finished_at = time.time()

    try:
        with transaction(conn):
            run_id = insert_run_log(
                conn,
                group_id=group_id,
                trigger_msg_id=int(rows[-1]["msg_id"]),  # newest in window, for cursor reference
                trigger_kind="lurk",
                phase_a_trace=audit_observation_trace,
                phase_b_trace=phase_b_trace,
                reply_text=None,
                started_at=started_at,
                finished_at=finished_at,
            )
            _upsert_lurk_cursor(
                conn,
                group_id=group_id,
                last_msg_id=newest_msg_id,
                run_id=run_id,
            )
            touched_persona, touched_memory = trace_touched_tables(phase_b_trace)
            if touched_persona or touched_memory:
                link_last_run_id(
                    conn,
                    group_id=group_id,
                    run_id=run_id,
                    touched_persona=touched_persona,
                    touched_memory=touched_memory,
                )
    except Exception:
        logger.exception("lurk: failed to write agent_run_log; trace still returned")

    if llm_log_path:
        dump_llm_call(
            llm_log_path,
            label=f"lurk  ::  group={group_id}",
            system=lurk_system,
            user=user_msg,
            raw="(lurk — no reply)",
            parsed={"phase_b_trace": phase_b_trace},
        )

    trace_block = _format_trace_for_log(audit_observation_trace, phase_b_trace)
    if log_path:
        append_log(log_path, int(rows[-1]["t"]),
                   f"lurk[{group_id}] msgs={len(rows)} range={oldest_msg_id}..{newest_msg_id}\n{trace_block}")
    logger.info(
        "lurk :: group_id={} msgs={} writes={} dur={:.1f}s",
        group_id, len(rows),
        sum(1 for s in phase_b_trace if s.get("kind") == "tool_call" and s.get("tool", "").startswith("update_")),
        finished_at - started_at,
    )
    return trace_block


def _chat_via_lurk_openclaw(
    *,
    conn: sqlite3.Connection,
    group_id: str,
    group_name: str | None,
    bot_name: str,
    lurk_system: str,
    user_msg: str,
    audit_observation_trace: list[dict[str, Any]],
    oldest_msg_id: int,
    newest_msg_id: int,
    n_msgs: int,
    started_at: float,
    log_path: Path | None,
    llm_log_path: Path | None,
) -> str:
    """OpenClaw-backed lurk pass.

    The in-process native lurk loop needs provider-side tool calls. In
    OpenClaw mode, the wechat-bot agent owns tool use through MCP, so this
    sends the same reflection task as one chat-completions turn and lets
    OpenClaw perform any `update_*` calls internally.
    """
    group_memory = get_group_memory(conn, group_id).strip()
    openclaw_contract = f"""

---
OpenClaw lurk contract:
- This is a silent background reflection pass for one WeChat group.
- group_id: {group_id}
- group_name: {group_name or ""}
- bot_name: {bot_name}
- Every WeChat-Oracle MCP tool requires this exact group_id. Never invent,
  omit, or substitute a different group_id.
- Read before write; when updating group memory/persona, write back the full
  merged text.
- Do not answer the chat. Return an empty assistant message after any useful
  memory/persona updates are done.

Current group_memory snapshot:
{group_memory or "(empty)"}
"""
    system_prompt = lurk_system + openclaw_contract
    client = OpenClawChatCompletions(
        gateway_url=settings.openclaw_gateway_url,
        token=settings.openclaw_token,
        agent_id=settings.openclaw_agent_id,
    )
    resp = client.complete(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        max_tokens=settings.write_max_tokens,
        label="lurk",
    )
    finished_at = time.time()
    phase_b_trace = [
        {
            "step": 0,
            "kind": "openclaw_lurk_call",
            "tool": "_openclaw",
            "args": {
                "agent_id": settings.openclaw_agent_id,
                "group_id": group_id,
                "oldest_msg_id": oldest_msg_id,
                "newest_msg_id": newest_msg_id,
                "recent_msgs": n_msgs,
                "duration_s": round(finished_at - started_at, 3),
                "usage": resp.usage,
            },
            "result": (resp.content.strip() or "(empty / silent)"),
        }
    ]

    try:
        with transaction(conn):
            run_id = insert_run_log(
                conn,
                group_id=group_id,
                trigger_msg_id=newest_msg_id,
                trigger_kind="lurk",
                phase_a_trace=audit_observation_trace,
                phase_b_trace=phase_b_trace,
                reply_text=None,
                started_at=started_at,
                finished_at=finished_at,
            )
            _upsert_lurk_cursor(
                conn,
                group_id=group_id,
                last_msg_id=newest_msg_id,
                run_id=run_id,
            )
    except Exception:
        logger.exception("openclaw lurk: failed to write agent_run_log; trace still returned")

    if llm_log_path:
        dump_llm_call(
            llm_log_path,
            label=f"openclaw-lurk  ::  group={group_id}",
            system=system_prompt,
            user=user_msg,
            raw=resp.content.strip() or "(silent)",
            parsed={"phase_b_trace": phase_b_trace, "usage": resp.usage},
        )

    trace_block = _format_trace_for_log(audit_observation_trace, phase_b_trace)
    if log_path:
        append_log(
            log_path,
            int(time.time()),
            f"openclaw-lurk[{group_id}] msgs={n_msgs} range={oldest_msg_id}..{newest_msg_id}\n{trace_block}",
        )
    logger.info(
        "openclaw lurk :: group_id={} msgs={} dur={:.1f}s",
        group_id, n_msgs, finished_at - started_at,
    )
    return trace_block
