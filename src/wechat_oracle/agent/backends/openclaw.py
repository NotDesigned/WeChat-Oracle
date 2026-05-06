"""OpenClaw backend: delegate one chat turn to a local OpenAI-compatible
gateway running the `wechat-bot` agent.

The dispatcher still owns trigger classification, DB status, and replying to
WeChat. OpenClaw owns the model loop and MCP tool use. Tool calls happen inside
OpenClaw via `wechat-oracle openclaw mcp-serve`, so this backend deliberately
does not parse tool calls.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from ... import prompts
from ...config import settings
from ...db import transaction
from ...llm import OpenClawChatCompletions
from ...log_utils import dump_llm_call
from ..media_paths import openclaw_quoted_hint, resolve_quoted_msg_meta
from ..memory import insert_run_log
from ..orchestrator import (
    _fetch_recent_for_agent,
    _format_recent_for_agent,
    _format_trace_for_log,
)
from ..persona import assemble_system_prompts
from ..tools_write import phase_b_system_prompt

if TYPE_CHECKING:
    from ...dispatcher import CommandContext


@dataclass
class OpenClawBackend:
    name: str = "openclaw"

    def chat(
        self,
        *,
        ctx: "CommandContext",
        user_question: str,
        trigger_kind: str,
        reflection_enabled: bool | None = None,
    ) -> tuple[str | None, str]:
        if not settings.openclaw_token:
            raise RuntimeError("WO_OPENCLAW_TOKEN is empty; set it before using WO_AGENT_BACKEND=openclaw")

        started_at = time.time()
        system_prompt, user_msg = _build_messages(
            ctx=ctx,
            user_question=user_question,
            trigger_kind=trigger_kind,
            reflection_enabled=reflection_enabled,
        )
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
            temperature=0.5,
            label=f"agent-chat:{trigger_kind}",
        )

        finished_at = time.time()
        reply_text = _normalize_reply_text(resp.content)
        phase_a_trace = [
            {
                "step": 0,
                "kind": "openclaw_call",
                "tool": "_openclaw",
                "args": {
                    "agent_id": settings.openclaw_agent_id,
                    "trigger_kind": trigger_kind,
                    "trigger_msg_id": ctx.trigger_msg_id,
                    "user_text": user_question,
                    "duration_s": round(finished_at - started_at, 3),
                    "usage": resp.usage,
                },
                "result": reply_text or "(empty / silent)",
            }
        ]

        try:
            with transaction(ctx.conn):
                insert_run_log(
                    ctx.conn,
                    group_id=ctx.group_id,
                    trigger_msg_id=ctx.trigger_msg_id,
                    trigger_kind=trigger_kind,
                    phase_a_trace=phase_a_trace,
                    phase_b_trace=[],
                    reply_text=reply_text,
                    started_at=started_at,
                    finished_at=finished_at,
                )
        except Exception:
            logger.exception("openclaw: failed to write agent_run_log; reply still returned")

        if ctx.llm_log_path:
            dump_llm_call(
                ctx.llm_log_path,
                label=f"openclaw-agent  ::  {user_question[:60]}",
                system=system_prompt,
                user=user_msg,
                raw=reply_text or "(silent)",
                parsed={"phase_a_trace": phase_a_trace, "usage": resp.usage},
            )

        return reply_text, _format_trace_for_log(phase_a_trace, [])


def _build_messages(
    *,
    ctx: "CommandContext",
    user_question: str,
    trigger_kind: str,
    reflection_enabled: bool | None,
) -> tuple[str, str]:
    recent_rows = _fetch_recent_for_agent(
        ctx.conn, ctx.group_id, settings.agent_recent_context_chat
    )
    recent_block = _format_recent_for_agent(recent_rows, bot_wxid=ctx.bot_wxid)
    persona_prompt, _ = assemble_system_prompts(
        conn=ctx.conn,
        group_id=ctx.group_id,
        group_name=ctx.group_name,
        bot_name=ctx.bot_name,
        personas_dir=settings.agent_personas_dir,
        base_phase_b_prompt=phase_b_system_prompt(),
    )
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
    # If the user quote-replied to a rich-content message (image / voice /
    # forward bundle), `quoted_text` on its own is just a placeholder like
    # `[图片]` or `[卡片消息]` — wechat-bot has no way to expand it without
    # being told which MCP tool + integer msg_id to use. Inject the hint here.
    if ctx.quoted_msg_id:
        quoted_msg_id_int, quoted_type = resolve_quoted_msg_meta(
            ctx.conn, ctx.quoted_msg_id,
        )
        # Always emit a hint — the unresolved-parent branch tells the bot the
        # quoted message isn't in our DB so it doesn't try read_image on the
        # trigger msg_id (which is the quote wrapper, not the image).
        hint = openclaw_quoted_hint(
            group_id=ctx.group_id,
            msg_id=quoted_msg_id_int,
            msg_type=quoted_type,
        )
        if hint:
            quoted_line = (quoted_line + "\n" + hint + "\n") if quoted_line else (hint + "\n")
    self_hint = prompts.CHAT_SELF_HINT.format(bot_wxid=ctx.bot_wxid) if ctx.bot_wxid else ""
    user_msg = prompts.CHAT_USER.format(
        now=now_str,
        trigger_line=prompts.CHAT_TRIGGER_LINE.format(
            trigger_kind=trigger_kind,
            trigger_msg_id=ctx.trigger_msg_id,
        ),
        requester_line=requester_line,
        quoted_line=quoted_line,
        self_hint=self_hint,
        recent_block=recent_block,
        user_question=user_question,
    )
    if trigger_kind == "local_ask" and reflection_enabled is False:
        local_contract = """
- This is a local operator ask, not a live WeChat trigger. The final answer is
  shown only in the local CLI/TUI and will not be sent to WeChat.
- This turn is read-only. Do not call update_group_memory or
  update_persona_drift. You may call read_group_memory and history/media tools.
"""
    elif trigger_kind == "local_task":
        local_contract = """
- This is a local operator task, not a live WeChat trigger. The final answer is
  shown only in the local CLI/TUI and will not be sent to WeChat.
- The operator explicitly enabled write mode. You may update group_memory or
  persona_drift only when the request asks for it or the run discovers stable
  long-term information worth preserving.
"""
    else:
        local_contract = ""

    openclaw_contract = f"""

---
OpenClaw runtime contract:
- You are answering for exactly one WeChat group.
- group_id: {ctx.group_id}
- group_name: {ctx.group_name or ""}
- Every WeChat-Oracle MCP tool requires this exact group_id. Never invent,
  omit, or substitute a different group_id.
- MCP tools already enforce group isolation. If a tool returns no data for this
  group_id, treat that as no data for this group.
- Group memory / persona are not pre-loaded into this prompt. **By default,
  call read_group_memory at the start of the turn** — it carries the group's
  ongoing context, member profiles, internal jokes, and prior decisions, and
  most replies depend on at least one of those. Only skip the call when the
  question is obviously self-contained and clearly does not need group context
  (e.g., a pure factual lookup the model can answer from general knowledge,
  or a one-line acknowledgement). Also call read_persona_drift / read_group_memory
  before any update_* write — both tables are full-replace, so read first,
  then write back the full merged text.
- For historical questions, prefer search_group_messages with start_date /
  end_date and sender filters over repeated broad substring guesses. After a
  key msg_id is found, call get_message_context to inspect nearby messages.
- For images, load_image returns the original MCP image block for direct
  visual inspection; read_image uses the configured WO_VISION_* model and
  returns a textual reading/OCR summary.
- {prompts.READ_IMAGE_OCR_FALLBACK}
- To stay silent, return an empty assistant message.
{local_contract}
"""
    return persona_prompt + openclaw_contract, user_msg


def _normalize_reply_text(content: str) -> str | None:
    """Map OpenClaw gateway placeholders for empty agent output to silence."""
    text = (content or "").strip()
    if not text:
        return None
    if text.casefold() == "no response from openclaw.".casefold():
        return None
    return text
