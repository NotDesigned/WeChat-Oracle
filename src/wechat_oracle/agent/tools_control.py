"""Phase A control tools.

These tools don't read archive content or write long-term memory. They control
runtime behavior around the current agent turn, such as scheduling a delayed
continuation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..config import settings
from .continuation import clamp_delay, clamp_max_followups, plan_followup
from .tools import Tool, ToolSpec


_SCHEDULE_FOLLOWUP_SPEC = ToolSpec(
    name="schedule_followup",
    description=(
        "Schedule one delayed follow-up by intent. Use this only when your "
        "reply explicitly promises a later supplement, or when the current "
        "discussion may deserve one more continuation if the group keeps "
        "talking about the same topic. The system stores intent only and will "
        "rerun you later with fresh context; it does not send pre-written text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["committed", "thread"],
                "description": (
                    "committed = you explicitly promised to come back even if "
                    "no one speaks; thread = only continue if the group keeps "
                    "discussing the same topic."
                ),
            },
            "delay_seconds": {
                "type": "integer",
                "description": "Delay before the system reevaluates this follow-up. Defaults to WO_AGENT_CONTINUATION_DELAY_SECONDS.",
                "minimum": 5,
            },
            "intent": {
                "type": "string",
                "description": "What the future run should accomplish. Do not put final reply text here.",
            },
            "reason": {
                "type": "string",
                "description": "Why this follow-up is warranted.",
            },
            "max_followups": {
                "type": "integer",
                "description": "Maximum follow-up messages in this continuation thread, excluding the source reply. Defaults to WO_AGENT_CONTINUATION_MAX_FOLLOWUPS.",
                "minimum": 1,
            },
        },
        "required": ["kind", "intent", "reason"],
    },
)


@dataclass
class ScheduleFollowupTool(Tool):
    spec = _SCHEDULE_FOLLOWUP_SPEC
    conn: sqlite3.Connection
    group_id: str
    group_name: str | None
    continuation_token: str
    source_trigger_msg_id: int | None
    source_trigger_kind: str | None
    source_job_id: int | None
    current_sequence: int
    inherited_max_sequence: int | None

    def call(self, args: dict[str, Any]) -> str:
        max_sequence = clamp_max_followups(
            args.get("max_followups"),
            inherited=self.inherited_max_sequence,
        )
        if max_sequence <= 0:
            return "follow-up not scheduled: continuation max_followups is 0"
        return plan_followup(
            self.conn,
            group_id=self.group_id,
            group_name=self.group_name,
            continuation_token=self.continuation_token,
            kind=str(args.get("kind") or ""),
            delay_seconds=clamp_delay(args.get("delay_seconds")),
            intent=str(args.get("intent") or ""),
            reason=str(args.get("reason") or ""),
            source_trigger_msg_id=self.source_trigger_msg_id,
            source_trigger_kind=self.source_trigger_kind,
            source_job_id=self.source_job_id,
            current_sequence=self.current_sequence,
            max_sequence=max_sequence,
            anchor_msg_id=self.source_trigger_msg_id,
        )


def register_phase_a_control_tools(
    tools: "GroupScopedTools",  # noqa: F821
    *,
    continuation_token: str,
    source_trigger_msg_id: int | None,
    source_trigger_kind: str | None,
    source_job_id: int | None,
    current_sequence: int,
    inherited_max_sequence: int | None,
) -> None:
    if not settings.agent_continuation_enabled:
        return
    tools.register(ScheduleFollowupTool(
        conn=tools.conn,
        group_id=tools.group_id,
        group_name=tools.group_name,
        continuation_token=continuation_token,
        source_trigger_msg_id=source_trigger_msg_id,
        source_trigger_kind=source_trigger_kind,
        source_job_id=source_job_id,
        current_sequence=current_sequence,
        inherited_max_sequence=inherited_max_sequence,
    ))
