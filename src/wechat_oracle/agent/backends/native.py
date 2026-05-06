"""Native backend: thin wrapper around `orchestrator.chat_via_agent`.

Behavior is identical to calling the orchestrator directly — this exists
solely so dispatcher call sites uniformly go through `AgentBackend.chat`,
which lets the openclaw backend slot in without touching dispatcher.

In native mode the in-process Phase A/B tool loop runs against the configured
WO_LLM_* provider; lurk also runs in-process via `orchestrator.chat_via_lurk`.
Switch to `WO_AGENT_BACKEND=openclaw` to delegate both chat and lurk to the
OpenClaw gateway (see F18 in CLAUDE.md / AGENTS.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...dispatcher import CommandContext


@dataclass
class NativeBackend:
    name: str = "native"

    def chat(
        self,
        *,
        ctx: "CommandContext",
        user_question: str,
        trigger_kind: str,
        reflection_enabled: bool | None = None,
    ) -> tuple[str | None, str]:
        from ..orchestrator import chat_via_agent
        return chat_via_agent(
            ctx=ctx,
            user_question=user_question,
            trigger_kind=trigger_kind,
            reflection_enabled=reflection_enabled,
        )
