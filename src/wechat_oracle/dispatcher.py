"""Command dispatcher.

Watches the messages table for inbound commands and dispatches each to a
`Command` subclass. The current command set is in `COMMANDS`:

    /find @<target> [since:YYYY[-MM[-DD]]] <description>
    /sum [from:<target>|@<target>] [since:YYYY[-MM[-DD]]] [limit:N] [topic]
    /recent [N]
    /balance
    /ask <question>
    /explain [question-or-text]
    /help [<command>]

Adding a new command = subclass `Command`, register in `COMMANDS`. The
parser returns one of three things:

    None         — the message isn't a command attempt at all (silent no-op)
    ParseError   — looked like a command but malformed (replies with help)
    Command      — ready to execute

For each command msg, `_process` either renders the parse-error reply, or
runs `cmd.execute(ctx)` and emits the result to stdout, the log, and (if
WO_REPLY=1) back into the original group via wx4py.

Decoupled from live ingest: dispatcher polls SQLite, no shared state beyond
the DB. SQLite WAL handles concurrency.

The reply path needs WeChat's main window visible (not minimized to tray) —
wx4py drives the UI. A failed connect disables replies for the run; a single
failed send logs and is non-fatal.
"""

from __future__ import annotations

import abc
import json
import logging
import queue
import random
import re
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, ClassVar

from loguru import logger

from . import prompts
from .agent.backend import AgentChatOutcome, get_agent_backend
from .agent.continuation import (
    arm_planned_followups,
    cancel_active_followups_for_group,
    cancel_planned_followups,
    claim_followup,
    complete_followup,
    due_followups,
    has_active_followups_for_group,
    latest_non_bot_message_after,
)
from .agent.orchestrator import chat_via_lurk, lurk_due_groups
from .config import settings
from .db import get_conn, init_db, transaction
from .llm import (
    LLMClient,
    OpenClawCompletionLLM,
    VisionLLM,
    build_llm_client,
    build_vision_client,
)
from .log_utils import append_event, append_log, dump_llm_call
from .message_render import render_message_body, render_quote_suffix
from .replier import Replier, build_replier


# ---------- Shared types ----------

@dataclass(frozen=True)
class Candidate:
    """One LLM-visible row. `cand_id` is a tagged string so messages and
    forwarded-record items live in one ID space:
        "m:<messages.msg_id>"   — original group message
        "f:<forwarded_records.id>" — child of a 合并转发 wrapper
    The LLM echoes these back verbatim in `hits`; we look them up by exact
    string match (no integer conversion).

    `parent_id` is set on `f:` rows to the wrapper's `m:` cand_id; the chat
    formatter uses it to render children indented under their parent rather
    than scattering them by their (much-earlier) original timestamps.
    """
    cand_id: str
    t: int
    sender: str
    content: str
    parent_id: str | None = None


@dataclass
class ExecResult:
    """What `Command.execute` returns. The dispatcher routes these three to
    different sinks: stdout for the operator, log file for history, chat for
    the WeChat group reply.
    """
    stdout: str
    chat: str
    summary: str  # short status saved to command_runs.result
    agent_outcome: AgentChatOutcome | None = None


@dataclass
class TriggerDecision:
    kind: str | None
    reason: str
    probability: float | None = None
    cooldown_remaining_s: float | None = None
    proactive_mode: str | None = None


@dataclass
class CommandContext:
    """Everything a command might need from the runtime."""
    conn: sqlite3.Connection
    llm: LLMClient
    model: str
    bot_name: str            # for excluding bot's own messages + command-shaped messages
    group_id: str
    group_name: str | None
    requester: str | None
    candidate_limit: int        # /find
    candidate_limit_chat: int   # @<bot> free-text fallback
    llm_log_path: Path | None  # if set, every LLM call is dumped here
    # The triggering message itself was a 引用回复 — these mirror messages.quote_text /
    # reply_to_wx_msg_id. ChatCommand uses `quoted_text` to inline the quoted snippet
    # into the LLM prompt; FindCommand ignores them.
    quoted_text: str | None = None
    quoted_msg_id: str | None = None
    # Optional vision second-pass for `@<bot>` chat. None → text-only (today's behavior).
    vision: VisionLLM | None = None
    vision_model: str = ""
    vision_max_images: int = 3
    vision_max_tokens: int | None = None
    # The triggering message itself — agent loop uses these to identify the
    # current trigger row for `agent_run_log.trigger_msg_id` and to anchor
    # the trigger context the agent reads. Other commands ignore them.
    trigger_msg_id: int | None = None
    trigger_t: int | None = None
    # Bot's own wxid (when known). Used by `chat_via_agent` to mark the
    # bot's own rows in the recent-context dump so the LLM doesn't
    # accidentally treat its prior replies as another user. None when
    # auto-discovery hasn't found a value yet — markers degrade to
    # showing the bot's wxid as just another sender.
    bot_wxid: str | None = None
    # Continuation state. Normal chat runs start at sequence 0; delayed
    # follow-up runs inherit the job id and sequence so schedule_followup can
    # enforce the configured thread cap.
    continuation_token: str | None = None
    continuation_job_id: int | None = None
    continuation_sequence: int = 0
    continuation_max_sequence: int | None = None


@dataclass
class ParseError:
    """The text looked like a command attempt but failed to parse."""
    reason: str
    show_help: type["Command"] | None = None  # which command's help to show

    def chat(self) -> str:
        body = f"⚠️ {self.reason}"
        if self.show_help is not None:
            body += "\n\n" + self.show_help.help()
        else:
            body += "\n\n输入 `/help` 看可用命令。"
        return body


# ---------- Command base + registry ----------

class Command(abc.ABC):
    """One subclass per slash-command.

    Class attributes describe the command for `/help`. Per-instance state holds
    parsed args. `parse` is a classmethod that returns either an instance or a
    `ParseError`; `execute` runs against a `CommandContext` and returns an
    `ExecResult`.
    """

    name: ClassVar[str]
    usage: ClassVar[str]
    description: ClassVar[str]
    examples: ClassVar[list[str]] = []

    @classmethod
    @abc.abstractmethod
    def parse(cls, args: str) -> "Command | ParseError":
        """`args` is the trimmed text after `/<name> `."""

    @abc.abstractmethod
    def execute(self, ctx: CommandContext) -> ExecResult:
        ...

    @classmethod
    def help(cls) -> str:
        out = [f"/{cls.name} — {cls.description}", f"  用法: {cls.usage}"]
        if cls.examples:
            out.append("  例子：")
            out.extend(f"    {e}" for e in cls.examples)
        return "\n".join(out)


COMMANDS: dict[str, type[Command]] = {}


def register(cls: type[Command]) -> type[Command]:
    COMMANDS[cls.name] = cls
    return cls


# ---------- /find ----------

@register
class FindCommand(Command):
    name = "find"
    usage = "/find [from:<人>|@<人>] [since:YYYY[-MM[-DD]]] <描述>"
    description = "在群历史里语义检索发言（LLM 精筛）；不指定人时查全员"
    examples = [
        "/find 关于股票的讨论                   # 全员",
        "/find from:张三 关于数学和物理的发言    # 限定张三（推荐，不会 ping 本人）",
        "/find @张三 关于数学和物理的发言        # 同上，但会 @ 通知张三（兼容老姿势）",
        "/find since:2024-01 关于股票",
        "/find from:张三 since:2024 关于X",
    ]

    def __init__(self, target: str | None, since_t: int | None, description: str):
        self.target = target            # None = search all members
        self.since_t = since_t
        self.description = description

    @classmethod
    def parse(cls, args: str) -> "FindCommand | ParseError":
        s = args.strip()
        if not s:
            return ParseError("/find 需要参数：<描述>，可选 from:<人> 和 since:<时间>", show_help=cls)

        target: str | None = None
        since_t: int | None = None

        # Greedily eat leading markers (`from:X`, `@X`, `since:Y`) in any order.
        while s:
            parts = s.split(maxsplit=1)
            first = parts[0]
            rest = parts[1] if len(parts) > 1 else ""

            if first.startswith("from:"):
                if target is not None:
                    return ParseError("/find 不能同时指定 from: 和 @<人>", show_help=cls)
                t = first[len("from:"):].strip()
                if not t:
                    return ParseError("from: 后面要跟人名", show_help=cls)
                target = t
                s = rest
                continue
            if first.startswith("@") and len(first) > 1:
                if target is not None:
                    return ParseError("/find 不能同时指定 from: 和 @<人>", show_help=cls)
                target = first[1:]
                s = rest
                continue
            if first.startswith("since:"):
                if since_t is not None:
                    return ParseError("/find 重复指定 since:", show_help=cls)
                raw = first[len("since:"):].strip()
                if not raw:
                    return ParseError("since: 后面要跟时间", show_help=cls)
                since_t = _parse_since(raw)
                if since_t is None:
                    return ParseError(
                        f"since:{raw} 格式错误，支持 YYYY / YYYY-MM / YYYY-MM-DD",
                        show_help=cls,
                    )
                s = rest
                continue
            break

        desc = s.strip()
        if not desc:
            return ParseError("缺少查询描述", show_help=cls)
        return cls(target=target, since_t=since_t, description=desc)

    def execute(self, ctx: CommandContext) -> ExecResult:
        cands = fetch_candidates(
            ctx.conn,
            group_id=ctx.group_id,
            target=self.target,
            since_t=self.since_t,
            limit=ctx.candidate_limit,
            bot_name=ctx.bot_name,
        )
        result = llm_filter(
            ctx.llm, ctx.model, self.description, cands,
            log_path=ctx.llm_log_path,
            label=f"/find @{self.target}",
        )
        hits = result.hits
        reason = result.reason
        used_fallback = False
        if not hits and result.keywords:
            fb = keyword_fallback(cands, result.keywords)
            if fb:
                hits = fb
                used_fallback = True
                reason = f"LLM 未匹配，关键词命中：{'/'.join(result.keywords)}"

        logger.info(
            "/find @{} :: {!r}  candidates={}  llm_hits={}  keywords={}  fallback={}",
            self.target, self.description, len(cands),
            len(result.hits), result.keywords, used_fallback,
        )
        summary = f"{len(hits)} hits" + (" (kw-fallback)" if used_fallback else "")
        return ExecResult(
            stdout=self._format_stdout(cands, hits, reason),
            chat=self._format_chat(cands, hits, reason),
            summary=summary,
        )

    def _target_label(self) -> str:
        return f"@{self.target}" if self.target else "全员"

    def _format_stdout(self, cands: list[Candidate], hits: list[str], reason: str) -> str:
        by_id = {c.cand_id: c for c in cands}
        head = f"/find {self._target_label()} :: {self.description}"
        if self.since_t:
            head += f"  [since:{datetime.fromtimestamp(self.since_t):%Y-%m-%d}]"
        head += f"  ({len(cands)} candidates -> {len(hits)} hits)"
        if not hits:
            body = f"  (no match — {reason or 'empty'})"
        else:
            lines = []
            for mid in hits:
                c = by_id.get(mid)
                if c is None:
                    continue
                ts = datetime.fromtimestamp(c.t).strftime("%Y-%m-%d %H:%M")
                lines.append(f"  - [{ts}] {c.sender}: {c.content}")
            body = "\n".join(lines)
            if reason:
                body += f"\n  -> {reason}"
        return f"{head}\n{body}"

    def _format_chat(self, cands: list[Candidate], hits: list[str], reason: str) -> str:
        if not hits:
            tail = f"（{reason}）" if reason else ""
            return f"没找到关于「{self.description}」的相关消息{tail}"
        by_id = {c.cand_id: c for c in cands}
        lines = [f"找到 {len(hits)} 条相关消息："]
        # When target is unspecified, hits may come from different senders — show sender
        # so the reader can tell who said what. When target is specified, sender is
        # known and would just repeat.
        show_sender = self.target is None
        for mid in hits:
            c = by_id.get(mid)
            if c is None:
                continue
            ts = datetime.fromtimestamp(c.t).strftime("%m-%d %H:%M")
            if show_sender:
                lines.append(f"[{ts}] {c.sender}: {c.content}")
            else:
                lines.append(f"[{ts}] {c.content}")
        if reason:
            lines.append(f"— {reason}")
        return "\n".join(lines)


# ---------- @<bot> <free text> fallback ----------

# ChatCommand is intentionally NOT in `COMMANDS` — it's the implicit handler
# for any `@<bot> <text>` that isn't a slash-command. Listed in /help via the
# overview text below.

class ChatCommand(Command):
    name = "(chat)"
    usage = "@<bot> <任意问题或话题>"
    # f-string at class-load time pulls the live default from settings, so
    # changing WO_DISPATCHER_CONTEXT_CHAT (or its default in config.py) doesn't
    # leave this string lying about a stale number — kills a drift point
    # between dispatcher.py help text and config.py default.
    description = (
        "兜底：直接 @ 机器人 + 提问。多轮 agent loop 决定怎么答——"
        f"先看最近 {settings.agent_recent_context_chat} 条群消息，"
        "需要时再调工具搜历史 / 看图 / 读语音 / 查成员笔记，"
        "也可以判断这次不该回应而保持沉默。"
    )
    examples = [
        "@<bot> 谁今天提到了股票？",
        "@<bot> 帮我总结一下昨晚的讨论",
        "@<bot> 张三最近在忙什么",
    ]

    def __init__(self, message: str):
        self.message = message

    @classmethod
    def parse(cls, args: str) -> "ChatCommand | ParseError":
        msg = args.strip()
        if not msg:
            return ParseError("@<bot> 后面要跟问题或话题", show_help=cls)
        return cls(message=msg)

    def execute(self, ctx: CommandContext) -> ExecResult:
        """Multi-turn tool-calling agent loop. Returns ExecResult with
        chat='' (the silent signal honored by `_process`) when the agent
        chose stay_silent. Full trace in `agent_run_log`; readable trace
        block lands in dispatcher.log via stdout."""
        started = time.time()
        append_event(
            "agent.start",
            msg_id=ctx.trigger_msg_id,
            group_id=ctx.group_id,
            group_name=ctx.group_name,
            sender=ctx.requester,
            trigger_kind="mention",
        )
        try:
            outcome = get_agent_backend().chat(
                ctx=ctx, user_question=self.message, trigger_kind="mention",
            )
            reply = outcome.reply_text
            trace_block = outcome.trace_block
        except Exception as e:
            append_event(
                "agent.end",
                msg_id=ctx.trigger_msg_id,
                group_id=ctx.group_id,
                group_name=ctx.group_name,
                sender=ctx.requester,
                trigger_kind="mention",
                status="error",
                duration_ms=round((time.time() - started) * 1000, 3),
                error=f"{type(e).__name__}: {e}",
            )
            raise
        append_event(
            "agent.end",
            msg_id=ctx.trigger_msg_id,
            group_id=ctx.group_id,
            group_name=ctx.group_name,
            sender=ctx.requester,
            trigger_kind="mention",
            status="ok",
            duration_ms=round((time.time() - started) * 1000, 3),
            reply_chars=len(reply or ""),
            silent=reply is None,
        )
        if reply is None:
            stdout_parts = [
                f"@<bot> agent  ::  {self.message}",
                "  (silent — see agent_run_log for full trace)",
            ]
            if trace_block:
                stdout_parts.append(trace_block)
            return ExecResult(
                stdout="\n".join(stdout_parts),
                chat="",
                summary="agent: silent",
                agent_outcome=outcome,
            )
        if not reply.strip():
            reply = "（agent 没返回内容，再问一次试试）"
        stdout_parts = [
            f"@<bot> agent  ::  {self.message}",
            f"  (reply_len={len(reply)})",
            reply,
        ]
        if trace_block:
            stdout_parts.append(trace_block)
        return ExecResult(
            stdout="\n".join(stdout_parts),
            chat=reply,
            summary=f"agent ({len(reply)} chars)",
            agent_outcome=outcome,
        )


# ---------- /ask ----------

@register
class AskCommand(Command):
    name = "ask"
    usage = "/ask <问题>"
    description = "轻量问答：不读取群聊上下文，只把问题发给 LLM，省 token"
    examples = [
        "/ask 帮我把这句话改得更礼貌：今晚别迟到",
        "/ask SQLite WAL 是什么？",
    ]

    def __init__(self, question: str):
        self.question = question

    @classmethod
    def parse(cls, args: str) -> "AskCommand | ParseError":
        question = args.strip()
        if not question:
            return ParseError("/ask 需要参数：<问题>", show_help=cls)
        return cls(question=question)

    def execute(self, ctx: CommandContext) -> ExecResult:
        # If the user 引用ed an image and we have a vision client, send the
        # bytes directly — same single-pass route as /explain. /ask still
        # honors its "no group history" promise: only the user's question
        # + the one quoted image go to the model, no candidate context.
        openclaw_mode = (settings.agent_backend or "native").lower() == "openclaw"
        image_path = (
            _resolve_quoted_image_path(ctx.conn, ctx.quoted_msg_id)
            if (ctx.vision is not None and not openclaw_mode) else None
        )
        if image_path is not None:
            return self._execute_vision(ctx, image_path)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        requester_line = f"提问者：{ctx.requester}\n" if ctx.requester else ""
        # 引用 a non-image message (text / link / card / etc.) → inline its
        # text so the model can answer about it. Without this the quote is
        # silently dropped and "/ask 这句话什么意思" gets answered against
        # thin air.
        quoted_line = (
            f"用户引用了一条消息：{ctx.quoted_text.strip()}\n"
            if ctx.quoted_text and ctx.quoted_text.strip() else ""
        )
        if openclaw_mode and ctx.quoted_msg_id:
            quoted_msg_id, quoted_type = _resolve_quoted_msg_meta(
                ctx.conn, ctx.quoted_msg_id,
            )
            hint = _openclaw_quoted_hint(
                group_id=ctx.group_id,
                msg_id=quoted_msg_id,
                msg_type=quoted_type,
            )
            if hint:
                quoted_line += hint + "\n"
        user = f"当前时间：{now_str}\n{requester_line}{quoted_line}用户问题：{self.question}"
        reply = ctx.llm.complete_text(
            model=ctx.model,
            system=prompts.ASK_SYSTEM,
            user=user,
            temperature=0.3,
            max_tokens=settings.short_max_tokens,
        )
        if not reply:
            reply = prompts.LLM_EMPTY_REPLY
        if ctx.llm_log_path:
            dump_llm_call(
                ctx.llm_log_path,
                label=f"/ask  ::  {self.question}",
                system=prompts.ASK_SYSTEM,
                user=user,
                raw=reply,
                parsed=None,
            )
        logger.info("ask :: {!r}  quoted={}  reply_len={}",
                    self.question[:60], bool(quoted_line), len(reply))
        stdout = f"/ask  ::  {self.question}\n  ({len(reply)} chars)\n{reply}"
        return ExecResult(stdout=stdout, chat=reply, summary=f"ask ({len(reply)} chars)")

    def _execute_vision(
        self, ctx: CommandContext, image_path: Path
    ) -> ExecResult:
        return _run_vision_on_quoted_image(
            ctx, image_path,
            system_prompt=prompts.ASK_SYSTEM,
            user_prompt=prompts.ASK_VISION_USER.format(question=self.question),
            log_label=f"/ask-vision  ::  {self.question[:60]}",
            stdout_header=f"/ask (图片直读)  ::  {self.question}",
            summary_label="ask-vision",
            fail_message=prompts.VISION_FAIL,
            temperature=0.3,
        )


# ---------- /sum ----------

@register
class SumCommand(Command):
    name = "sum"
    usage = "/sum [from:<人>|@<人>] [since:YYYY[-MM[-DD]]] [limit:N] [主题]"
    description = "总结当前群的一段聊天；可按人、时间和主题收窄"
    examples = [
        "/sum",
        "/sum 今天讨论了什么",
        "/sum since:2026-05-01 关于装修",
        "/sum from:张三 limit:100",
    ]

    def __init__(self, target: str | None, since_t: int | None, limit: int | None, topic: str):
        self.target = target
        self.since_t = since_t
        self.limit = limit
        self.topic = topic

    @classmethod
    def parse(cls, args: str) -> "SumCommand | ParseError":
        s = args.strip()
        target: str | None = None
        since_t: int | None = None
        limit: int | None = None

        while s:
            parts = s.split(maxsplit=1)
            first = parts[0]
            rest = parts[1] if len(parts) > 1 else ""

            if first.startswith("from:"):
                if target is not None:
                    return ParseError("/sum 不能同时指定 from: 和 @<人>", show_help=cls)
                target = first[len("from:"):].strip()
                if not target:
                    return ParseError("from: 后面要跟人名", show_help=cls)
                s = rest
                continue
            if first.startswith("@") and len(first) > 1:
                if target is not None:
                    return ParseError("/sum 不能同时指定 from: 和 @<人>", show_help=cls)
                target = first[1:]
                s = rest
                continue
            if first.startswith("since:"):
                if since_t is not None:
                    return ParseError("/sum 重复指定 since:", show_help=cls)
                raw = first[len("since:"):].strip()
                since_t = _parse_since(raw)
                if since_t is None:
                    return ParseError(
                        f"since:{raw} 格式错误，支持 YYYY / YYYY-MM / YYYY-MM-DD",
                        show_help=cls,
                    )
                s = rest
                continue
            if first.startswith("limit:"):
                if limit is not None:
                    return ParseError("/sum 重复指定 limit:", show_help=cls)
                raw = first[len("limit:"):].strip()
                if not raw.isdigit() or int(raw) <= 0:
                    return ParseError("limit: 后面要跟正整数", show_help=cls)
                limit = min(int(raw), 2000)
                s = rest
                continue
            break

        return cls(target=target, since_t=since_t, limit=limit, topic=s.strip())

    def execute(self, ctx: CommandContext) -> ExecResult:
        limit = self.limit or min(ctx.candidate_limit_chat, 500)
        cands = fetch_candidates(
            ctx.conn,
            group_id=ctx.group_id,
            target=self.target,
            since_t=self.since_t,
            limit=limit,
            bot_name=ctx.bot_name,
        )
        if not cands:
            return ExecResult(stdout="/sum: no candidates", chat="没有可总结的群聊消息。", summary="sum: empty")
        reply = summarize_chat(
            ctx.llm,
            ctx.model,
            cands,
            topic=self.topic,
            log_path=ctx.llm_log_path,
        )
        if not reply:
            reply = prompts.LLM_EMPTY_REPLY
        logger.info("sum :: topic={!r} target={!r} candidates={} reply_len={}",
                    self.topic, self.target, len(cands), len(reply))
        stdout = f"/sum :: {self.topic or '(all)'}\n  ({len(cands)} ctx msgs -> {len(reply)} chars)\n{reply}"
        return ExecResult(stdout=stdout, chat=reply, summary=f"sum ({len(cands)} ctx)")


# ---------- /recent ----------

@register
class RecentCommand(Command):
    name = "recent"
    usage = "/recent [N]"
    description = "列出当前群最近 N 条入库消息，不调用 LLM"
    examples = [
        "/recent",
        "/recent 20",
    ]

    def __init__(self, limit: int):
        self.limit = limit

    @classmethod
    def parse(cls, args: str) -> "RecentCommand | ParseError":
        s = args.strip()
        if not s:
            return cls(limit=10)
        if not s.isdigit() or int(s) <= 0:
            return ParseError("/recent 的参数必须是正整数 N", show_help=cls)
        return cls(limit=min(int(s), 50))

    def execute(self, ctx: CommandContext) -> ExecResult:
        cands = fetch_candidates(
            ctx.conn,
            group_id=ctx.group_id,
            target=None,
            since_t=None,
            limit=self.limit,
            bot_name=ctx.bot_name,
        )
        if not cands:
            text = "当前群没有可显示的入库消息。"
            return ExecResult(stdout=text, chat=text, summary="recent: empty")
        lines = [f"最近 {len(cands)} 条："]
        for c in cands:
            ts = datetime.fromtimestamp(c.t).strftime("%m-%d %H:%M")
            content = _clip_one_line(c.content, 80)
            lines.append(f"[{ts}] {c.sender}: {content}")
        text = "\n".join(lines)
        return ExecResult(stdout=text, chat=text, summary=f"recent ({len(cands)})")


# ---------- /balance ----------

@register
class BalanceCommand(Command):
    name = "balance"
    usage = "/balance"
    description = "查询当前 LLM API 账号余额；DeepSeek 兼容接口"
    examples = [
        "/balance",
    ]

    @classmethod
    def parse(cls, args: str) -> "BalanceCommand | ParseError":
        if args.strip():
            return ParseError("/balance 不需要参数", show_help=cls)
        return cls()

    def execute(self, ctx: CommandContext) -> ExecResult:
        if not settings.llm_api_key:
            text = "WO_LLM_API_KEY 为空，无法查询余额。"
            return ExecResult(stdout=text, chat=text, summary="balance: missing key")
        payload = fetch_llm_balance()
        text = format_llm_balance(payload)
        return ExecResult(stdout=text, chat=text, summary="balance")


# ---------- /explain ----------

@register
class ExplainCommand(Command):
    name = "explain"
    usage = "/explain [补充问题或待解释文本]"
    description = "解释引用消息或给定文本；不读取群聊上下文"
    examples = [
        "/explain",
        "/explain 这句话是什么意思：SQLite 开了 WAL",
        "引用一条消息后发送 /explain",
    ]

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def parse(cls, args: str) -> "ExplainCommand":
        return cls(text=args.strip())

    def execute(self, ctx: CommandContext) -> ExecResult:
        quoted = ctx.quoted_text.strip() if ctx.quoted_text else ""
        explicit = self.text.strip()
        if not quoted and not explicit:
            text = "请引用一条消息后发送 `/explain`，或者写成 `/explain <待解释文本>`。"
            return ExecResult(stdout=text, chat=text, summary="explain: missing input")

        # If the user 引用ed an image and we have a vision client, send the
        # actual bytes directly — there's no point asking the text model to
        # explain a `[图片]` placeholder. Single-pass: the user has already
        # pointed at the exact message, so no <NEED_IMAGES> selector needed.
        openclaw_mode = (settings.agent_backend or "native").lower() == "openclaw"
        image_path = (
            _resolve_quoted_image_path(ctx.conn, ctx.quoted_msg_id)
            if (ctx.vision is not None and not openclaw_mode) else None
        )
        if image_path is not None:
            return self._execute_vision(ctx, image_path, explicit)

        if quoted:
            source = f"引用内容：{quoted}"
            if explicit:
                source += f"\n用户补充：{explicit}"
        else:
            source = f"待解释文本：{explicit}"
        if openclaw_mode and ctx.quoted_msg_id:
            quoted_msg_id, quoted_type = _resolve_quoted_msg_meta(
                ctx.conn, ctx.quoted_msg_id,
            )
            hint = _openclaw_quoted_hint(
                group_id=ctx.group_id,
                msg_id=quoted_msg_id,
                msg_type=quoted_type,
            )
            if hint:
                source += "\n" + hint

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        user = f"当前时间：{now_str}\n{source}"
        reply = ctx.llm.complete_text(
            model=ctx.model,
            system=prompts.EXPLAIN_SYSTEM,
            user=user,
            temperature=0.2,
            max_tokens=settings.short_max_tokens,
        )
        if not reply:
            reply = prompts.LLM_EMPTY_REPLY
        if ctx.llm_log_path:
            dump_llm_call(
                ctx.llm_log_path,
                label=f"/explain  ::  {explicit or quoted[:60]}",
                system=prompts.EXPLAIN_SYSTEM,
                user=user,
                raw=reply,
                parsed=None,
            )
        logger.info("explain :: quoted={} explicit_len={} reply_len={}",
                    bool(quoted), len(explicit), len(reply))
        stdout = f"/explain\n{source}\n\n{reply}"
        return ExecResult(stdout=stdout, chat=reply, summary=f"explain ({len(reply)} chars)")

    def _execute_vision(
        self, ctx: CommandContext, image_path: Path, explicit: str
    ) -> ExecResult:
        prompt = prompts.EXPLAIN_VISION_USER_HEAD
        if explicit:
            prompt += prompts.EXPLAIN_VISION_USER_EXPLICIT.format(explicit=explicit)
        prompt += prompts.EXPLAIN_VISION_USER_TAIL
        return _run_vision_on_quoted_image(
            ctx, image_path,
            system_prompt=prompts.EXPLAIN_SYSTEM,
            user_prompt=prompt,
            log_label=f"/explain-vision  ::  {explicit[:60] or '(quoted image)'}",
            stdout_header="/explain (图片直读)",
            summary_label="explain-vision",
            fail_message=prompts.VISION_FAIL,
            temperature=0.2,
        )


# ---------- /help ----------

@register
class HelpCommand(Command):
    name = "help"
    usage = "/help [<command>]"
    description = "列出所有命令，或显示某条命令的详细用法"
    examples = [
        "/help",
        "/help find",
    ]

    def __init__(self, target_name: str | None):
        self.target_name = target_name

    @classmethod
    def parse(cls, args: str) -> "HelpCommand | ParseError":
        s = args.strip().lstrip("/")
        return cls(target_name=s or None)

    def execute(self, ctx: CommandContext) -> ExecResult:
        if self.target_name:
            cmd_cls = COMMANDS.get(self.target_name)
            if cmd_cls is None:
                text = f"未知命令 /{self.target_name}\n\n" + _help_overview()
                return ExecResult(stdout=text, chat=text, summary="help: unknown")
            text = cmd_cls.help()
            return ExecResult(stdout=text, chat=text, summary=f"help: {self.target_name}")
        text = _help_overview()
        return ExecResult(stdout=text, chat=text, summary="help: overview")


def _help_overview() -> str:
    lines = ["可用命令："]
    for cls in COMMANDS.values():
        lines.append(f"/{cls.name} — {cls.description}")
        lines.append(f"  {cls.usage}")
    lines.append(
        f"不带 /：直接问，agent 多轮 loop 处理"
        f"（最近 {settings.agent_recent_context_chat} 条 + 按需调工具）。"
    )
    lines.append("输入 `/help <命令>` 查看示例，比如 `/help sum`。")
    return "\n".join(lines)


# ---------- Top-level parse ----------

def parse_command(content_text: str | None, bot_name: str) -> Command | ParseError | None:
    """Three-state result.

    None        — message isn't an `@<bot>` ping at all (silent no-op)
    ParseError  — `@<bot> /<known>` but args malformed, OR `@<bot> /<unknown>`
    Command     — `@<bot> /<known> <args>` parsed cleanly,
                  OR `@<bot> <free text>` → ChatCommand fallback

    The `@<bot>` mention may appear ANYWHERE in the message, not just at the
    start. Body is the message text with the mention token removed; surrounding
    text on both sides is preserved so e.g. "你看看 @<bot> 这个问题" hands
    "你看看  这个问题" to ChatCommand.

    `@<bot>` must be terminated by whitespace or end-of-string so substring
    `@<bot>x` (a different nick that happens to start with the bot's name)
    is not treated as a ping.
    """
    if not content_text or not bot_name:
        return None
    mention_re = rf"@{re.escape(bot_name)}(?=\s|$)"
    if not re.search(mention_re, content_text):
        return None
    # Strip the @<bot> token(s); keep everything else.
    body = re.sub(mention_re, "", content_text).strip()
    if not body:
        return None

    if body.startswith("/"):
        cm = re.match(r"/(\S+)\s*(.*?)$", body, re.DOTALL)
        if not cm:
            return ParseError(reason="缺少命令名（/ 后面要跟命令）", show_help=None)
        cmd_name = cm.group(1)
        args = cm.group(2) or ""
        cmd_cls = COMMANDS.get(cmd_name)
        if cmd_cls is None:
            return ParseError(reason=f"未知命令 /{cmd_name}", show_help=None)
        return cmd_cls.parse(args)

    # Fallback: free-form @<bot> question/topic → ChatCommand
    return ChatCommand.parse(body)


def _try_parse_slash(content_text: str | None) -> Command | None:
    """Detect a known slash command at the start of `content_text` (after
    stripping leading whitespace), with NO `@<bot>` mention required.

    Lets a user invoke `/ask`, `/find`, etc. by:
      - quote-replying to the bot and writing `/ask X` (no @-mention)
      - typing `/find xyz` in conversation (no @-mention, no reply)
      - the existing `@<bot> /ask X` mention path also still works
        through `parse_command`; this helper returns None when text
        starts with `@`, so the mention path keeps its semantics.

    Returns the parsed `Command` for clean `/<known_cmd> [args]`. Returns
    `None` for: empty text, no leading slash, unknown command name, or a
    known command whose args fail to parse. Quiet on errors so that random
    `/foo` typed in conversation doesn't spam help into the group —
    explicit `@<bot> /xxx` still routes through `parse_command` which DOES
    surface ParseError to the user (preserves the old mention-path UX).
    """
    body = (content_text or "").strip()
    if not body.startswith("/"):
        return None
    cm = re.match(r"/(\S+)\s*(.*?)$", body, re.DOTALL)
    if not cm:
        return None
    cmd_name = cm.group(1)
    args = cm.group(2) or ""
    cmd_cls = COMMANDS.get(cmd_name)
    if cmd_cls is None:
        return None
    parsed = cmd_cls.parse(args)
    if isinstance(parsed, ParseError):
        return None
    return parsed


def _parse_since(s: str) -> int | None:
    """Accepts YYYY, YYYY-MM, or YYYY-MM-DD. Returns unix seconds at start of
    that period in local time. Bad input returns None.
    """
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None


# ---------- Candidate retrieval ----------

def fetch_candidates(
    conn: sqlite3.Connection,
    group_id: str,
    target: str | None,
    since_t: int | None,
    limit: int,
    bot_name: str | None = None,
    *,
    for_chat: bool = False,
) -> list[Candidate]:
    """Recent messages from `group_id`, most recent first capped at `limit`.

    Unions two sources behind one ID-tagged Candidate stream:
      - direct group messages (`messages`), ID prefixed `m:`
      - children of 合并转发 wrappers (`forwarded_records`), ID prefixed `f:`

    All message types pass through. SQL fetches raw normalized fields; the
    LLM-visible body is rendered by `message_render.py` so agent paths and
    command paths share OCR/ASR prefixes, media placeholders, and quote
    suffixes.

    The only behaviour `for_chat` controls is whether to keep `@<bot> /xxx`
    slash-command messages in the candidate set:
      - `False` (DEFAULT — `/find` etc.): excludes them. Other users' earlier
        `/find` calls are not topical signal for the current query.
      - `True` (ChatCommand free-form): keeps them, since "我刚才让 bot 查了
        X 然后..." is part of the conversation flow.

    `target=None` returns messages from every sender. Otherwise matches
    `sender_display` (and for messages also `sender_wxid`) exactly.
    Forwarded items only have a display name.

    `since_t` filters on each row's own timestamp — for forwarded items that
    is the original source-group time (`<srcMsgCreateTime>`), so a message
    forwarded into the group keeps its true age.

    When `bot_name` is given, excludes the bot's own captured replies in both
    modes (the bot's output isn't useful as candidate or as context).

    Quote-reply rendering: live and backfill are normalized at this output
    boundary. Parent metadata is joined when available so the shared renderer
    can append `[引用 ...]` consistently.
    """
    main_sql = """
        SELECT 'm:' || m.msg_id AS cand_id, m.t,
               COALESCE(m.sender_display, m.sender_wxid, '?') AS sender,
               NULL AS parent_id,
               m.msg_id, m.type, m.sender_wxid, m.sender_display,
               m.content_text, m.transcript, m.quote_text,
               orig.msg_id AS parent_msg_id,
               orig.type AS parent_type,
               orig.sender_display AS parent_sender,
               orig.sender_wxid AS parent_sender_wxid,
               NULL AS fwd_content
          FROM messages m
          LEFT JOIN messages orig
                 ON orig.wx_msg_id = m.reply_to_wx_msg_id
                AND orig.group_id  = m.group_id
         WHERE m.group_id = ?
    """
    main_params: list[object] = [group_id]
    if target is not None:
        main_sql += " AND (m.sender_display = ? OR m.sender_wxid = ?)"
        main_params.extend([target, target])
    if since_t is not None:
        main_sql += " AND m.t >= ?"
        main_params.append(since_t)
    if bot_name:
        main_sql += " AND m.sender_display != ?"
        main_params.append(bot_name)
        if not for_chat:
            # /find: drop slash-command messages from the candidate pool —
            # other users' earlier `/find ...` calls aren't topical signal.
            # chat: keep them; they're part of the conversation flow.
            main_sql += " AND m.content_text NOT LIKE ?"
            main_params.append(f"%@{bot_name}%/%")

    fwd_sql = """
        SELECT 'f:' || f.id AS cand_id, f.t,
               COALESCE(f.sender_display, '?') AS sender,
               'm:' || m.msg_id AS parent_id,
               NULL AS msg_id, 'forward_child' AS type,
               NULL AS sender_wxid, f.sender_display,
               NULL AS content_text, NULL AS transcript, NULL AS quote_text,
               NULL AS parent_msg_id, NULL AS parent_type,
               NULL AS parent_sender, NULL AS parent_sender_wxid,
               f.content AS fwd_content
          FROM forwarded_records f
          JOIN messages m ON m.msg_id = f.parent_msg_id
         WHERE m.group_id = ?
           AND f.content IS NOT NULL AND f.content <> ''
    """
    fwd_params: list[object] = [group_id]
    if target is not None:
        fwd_sql += " AND f.sender_display = ?"
        fwd_params.append(target)
    if since_t is not None:
        fwd_sql += " AND f.t >= ?"
        fwd_params.append(since_t)

    sql = f"""
        SELECT * FROM (
            {main_sql}
            UNION ALL
            {fwd_sql}
        )
        ORDER BY t DESC LIMIT ?
    """
    params = main_params + fwd_params + [limit]
    rows = conn.execute(sql, params).fetchall()
    rows.reverse()  # chronological for the LLM
    candidates: list[Candidate] = []
    for r in rows:
        content = (
            r["fwd_content"]
            if r["fwd_content"] is not None
            else render_message_body(r) + render_quote_suffix(r, style="inline")
        )
        if not content.strip():
            continue
        candidates.append(Candidate(
            cand_id=r["cand_id"], t=r["t"], sender=r["sender"],
            content=content, parent_id=r["parent_id"],
        ))
    return candidates


# ---------- LLM filter ----------

def _format_candidates_for_llm(cands: list[Candidate]) -> str:
    """Render candidates for the LLM. `m:` rows print at their own time;
    `f:` rows (合并转发 children) get **inlined directly under their parent
    wrapper, indented**, so the LLM sees the conversation structure rather
    than children scattered by their (much-earlier) original timestamps.

    Orphan `f:` rows (parent fell outside the candidate window) print in
    their own time slot with a small `[父帖不在窗口]` note.
    """
    children: dict[str, list[Candidate]] = {}
    for c in cands:
        if c.parent_id:
            children.setdefault(c.parent_id, []).append(c)

    parents_in_window = {c.cand_id for c in cands if not c.parent_id}
    lines: list[str] = []
    for c in cands:
        ts = datetime.fromtimestamp(c.t).strftime("%Y-%m-%d %H:%M")
        if c.parent_id:
            if c.parent_id in parents_in_window:
                continue  # will be printed under its parent below
            # orphan child: parent rolled out of the window
            lines.append(
                f"[{c.cand_id}] ({ts}) {c.sender}:{c.content}  [父帖不在窗口]"
            )
            continue
        lines.append(f"[{c.cand_id}] ({ts}) {c.sender}:{c.content}")
        for child in children.get(c.cand_id, []):
            cts = datetime.fromtimestamp(child.t).strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"  ↳ [{child.cand_id}] (原 {cts}) {child.sender}:{child.content}"
            )
    return "\n".join(lines)


@dataclass
class LLMFilterResult:
    hits: list[str]
    keywords: list[str]
    reason: str


def llm_filter(
    client: LLMClient,
    model: str,
    description: str,
    cands: list[Candidate],
    log_path: Path | None = None,
    label: str = "/find",
) -> LLMFilterResult:
    """Ask the LLM to rank candidates AND extract search keywords. Keywords are
    the safety net for the keyword-fallback step in `FindCommand.execute`.

    If `log_path` is given, the full system + user + raw response + parsed JSON
    is appended there for offline inspection.
    """
    if not cands:
        return LLMFilterResult(hits=[], keywords=[], reason="no candidates")
    user = f"查询：{description}\n\n候选消息：\n{_format_candidates_for_llm(cands)}"
    raw = client.complete_json(
        model=model,
        system=prompts.FIND_SYSTEM,
        user=user,
        temperature=0.0,
    )
    payload: object | None = None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("LLM returned non-JSON: {!r}", raw[:200])
        if log_path:
            dump_llm_call(log_path, f"{label}  ::  {description}",
                           prompts.FIND_SYSTEM, user, raw, None, note=f"JSONDecodeError: {e}")
        return LLMFilterResult(hits=[], keywords=[], reason=f"bad LLM response: {e}")

    if log_path:
        dump_llm_call(log_path, f"{label}  ::  {description}",
                       prompts.FIND_SYSTEM, user, raw, payload)

    valid_ids = {c.cand_id for c in cands}
    raw_hits = (payload.get("hits") if isinstance(payload, dict) else []) or []
    hits = [str(x) for x in raw_hits if str(x) in valid_ids]
    raw_keywords = (payload.get("keywords") if isinstance(payload, dict) else []) or []
    keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
    reason_text = str((payload.get("reason") if isinstance(payload, dict) else "") or "")
    return LLMFilterResult(hits=hits, keywords=keywords, reason=reason_text)


def _run_vision_on_quoted_image(
    ctx: "CommandContext",
    image_path: Path,
    system_prompt: str,
    user_prompt: str,
    *,
    log_label: str,
    stdout_header: str,
    summary_label: str,
    fail_message: str,
    temperature: float = 0.2,
) -> "ExecResult":
    """Single-pass vision call shared by /explain and /ask when the quoted
    message is an image. Caller already verified `ctx.vision is not None`
    and resolved the path via `_resolve_quoted_image_path`.

    Failure modes (vision API down, bytes unreadable) → return an
    ExecResult that says so; caller doesn't get a partial / confusing
    text-pass since the user explicitly pointed at an image."""
    assert ctx.vision is not None
    try:
        image_bytes = image_path.read_bytes()
        reply_raw = ctx.vision.complete_with_images(
            model=ctx.vision_model,
            system=system_prompt,
            user=user_prompt,
            images=[image_bytes],
            temperature=temperature,
            max_tokens=ctx.vision_max_tokens or settings.short_max_tokens,
        )
    except Exception as e:
        logger.warning("{} vision failed ({}); returning fallback message", log_label, e)
        return ExecResult(stdout=fail_message, chat=fail_message, summary=f"{summary_label}: {e}")
    reply = (reply_raw or "").strip() or prompts.LLM_EMPTY_REPLY
    if ctx.llm_log_path:
        dump_llm_call(
            ctx.llm_log_path,
            label=log_label,
            system=system_prompt,
            user=user_prompt,
            raw=reply_raw,
            parsed=None,
        )
    logger.info("{} :: image={} reply_len={}", summary_label, image_path.name, len(reply))
    stdout = f"{stdout_header}\n{image_path}\n\n{reply}"
    return ExecResult(stdout=stdout, chat=reply, summary=f"{summary_label} ({len(reply)} chars)")


# Image-path resolution lives in agent/media_paths.py so the agent's
# read_image tool and dispatcher's /explain & /ask paths share one
# implementation.
from .agent.media_paths import (
    openclaw_quoted_hint as _openclaw_quoted_hint,
    resolve_quoted_image_path as _resolve_quoted_image_path,
    resolve_quoted_msg_meta as _resolve_quoted_msg_meta,
)


# Agent loop integration (chat_via_agent / chat_via_lurk + trace rendering +
# lurk cursor SQL) lives in agent/orchestrator.py. Dispatcher only imports
# the public entry points at the top of this file.


def summarize_chat(
    client: LLMClient,
    model: str,
    context: list[Candidate],
    topic: str,
    log_path: Path | None = None,
) -> str:
    ctx_text = _format_candidates_for_llm(context)
    user = (
        f"总结主题：{topic or '不限主题，概括这段群聊'}\n\n"
        f"候选消息（按时间正序）：\n{ctx_text}"
    )
    raw = client.complete_text(
        model=model,
        system=prompts.SUM_SYSTEM,
        user=user,
        temperature=0.2,
        max_tokens=settings.sum_max_tokens,
    )
    if log_path:
        dump_llm_call(
            log_path,
            label=f"/sum  ::  {topic or '(all)'}",
            system=prompts.SUM_SYSTEM,
            user=user,
            raw=raw,
            parsed=None,
        )
    return raw


def _clip_one_line(text: str, limit: int) -> str:
    one = " ".join(text.split())
    if len(one) <= limit:
        return one
    return one[:limit - 1] + "…"


def _terminal_print(text: str = "") -> None:
    """Best-effort operator output; never fail message processing."""
    try:
        print(text, flush=True)
    except (OSError, ValueError, UnicodeEncodeError) as e:
        logger.debug("terminal output suppressed: {}: {}", type(e).__name__, e)


def _llm_balance_url() -> str:
    endpoint = settings.llm_endpoint.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]
    return endpoint + "/user/balance"


def fetch_llm_balance() -> dict[str, object]:
    import httpx

    resp = httpx.get(
        _llm_balance_url(),
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=20.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("balance API returned non-object JSON")
    return payload


def format_llm_balance(payload: dict[str, object]) -> str:
    available = payload.get("is_available")
    lines = [f"LLM 余额状态：{'可用' if available else '不可用'}"]
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        lines.append("未返回余额明细。")
        return "\n".join(lines)
    for item in infos:
        if not isinstance(item, dict):
            continue
        currency = item.get("currency") or item.get("currency_code") or "?"
        total = item.get("total_balance", "?")
        granted = item.get("granted_balance")
        topped = item.get("topped_up_balance")
        line = f"{currency}: total={total}"
        if granted is not None:
            line += f", granted={granted}"
        if topped is not None:
            line += f", topped_up={topped}"
        lines.append(line)
    return "\n".join(lines)


def keyword_fallback(cands: list[Candidate], keywords: list[str], cap: int = 5) -> list[str]:
    """Substring search across candidates as a safety net for over-strict LLM
    rejection. Matches if ANY keyword appears in `content` (case-sensitive on
    Chinese, no folding needed). Returns cand_ids in chronological order, capped.
    """
    if not keywords:
        return []
    hits: list[str] = []
    for c in cands:
        if any(k in c.content for k in keywords):
            hits.append(c.cand_id)
            if len(hits) >= cap:
                break
    return hits


# ---------- Run loop ----------


# Per-process per-group "when did the bot last actually speak" map. Used by
# the trigger layer to enforce cooldown for probability triggers (and for
# nothing else; mention/reply must always go through). Module-level so the
# dispatcher loop and any helpers it calls share one view; reset on restart
# is acceptable.
_BOT_LAST_SPOKE_AT: dict[str, float] = {}
_BOT_LAST_SPOKE_AT_LOCK = threading.Lock()


def _resolve_bot_wxid(conn: sqlite3.Connection, bot_name: str) -> str | None:
    """Find the bot's own wxid.

    Order:
      1. `WO_BOT_WXID` config (explicit) — wins immediately.
      2. Auto-discover from `messages`: look for the newest row where
         `sender_display == bot_name` AND `sender_wxid IS NOT NULL`.
         Only succeeds after WeFlow SSE has echoed at least one of the bot's
         own messages back into the table.

    Returns None when neither path resolves. Callers (trigger classifier)
    must tolerate a None bot_wxid by skipping the reply-to-bot path.
    """
    if settings.bot_wxid:
        return settings.bot_wxid
    row = conn.execute(
        """
        SELECT sender_wxid FROM messages
         WHERE sender_display = ?
           AND sender_wxid IS NOT NULL
           AND sender_wxid != ''
         ORDER BY t DESC
         LIMIT 1
        """,
        (bot_name,),
    ).fetchone()
    return row["sender_wxid"] if row else None


def _has_bot_mention(text: str, bot_name: str) -> bool:
    """Cheap mention test used before the full command parser.

    Keep this aligned with `parse_command`: `@<bot>` must be a real mention,
    not a prefix of another nickname like `@<bot>x`.
    """
    if not text or not bot_name:
        return False
    return re.search(rf"@{re.escape(bot_name)}(?:\s|$)", text, re.DOTALL) is not None


def _is_reply_to_bot(
    conn: sqlite3.Connection, row: sqlite3.Row, bot_wxid: str | None
) -> bool:
    """True iff `row` is a quote-reply whose parent (matched by
    reply_to_wx_msg_id → wx_msg_id) was sent by the bot."""
    if bot_wxid is None:
        return False
    parent_id = row["reply_to_wx_msg_id"]
    if not parent_id:
        return False
    parent = conn.execute(
        "SELECT sender_wxid FROM messages WHERE wx_msg_id = ? AND group_id = ?",
        (parent_id, row["group_id"]),
    ).fetchone()
    return parent is not None and parent["sender_wxid"] == bot_wxid


def _classify_trigger(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    bot_name: str,
    bot_wxid: str | None,
    now: float,
) -> TriggerDecision:
    """Decide whether to wake the agent for this row. Returns one of
    'mention' / 'reply' / 'probability' or None (skip silently).

    Order matters:
      - mention always wakes (even within cooldown)
      - reply-to-bot always wakes (same)
      - probability is gated by WO_AGENT_PROACTIVE_MODE, cooldown, and
        WO_AGENT_BASE_PROBABILITY

    Cooldown for probability is enforced under lock with a CAS pattern:
    with multiple worker threads classifying concurrent batches, a naive
    "read last; check window; later update" races — each worker sees a
    stale `_BOT_LAST_SPOKE_AT[gid]` because none of them has finished
    replying yet, so all of them fire probability simultaneously. We
    update the timer **inside the same lock** the moment probability
    is granted, so only one worker per cooldown window wins. Even if
    Phase A subsequently chooses stay_silent, the cooldown stays — the
    LLM was burned anyway, the group should get a quiet moment.
    """
    text = row["content_text"] or ""
    if _has_bot_mention(text, bot_name):
        return TriggerDecision("mention", "mention_match")
    if _is_reply_to_bot(conn, row, bot_wxid):
        return TriggerDecision("reply", "reply_to_bot")
    # Probability path — first honor the participation posture, then the
    # numeric wake chance.
    proactive_mode = settings.agent_proactive_mode
    if proactive_mode == "off":
        return TriggerDecision(
            None, "proactive_mode_off", probability=0.0, proactive_mode=proactive_mode
        )
    p = settings.agent_base_probability
    if p <= 0.0:
        return TriggerDecision(
            None, "probability_disabled", probability=p, proactive_mode=proactive_mode
        )
    if has_active_followups_for_group(conn, row["group_id"]):
        return TriggerDecision(
            None, "continuation_pending", probability=p, proactive_mode=proactive_mode
        )
    # Type gate: an empty random wake-up burns the entire system prompt + recent
    # window for one decision, so only let substantive types in. text/quote
    # carry the user's actual words; image/voice qualify only once OCR/ASR has
    # produced a non-empty transcript. Stickers, raw image/voice (no
    # transcript yet), video, link cards, forward bundles, etc. wait for an
    # explicit @mention or quote-reply instead.
    msg_type = row["type"]
    if msg_type in ("text", "quote"):
        pass
    elif msg_type in ("image", "voice"):
        transcript = (row["transcript"] or "").strip() if "transcript" in row.keys() else ""
        if not transcript:
            return TriggerDecision(
                None,
                "type_gate_no_transcript",
                probability=p,
                proactive_mode=proactive_mode,
            )
    else:
        return TriggerDecision(
            None, "type_gate", probability=p, proactive_mode=proactive_mode
        )
    if random.random() >= p:
        return TriggerDecision(
            None, "dice_miss", probability=p, proactive_mode=proactive_mode
        )
    # Won the dice roll. Atomically check + reserve cooldown.
    with _BOT_LAST_SPOKE_AT_LOCK:
        last = _BOT_LAST_SPOKE_AT.get(row["group_id"], 0.0)
        cooldown_remaining = settings.agent_cooldown_seconds - (now - last)
        if cooldown_remaining > 0:
            return TriggerDecision(
                None,
                "cooldown",
                probability=p,
                cooldown_remaining_s=round(cooldown_remaining, 3),
                proactive_mode=proactive_mode,
            )
        _BOT_LAST_SPOKE_AT[row["group_id"]] = now
    return TriggerDecision(
        "probability", "probability_won", probability=p, proactive_mode=proactive_mode
    )


def _mark_bot_spoke(group_id: str, when: float | None = None) -> None:
    with _BOT_LAST_SPOKE_AT_LOCK:
        _BOT_LAST_SPOKE_AT[group_id] = when or time.time()


def _claim(conn: sqlite3.Connection, msg_id: int) -> bool:
    """INSERT a 'running' row. False if another worker (or a previous run) has it."""
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO command_runs (msg_id, started_at, status) VALUES (?, ?, 'running')",
                (msg_id, int(time.time())),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def _finalize(conn: sqlite3.Connection, msg_id: int, status: str, result: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE command_runs SET finished_at = ?, status = ?, result = ? WHERE msg_id = ?",
            (int(time.time()), status, result, msg_id),
        )


def _row_event_fields(row: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    return {
        "msg_id": row["msg_id"],
        "group_id": row["group_id"],
        "group_name": row["group_name"],
        "msg_type": row["type"],
        "sender": row["sender_display"] or row["sender_wxid"],
    }


def _send_with_events(
    replier: "Replier",
    row: sqlite3.Row | dict[str, object],
    requester: str | None,
    text: str,
    reply_kind: str,
    terminal: bool = False,
) -> bool:
    fields = _row_event_fields(row)
    mention = bool(requester) and _should_mention_reply(reply_kind)
    send_requester = requester if mention else None
    append_event(
        "reply.attempt",
        **fields,
        reply_kind=reply_kind,
        requester=requester,
        mention=mention,
        mention_policy=settings.reply_mention_policy,
        chars=len(text),
    )
    started = time.time()
    try:
        replier.send(row["group_name"], send_requester, text)
    except Exception as e:
        duration_ms = round((time.time() - started) * 1000, 3)
        append_event(
            "reply.end",
            **fields,
            reply_kind=reply_kind,
            status="error",
            duration_ms=duration_ms,
            error=f"{type(e).__name__}: {e}",
            mention=mention,
            mention_policy=settings.reply_mention_policy,
        )
        if terminal:
            _terminal_print(
                f"  send: failed {_mention_terminal_suffix(mention)}dur={duration_ms/1000:.1f}s "
                f"error={type(e).__name__}: {e}"
            )
        return False
    duration_ms = round((time.time() - started) * 1000, 3)
    append_event(
        "reply.end",
        **fields,
        reply_kind=reply_kind,
        status="ok",
        duration_ms=duration_ms,
        mention=mention,
        mention_policy=settings.reply_mention_policy,
    )
    if terminal:
        _terminal_print(f"  send: ok {_mention_terminal_suffix(mention)}dur={duration_ms/1000:.1f}s")
    return True


def _settle_continuation_after_reply(
    conn: sqlite3.Connection,
    *,
    outcome: AgentChatOutcome | None,
    source_trigger_msg_id: int | None,
    source_trigger_kind: str,
    group_name: str | None = None,
    source_job_id: int | None = None,
    sent: bool,
    reply_had_text: bool,
) -> None:
    if outcome is None or not outcome.continuation_token:
        return
    try:
        with transaction(conn):
            if sent and reply_had_text:
                arm_planned_followups(
                    conn,
                    continuation_token=outcome.continuation_token,
                    source_run_id=outcome.run_id,
                    source_trigger_msg_id=source_trigger_msg_id,
                    source_trigger_kind=source_trigger_kind,
                    group_name=group_name,
                    source_job_id=source_job_id,
                )
            else:
                reason = (
                    "source reply was not sent"
                    if reply_had_text else "source agent stayed silent"
                )
                cancel_planned_followups(
                    conn,
                    continuation_token=outcome.continuation_token,
                    reason=reason,
                )
    except Exception:
        logger.exception("failed to settle continuation token={}", outcome.continuation_token)


def _should_mention_reply(reply_kind: str) -> bool:
    policy = settings.reply_mention_policy
    if policy == "always":
        return True
    if policy == "never":
        return False
    return reply_kind not in {"agent:probability", "agent:proactive_followup"}


def _mention_terminal_suffix(mention: bool) -> str:
    return "" if mention else "no-mention "


def _terminal_message_body(row: sqlite3.Row | dict[str, object]) -> str:
    body = render_message_body(row)
    return _clip_one_line(body, 120) if body else ""


def _print_trigger_terminal(
    row: sqlite3.Row | dict[str, object],
    *,
    trigger: str,
    reason: str,
    command: str | None = None,
) -> None:
    sender = row["sender_display"] or row["sender_wxid"] or "?"
    group = row["group_name"] or row["group_id"]
    body = _terminal_message_body(row)
    suffix = f" command=/{command}" if command else ""
    _terminal_print(f"\nmsg={row['msg_id']} group={group} from={sender} type={row['type']}")
    _terminal_print(f"  trigger: {trigger} reason={reason}{suffix}")
    if body:
        _terminal_print(f"  text: {body}")


def _read_recent_mcp_events(group_id: str, since_ts: float) -> list[dict[str, object]]:
    path = settings.data_dir / "mcp.log"
    if not path.exists():
        return []
    out: list[dict[str, object]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    # Historical mcp.log may contain pretty-printed JSON objects and JSONL
    # entries concatenated as `}{`. Decode it as a stream from the tail rather
    # than assuming one object per line.
    start = max(0, len(text) - 500_000)
    chunk = text[start:]
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(chunk):
        next_obj = chunk.find("{", idx)
        if next_obj < 0:
            break
        try:
            item, end = decoder.raw_decode(chunk[next_obj:])
        except json.JSONDecodeError:
            idx = next_obj + 1
            continue
        idx = next_obj + end
        if not isinstance(item, dict):
            continue
        if item.get("group_id") != group_id:
            continue
        ts = item.get("ts")
        if not isinstance(ts, str):
            continue
        try:
            when = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            continue
        if when >= since_ts - 1.0:
            out.append(item)
    return out


def _event_epoch(event: dict[str, object]) -> float | None:
    ts = event.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _format_token_count(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))


def _openclaw_usage_from_trace(trace_block: str) -> dict[str, int] | None:
    match = re.search(
        r"usage=prompt:(\d+)\s+completion:(\d+)\s+total:(\d+)",
        trace_block,
    )
    if not match:
        return None
    return {
        "prompt": int(match.group(1)),
        "completion": int(match.group(2)),
        "total": int(match.group(3)),
    }


def _print_openclaw_timing_terminal(
    *,
    started_at: float,
    ended_at: float,
    trace_block: str,
    mcp_events: list[dict[str, object]],
) -> None:
    if "openclaw" not in trace_block.lower():
        return

    parts: list[str] = []
    usage = _openclaw_usage_from_trace(trace_block)
    if usage:
        parts.append(
            "usage="
            f"{_format_token_count(usage['prompt'])}+"
            f"{_format_token_count(usage['completion'])}/"
            f"{_format_token_count(usage['total'])} tok"
        )

    event_times = sorted(
        (when for event in mcp_events if (when := _event_epoch(event)) is not None)
    )
    if event_times:
        first_tool = max(0.0, event_times[0] - started_at)
        tool_span = max(0.0, event_times[-1] - event_times[0])
        after_tools = max(0.0, ended_at - event_times[-1])
        parts.extend(
            [
                f"first_tool={first_tool:.1f}s",
                f"tool_span={tool_span:.1f}s",
                f"after_tools={after_tools:.1f}s",
            ]
        )

    if parts:
        _terminal_print(f"  openclaw: {' '.join(parts)}")


def _print_agent_activity_terminal(
    *,
    group_id: str,
    started_at: float,
    ended_at: float,
    trace_block: str,
) -> None:
    mcp_events = _read_recent_mcp_events(group_id, started_at)
    _print_openclaw_timing_terminal(
        started_at=started_at,
        ended_at=ended_at,
        trace_block=trace_block,
        mcp_events=mcp_events,
    )
    if mcp_events:
        reads: list[str] = []
        writes: list[str] = []
        seen: dict[str, int] = {}
        for event in mcp_events:
            tool = str(event.get("tool") or "?")
            ok = bool(event.get("ok", False))
            dur = event.get("dur_s")
            item = f"{tool}{'' if ok else '!'}"
            if isinstance(dur, (int, float)):
                item += f" {dur:.1f}s"
            if tool.startswith("update_"):
                writes.append(item)
            else:
                reads.append(item)
            seen[tool] = seen.get(tool, 0) + 1
        reads = [
            f"{item} x{seen[item.split()[0].rstrip('!')]}"
            if seen.get(item.split()[0].rstrip("!"), 0) > 1 else item
            for item in reads
            if reads.index(item) == next(i for i, x in enumerate(reads) if x.split()[0] == item.split()[0])
        ]
        writes = [
            f"{item} x{seen[item.split()[0].rstrip('!')]}"
            if seen.get(item.split()[0].rstrip("!"), 0) > 1 else item
            for item in writes
            if writes.index(item) == next(i for i, x in enumerate(writes) if x.split()[0] == item.split()[0])
        ]
        if reads:
            _terminal_print(f"  tools: {', '.join(reads[:8])}")
        if writes:
            _terminal_print(f"  memory: {', '.join(writes[:6])}")
        return

    tool_lines: list[str] = []
    memory_lines: list[str] = []
    for line in trace_block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("step") or "→" not in stripped:
            continue
        if "update_group_memory" in stripped or "update_persona_drift" in stripped:
            memory_lines.append(_clip_one_line(stripped, 120))
        elif "(" in stripped:
            tool_lines.append(_clip_one_line(stripped, 120))
    if tool_lines:
        _terminal_print(f"  tools: {'; '.join(tool_lines[:4])}")
    if memory_lines:
        _terminal_print(f"  memory: {'; '.join(memory_lines[:3])}")
    if not tool_lines and not memory_lines:
        _terminal_print(
            "  tools: none captured via WeChat-Oracle MCP; see data/openclaw.log for the full OpenClaw turn"
        )


def _is_expected_runtime_failure(parsed: "Command", exc: Exception) -> bool:
    if parsed.name == "(chat)":
        return True
    msg = str(exc)
    return (
        "OpenClaw gateway request failed" in msg
        or "LLM" in msg
        or "timed out" in msg
    )


def _print_terminal_result(
    *,
    msg_id: int,
    parsed: "Command",
    result: ExecResult,
    duration_ms: float,
) -> None:
    """Print the short operator-facing line.

    Full command/agent details still go to dispatcher.log. The terminal should
    stay useful during long-running dispatcher sessions, so free-form agent
    chat only prints a one-line summary instead of the Phase trace.
    """
    if parsed.name == "(chat)":
        _terminal_print(f"  agent: {result.summary} dur={duration_ms/1000:.1f}s")
        return
    _terminal_print(result.stdout)


def _next_unprocessed(
    conn: sqlite3.Connection,
    bot_name: str,
    bot_wxid: str | None = None,
    batch: int = 20,
) -> list[sqlite3.Row]:
    """Oldest `batch` live messages with no command_runs row yet, globally.

    `_GlobalScheduler` serializes processing per group while allowing
    different groups to run in parallel. `_GlobalScheduler.submit` claims rows
    before queueing them, so queued/in-flight rows immediately disappear from
    this query's output and cannot starve later groups behind a same-group batch.

    Includes all message types except 'system' (撤回 / 入群 / 退群 — ambient
    events, not user speech). `forward` / `link` / `image` / `voice` /
    `quote` all flow through; the agent decides whether they warrant a
    reply via `stay_silent`.

    `sender_display != bot_name` excludes the bot's own echoes; when
    bot_wxid is known, the wxid check is the stronger guard (display name
    can drift if you rename the bot in-group).
    """
    own_wxid_clause = ""
    params: list[object] = [bot_name]
    if bot_wxid:
        own_wxid_clause = "AND (m.sender_wxid IS NULL OR m.sender_wxid != ?)"
        params.append(bot_wxid)
    params.append(batch)
    return conn.execute(
        f"""
        SELECT m.msg_id, m.group_id, m.group_name, m.t, m.type, m.content_text,
               m.transcript, m.sender_display, m.sender_wxid,
               m.quote_text, m.reply_to_wx_msg_id, m.wx_msg_id
          FROM messages m
     LEFT JOIN command_runs r ON r.msg_id = m.msg_id
         WHERE m.source = 'live'
           AND m.type != 'system'
           AND r.msg_id IS NULL
           AND (m.sender_display IS NULL OR m.sender_display != ?)
           {own_wxid_clause}
         ORDER BY m.t ASC, m.msg_id ASC
         LIMIT ?
        """,
        params,
    ).fetchall()


def _build_llm_client() -> LLMClient:
    if (settings.agent_backend or "native").lower() == "openclaw":
        return OpenClawCompletionLLM(
            gateway_url=settings.openclaw_gateway_url,
            token=settings.openclaw_token,
            agent_id=settings.openclaw_agent_id,
        )
    native = build_llm_client(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        endpoint=settings.llm_endpoint,
        json_mode=settings.llm_json_mode,
    )
    return native


def _build_vision_client() -> VisionLLM | None:
    """None when WO_VISION_API_KEY is empty — chat falls back to text-only."""
    return build_vision_client(
        provider=settings.vision_provider,
        api_key=settings.vision_api_key,
        endpoint=settings.vision_endpoint,
    )


def _configure_wx4py_logging() -> None:
    level = getattr(logging, settings.wx4py_log_level.upper(), logging.WARNING)
    for name in (
        "wx4py",
        "wx4py.client",
        "wx4py.core",
        "wx4py.core.window",
        "wx4py.features",
        "wx4py.features.chat",
    ):
        logging.getLogger(name).setLevel(level)


def _process(
    conn: sqlite3.Connection,
    llm: LLMClient,
    replier: "Replier",
    row: sqlite3.Row,
    log_path: Path,
    llm_log_path: Path | None,
    vision: VisionLLM | None = None,
    bot_wxid: str | None = None,
) -> None:
    """Route this row to a slash command or to the agent.

    Two-stage routing:
      1. **Slash detection** (`_try_parse_slash`) runs FIRST regardless of
         trigger classification. If text starts with `/<known_cmd>`, dispatch
         it — works without `@<bot>` mention, so quote-replies-to-bot with
         `/ask X`, or bare `/find xxx` typed into the group, both reach the
         right command. Bypasses cooldown (slash commands are explicit user
         intent).
      2. **Trigger classification** (`_classify_trigger`) for the rest:
         - 'mention'     → `parse_command` (slash via `@<bot>` still works
                           there; bare `@<bot> text` → ChatCommand → agent)
         - 'reply'       → straight to chat_via_agent with the quoted text
         - 'probability' → straight to chat_via_agent with no question
         - None          → silently finalize (most messages — bot stays
                           out of group chatter)
    """
    msg_id = row["msg_id"]
    requester = row["sender_display"] or row["sender_wxid"]
    now = time.time()

    # Stage 1: standalone slash command? `_try_parse_slash` is silent on
    # unknown commands / parse errors, so non-slash text or random "/foo"
    # falls through to classification. `@<bot> /xxx` bodies start with @
    # so they fall through too — handled by the existing mention path's
    # `parse_command` (which preserves help-on-error UX for explicit pings).
    slash_cmd = _try_parse_slash(row["content_text"])

    kind: str | None = None
    if slash_cmd is None:
        decision = _classify_trigger(conn, row, settings.bot_name, bot_wxid, now)
        kind = decision.kind
        if kind is not None:
            append_event(
                "trigger.decision",
                **_row_event_fields(row),
                decision=kind,
                reason=decision.reason,
                probability=decision.probability,
                cooldown_remaining_s=decision.cooldown_remaining_s,
                proactive_mode=decision.proactive_mode,
            )
            _print_trigger_terminal(row, trigger=kind, reason=decision.reason)
            if kind in {"mention", "reply"}:
                with transaction(conn):
                    cancel_active_followups_for_group(
                        conn,
                        group_id=row["group_id"],
                        reason=f"cancelled by explicit {kind} trigger msg_id={msg_id}",
                    )
        if kind is None:
            _finalize(conn, msg_id, "ok", "(no-trigger)")
            return
    else:
        append_event(
            "trigger.decision",
            **_row_event_fields(row),
            decision="command",
            reason="slash_command",
            command=slash_cmd.name,
        )
        _print_trigger_terminal(
            row, trigger="command", reason="slash_command", command=slash_cmd.name
        )

    quoted_text = None
    quoted_msg_id = None
    try:
        quoted_text = row["quote_text"]
        quoted_msg_id = row["reply_to_wx_msg_id"]
    except (KeyError, IndexError):
        pass

    ctx = CommandContext(
        conn=conn,
        llm=llm,
        model=settings.llm_model,
        bot_name=settings.bot_name,
        group_id=row["group_id"],
        group_name=row["group_name"],
        requester=requester,
        candidate_limit=settings.dispatcher_candidate_limit,
        candidate_limit_chat=settings.dispatcher_context_chat,
        llm_log_path=llm_log_path,
        quoted_text=quoted_text,
        quoted_msg_id=quoted_msg_id,
        vision=vision,
        vision_model=settings.vision_model,
        vision_max_images=settings.vision_max_images,
        vision_max_tokens=settings.vision_max_tokens,
        trigger_msg_id=int(msg_id),
        trigger_t=int(row["t"]),
        bot_wxid=bot_wxid,
    )

    if slash_cmd is not None:
        _run_command(conn, replier, row, ctx, slash_cmd, log_path, now)
        return

    if kind != "mention":
        # reply / probability: skip slash-command parsing, jump straight to
        # the agent. The user's text is the trigger context itself.
        _process_agent_only(conn, replier, row, ctx, log_path, kind)
        return

    # 'mention' path: keep the existing parse_command flow so `@<bot> /xxx`
    # surfaces ParseError on malformed args. Bare `@<bot> <text>` falls into
    # ChatCommand which runs the agent loop too — same agent, just dispatched
    # through Command.
    parsed = parse_command(row["content_text"], settings.bot_name)
    if parsed is None:
        # @<bot> mention without a body in this message — common when WeChat
        # splits the question and the @ into separate sends, or when the user
        # @s the bot intending to point at recent group context. Hand to the
        # agent with a stub framing; the recent-messages block gives it the
        # surrounding chat to respond to. Bot can still stay_silent.
        parsed = ChatCommand(message=prompts.MENTION_NO_BODY)

    if isinstance(parsed, ParseError):
        text = parsed.chat()
        _terminal_print(text)
        append_log(log_path, row["t"], text)
        _send_with_events(replier, row, requester, text, "parse_error")
        _finalize(conn, msg_id, "ok", f"parse-error: {parsed.reason}")
        return

    _run_command(conn, replier, row, ctx, parsed, log_path, now)


def _run_command(
    conn: sqlite3.Connection,
    replier: "Replier",
    row: sqlite3.Row,
    ctx: "CommandContext",
    parsed: "Command",
    log_path: Path,
    now: float,
) -> None:
    """Execute a parsed Command, dump stdout / log, send chat reply, finalize.
    Shared by the standalone slash path and the @<bot> mention path."""
    msg_id = row["msg_id"]
    requester = row["sender_display"] or row["sender_wxid"]
    started = time.time()
    append_event("command.start", **_row_event_fields(row), command=parsed.name)
    try:
        result = parsed.execute(ctx)
    except Exception as e:
        if _is_expected_runtime_failure(parsed, e):
            logger.warning("execute failed for /{}: {}", parsed.name, e)
        else:
            logger.exception("execute() crashed for /{}", parsed.name)
        msg = f"⚠️ /{parsed.name} 执行失败：{e}"
        if parsed.name == "(chat)":
            duration_ms = round((time.time() - started) * 1000, 3)
            _terminal_print(
                f"  agent: failed dur={duration_ms/1000:.1f}s error={type(e).__name__}: {e}"
            )
        _terminal_print(msg)
        append_log(log_path, row["t"], msg)
        _send_with_events(replier, row, requester, msg, f"command:{parsed.name}:error")
        _finalize(conn, msg_id, "error", str(e))
        append_event(
            "command.end",
            **_row_event_fields(row),
            command=parsed.name,
            status="error",
            duration_ms=round((time.time() - started) * 1000, 3),
            error=f"{type(e).__name__}: {e}",
        )
        return

    duration_ms = round((time.time() - started) * 1000, 3)
    _print_terminal_result(
        msg_id=msg_id,
        parsed=parsed,
        result=result,
        duration_ms=duration_ms,
    )
    append_log(log_path, row["t"], result.stdout)
    if parsed.name == "(chat)":
        _print_agent_activity_terminal(
            group_id=row["group_id"],
            started_at=started,
            ended_at=started + duration_ms / 1000,
            trace_block=result.stdout,
        )
        if result.chat.strip():
            _terminal_print(f"  reply: {_clip_one_line(result.chat, 180)}")
    sent = False
    reply_had_text = bool(result.chat.strip())
    if reply_had_text:
        sent = _send_with_events(
            replier,
            row,
            requester,
            result.chat,
            f"command:{parsed.name}",
            terminal=parsed.name == "(chat)",
        )
        if sent:
            _mark_bot_spoke(row["group_id"], now)
    if parsed.name == "(chat)":
        _settle_continuation_after_reply(
            conn,
            outcome=result.agent_outcome,
            source_trigger_msg_id=msg_id,
            source_trigger_kind="mention",
            group_name=row["group_name"],
            sent=sent,
            reply_had_text=reply_had_text,
        )
    _finalize(conn, msg_id, "ok", result.summary)
    append_event(
        "command.end",
        **_row_event_fields(row),
        command=parsed.name,
        status="ok",
        duration_ms=duration_ms,
        reply_chars=len(result.chat or ""),
        summary=result.summary,
    )


def _process_agent_only(
    conn: sqlite3.Connection,
    replier: "Replier",
    row: sqlite3.Row,
    ctx: "CommandContext",
    log_path: Path,
    kind: str,
) -> None:
    """Reply-to-bot and probability paths bypass slash-command parsing — the
    triggering message text IS the prompt. For probability triggers there
    may be no actionable user question at all, so we feed the agent a stub
    "在群里看到这条消息" framing and let it decide whether to chime in."""
    msg_id = row["msg_id"]
    text = (row["content_text"] or "").strip()
    requester = row["sender_display"] or row["sender_wxid"]
    started = time.time()
    append_event("agent.start", **_row_event_fields(row), trigger_kind=kind)
    if kind == "reply":
        # User replied to one of bot's prior messages. Their reply text is
        # the user_question; quote_text is the bot's prior message we set
        # via ctx.quoted_text and the agent prompt already inlines it.
        user_question = text or prompts.REPLY_EMPTY_FALLBACK
    else:  # probability
        # Dice-roll wake; permissive judgment-call framing. See
        # prompts.PROBABILITY_USER for the full text + history note.
        mode_instruction = (
            prompts.PROBABILITY_MODE_PROACTIVE
            if settings.agent_proactive_mode == "proactive"
            else prompts.PROBABILITY_MODE_REACTIVE
        )
        user_question = prompts.PROBABILITY_USER.format(
            text=text or prompts.PROBABILITY_NON_TEXT_PLACEHOLDER,
            mode_instruction=mode_instruction,
        )

    try:
        outcome = get_agent_backend().chat(
            ctx=ctx, user_question=user_question, trigger_kind=kind,
        )
        reply = outcome.reply_text
        trace_block = outcome.trace_block
    except Exception as e:
        logger.exception("agent crashed on msg_id={} kind={}", msg_id, kind)
        _finalize(conn, msg_id, "error", f"agent-crash: {e}")
        append_event(
            "agent.end",
            **_row_event_fields(row),
            trigger_kind=kind,
            status="error",
            duration_ms=round((time.time() - started) * 1000, 3),
            error=f"{type(e).__name__}: {e}",
        )
        return

    summary = f"agent[{kind}]: " + ("silent" if reply is None else f"{len(reply)} chars")
    duration_ms = round((time.time() - started) * 1000, 3)
    _terminal_print(f"  agent: {summary} dur={duration_ms/1000:.1f}s")
    log_block_parts = [
        f"agent[{kind}] msg_id={msg_id}",
        reply or "(silent)",
    ]
    if trace_block:
        log_block_parts.append(trace_block)
    append_log(log_path, row["t"], "\n".join(log_block_parts))
    _print_agent_activity_terminal(
        group_id=row["group_id"],
        started_at=started,
        ended_at=started + duration_ms / 1000,
        trace_block=trace_block,
    )
    sent = False
    reply_had_text = bool(reply and reply.strip())
    if reply_had_text:
        _terminal_print(f"  reply: {_clip_one_line(reply, 180)}")
    if reply_had_text:
        sent = _send_with_events(
            replier, row, requester, reply, f"agent:{kind}", terminal=True
        )
        if sent:
            _mark_bot_spoke(row["group_id"])
    _settle_continuation_after_reply(
        conn,
        outcome=outcome,
        source_trigger_msg_id=msg_id,
        source_trigger_kind=kind,
        group_name=row["group_name"],
        sent=sent,
        reply_had_text=reply_had_text,
    )
    _finalize(conn, msg_id, "ok", summary)
    append_event(
        "agent.end",
        **_row_event_fields(row),
        trigger_kind=kind,
        status="ok",
        duration_ms=duration_ms,
        reply_chars=len(reply or ""),
        silent=reply is None,
    )


def _followup_row(job: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    return {
        "msg_id": -int(job["job_id"]),
        "group_id": job["group_id"],
        "group_name": job["group_name"],
        "type": "continuation",
        "sender_display": settings.bot_name,
        "sender_wxid": settings.bot_wxid or "",
        "content_text": job["intent"],
        "t": int(time.time()),
    }


def _followup_user_question(job: sqlite3.Row | dict[str, object], latest_msg_id: int | None) -> str:
    kind = str(job["kind"])
    seq = int(job["sequence"])
    max_seq = int(job["max_sequence"])
    latest_line = (
        f"本轮可参考的新消息截止到 msg_id={latest_msg_id}。"
        if latest_msg_id is not None else "本轮没有新的群消息要求；这是承诺式履约。"
    )
    mode_rule = (
        "这是 committed 承诺式后续：你之前已经承诺稍后补充。请履约；如果查不到，简短说明查不到什么。"
        if kind == "committed"
        else "这是 thread 讨论式后续：只有当前上下文仍在同一话题、且你能补充新信息时才回复；否则调用 stay_silent。"
    )
    return (
        "这是系统触发的 proactive_followup，不是新的 @。不要 @ 群成员。\n"
        f"{mode_rule}\n"
        f"后续进度：第 {seq}/{max_seq} 条 follow-up。\n"
        f"原始 intent：{job['intent']}\n"
        f"安排原因：{job['reason'] or '(未写)'}\n"
        f"{latest_line}\n"
        "如果需要再补一条，只有在本次回复正文里明确承诺后续时，才调用 schedule_followup。"
    )


def _process_followup(
    conn: sqlite3.Connection,
    llm: LLMClient,
    replier: "Replier",
    job: sqlite3.Row | dict[str, object],
    log_path: Path,
    llm_log_path: Path | None,
    vision: VisionLLM | None = None,
    bot_wxid: str | None = None,
) -> None:
    job_id = int(job["job_id"])
    fresh = conn.execute(
        "SELECT * FROM agent_proactive_outbox WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if fresh is None or fresh["status"] != "running":
        status = fresh["status"] if fresh is not None else "missing"
        _terminal_print(f"\nfollowup={job_id} skipped status={status}")
        return
    job = fresh
    group_id = str(job["group_id"])
    row = _followup_row(job)
    started = time.time()
    append_event(
        "continuation.start",
        job_id=job_id,
        group_id=group_id,
        group_name=job["group_name"],
        kind=job["kind"],
        sequence=job["sequence"],
        max_sequence=job["max_sequence"],
    )
    _terminal_print(
        f"\nfollowup={job_id} group={job['group_name'] or group_id} "
        f"kind={job['kind']} seq={job['sequence']}/{job['max_sequence']}"
    )
    _terminal_print(f"  intent: {_clip_one_line(str(job['intent']), 180)}")

    latest_msg_id = latest_non_bot_message_after(
        conn,
        group_id=group_id,
        after_msg_id=job["anchor_msg_id"],
        bot_wxid=bot_wxid,
        bot_name=settings.bot_name,
    )
    if job["kind"] == "thread" and latest_msg_id is None:
        complete_followup(
            conn,
            job_id,
            status="cancelled",
            result="thread follow-up cancelled: no new non-bot message",
        )
        _terminal_print("  followup: cancelled no-new-message")
        return

    if latest_msg_id is not None:
        conn.execute(
            "UPDATE agent_proactive_outbox SET latest_msg_id=?, updated_at=? WHERE job_id=?",
            (latest_msg_id, time.time(), job_id),
        )

    ctx = CommandContext(
        conn=conn,
        llm=llm,
        model=settings.llm_model,
        bot_name=settings.bot_name,
        group_id=group_id,
        group_name=job["group_name"],
        requester=None,
        candidate_limit=settings.dispatcher_candidate_limit,
        candidate_limit_chat=settings.dispatcher_context_chat,
        llm_log_path=llm_log_path,
        vision=vision,
        vision_model=settings.vision_model,
        vision_max_images=settings.vision_max_images,
        vision_max_tokens=settings.vision_max_tokens,
        trigger_msg_id=latest_msg_id or job["source_trigger_msg_id"] or job["anchor_msg_id"],
        trigger_t=int(time.time()),
        bot_wxid=bot_wxid,
        continuation_token=str(job["continuation_token"]),
        continuation_job_id=job_id,
        continuation_sequence=int(job["sequence"]),
        continuation_max_sequence=int(job["max_sequence"]),
    )
    try:
        outcome = get_agent_backend().chat(
            ctx=ctx,
            user_question=_followup_user_question(job, latest_msg_id),
            trigger_kind="proactive_followup",
        )
    except Exception as e:
        logger.exception("continuation agent crashed on job_id={}", job_id)
        complete_followup(conn, job_id, status="failed", result=f"agent-crash: {e}")
        _terminal_print(f"  agent: failed error={type(e).__name__}: {e}")
        return

    reply = outcome.reply_text
    duration_ms = round((time.time() - started) * 1000, 3)
    _terminal_print(
        f"  agent: followup {'silent' if reply is None else str(len(reply)) + ' chars'} "
        f"dur={duration_ms/1000:.1f}s"
    )
    _print_agent_activity_terminal(
        group_id=group_id,
        started_at=started,
        ended_at=started + duration_ms / 1000,
        trace_block=outcome.trace_block,
    )

    reply_had_text = bool(reply and reply.strip())
    sent = False
    if reply_had_text:
        _terminal_print(f"  reply: {_clip_one_line(reply, 180)}")
        sent = _send_with_events(
            replier,
            row,
            requester=None,
            text=reply,
            reply_kind="agent:proactive_followup",
            terminal=True,
        )
        if sent:
            _mark_bot_spoke(group_id)

    append_log(
        log_path,
        int(time.time()),
        "\n".join([
            f"agent[proactive_followup] job_id={job_id}",
            reply or "(silent)",
            outcome.trace_block,
        ]),
    )
    _settle_continuation_after_reply(
        conn,
        outcome=outcome,
        source_trigger_msg_id=ctx.trigger_msg_id,
        source_trigger_kind="proactive_followup",
        group_name=job["group_name"],
        source_job_id=job_id,
        sent=sent,
        reply_had_text=reply_had_text,
    )
    if sent and reply_had_text:
        complete_followup(conn, job_id, status="sent", result=f"sent {len(reply)} chars")
    elif reply_had_text:
        complete_followup(conn, job_id, status="failed", result="reply send failed")
    else:
        complete_followup(conn, job_id, status="cancelled", result="agent stayed silent")


@dataclass
class _SendJob:
    group_name: str | None
    requester: str | None
    text: str
    done: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None


class _SerialReplier:
    """Run the real replier from one thread.

    wx4py drives a single GUI, so worker threads must never call it directly.
    `send()` blocks until the sender thread finishes that one GUI operation,
    preserving the old "finalize after send attempt" behavior.
    """

    def __init__(self, inner: Replier) -> None:
        self._inner = inner
        self._queue: queue.Queue[_SendJob | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="wechat-oracle-sender", daemon=True
        )
        self._thread.start()

    def send(self, group_name: str | None, requester: str | None, text: str) -> None:
        job = _SendJob(group_name=group_name, requester=requester, text=text)
        self._queue.put(job)
        job.done.wait()
        if job.error is not None:
            raise job.error

    def disconnect(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=10)
        self._inner.disconnect()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                self._inner.send(job.group_name, job.requester, job.text)
            except Exception as e:
                logger.warning("queued replier send failed: {}", e)
                job.error = e
            finally:
                job.done.set()


class _GlobalScheduler:
    """Global thread pool with per-conversation FIFO queues.

    Messages from the same group run serially; different groups can occupy
    different worker threads. This prevents a probability wake-up from
    racing a preceding mention before the mention has updated cooldown/state.

    The replier is a `_SerialReplier` — wx4py drives one GUI window, so
    the actual `send` call must happen from a single thread. Workers
    enqueue into the sender thread and block until the GUI op finishes,
    preserving the "finalize after send attempt" ordering.

    Each worker thread keeps its own LLM / vision client (sqlite + LLM SDK
    are not necessarily thread-safe across calls).
    """

    def __init__(
        self,
        *,
        replier: Replier,
        log_path: Path,
        llm_log_path: Path | None,
        bot_wxid_getter: Callable[[], str | None],
        max_workers: int,
    ) -> None:
        self._replier = replier
        self._log_path = log_path
        self._llm_log_path = llm_log_path
        self._bot_wxid_getter = bot_wxid_getter
        self._local = threading.local()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="wechat-oracle-msg",
        )
        self._lock = threading.Condition(threading.Lock())
        self._group_queues: dict[str, deque[dict[str, object]]] = {}
        self._active_group_ids: set[str] = set()
        self._closed = False

    def _llm(self) -> LLMClient:
        llm = getattr(self._local, "llm", None)
        if llm is None:
            llm = _build_llm_client()
            self._local.llm = llm
        return llm

    def _vision(self) -> VisionLLM | None:
        if not hasattr(self._local, "vision"):
            self._local.vision = _build_vision_client()
        return self._local.vision

    def submit(self, row: sqlite3.Row) -> bool:
        """Enqueue this row's processing. Returns False if it's already
        in-flight (dedup) or the scheduler is shutting down. The poll loop
        treats `submitted == 0` for an entire batch as "pool saturated" and
        sleeps."""
        row_dict = dict(row)
        row_dict["_work_type"] = "message"
        msg_id = int(row_dict["msg_id"])
        group_key = self._group_key(row_dict)
        with self._lock:
            if self._closed:
                return False

        with get_conn() as conn:
            if not _claim(conn, msg_id):
                return False
            append_event("dispatcher.claim", **_row_event_fields(row_dict))

        next_row: dict[str, object] | None = None
        with self._lock:
            if self._closed:
                self._finalize_abandoned_claim(msg_id)
                return False
            queue_for_group = self._group_queues.setdefault(group_key, deque())
            queue_for_group.append(row_dict)
            if group_key not in self._active_group_ids:
                self._active_group_ids.add(group_key)
                next_row = queue_for_group.popleft()
        if next_row is not None:
            self._executor.submit(self._handle, group_key, next_row)
        return True

    def submit_followup(self, job: sqlite3.Row) -> bool:
        job_id = int(job["job_id"])
        group_key = f"group:{job['group_id']}"
        with self._lock:
            if self._closed:
                return False

        with get_conn() as conn:
            claimed = claim_followup(conn, job_id)
            if claimed is None:
                return False
            work = dict(claimed)
            work["_work_type"] = "followup"
            append_event(
                "continuation.claim",
                job_id=job_id,
                group_id=work["group_id"],
                group_name=work["group_name"],
                kind=work["kind"],
                sequence=work["sequence"],
                max_sequence=work["max_sequence"],
            )

        next_row: dict[str, object] | None = None
        with self._lock:
            if self._closed:
                self._finalize_abandoned_followup(job_id)
                return False
            queue_for_group = self._group_queues.setdefault(group_key, deque())
            queue_for_group.append(work)
            if group_key not in self._active_group_ids:
                self._active_group_ids.add(group_key)
                next_row = queue_for_group.popleft()
        if next_row is not None:
            self._executor.submit(self._handle, group_key, next_row)
        return True

    @staticmethod
    def _group_key(row: dict[str, object]) -> str:
        group_id = row.get("group_id")
        if group_id:
            return f"group:{group_id}"
        sender = row.get("sender_wxid") or row.get("sender_display") or row["msg_id"]
        return f"direct:{sender}"

    @staticmethod
    def _finalize_abandoned_claim(msg_id: int) -> None:
        try:
            with get_conn() as conn:
                _finalize(conn, msg_id, "error", "scheduler closed after claim")
        except Exception:
            logger.exception("failed to finalize abandoned claimed msg_id={}", msg_id)

    @staticmethod
    def _finalize_abandoned_followup(job_id: int) -> None:
        try:
            with get_conn() as conn:
                complete_followup(
                    conn,
                    job_id,
                    status="failed",
                    result="scheduler closed after claim",
                )
        except Exception:
            logger.exception("failed to finalize abandoned followup job_id={}", job_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            while self._active_group_ids:
                self._lock.wait(timeout=1)
        self._executor.shutdown(wait=True)

    def _complete_one(self, group_key: str) -> None:
        next_row: dict[str, object] | None = None
        with self._lock:
            queue_for_group = self._group_queues.get(group_key)
            if queue_for_group:
                next_row = queue_for_group.popleft()
            else:
                self._group_queues.pop(group_key, None)
                self._active_group_ids.discard(group_key)
                if not self._active_group_ids:
                    self._lock.notify_all()
        if next_row is not None:
            self._executor.submit(self._handle, group_key, next_row)

    def _handle(self, group_key: str, row: dict[str, object]) -> None:
        work_type = row.get("_work_type") or "message"
        msg_id = int(row["msg_id"]) if "msg_id" in row else -int(row["job_id"])
        try:
            try:
                with get_conn() as conn:
                    if work_type == "followup":
                        _process_followup(
                            conn,
                            self._llm(),
                            self._replier,
                            row,
                            self._log_path,
                            self._llm_log_path,
                            vision=self._vision(),
                            bot_wxid=self._bot_wxid_getter(),
                        )
                    else:
                        _process(
                            conn,
                            self._llm(),
                            self._replier,
                            row,
                            self._log_path,
                            self._llm_log_path,
                            vision=self._vision(),
                            bot_wxid=self._bot_wxid_getter(),
                        )
            except Exception as e:
                if work_type == "followup":
                    job_id = int(row["job_id"])
                    logger.exception("dispatcher crashed on followup job_id={}", job_id)
                    try:
                        with get_conn() as conn:
                            complete_followup(conn, job_id, status="failed", result=f"crashed: {e}")
                    except Exception:
                        logger.exception("failed to finalize crashed followup job_id={}", job_id)
                else:
                    logger.exception("dispatcher crashed on msg_id={}", msg_id)
                    try:
                        with get_conn() as conn:
                            _finalize(conn, msg_id, "error", f"crashed: {e}")
                    except Exception:
                        logger.exception("failed to finalize crashed msg_id={}", msg_id)
        finally:
            self._complete_one(group_key)


class _LurkScheduler:
    """Low-priority background learner.

    It has its own single worker so lurk never occupies chat response workers,
    and it never touches the replier/wx4py sender path.
    """

    def __init__(
        self,
        *,
        log_path: Path,
        llm_log_path: Path | None,
        bot_wxid_getter: Callable[[], str | None],
    ) -> None:
        self._log_path = log_path
        self._llm_log_path = llm_log_path
        self._bot_wxid_getter = bot_wxid_getter
        self._local = threading.local()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="wechat-oracle-lurk",
        )
        self._lock = threading.Lock()
        self._scheduled_group_ids: set[str] = set()
        self._closed = False

    def _llm(self) -> LLMClient:
        llm = getattr(self._local, "llm", None)
        if llm is None:
            llm = _build_llm_client()
            self._local.llm = llm
        return llm

    def submit(self, group_id: str, group_name: str | None) -> bool:
        with self._lock:
            if self._closed or group_id in self._scheduled_group_ids:
                return False
            self._scheduled_group_ids.add(group_id)
            self._executor.submit(self._handle, group_id, group_name)
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)

    def _forget(self, group_id: str) -> None:
        with self._lock:
            self._scheduled_group_ids.discard(group_id)

    def _handle(self, group_id: str, group_name: str | None) -> None:
        try:
            with get_conn() as conn:
                chat_via_lurk(
                    conn=conn,
                    llm=self._llm(),
                    model=settings.llm_model,
                    bot_name=settings.bot_name,
                    bot_wxid=self._bot_wxid_getter(),
                    group_id=group_id,
                    group_name=group_name,
                    log_path=self._log_path,
                    llm_log_path=self._llm_log_path,
                )
        except Exception:
            logger.exception("lurk scheduler crashed for group_id={}", group_id)
        finally:
            self._forget(group_id)


def _skip_backlog(conn: sqlite3.Connection, bot_name: str) -> int:
    """Mark every pre-existing live message (any type except 'system') as
    already-processed. Run once at dispatcher startup so a cold start doesn't
    flood the group with probability-triggered replies to historical messages
    — backlogs arise from long downtime, fresh DB imports, or restarting
    after a one-shot historical re-pull.

    Scope expanded from the @-only version to match `_next_unprocessed`:
    the dispatcher now scans all live messages (not just @ mentions), so
    we have to skip them all on cold start too.
    """
    now = int(time.time())
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO command_runs (msg_id, started_at, finished_at, status, result)
            SELECT m.msg_id, ?, ?, 'ok', '(startup-skip)'
              FROM messages m
         LEFT JOIN command_runs r ON r.msg_id = m.msg_id
             WHERE m.source = 'live'
               AND m.type != 'system'
               AND r.msg_id IS NULL
               AND (m.sender_display IS NULL OR m.sender_display != ?)
            """,
            (now, now, bot_name),
        )
    return cur.rowcount or 0


def run_dispatcher() -> None:
    if not settings.bot_name:
        raise RuntimeError(
            "WO_BOT_NAME is empty; set it to your alt-account's group nickname in .env"
        )

    _configure_wx4py_logging()
    init_db()
    settings.ensure_dirs()
    log_path = settings.data_dir / "dispatcher.log"
    llm_log_path = settings.data_dir / "llm_debug.log"
    llm = _build_llm_client()
    vision = _build_vision_client()
    replier = _SerialReplier(build_replier())
    _configure_wx4py_logging()
    interval = settings.dispatcher_poll_interval
    worker_threads = max(1, settings.dispatcher_worker_threads)

    logger.info(
        "dispatcher: bot={!r} model={} llm={} vision={} agent_backend={} agent_max_steps={} workers={} interval={}s replier={} wx4py_log={} commands={} log={} llm_log={}",
        settings.bot_name, settings.llm_model,
        getattr(llm, "name", type(llm).__name__),
        f"{settings.vision_model}" if vision else "off",
        settings.agent_backend,
        settings.agent_max_steps,
        worker_threads,
        interval,
        type(replier).__name__, settings.wx4py_log_level, list(COMMANDS), log_path, llm_log_path,
    )

    with get_conn() as conn:
        skipped = _skip_backlog(conn, settings.bot_name)
        if skipped:
            logger.info(
                "startup: skipped {} pre-existing live messages (won't trigger on backlog)",
                skipped,
            )
        bot_wxid = _resolve_bot_wxid(conn, settings.bot_name)
        bot_wxid_lock = threading.Lock()

        def get_bot_wxid() -> str | None:
            with bot_wxid_lock:
                return bot_wxid

        def set_bot_wxid(value: str | None) -> None:
            nonlocal bot_wxid
            with bot_wxid_lock:
                bot_wxid = value

        if bot_wxid:
            logger.info(
                "bot_wxid resolved: {} ({})",
                bot_wxid,
                "from WO_BOT_WXID" if settings.bot_wxid else "auto-discovered from messages",
            )
        else:
            logger.warning(
                "bot_wxid unknown — reply-to-bot trigger disabled until WeFlow SSE echoes a bot reply back. "
                "Set WO_BOT_WXID in .env to skip the discovery delay."
            )
        loops_since_wxid_retry = 0
        scheduler = _GlobalScheduler(
            replier=replier,
            log_path=log_path,
            llm_log_path=llm_log_path,
            bot_wxid_getter=get_bot_wxid,
            max_workers=worker_threads,
        )
        lurk_scheduler = (
            _LurkScheduler(
                log_path=log_path,
                llm_log_path=llm_log_path,
                bot_wxid_getter=get_bot_wxid,
            )
            if settings.agent_lurk_enabled else None
        )
        next_lurk_check = time.time() + max(1, settings.agent_lurk_interval_seconds)
        if lurk_scheduler is not None:
            logger.info(
                "lurk scheduler enabled: interval={}s min_new={} batch={}",
                settings.agent_lurk_interval_seconds,
                settings.agent_lurk_min_new_messages,
                settings.agent_lurk_recent_msgs,
            )
        try:
            while True:
                rows = _next_unprocessed(
                    conn,
                    settings.bot_name,
                    bot_wxid=get_bot_wxid(),
                )
                submitted = 0
                for row in rows:
                    if scheduler.submit(row):
                        submitted += 1
                due_jobs = due_followups(conn, limit=max(1, worker_threads))
                submitted_followups = 0
                for job in due_jobs:
                    if scheduler.submit_followup(job):
                        submitted_followups += 1
                if submitted_followups:
                    logger.info("continuation scheduler submitted {} job(s)", submitted_followups)
                # Lazy retry of bot_wxid discovery so reply-to-bot starts working
                # automatically once WeFlow SSE echoes the first bot reply back.
                # Cheap (one indexed query) but only if we don't have a value yet.
                if get_bot_wxid() is None:
                    loops_since_wxid_retry += 1
                    if loops_since_wxid_retry >= 5:
                        loops_since_wxid_retry = 0
                        resolved = _resolve_bot_wxid(conn, settings.bot_name)
                        if resolved:
                            set_bot_wxid(resolved)
                            logger.info(
                                "bot_wxid auto-discovered from echoed reply: {}",
                                resolved,
                            )
                if lurk_scheduler is not None and time.time() >= next_lurk_check:
                    next_lurk_check = time.time() + max(
                        1, settings.agent_lurk_interval_seconds
                    )
                    due_groups = lurk_due_groups(
                        conn,
                        min_new_messages=settings.agent_lurk_min_new_messages,
                        limit=max(1, worker_threads),
                    )
                    submitted_lurks = 0
                    for g in due_groups:
                        if lurk_scheduler.submit(g["group_id"], g["group_name"]):
                            submitted_lurks += 1
                    if submitted_lurks:
                        logger.info("lurk scheduler submitted {} group(s)", submitted_lurks)
                if (not rows or submitted == 0) and submitted_followups == 0:
                    time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("dispatcher stopped by user")
        finally:
            scheduler.close()
            if lurk_scheduler is not None:
                lurk_scheduler.close()
            replier.disconnect()
