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
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from ..log_utils import append_event
from .. import prompts
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


@dataclass
class ToolBudget:
    """Phase A tool-call budget.

    `None` or <=0 means unlimited for a given field. Counters live for one
    agent run and are shared across steps.
    """
    max_per_run: int | None = None
    max_per_step: int | None = None
    max_image_reads: int | None = None
    max_voice_reads: int | None = None
    total_calls: int = 0
    image_reads: int = 0
    voice_reads: int = 0

    @staticmethod
    def _limited(limit: int | None) -> bool:
        return limit is not None and limit > 0

    def check_and_count(self, call: ToolCall, step_call_idx: int) -> str | None:
        max_per_step = self.max_per_step
        if self._limited(max_per_step) and step_call_idx >= max_per_step:
            return f"tool budget exceeded: max {max_per_step} tool calls per step"
        max_per_run = self.max_per_run
        if self._limited(max_per_run) and self.total_calls >= max_per_run:
            return f"tool budget exceeded: max {max_per_run} tool calls per run"
        if call.name == "read_image":
            max_image_reads = self.max_image_reads
            if self._limited(max_image_reads) and self.image_reads >= max_image_reads:
                return f"tool budget exceeded: max {max_image_reads} read_image calls per run"
            self.image_reads += 1
        elif call.name == "read_voice":
            max_voice_reads = self.max_voice_reads
            if self._limited(max_voice_reads) and self.voice_reads >= max_voice_reads:
                return f"tool budget exceeded: max {max_voice_reads} read_voice calls per run"
            self.voice_reads += 1
        self.total_calls += 1
        return None


def _execute_tool_calls(
    tools: GroupScopedTools,
    calls: list[ToolCall],
    trace: list[dict[str, Any]],
    step_idx: int,
    budget: ToolBudget | None = None,
) -> list[dict[str, Any]]:
    """Run every tool call in order, recording the result in `trace` and
    returning the list of `tool` role messages to feed back to the model."""
    tool_messages: list[dict[str, Any]] = []
    for step_call_idx, call in enumerate(calls):
        if budget is not None:
            budget_error = budget.check_and_count(call, step_call_idx)
            if budget_error is not None:
                trace.append({
                    "step": step_idx,
                    "kind": "tool_budget_exceeded",
                    "tool": call.name,
                    "error": budget_error,
                })
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": budget_error,
                })
                continue

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

        started = time.time()
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
        append_event(
            "agent.tool_call",
            group_id=tools.group_id,
            tool=call.name,
            step=step_idx,
            status="error" if result.startswith("error:") or result.startswith("internal error:") else "ok",
            duration_ms=round((time.time() - started) * 1000, 3),
            result_len=len(result),
        )

        tool_messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": result,
        })
    return tool_messages


def _looks_like_tool_markup(content: str | None) -> bool:
    return bool(content and "DSML" in content and "tool_calls" in content)


_TOOL_STEPS_EXHAUSTED_REPLY = "我还需要继续查上下文，但这轮工具调用步数已经用完了。你可以让我继续查一次。"
_PHASE_A_EXHAUSTED_KINDS = {"tool_call_ignored_final", "tool_markup_blocked_final"}


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
    tool_budget: ToolBudget | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Run the Phase A read-only loop. Returns `(reply_text, trace)`.

    Three exits, all observable in the trace:
      A. Explicit stay_silent     → trace ends with `kind=terminate reason=stay_silent`
      B. Final text (incl. empty) → trace ends with `kind=final content=<text|None>`
                                     If empty on first turn (no tool calls + no
                                     stay_silent), retried once with a nudge
                                     (`kind=empty_final_retry` step recorded).
      C. Max steps hit            → last step is `kind=max_steps_hit`. The last
                                     non-tool-call turn forces tool_choice='none'
                                     so the model MUST emit text (or stay_silent)
                                     — C should be very rare in practice.

    The user_message is augmented with a runtime hint telling the model how
    many steps it has; without that the model can't pace itself. On the
    penultimate turn we add a wrap-up nudge; on the last turn we both nudge
    AND set tool_choice='none' so any further tool calls would be ignored.
    """
    augmented_user = user_message + prompts.PHASE_A_BUDGET_HINT.format(max_steps=max_steps)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": augmented_user},
    ]
    tool_specs = tools.openai_specs()
    trace: list[dict[str, Any]] = []
    empty_retry_used = False

    for step in range(max_steps):
        is_last_step = step == max_steps - 1
        is_penultimate = step == max_steps - 2

        # Penultimate-step warning (only when there are at least 2 steps).
        if is_penultimate and max_steps >= 2:
            messages.append({
                "role": "system",
                "content": prompts.PHASE_A_PENULTIMATE_WARNING.format(
                    step=step + 1, max_steps=max_steps,
                ),
            })

        # On the last step: force final text or stay_silent — no more
        # tool-gathering. tool_choice='none' tells the provider to disallow
        # function calls; the system message is belt-and-braces in case the
        # provider ignores tool_choice.
        if is_last_step:
            messages.append({
                "role": "system",
                "content": prompts.PHASE_A_LAST_STEP_FORCE.format(
                    step=step + 1, max_steps=max_steps,
                ),
            })
        tool_choice = "none" if is_last_step else "auto"

        resp = llm.complete_with_tools(
            model=model,
            messages=messages,
            tools=tool_specs,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )
        messages.append(resp.assistant_message)

        if resp.tool_calls and not is_last_step:
            silent = any(c.name == "stay_silent" for c in resp.tool_calls)
            tool_msgs = _execute_tool_calls(
                tools, resp.tool_calls, trace, step, budget=tool_budget
            )
            messages.extend(tool_msgs)
            if silent:
                trace.append({"step": step, "kind": "terminate", "reason": "stay_silent"})
                return None, trace
            continue

        if resp.tool_calls and is_last_step:
            trace.append({
                "step": step,
                "kind": "tool_call_ignored_final",
                "tools": [c.name for c in resp.tool_calls],
                "content": resp.content,
            })
            return _TOOL_STEPS_EXHAUSTED_REPLY, trace

        # No tool calls → final text (possibly empty).
        reply = (resp.content or "").strip() or None
        if _looks_like_tool_markup(reply):
            trace.append({
                "step": step,
                "kind": "tool_markup_blocked_final",
                "content": reply,
            })
            return _TOOL_STEPS_EXHAUSTED_REPLY, trace
        trace.append({"step": step, "kind": "final", "content": reply})

        # Empty-final retry: model output nothing, didn't call any tool, and
        # didn't call stay_silent. Likely confused about whether to answer.
        # Give one explicit nudge before accepting silence; only retry once,
        # only on the first step (later retries would compound oddly).
        if (
            reply is None
            and step == 0
            and not empty_retry_used
            and not is_last_step
        ):
            empty_retry_used = True
            trace.append({"step": step, "kind": "empty_final_retry"})
            messages.append({
                "role": "system",
                "content": prompts.PHASE_A_EMPTY_FINAL_NUDGE,
            })
            continue

        return reply, trace

    # Max-steps fallback. Should rarely fire because the last step uses
    # tool_choice='none', but if the provider ignores that and emits only
    # tool calls, fall back to whatever assistant text we did see.
    trace.append({"step": max_steps, "kind": "max_steps_hit"})
    last_content: str | None = None
    for m in reversed(messages):
        if m.get("role") == "assistant":
            last_content = (m.get("content") or "").strip() or None
            break
    return last_content, trace


def _run_simple_tool_loop(
    *,
    llm: ToolingLLM,
    model: str,
    system_prompt: str,
    user_message: str,
    tools: GroupScopedTools,
    max_steps: int,
    temperature: float,
    max_tokens: int | None,
    tool_budget: ToolBudget | None,
) -> list[dict[str, Any]]:
    """Bare tool-calling loop with `tool_choice='auto'` — terminates on the
    first assistant turn with no tool calls (returns whatever text is there
    as the `final` step), or on max_steps. No empty-final retry, no
    last-step force, no stay_silent termination — that's `run_phase_a`'s
    job. Used by both Phase B reflection and lurk background-learning.
    """
    trace: list[dict[str, Any]] = []
    if not tools.names() or max_steps <= 0:
        return trace
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    tool_specs = tools.openai_specs()
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
        tool_msgs = _execute_tool_calls(
            tools, resp.tool_calls, trace, step, budget=tool_budget
        )
        messages.extend(tool_msgs)
    trace.append({"step": max_steps, "kind": "max_steps_hit"})
    return trace


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
    tool_budget: ToolBudget | None = None,
) -> list[dict[str, Any]]:
    """Reflection over a chat agent run. The model sees a digest of Phase A
    plus the eventual reply, and decides whether to write notes."""
    digest = json.dumps(phase_a_trace, ensure_ascii=False, indent=2)
    user_message = prompts.PHASE_B_USER.format(
        trace_digest=digest, reply_text=repr(reply_text),
    )
    return _run_simple_tool_loop(
        llm=llm,
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
        tools=write_tools,
        max_steps=max_steps,
        temperature=temperature,
        max_tokens=max_tokens,
        tool_budget=tool_budget,
    )


def run_lurk_reflection(
    *,
    llm: ToolingLLM,
    model: str,
    system_prompt: str,
    user_message: str,
    tools: GroupScopedTools,
    max_steps: int,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    tool_budget: ToolBudget | None = None,
) -> list[dict[str, Any]]:
    """Silent background-learning loop. Same shape as `run_phase_b` but the
    caller supplies `user_message` directly (built from observed messages,
    not from a Phase A trace) and we add a runtime hint about the budget so
    the model can pace itself across the few rounds it has."""
    augmented_user = user_message + prompts.LURK_BUDGET_HINT.format(max_steps=max_steps)
    return _run_simple_tool_loop(
        llm=llm,
        model=model,
        system_prompt=system_prompt,
        user_message=augmented_user,
        tools=tools,
        max_steps=max_steps,
        temperature=temperature,
        max_tokens=max_tokens,
        tool_budget=tool_budget,
    )


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
    write_max_tokens: int | None = None,
    tool_budget: ToolBudget | None = None,
) -> AgentRunResult:
    """Convenience wrapper: run Phase A, then (if enabled) Phase B with the
    Phase A trace fed back in as the reflection input.

    `max_tokens` caps Phase A (chat reply). `write_max_tokens` caps Phase B
    (memory write); falls back to `max_tokens` when not given. The split
    matters because Phase B emits the full new memory document as a JSON
    arg and easily blows a small token cap mid-string."""
    reply, phase_a_trace = run_phase_a(
        llm=llm,
        model=model,
        system_prompt=phase_a_system,
        user_message=phase_a_user,
        tools=read_tools,
        max_steps=max_steps,
        temperature=temperature,
        max_tokens=max_tokens,
        tool_budget=tool_budget,
    )
    phase_b_trace: list[dict[str, Any]] | None = None
    if reflection_enabled and write_tools is not None and phase_b_system:
        last_kind = phase_a_trace[-1].get("kind") if phase_a_trace else None
        if last_kind in _PHASE_A_EXHAUSTED_KINDS:
            phase_b_trace = [{
                "step": 0,
                "kind": "reflection_skipped",
                "reason": "phase_a_exhausted_tool_budget",
            }]
        else:
            phase_b_trace = run_phase_b(
                llm=llm,
                model=model,
                system_prompt=phase_b_system,
                phase_a_trace=phase_a_trace,
                reply_text=reply,
                write_tools=write_tools,
                max_steps=reflect_max_steps,
                max_tokens=write_max_tokens or max_tokens,
            )
    return AgentRunResult(
        reply_text=reply,
        phase_a_trace=phase_a_trace,
        phase_b_trace=phase_b_trace,
    )
