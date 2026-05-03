"""Tool ABC + GroupScopedTools registry for the agent loop.

Design contract (see CLAUDE.md F17 once added):

- Tools are constructor-bound to a `(group_id, conn)` pair via
  `GroupScopedTools`. The LLM never sees `group_id` as a parameter — it is
  baked in at registration time so the model literally cannot read or write
  rows from another group. This is the privacy fence.
- Each `Tool` subclass declares a `spec: ToolSpec` (name + description +
  JSON-schema parameters) and implements `call(args: dict) -> str`. Return
  the result as plain text or JSON string; the runtime forwards it verbatim
  to the model in the next `tool` role turn.
- Errors that the model can recover from (bad msg_id, malformed args,
  missing row) → raise `ToolError(msg)`. The runtime captures it and feeds
  the message back to the model as the tool result. Internal bugs → let
  the exception propagate; the runtime traps and writes it to the trace.

Tools split into two phases (Phase A read-only, Phase B write-only); see
`tools_read.py` and `tools_write.py`. This module only defines the base
plumbing.
"""

from __future__ import annotations

import abc
import sqlite3
from dataclasses import dataclass, field
from typing import Any, ClassVar


class ToolError(Exception):
    """Raised by tool implementations to surface a recoverable error to the
    LLM. The runtime catches it, formats `str(exc)` as the tool result, and
    lets the model retry with corrected arguments."""


@dataclass(frozen=True)
class ToolSpec:
    """OpenAI-compatible tool descriptor. `parameters` is a JSON schema dict
    (the model uses it to pick argument shapes) — top-level
    `{"type": "object", "properties": {...}, "required": [...]}`."""
    name: str
    description: str
    parameters: dict[str, Any]


class Tool(abc.ABC):
    """One agent-callable function. Subclasses set `spec` and implement
    `call`. Bind any group-scoped state via `__init__` from
    `GroupScopedTools`; do NOT accept `group_id` as a tool argument."""

    spec: ClassVar[ToolSpec]

    @abc.abstractmethod
    def call(self, args: dict[str, Any]) -> str:
        """Execute the tool. `args` is parsed JSON the model emitted (already
        a dict, not a string). Return the result text the model will see."""


@dataclass
class GroupScopedTools:
    """Registry of tools all bound to the same group + DB connection.

    `conn` is the SQLite connection the tools read/write through (same one
    the dispatcher already owns; agent runs are synchronous). `group_id` is
    enforced INSIDE every tool's SQL — never as an LLM-visible parameter.
    """

    conn: sqlite3.Connection
    group_id: str
    group_name: str | None = None
    bot_name: str = ""
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_specs(self) -> list[dict[str, Any]]:
        """Render the registered tools into OpenAI's tool-calling spec list."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.spec.name,
                    "description": t.spec.description,
                    "parameters": t.spec.parameters,
                },
            }
            for t in self._tools.values()
        ]


# --- shared helpers --------------------------------------------------------


def truncate_for_llm(text: str, *, limit: int = 1500) -> str:
    """Tool results that flow back into the model context should be capped so
    one runaway query doesn't blow the agent's window. Append a marker so the
    model knows it's truncated and can refine."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated, original {len(text)} chars]"


# --- terminator tool (Phase A) ---------------------------------------------


_STAY_SILENT_SPEC = ToolSpec(
    name="stay_silent",
    description=(
        "Decide not to reply this turn. Call this when the trigger doesn't "
        "warrant a response — e.g. the user wasn't really asking you, or "
        "anything you'd say would be noise. The bot will stay quiet."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short note for the audit log explaining why staying silent is correct.",
            },
        },
        "required": ["reason"],
    },
)


class StaySilentTool(Tool):
    """Call this to terminate Phase A with no reply. The runtime detects the
    tool name and stops the loop; this `call` body is the audit-trace stub."""

    spec = _STAY_SILENT_SPEC

    def call(self, args: dict[str, Any]) -> str:
        reason = args.get("reason") or ""
        return f"acknowledged: stay_silent ({reason})"
