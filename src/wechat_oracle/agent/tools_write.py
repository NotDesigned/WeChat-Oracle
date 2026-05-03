"""Phase B (write-only) tool implementations + a small read subset for
the agent to consult current state before overwriting.

Why read tools live in the "write" phase: `write_member_note` and
`update_persona_drift` use replace-on-write semantics, so the agent has
to know the current value before producing a merged new one. The Phase A
trace might or might not have surfaced that state, so we let Phase B
re-read from the memory tables.

Phase B does NOT get `recall_group_history`, vision, ASR, etc. — the
trace already digests anything the agent learned in Phase A; reflection
should be cheap.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .memory import (
    get_member_notes,
    get_persona_drift,
    insert_group_note,
    list_group_notes,
    upsert_member_note,
    upsert_persona_drift,
)
from .tools import Tool, ToolError, ToolSpec, truncate_for_llm
from .tools_read import (
    ReadGroupNotesTool,
    ReadMemberNotesTool,
)


# --- read_persona_drift (Phase B read helper) ------------------------------


_READ_PERSONA_DRIFT_SPEC = ToolSpec(
    name="read_persona_drift",
    description=(
        "Read this group's evolvable persona supplement (drift). The static "
        "core persona lives elsewhere; what you can edit is just this "
        "addendum. Call this before `update_persona_drift` so you replace "
        "with a merged version, not blow away history."
    ),
    parameters={"type": "object", "properties": {}},
)


@dataclass
class ReadPersonaDriftTool(Tool):
    spec = _READ_PERSONA_DRIFT_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        text = get_persona_drift(self.conn, self.group_id)
        return text or "(no drift recorded yet)"


# --- write_member_note -----------------------------------------------------


_WRITE_MEMBER_NOTE_SPEC = ToolSpec(
    name="write_member_note",
    description=(
        "Replace this group's notes about a single member. Read the current "
        "notes via `read_member_notes` first, then write the merged version "
        "(don't drop existing observations unless they've been disproven). "
        "Use sparingly — only when something concrete and durable was "
        "learned this turn (a stable preference, an interest, a recurring "
        "behavior). Idle chat doesn't need a note."
    ),
    parameters={
        "type": "object",
        "properties": {
            "sender_wxid": {
                "type": "string",
                "description": "Target member's wxid (from a [N] context line).",
            },
            "notes_text": {
                "type": "string",
                "description": "Full new notes text. Will replace what's there. Keep it terse — bullet-style observations, not narrative.",
            },
        },
        "required": ["sender_wxid", "notes_text"],
    },
)


_NOTES_TEXT_MAX = 2000


@dataclass
class WriteMemberNoteTool(Tool):
    spec = _WRITE_MEMBER_NOTE_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        wxid = args.get("sender_wxid")
        text = args.get("notes_text")
        if not isinstance(wxid, str) or not wxid.strip():
            raise ToolError("sender_wxid must be a non-empty string")
        if not isinstance(text, str):
            raise ToolError("notes_text must be a string")
        text = text.strip()
        if len(text) > _NOTES_TEXT_MAX:
            raise ToolError(
                f"notes_text too long ({len(text)} chars; max {_NOTES_TEXT_MAX}). "
                "Compact / merge before writing."
            )
        previous = get_member_notes(self.conn, self.group_id, wxid.strip())
        upsert_member_note(self.conn, self.group_id, wxid.strip(), text)
        return (
            f"member note updated for {wxid}. "
            f"prev_len={len(previous)} new_len={len(text)}"
        )


# --- write_group_note ------------------------------------------------------


_WRITE_GROUP_NOTE_SPEC = ToolSpec(
    name="write_group_note",
    description=(
        "Append a group-level note. Use for events, decisions, or shared "
        "context that affects the group as a whole — not for individual "
        "member observations (those go in member_notes). Append-only: each "
        "call adds a new row; the history shows how the group evolves. "
        "Also use sparingly."
    ),
    parameters={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Short tag to group-by ('events', 'decisions', 'tooling', etc.). Reuse existing topics when possible — call read_group_notes first if unsure.",
            },
            "notes_text": {
                "type": "string",
                "description": "The actual note. One coherent observation per call; don't batch unrelated facts.",
            },
        },
        "required": ["topic", "notes_text"],
    },
)


@dataclass
class WriteGroupNoteTool(Tool):
    spec = _WRITE_GROUP_NOTE_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        topic = args.get("topic")
        text = args.get("notes_text")
        if not isinstance(topic, str) or not topic.strip():
            raise ToolError("topic must be a non-empty string")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("notes_text must be non-empty")
        text = text.strip()
        if len(text) > _NOTES_TEXT_MAX:
            raise ToolError(
                f"notes_text too long ({len(text)} chars; max {_NOTES_TEXT_MAX})"
            )
        note_id = insert_group_note(
            self.conn, self.group_id,
            topic=topic.strip(), notes_text=text,
        )
        return f"group note appended (id={note_id}, topic={topic.strip()!r}, len={len(text)})"


# --- update_persona_drift --------------------------------------------------


_UPDATE_PERSONA_DRIFT_SPEC = ToolSpec(
    name="update_persona_drift",
    description=(
        "Replace this group's persona drift (the editable supplement to the "
        "static persona core). Read first with `read_persona_drift` and "
        "merge — overwriting wholesale loses prior calibration. Reserve "
        "this for genuine shifts in how you should behave in this group: "
        "e.g. the group asked you to be terser, or you noticed a topic "
        "you should always volunteer to help with. Most agent runs do NOT "
        "warrant a drift update."
    ),
    parameters={
        "type": "object",
        "properties": {
            "drift_text": {
                "type": "string",
                "description": "Full new drift text. Replaces the existing entry. Keep concise — terse rules, not narrative.",
            },
        },
        "required": ["drift_text"],
    },
)


_DRIFT_TEXT_MAX = 2000


@dataclass
class UpdatePersonaDriftTool(Tool):
    spec = _UPDATE_PERSONA_DRIFT_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        text = args.get("drift_text")
        if not isinstance(text, str):
            raise ToolError("drift_text must be a string")
        text = text.strip()
        if len(text) > _DRIFT_TEXT_MAX:
            raise ToolError(
                f"drift_text too long ({len(text)} chars; max {_DRIFT_TEXT_MAX})"
            )
        previous = get_persona_drift(self.conn, self.group_id)
        upsert_persona_drift(self.conn, self.group_id, text)
        return (
            f"persona drift updated for group_id={self.group_id}. "
            f"prev_len={len(previous)} new_len={len(text)}"
        )


# --- factory ---------------------------------------------------------------


_PHASE_B_SYSTEM_PROMPT = (
    "你刚处理完一次群聊互动，现在进入反思阶段。"
    " 检查刚才的 Phase A trace 和你给出的最终回复，"
    "判断是否需要把任何观察写进笔记。\n\n"
    "可用工具（读 + 写）：\n"
    " 读：read_member_notes / read_group_notes / read_persona_drift\n"
    " 写：write_member_note / write_group_note / update_persona_drift\n\n"
    "**写笔记前必须先读现状**——member_note 和 persona_drift 是替换语义，"
    "不读就写等于丢失历史。group_note 是追加语义，不读也行但要避免重复。\n\n"
    "什么时候不写（绝大多数情况都不写）：\n"
    "- 这次只是普通寒暄 / 解答 / 转述，没新事实\n"
    "- stay_silent 的运行——通常没什么可记的\n"
    "- 笔记内容只是已有信息的复述\n"
    "- 你不确定该不该写——那就不写\n\n"
    "什么时候值得写：\n"
    "- member：成员明确表达的偏好、领域、行为模式\n"
    "- group：群里达成的共识、关键事件、规则变化\n"
    "- drift：群明示或暗示要你调整说话风格 / 行为\n\n"
    "决定不写就直接输出空文本结束反思。"
)


def phase_b_system_prompt() -> str:
    """The reflection-phase system prompt. Exposed as a function so commit
    6's persona yaml loader can decorate it with group-specific drift."""
    return _PHASE_B_SYSTEM_PROMPT


def register_phase_b_tools(tools: "GroupScopedTools") -> None:  # noqa: F821 - structural-only ref
    """Register Phase B tools into the registry: three writers + the read
    subset needed for replace-on-write semantics. Caller wires this from
    dispatcher integration."""
    tools.register(ReadMemberNotesTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ReadGroupNotesTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ReadPersonaDriftTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(WriteMemberNoteTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(WriteGroupNoteTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(UpdatePersonaDriftTool(conn=tools.conn, group_id=tools.group_id))
