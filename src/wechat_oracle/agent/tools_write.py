"""Phase B (write-only) tool implementations + the read pair needed for
replace-on-write semantics.

Two writers, both replace-on-write per group:
  - update_persona_drift(drift_text)  — bot's own behavior supplement
  - update_group_memory(notes_text)   — single freeform doc covering members,
                                        events, and group culture; bounded
                                        by WO_AGENT_MEMORY_MAX_CHARS

Why one blob instead of per-member rows: per-id modeling adds structure the
agent doesn't actually need. A single freeform document the agent organizes
internally is cheaper to read/write and easier to audit by hand. The hard
size cap forces compaction once full — agent must summarize old material
before adding new.

Phase B does NOT get recall_group_history / vision / ASR — the trace already
digests what was learned in Phase A; reflection should be cheap.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..config import settings
from .memory import (
    get_group_memory,
    get_persona_drift,
    upsert_group_memory,
    upsert_persona_drift,
)
from .tools import Tool, ToolError, ToolSpec


# --- read pair (Phase B uses these to read-before-write) ------------------


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class MemoryWriteSession:
    """Per-agent-run snapshots used to prevent replace-on-write lost updates."""

    persona_hash: str | None = None
    group_memory_hash: str | None = None


_READ_PERSONA_DRIFT_SPEC = ToolSpec(
    name="read_persona_drift",
    description=(
        "Read this group's evolvable persona supplement (drift). The static "
        "persona core lives elsewhere; what you can edit is just this "
        "addendum. Call before `update_persona_drift` so you replace with a "
        "merged version, not blow away history."
    ),
    parameters={"type": "object", "properties": {}},
)


@dataclass
class ReadPersonaDriftTool(Tool):
    spec = _READ_PERSONA_DRIFT_SPEC
    conn: sqlite3.Connection
    group_id: str
    session: MemoryWriteSession

    def call(self, args: dict[str, Any]) -> str:
        text = get_persona_drift(self.conn, self.group_id)
        self.session.persona_hash = _text_hash(text)
        return text or "(no drift recorded yet)"


_READ_GROUP_MEMORY_FOR_WRITE_SPEC = ToolSpec(
    name="read_group_memory",
    description=(
        "Read this group's freeform memory document. Call before "
        "`update_group_memory` so you replace with a merged version rather "
        "than overwriting blindly."
    ),
    parameters={"type": "object", "properties": {}},
)


@dataclass
class ReadGroupMemoryForWriteTool(Tool):
    """Same as the Phase A read tool but lives here so Phase B's tool
    registry is self-contained. Same return semantics."""
    spec = _READ_GROUP_MEMORY_FOR_WRITE_SPEC
    conn: sqlite3.Connection
    group_id: str
    session: MemoryWriteSession

    def call(self, args: dict[str, Any]) -> str:
        text = get_group_memory(self.conn, self.group_id)
        self.session.group_memory_hash = _text_hash(text)
        if not text:
            return "(empty — nothing learned about this group yet)"
        return text


# --- update_group_memory ---------------------------------------------------


_UPDATE_GROUP_MEMORY_SPEC = ToolSpec(
    name="update_group_memory",
    description=(
        "Replace this group's memory document with a new freeform text. "
        "Read first via read_group_memory, merge with what you've just "
        "learned, and write back the full new version. Organize the "
        "document however you want internally — facts about members, group "
        "events, recurring topics, all in one. No need to use any specific "
        "format; you'll re-read your own structure later. Hard cap on size "
        "(see error message); when full, COMPACT older / less relevant "
        "material before adding new."
    ),
    parameters={
        "type": "object",
        "properties": {
            "notes_text": {
                "type": "string",
                "description": "Full new document text. Replaces what's there. Empty string = wipe (use sparingly).",
            },
        },
        "required": ["notes_text"],
    },
)


@dataclass
class UpdateGroupMemoryTool(Tool):
    spec = _UPDATE_GROUP_MEMORY_SPEC
    conn: sqlite3.Connection
    group_id: str
    session: MemoryWriteSession

    def call(self, args: dict[str, Any]) -> str:
        text = args.get("notes_text")
        if not isinstance(text, str):
            raise ToolError("notes_text must be a string")
        cap = settings.agent_memory_max_chars
        if len(text) > cap:
            raise ToolError(
                f"notes_text too long ({len(text)} chars; max {cap}). "
                "Compact older / less relevant material before adding new."
            )
        previous = get_group_memory(self.conn, self.group_id)
        if self.session.group_memory_hash is None:
            raise ToolError(
                "read_group_memory must be called immediately before update_group_memory"
            )
        if _text_hash(previous) != self.session.group_memory_hash:
            self.session.group_memory_hash = _text_hash(previous)
            raise ToolError(
                "group_memory changed since you read it. Call read_group_memory again, "
                "merge your update with the current text, then retry."
            )
        upsert_group_memory(self.conn, self.group_id, text)
        self.session.group_memory_hash = _text_hash(text)
        return (
            f"group_memory updated. prev_len={len(previous)} new_len={len(text)} "
            f"({len(text)*100//cap}% of cap)"
        )


# --- update_persona_drift --------------------------------------------------


_UPDATE_PERSONA_DRIFT_SPEC = ToolSpec(
    name="update_persona_drift",
    description=(
        "Replace this group's persona drift (the editable supplement to the "
        "static persona core). Read first with `read_persona_drift` and "
        "merge — overwriting wholesale loses prior calibration. Reserve "
        "this for genuine shifts in how you should behave in this group: "
        "e.g. group asked you to be terser, or you noticed a topic you "
        "should always volunteer to help with. Most agent runs do NOT "
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


_DRIFT_TEXT_MAX = 4000


@dataclass
class UpdatePersonaDriftTool(Tool):
    spec = _UPDATE_PERSONA_DRIFT_SPEC
    conn: sqlite3.Connection
    group_id: str
    session: MemoryWriteSession

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
        if self.session.persona_hash is None:
            raise ToolError(
                "read_persona_drift must be called immediately before update_persona_drift"
            )
        if _text_hash(previous) != self.session.persona_hash:
            self.session.persona_hash = _text_hash(previous)
            raise ToolError(
                "persona_drift changed since you read it. Call read_persona_drift again, "
                "merge your update with the current text, then retry."
            )
        upsert_persona_drift(self.conn, self.group_id, text)
        self.session.persona_hash = _text_hash(text)
        return (
            f"persona drift updated. prev_len={len(previous)} new_len={len(text)}"
        )


# --- factory ---------------------------------------------------------------


_PHASE_B_SYSTEM_PROMPT = (
    "反思阶段。检查刚才的 Phase A trace 和最终回复，决定是否要 update 记忆。\n\n"
    "可用工具：\n"
    " 读：read_persona_drift / read_group_memory\n"
    " 写：update_persona_drift / update_group_memory\n\n"
    "**写之前必须先读现状**——都是整段替换语义；不读就写等于丢历史。\n"
    "group_memory 有硬上限（write 时 ToolError 提示），接近上限时主动压缩旧的、不重要的内容。\n\n"
    "绝大多数情况下不写。出现以下三类情况才值得写：\n"
    " 1. 群友明确表达的偏好 / 事实（写 group_memory）\n"
    " 2. 群里的关键事件 / 共识 / 规则变化（写 group_memory）\n"
    " 3. 群明示或暗示要你调整说话风格 / 行为（写 persona_drift）\n\n"
    "不确定就不写。决定不写 → 直接输出空文本结束反思。"
)


def phase_b_system_prompt() -> str:
    """Reflection-phase system prompt (the static "what to do in Phase B"
    instructions). Persona module composes this with voice/identity from yaml."""
    return _PHASE_B_SYSTEM_PROMPT


def register_phase_b_tools(tools: "GroupScopedTools") -> None:  # noqa: F821 - structural-only ref
    """Register Phase B tools: two writers + the two readers needed for
    replace-on-write."""
    session = MemoryWriteSession()
    tools.register(ReadPersonaDriftTool(
        conn=tools.conn, group_id=tools.group_id, session=session,
    ))
    tools.register(ReadGroupMemoryForWriteTool(
        conn=tools.conn, group_id=tools.group_id, session=session,
    ))
    tools.register(UpdatePersonaDriftTool(
        conn=tools.conn, group_id=tools.group_id, session=session,
    ))
    tools.register(UpdateGroupMemoryTool(
        conn=tools.conn, group_id=tools.group_id, session=session,
    ))


# --- trace inspection (used by dispatcher to know which memory rows to link) ---


def trace_touched_tables(phase_b_trace: list[dict[str, Any]] | None) -> tuple[bool, bool]:
    """Walk the Phase B trace and return (touched_persona_drift, touched_group_memory).
    Used by dispatcher.chat_via_agent post-run to UPDATE last_run_id pointers
    on the right rows."""
    if not phase_b_trace:
        return (False, False)
    persona = memory = False
    for step in phase_b_trace:
        if step.get("kind") != "tool_call":
            continue
        name = step.get("tool")
        if name == "update_persona_drift":
            persona = True
        elif name == "update_group_memory":
            memory = True
    return (persona, memory)
