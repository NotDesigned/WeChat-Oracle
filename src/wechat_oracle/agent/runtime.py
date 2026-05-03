"""Two-phase agent loop: Phase A read-only decisions, Phase B write-only reflection.

Phase A (`run_phase_a`):
  - LLM gets system_prompt + initial user turn, plus the read-only tools.
  - Each turn the LLM either calls tool(s) or emits final text.
  - Final text → `reply_text` (the bot speaks).
  - `stay_silent` tool call → `reply_text=None` (bot stays quiet).
  - Loop capped at `max_steps`; busted cap → return whatever final text we
    have, or None.

Phase B (`run_phase_b`):
  - Runs only if `reflection_enabled` AND there's a write_tools registry.
  - LLM sees the Phase A trace + reply, decides whether to write notes.
  - Termination: assistant message with no tool_calls (model said its piece).
  - Capped at `reflect_max_steps`.

Both phases append a structured trace dict per step to a list the caller
serializes into `agent_run_log`. The trace is the only debugging hook; if
you can't reconstruct what happened from it, it's a bug in the trace
shape, not in your reading.

This module is provider-agnostic — it talks to a `ToolingLLM` (OpenAI-compat
tool-calling). Currently only `OpenAICompatLLM` implements that Protocol.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from ..llm import ToolCall, ToolingLLM
from .tools import GroupScopedTools, ToolError


@dataclass
class AgentRunResult:
    """End-to-end outcome of one agent run, plus the audit trace.

    `reply_text` is what the bot will say in chat; `None` means stay silent.
    `phase_a_trace` / `phase_b_trace` are JSON-serializable (list of dict);
    callers (dispatcher) pass them through to `memory.insert_run_log`.
    """
    reply_text: str | None
    phase_a_trace: list[dict[str, Any]]
    phase_b_trace: list[dict[str, Any]] | None  # None when reflection skipped


def _execute_tool_calls(
    tools: GroupScopedTools,
    calls: list[ToolCall],
    trace: list[dict[str, Any]],
    step_idx: int,
) -> list[dict[str, Any]]:
    """Run every tool call in order, recording the result in `trace` and
    returning the list of `tool` role messages to feed back to the model."""
    tool_messages: list[dict[str, Any]] = []
    for call in calls:
        try:
            args: dict[str, Any] = json.loads(call.arguments_json or "{}")
            if not isinstance(args, dict):
                raise ToolError("tool arguments must be a JSON object")
        except (json.JSONDecodeError, ToolError) as e:
            err = f"invalid tool args: {e}"
            trace.append({
                "step": step_idx,
                "kind": "tool_error",
                "tool": call.name,
                "args_raw": call.arguments_json,
                "error": err,
            })
            tool_messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": err,
            })
            continue

        impl = tools.get(call.name)
        if impl is None:
            err = f"unknown tool: {call.name}"
            trace.append({
                "step": step_idx,
                "kind": "tool_error",
                "tool": call.name,
                "args": args,
                "error": err,
            })
            tool_messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": err,
            })
            continue

        try:
            result = impl.call(args)
        except ToolError as e:
            result = f"error: {e}"
            trace.append({
                "step": step_idx,
                "kind": "tool_error",
                "tool": call.name,
                "args": args,
                "error": str(e),
            })
        except Exception as e:
            logger.exception("agent tool {} crashed (group_id={})", call.name, tools.group_id)
            result = f"internal error: {e}"
            trace.append({
                "step": step_idx,
                "kind": "tool_crash",
                "tool": call.name,
                "args": args,
                "error": str(e),
            })
        else:
            trace.append({
                "step": step_idx,
                "kind": "tool_call",
                "tool": call.name,
                "args": args,
                "result": result,
            })

        tool_messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": result,
        })
    return tool_messages


def run_phase_a(
    *,
    llm: ToolingLLM,
    model: str,
    system_prompt: str,
    user_message: str,
    tools: GroupScopedTools,
    max_steps: int,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Run the Phase A read-only loop. Returns `(reply_text, trace)`."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    tool_specs = tools.openai_specs()
    trace: list[dict[str, Any]] = []
    reply: str | None = ""  # default to "" so bare empty content → silent

    for step in range(max_steps):
        resp = llm.complete_with_tools(
            model=model,
            messages=messages,
            tools=tool_specs,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice="auto",
        )
        messages.append(resp.assistant_message)

        if resp.tool_calls:
            # Special-case stay_silent BEFORE running it: terminate the loop
            # with reply=None even if other calls share this turn.
            silent = any(c.name == "stay_silent" for c in resp.tool_calls)
            tool_msgs = _execute_tool_calls(tools, resp.tool_calls, trace, step)
            messages.extend(tool_msgs)
            if silent:
                trace.append({"step": step, "kind": "terminate", "reason": "stay_silent"})
                return None, trace
            continue

        # No tool calls → the assistant emitted final text (or empty).
        reply = (resp.content or "").strip() or None
        trace.append({"step": step, "kind": "final", "content": reply})
        return reply, trace

    # Hit the step cap without final text. Best-effort: take whatever the
    # last assistant content was; otherwise fall through silent.
    trace.append({"step": max_steps, "kind": "max_steps_hit"})
    last_content: str | None = None
    for m in reversed(messages):
        if m.get("role") == "assistant":
            last_content = (m.get("content") or "").strip() or None
            break
    return last_content, trace


def run_phase_b(
    *,
    llm: ToolingLLM,
    model: str,
    system_prompt: str,
    phase_a_trace: list[dict[str, Any]],
    reply_text: str | None,
    write_tools: GroupScopedTools,
    max_steps: int,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Run the Phase B write-only loop. Returns the trace; no reply."""
    trace: list[dict[str, Any]] = []
    if not write_tools.names() or max_steps <= 0:
        return trace

    digest = json.dumps(phase_a_trace, ensure_ascii=False, indent=2)
    user = (
        f"刚才的 Phase A trace（按时间正序）：\n{digest}\n\n"
        f"最终回复：{reply_text!r}\n\n"
        "现在是反思阶段：根据本次发生的事，决定要不要写笔记。"
        " 大多数情况下不需要写——只有当出现新事实、关键观点或群文化变化时才写。"
        " 若不需要写，直接输出空文本结束。"
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
    ]
    tool_specs = write_tools.openai_specs()

    for step in range(max_steps):
        resp = llm.complete_with_tools(
            model=model,
            messages=messages,
            tools=tool_specs,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice="auto",
        )
        messages.append(resp.assistant_message)

        if not resp.tool_calls:
            trace.append({"step": step, "kind": "final", "content": resp.content})
            return trace

        tool_msgs = _execute_tool_calls(write_tools, resp.tool_calls, trace, step)
        messages.extend(tool_msgs)

    trace.append({"step": max_steps, "kind": "max_steps_hit"})
    return trace


def run_agent(
    *,
    llm: ToolingLLM,
    model: str,
    phase_a_system: str,
    phase_a_user: str,
    read_tools: GroupScopedTools,
    write_tools: GroupScopedTools | None,
    phase_b_system: str | None,
    max_steps: int,
    reflect_max_steps: int,
    reflection_enabled: bool,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> AgentRunResult:
    """Convenience wrapper: run Phase A, then (if enabled) Phase B with the
    Phase A trace fed back in as the reflection input."""
    reply, phase_a_trace = run_phase_a(
        llm=llm,
        model=model,
        system_prompt=phase_a_system,
        user_message=phase_a_user,
        tools=read_tools,
        max_steps=max_steps,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    phase_b_trace: list[dict[str, Any]] | None = None
    if reflection_enabled and write_tools is not None and phase_b_system:
        phase_b_trace = run_phase_b(
            llm=llm,
            model=model,
            system_prompt=phase_b_system,
            phase_a_trace=phase_a_trace,
            reply_text=reply,
            write_tools=write_tools,
            max_steps=reflect_max_steps,
        )
    return AgentRunResult(
        reply_text=reply,
        phase_a_trace=phase_a_trace,
        phase_b_trace=phase_b_trace,
    )
