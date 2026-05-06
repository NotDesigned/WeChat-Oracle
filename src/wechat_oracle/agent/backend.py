"""Agent backend abstraction: native (in-process Phase A/B + tools) vs
openclaw (delegates the entire agent loop to a local OpenClaw gateway running
a wechat-bot agent).

Why two backends: OpenClaw lets the bot use a Claude subscription (Pro/Max)
instead of paying per-token API rates — for a chatty group bot this is a
significant cost difference, and the real reason openclaw is the recommended
backend. Native is kept as fallback / A-B reference / for users who don't
want to install OpenClaw.

The protocol intentionally takes `CommandContext` directly rather than a
shrunken DTO — every field on CommandContext is already needed by
`chat_via_agent` and copying them just to flatten the shape adds churn for
zero readability gain.

Lurk path (`chat_via_lurk`) remains a separate dispatcher idle-loop entry, but
it also delegates to OpenClaw when `WO_AGENT_BACKEND=openclaw` so background
reflection uses the same subscription-backed route as chat.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..config import settings

if TYPE_CHECKING:
    from ..dispatcher import CommandContext


class AgentBackend(Protocol):
    name: str

    def chat(
        self,
        *,
        ctx: "CommandContext",
        user_question: str,
        trigger_kind: str,
        reflection_enabled: bool | None = None,
    ) -> tuple[str | None, str]:
        """Run one chat-trigger turn. Returns (reply_text_or_None, trace_block).
        Same return shape as `orchestrator.chat_via_agent` so call sites are
        identical regardless of backend."""
        ...


_backend: AgentBackend | None = None
_backend_name: str | None = None


def get_agent_backend() -> AgentBackend:
    """Module-level singleton. Backends are stateless / re-entrant so a single
    instance shared across worker threads is fine."""
    global _backend, _backend_name
    name = (settings.agent_backend or "native").lower()
    if _backend is None or _backend_name != name:
        _backend = _build()
        _backend_name = name
    return _backend


def _build() -> AgentBackend:
    name = (settings.agent_backend or "native").lower()
    if name == "native":
        from .backends.native import NativeBackend
        return NativeBackend()
    if name == "openclaw":
        from .backends.openclaw import OpenClawBackend
        return OpenClawBackend()
    raise ValueError(
        f"unknown WO_AGENT_BACKEND={name!r} (valid: native, openclaw)"
    )
