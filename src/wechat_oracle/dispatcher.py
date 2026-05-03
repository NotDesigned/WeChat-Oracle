"""Command dispatcher.

Watches the messages table for inbound commands and dispatches each to a
`Command` subclass. The current command set is in `COMMANDS`:

    /find @<target> [since:YYYY[-MM[-DD]]] <description>
    /sum [from:<target>|@<target>] [since:YYYY[-MM[-DD]]] [limit:N] [topic]
    /recent [N]
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
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from loguru import logger

from .config import settings
from .db import get_conn, init_db, transaction
from .llm import LLMClient, build_llm_client
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
    description = f"兜底：直接 @ 机器人 + 提问，把最近 {settings.dispatcher_context_chat} 条群消息当上下文，由 LLM 自由回答"
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
        # Chat context: include EVERY message type (link cards, files, video
        # shares, transfers, system events, image/voice placeholders, etc.)
        # so the LLM sees the full timeline. `for_chat=True` widens the SQL
        # filter and substitutes type-based placeholders for NULL content_text.
        # See CLAUDE.md F7.
        context = fetch_candidates(
            ctx.conn,
            group_id=ctx.group_id,
            target=None,
            since_t=None,
            limit=ctx.candidate_limit_chat,
            bot_name=ctx.bot_name,
            for_chat=True,
        )
        # When the user 引用ed a message and addressed the bot, inline the
        # quoted snippet so the LLM treats it as the topic of the question
        # without having to fish it out of the context window.
        message = self.message
        if ctx.quoted_text:
            message = f"[用户引用了一条消息：{ctx.quoted_text}]\n{message}"
        reply = chat_assistant(
            ctx.llm, ctx.model, message, context,
            log_path=ctx.llm_log_path,
            requester=ctx.requester,
        )
        if not reply:
            reply = "（模型没返回内容，再问一次试试）"
        logger.info(
            "chat :: {!r}  context={}  reply_len={}",
            self.message[:60], len(context), len(reply),
        )
        stdout = (
            f"@<bot> chat  ::  {self.message}\n"
            f"  ({len(context)} ctx msgs -> {len(reply)} chars)\n"
            f"{reply}"
        )
        return ExecResult(stdout=stdout, chat=reply, summary=f"chat ({len(context)} ctx)")


# ---------- /ask ----------

_ASK_SYSTEM_PROMPT = """你是微信群里的轻量问答助手。用户显式使用 /ask，表示这次不要读取群聊历史，只按通用知识和用户问题本身回答。

回答要求：
- 直接回答，不要声称看过群聊上下文
- 信息不足时说明缺少什么，不要编造
- 中文回答，除非问题明显要求其它语言
- 控制在 2-6 句
- 不要 @ 任何人；不要使用 markdown 语法"""


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
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        requester_line = f"提问者：{ctx.requester}\n" if ctx.requester else ""
        user = f"当前时间：{now_str}\n{requester_line}用户问题：{self.question}"
        reply = ctx.llm.complete_text(
            model=ctx.model,
            system=_ASK_SYSTEM_PROMPT,
            user=user,
            temperature=0.3,
            max_tokens=settings.short_max_tokens,
        )
        if not reply:
            reply = "（模型没返回内容，再问一次试试）"
        if ctx.llm_log_path:
            _dump_llm_call(
                ctx.llm_log_path,
                label=f"/ask  ::  {self.question}",
                system=_ASK_SYSTEM_PROMPT,
                user=user,
                raw=reply,
                parsed=None,
            )
        logger.info("ask :: {!r}  reply_len={}", self.question[:60], len(reply))
        stdout = f"/ask  ::  {self.question}\n  ({len(reply)} chars)\n{reply}"
        return ExecResult(stdout=stdout, chat=reply, summary=f"ask ({len(reply)} chars)")


# ---------- /sum ----------

_SUM_SYSTEM_PROMPT = """你是微信群聊摘要助手。根据用户给出的当前群候选消息，提炼讨论重点。

要求：
- 只总结候选消息里明确出现的信息，不要编造
- 如果用户给了主题，只总结与主题相关的内容
- 按“结论 / 分歧 / 待办或决定”组织，但没有的部分不要硬写
- 中文回答，控制在 4-10 句
- 不要 @ 任何人；不要使用 markdown 语法"""


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
            reply = "（模型没返回内容，再问一次试试）"
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


# ---------- /explain ----------

_EXPLAIN_SYSTEM_PROMPT = """你是微信群里的简明解释助手。用户显式使用 /explain，通常是在引用一条消息后要求解释。

要求：
- 只解释提供的文本或引用内容，不读取群聊历史
- 说明这句话可能是什么意思、关键信息是什么、必要时指出不确定点
- 信息不足时直接说缺少上下文
- 中文回答，控制在 2-6 句
- 不要 @ 任何人；不要使用 markdown 语法"""


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
        if quoted:
            source = f"引用内容：{quoted}"
            if explicit:
                source += f"\n用户补充：{explicit}"
        elif explicit:
            source = f"待解释文本：{explicit}"
        else:
            text = "请引用一条消息后发送 `/explain`，或者写成 `/explain <待解释文本>`。"
            return ExecResult(stdout=text, chat=text, summary="explain: missing input")

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        user = f"当前时间：{now_str}\n{source}"
        reply = ctx.llm.complete_text(
            model=ctx.model,
            system=_EXPLAIN_SYSTEM_PROMPT,
            user=user,
            temperature=0.2,
            max_tokens=settings.short_max_tokens,
        )
        if not reply:
            reply = "（模型没返回内容，再问一次试试）"
        if ctx.llm_log_path:
            _dump_llm_call(
                ctx.llm_log_path,
                label=f"/explain  ::  {explicit or quoted[:60]}",
                system=_EXPLAIN_SYSTEM_PROMPT,
                user=user,
                raw=reply,
                parsed=None,
            )
        logger.info("explain :: quoted={} explicit_len={} reply_len={}",
                    bool(quoted), len(explicit), len(reply))
        stdout = f"/explain\n{source}\n\n{reply}"
        return ExecResult(stdout=stdout, chat=reply, summary=f"explain ({len(reply)} chars)")


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
        f"不带 /：直接问，会带最近 {settings.dispatcher_context_chat} 条当前群上下文。"
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
    """
    if not content_text or not bot_name:
        return None
    # Match `@<bot>` followed by whitespace and any body. The `\s+` requires
    # at least one whitespace separator so substring `@<bot>x` (without space)
    # is not treated as a ping.
    pattern = rf"@{re.escape(bot_name)}\s+(.+?)$"
    m = re.search(pattern, content_text, re.DOTALL)
    if not m:
        return None
    body = m.group(1).strip()
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

    All message types pass through. NULL content_text for media (image / voice
    / video / sticker) gets replaced by a typed placeholder via SQL CASE so
    every message at least appears as an event in the timeline. Link cards /
    files / video shares / transfers / red-packets carry their formatted
    preview (built by `format_appmsg_content` at ingest time). The LLM is
    trusted to recognise placeholders like `[图片]` as opaque events vs
    user-typed text. (See CLAUDE.md F7.)

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

    Quote-reply rendering: we unify the LLM-visible shape so live and backfill
    look identical. backfill's content_text is already
    `"<reply>[引用 <orig>:<quoted>]"` (WeFlow file-export pre-parsed). For
    live, content_text is just `"<reply>"` and `quote_text` is separate; the
    CASE below appends the `[引用 ...]` suffix and resolves the original
    sender via LEFT JOIN on `wx_msg_id (== refermsg.svrid)`.
    """
    # SQL note: SQLite WHERE can't reference SELECT aliases, so the `content`
    # NULL/empty filter happens at the outer UNION level (see `sql` below).
    main_sql = """
        SELECT 'm:' || m.msg_id AS cand_id, m.t,
               COALESCE(m.sender_display, m.sender_wxid, '?') AS sender,
               NULL AS parent_id,
               CASE
                   WHEN m.type = 'quote'
                        AND m.quote_text IS NOT NULL AND m.quote_text <> ''
                        AND COALESCE(m.content_text, '') NOT LIKE '%[引用%'
                   THEN COALESCE(m.content_text, '')
                        || '[引用 '
                        || COALESCE(orig.sender_display, orig.sender_wxid, '?')
                        || '：' || m.quote_text || ']'
                   -- OCR/ASR transcript wins for media when worker has filled it.
                   -- The `·OCR`/`·ASR` suffix tells the LLM "this text was machine-
                   -- transcribed, not user-typed" — distinct from the bare `[图片]`
                   -- placeholder which means we don't have content. (See CLAUDE.md F15.)
                   WHEN m.transcript IS NOT NULL AND m.transcript <> ''
                   THEN CASE m.type
                            WHEN 'image'   THEN '[图片·OCR] '
                            WHEN 'voice'   THEN '[语音·ASR] '
                            WHEN 'video'   THEN '[视频·识别] '
                            WHEN 'sticker' THEN '[表情·OCR] '
                            ELSE '[' || m.type || '·识别] '
                        END || m.transcript
                   WHEN m.content_text IS NOT NULL AND m.content_text <> ''
                   THEN m.content_text
                   ELSE CASE m.type
                       WHEN 'image'   THEN '[图片]'
                       WHEN 'voice'   THEN '[语音]'
                       WHEN 'video'   THEN '[视频]'
                       WHEN 'sticker' THEN '[表情]'
                       ELSE NULL
                   END
               END AS content
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
               f.content
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

    # Outer SELECT lets us filter NULL/empty content (the inner CASE returns
    # NULL for unknown types whose content_text is also empty — these would be
    # useless to the LLM).
    sql = f"""
        SELECT cand_id, t, sender, parent_id, content FROM (
            {main_sql}
            UNION ALL
            {fwd_sql}
        ) WHERE content IS NOT NULL AND content <> ''
        ORDER BY t DESC LIMIT ?
    """
    params = main_params + fwd_params + [limit]
    rows = conn.execute(sql, params).fetchall()
    rows.reverse()  # chronological for the LLM
    return [
        Candidate(
            cand_id=r["cand_id"], t=r["t"], sender=r["sender"],
            content=r["content"], parent_id=r["parent_id"],
        )
        for r in rows
    ]


# ---------- LLM filter ----------

_SYSTEM_PROMPT = """你是聊天记录精筛助手。根据「查询描述」从「候选消息」里挑出相关条目。

候选行格式约定：
- 普通文字：正文就是该用户打出来的字
- `[图片·OCR] 文字内容` / `[语音·ASR] 文字内容` —— 中点后是机器识别出来的内容，**视同该 sender 通过图片/语音表达**，要参与匹配
- 仅 `[图片]` / `[语音]`（没有 ·OCR / ·ASR 后缀） —— 该消息还没识别或无文字可识别，按"事件"算，匹配不到具体内容
- `...[引用 X：Y]` —— Y 是被引用消息的内容，连同前面的回复一起匹配
- 合并转发：父行 `[聊天记录]` 后会跟一组 `↳ [f:N] (原时间) 原作者:正文` 缩进子项，子项内容也参与匹配（视同原作者在原时间说了那些话）
- `[链接] 标题\\nURL` / `[卡片消息]` 等 —— 按字面意义理解

排除规则（先判断，命中即跳过该候选）：
- 命令消息：形如 `@<某机器人> /xxx ...` 这种向机器人发指令的消息，属于操作指令而非被查询者的发言，一律忽略
- 机器人自己发出的回复（包括格式化结果、错误提示）也忽略

匹配原则（宁宽勿窄）：
1. 任何字面/关键词上提到查询所述事物（人名、专有名词、概念、话题）的消息，必须算作相关
2. 表达了与查询主旨相关的想法/态度/讨论的消息，按语义相关度纳入
3. 只有所有候选都与查询毫无关联时，才返回空 hits

返回 JSON（只输出 JSON，不要前后文）：
{
  "hits": ["<msg_id>", ...],       // 按相关度从高到低，最多 5 条；msg_id 是字符串，照抄候选行 [] 里的 token
  "keywords": ["<核心检索词>", ...], // 从查询里提取的 1-3 个核心实体/概念，用作 fallback 关键词
  "reason": "<一句话说明>"
}

严禁编造 ID，只能从候选中挑。注意 msg_id 形如 "m:123" 或 "f:456"，必须原样保留前缀。"""


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


_LLM_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB rotate threshold


def _maybe_rotate(path: Path, max_bytes: int) -> None:
    """Single-backup rotation: when file ≥ max_bytes, rename to <path>.1
    (overwriting any prior .1) and let the next write start a fresh file.
    """
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    backup = path.with_suffix(path.suffix + ".1")
    if backup.exists():
        backup.unlink()
    path.rename(backup)


def _dump_llm_call(
    log_path: Path,
    label: str,
    system: str,
    user: str,
    raw: str,
    parsed: object,
    note: str = "",
) -> None:
    """Append one LLM round-trip to the debug log. No-op if log_path is None."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _maybe_rotate(log_path, _LLM_LOG_MAX_BYTES)
    ts = datetime.now().isoformat(timespec="seconds")
    sep = "=" * 70
    parts = [
        f"\n{sep}",
        f"{ts}  {label}",
        sep,
        "--- SYSTEM ---",
        system,
        "--- USER ---",
        user,
        "--- RAW RESPONSE ---",
        raw,
    ]
    if parsed is not None:
        parts.append("--- PARSED ---")
        parts.append(json.dumps(parsed, ensure_ascii=False, indent=2))
    if note:
        parts.append(f"--- NOTE ---\n{note}")
    parts.append("")  # trailing newline
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(parts))


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
        system=_SYSTEM_PROMPT,
        user=user,
        temperature=0.0,
    )
    payload: object | None = None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("LLM returned non-JSON: {!r}", raw[:200])
        if log_path:
            _dump_llm_call(log_path, f"{label}  ::  {description}",
                           _SYSTEM_PROMPT, user, raw, None, note=f"JSONDecodeError: {e}")
        return LLMFilterResult(hits=[], keywords=[], reason=f"bad LLM response: {e}")

    if log_path:
        _dump_llm_call(log_path, f"{label}  ::  {description}",
                       _SYSTEM_PROMPT, user, raw, payload)

    valid_ids = {c.cand_id for c in cands}
    raw_hits = (payload.get("hits") if isinstance(payload, dict) else []) or []
    hits = [str(x) for x in raw_hits if str(x) in valid_ids]
    raw_keywords = (payload.get("keywords") if isinstance(payload, dict) else []) or []
    keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
    reason_text = str((payload.get("reason") if isinstance(payload, dict) else "") or "")
    return LLMFilterResult(hits=hits, keywords=keywords, reason=reason_text)


_CHAT_SYSTEM_PROMPT = """你是这个微信群里的小助手。用户 @ 了你并问了问题/提了话题，你需要结合下面提供的「最近群聊上下文」来作答。

上下文行的格式约定（必须读懂，否则会忽略真实内容）：
- 普通文字：`[id] (时间) 用户:正文`，正文就是该用户打出来的字
- 引用回复：`...回复正文[引用 原作者：原内容]`，方括号是被引用消息的 sender + 正文
- **图片识别**：`[图片·OCR] <文字>` —— 中点后的内容是机器从这张图片里识别出来的字（可能是文章截图、表情包字幕、表格、聊天记录截图等）。**视同该 sender 用图片表达**，可以转述、复读、引用。
- **语音识别**：`[语音·ASR] <文字>` —— 同上，机器从语音里转出来的字。可能不准、可能很短，但**就是用户当时说的话**，应直接转述。
- 仅 `[图片]` / `[语音]`（不带 ·OCR / ·ASR 后缀）—— 该消息还没识别（图里没字 / 语音太短失败），按"事件"看，不要假设内容。
- 系统消息：`[id] (时间) ?:对方撤回了一条消息`、入群退群提示等，可作时间线感知；通常不直接回应。
- **合并转发的聊天记录**：父行是 `[id] (转发时间) 发起人:[聊天记录]`，紧接着会跟若干 `↳ [f:N] (原时间) 原作者:正文` 缩进子项——**这些缩进行就是被打包转发进来的聊天内容**，原作者/原时间是它们在源群里发出时的信息；要回答"这个聊天记录里说了什么"就直接读这些子项。子项的"原时间"可能比父行早数小时甚至数年（源消息很老）。
- 其他来源标签：`[链接] 标题\\nURL`、`[卡片消息]`、`[转账]` 等，按字面意义理解。

回答要求：
- **直接答**，不要"根据上下文…"、"我看到群里…"这类废话开头
- 上下文不足或没相关信息时如实说"群里没出现过相关讨论"，不要编
- **控制 2–6 句**；可转述/概括、可复读 OCR/ASR 出来的文字（这不算"复制聊天原文"——那段字本就是用户通过图片/语音表达的，需要时一字一句念出来也没问题），但**不要**贴大段普通发言原文
- 中文回答（除非问题明显是英文）
- 不要 @ 任何人；不要用 markdown 语法（不要 `**` `#` `-` 这些）
- 如果问题本身就跟群无关（"今天天气怎么样"），直接答即可，不要硬扯群聊
- 提到"刚才/刚刚/最近"时，参考下方提供的"当前时间"做时间锚定；用户说"我"/"我刚才说了什么"等指代自己时，参考"提问者"字段定位他在群里的发言"""


def chat_assistant(
    client: LLMClient,
    model: str,
    question: str,
    context: list[Candidate],
    log_path: Path | None = None,
    requester: str | None = None,
) -> str:
    """Free-form group-chat-assistant call. Returns plain text reply.

    `requester` (the sender's display name) gets a dedicated header line so the
    LLM can resolve "我" / "我刚才说了啥" against the right rows in `context`.
    """
    if context:
        ctx_text = _format_candidates_for_llm(context)
    else:
        ctx_text = "（无群聊上下文）"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    requester_line = (
        f"提问者：{requester}（即上下文里 sender == {requester!r} 的那位是「我」）\n"
        if requester else ""
    )
    user = (
        f"当前时间：{now_str}\n"
        f"{requester_line}"
        f"用户问题：{question}\n\n"
        f"最近群聊（按时间正序，最旧 → 最新）：\n{ctx_text}"
    )
    raw = client.complete_text(
        model=model,
        system=_CHAT_SYSTEM_PROMPT,
        user=user,
        temperature=0.3,
        max_tokens=settings.chat_max_tokens,
    )
    if log_path:
        _dump_llm_call(
            log_path,
            label=f"chat  ::  {question}",
            system=_CHAT_SYSTEM_PROMPT,
            user=user,
            raw=raw,
            parsed=None,
        )
    return raw


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
        system=_SUM_SYSTEM_PROMPT,
        user=user,
        temperature=0.2,
        max_tokens=settings.sum_max_tokens,
    )
    if log_path:
        _dump_llm_call(
            log_path,
            label=f"/sum  ::  {topic or '(all)'}",
            system=_SUM_SYSTEM_PROMPT,
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


def _next_unprocessed(
    conn: sqlite3.Connection, bot_name: str, batch: int = 20
) -> list[sqlite3.Row]:
    """Live text-or-quote rows that ping the bot (any `@<bot>...`, slash command
    or free-form question) and have no command_runs row yet. Final dispatch is
    in Python (parse_command).

    Quote rows are included because a user can right-click → 引用 a previous
    message and add `@<bot> ...` to it. The reply text (which is what addresses
    the bot) lives in `content_text` exactly the same as for plain text. See
    CLAUDE.md F4 / F7.
    """
    # Exclude messages the bot itself sent — otherwise replies that quote /
    # echo earlier `@<bot>` chatter (e.g. /recent listings) get LIKE-matched
    # and processed as new commands, looping the bot back into its own input.
    needle = f"%@{bot_name}%"
    return conn.execute(
        """
        SELECT m.msg_id, m.group_id, m.group_name, m.t, m.content_text,
               m.sender_display, m.sender_wxid,
               m.quote_text, m.reply_to_wx_msg_id
          FROM messages m
     LEFT JOIN command_runs r ON r.msg_id = m.msg_id
         WHERE m.source = 'live'
           AND m.type IN ('text', 'quote')
           AND m.content_text LIKE ?
           AND r.msg_id IS NULL
           AND (m.sender_display IS NULL OR m.sender_display != ?)
         ORDER BY m.t ASC
         LIMIT ?
        """,
        (needle, bot_name, batch),
    ).fetchall()


def _build_llm_client() -> LLMClient:
    return build_llm_client(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        endpoint=settings.llm_endpoint,
        json_mode=settings.llm_json_mode,
    )


def _append_log(log_path: Path, command_t: int, block: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    when = datetime.fromtimestamp(command_t).strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n=== {when} ===\n{block}\n")


def _process(
    conn: sqlite3.Connection,
    llm: LLMClient,
    replier: "Replier",
    row: sqlite3.Row,
    log_path: Path,
    llm_log_path: Path | None,
) -> None:
    msg_id = row["msg_id"]
    parsed = parse_command(row["content_text"], settings.bot_name)

    if parsed is None:
        # LIKE matched but the message isn't a `@<bot> /<word>` attempt.
        _finalize(conn, msg_id, "ok", "(not a command)")
        return

    requester = row["sender_display"] or row["sender_wxid"]

    if isinstance(parsed, ParseError):
        text = parsed.chat()
        print(text, flush=True)
        _append_log(log_path, row["t"], text)
        replier.send(row["group_name"], requester, text)
        _finalize(conn, msg_id, "ok", f"parse-error: {parsed.reason}")
        return

    # Row may carry quote-reply info; pass through so ChatCommand can inline
    # what the user 引用ed into the LLM prompt. Other commands ignore it.
    quoted_text = None
    quoted_msg_id = None
    try:
        quoted_text = row["quote_text"]
        quoted_msg_id = row["reply_to_wx_msg_id"]
    except (KeyError, IndexError):
        pass  # older code path / non-quote source — fields absent

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
    )
    try:
        result = parsed.execute(ctx)
    except Exception as e:
        logger.exception("execute() crashed for /{}", parsed.name)
        msg = f"⚠️ /{parsed.name} 执行失败：{e}"
        print(msg, flush=True)
        _append_log(log_path, row["t"], msg)
        replier.send(row["group_name"], requester, msg)
        _finalize(conn, msg_id, "error", str(e))
        return

    print(result.stdout, flush=True)
    _append_log(log_path, row["t"], result.stdout)
    replier.send(row["group_name"], requester, result.chat)
    _finalize(conn, msg_id, "ok", result.summary)


def _skip_backlog(conn: sqlite3.Connection, bot_name: str) -> int:
    """Mark every existing un-claimed `@<bot>` message as already-processed
    (status='ok', result='startup-skip'). Run once at dispatcher startup so a
    cold start doesn't flood the group with replies to historical questions —
    backlogs may arise from a long downtime, a fresh DB import, or restarting
    after a one-shot historical re-pull. Future messages stream through
    `_next_unprocessed` normally.
    """
    now = int(time.time())
    needle = f"%@{bot_name}%"
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO command_runs (msg_id, started_at, finished_at, status, result)
            SELECT m.msg_id, ?, ?, 'ok', '(startup-skip)'
              FROM messages m
         LEFT JOIN command_runs r ON r.msg_id = m.msg_id
             WHERE m.source = 'live'
               AND m.type IN ('text', 'quote')
               AND m.content_text LIKE ?
               AND r.msg_id IS NULL
               AND (m.sender_display IS NULL OR m.sender_display != ?)
            """,
            (now, now, needle, bot_name),
        )
    return cur.rowcount or 0


def run_dispatcher() -> None:
    if not settings.bot_name:
        raise RuntimeError(
            "WO_BOT_NAME is empty; set it to your alt-account's group nickname in .env"
        )

    init_db()
    settings.ensure_dirs()
    log_path = settings.data_dir / "dispatcher.log"
    llm_log_path = settings.data_dir / "llm_debug.log"
    llm = _build_llm_client()
    replier = build_replier()
    interval = settings.dispatcher_poll_interval

    logger.info(
        "dispatcher: bot={!r} model={} interval={}s replier={} commands={} log={} llm_log={}",
        settings.bot_name, settings.llm_model, interval,
        type(replier).__name__, list(COMMANDS), log_path, llm_log_path,
    )

    with get_conn() as conn:
        skipped = _skip_backlog(conn, settings.bot_name)
        if skipped:
            logger.info("startup: skipped {} pre-existing @-mentions (won't reply to backlog)", skipped)
        try:
            while True:
                rows = _next_unprocessed(conn, settings.bot_name)
                for row in rows:
                    if not _claim(conn, row["msg_id"]):
                        continue
                    try:
                        _process(conn, llm, replier, row, log_path, llm_log_path)
                    except Exception as e:
                        logger.exception("dispatcher crashed on msg_id={}", row["msg_id"])
                        _finalize(conn, row["msg_id"], "error", f"crashed: {e}")
                if not rows:
                    time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("dispatcher stopped by user")
        finally:
            replier.disconnect()
