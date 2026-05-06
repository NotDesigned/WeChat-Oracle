"""Shared file-log writers used by both dispatcher and agent orchestrator.

Three sinks live here so `dispatcher.py`, `agent/orchestrator.py`, and the
MCP server can all write without importing each other (would be circular):

  - `dispatcher.log`  — human-readable per-command stdout dump.
                        Written by slash commands AND by the agent path.
                        Single-backup rotation at 10 MB.
                        `append_log(path, t, block)`
  - `llm_debug.log`   — full LLM round-trip dump (system / user / raw /
                        parsed) for offline post-mortem. Single-backup
                        rotation at 10 MB.
                        `dump_llm_call(path, label, system, user, raw,
                                       parsed, note="")`
  - `<name>.process.log` — long-running process loguru sink (INFO+),
                        rotation 10 MB × 3 backups. `setup_process_log(name)`
                        called from cli entry points (ingest live /
                        dispatcher / worker mm) so process-level errors
                        and warnings are persisted, not just on stderr.

Both file helpers acquire a module-level lock; multiple threads in the
dispatcher pool can call them concurrently.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


_DISPATCHER_LOG_LOCK = threading.Lock()
_LLM_DEBUG_LOG_LOCK = threading.Lock()
_EVENT_LOG_LOCK = threading.Lock()
_DISPATCHER_LOG_MAX_BYTES = 10 * 1024 * 1024
_LLM_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB rotate threshold
_EVENT_LOG_MAX_BYTES = 10 * 1024 * 1024


def append_log(log_path: Path, command_t: int, block: str) -> None:
    """Append one timestamped block to dispatcher.log."""
    with _DISPATCHER_LOG_LOCK:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate(log_path, _DISPATCHER_LOG_MAX_BYTES)
        when = datetime.fromtimestamp(command_t).strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {when} ===\n{block}\n")


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


def dump_llm_call(
    log_path: Path,
    label: str,
    system: str,
    user: str,
    raw: str,
    parsed: Any,
    note: str = "",
) -> None:
    """Append one LLM round-trip to the debug log."""
    with _LLM_DEBUG_LOG_LOCK:
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


def append_event(event: str, **fields: Any) -> None:
    """Append one lightweight machine-readable lifecycle event.

    This is the cross-cutting index line for normal operation: small JSONL
    records that link ingest, trigger decisions, agent runs, tool summaries,
    memory writes, and reply attempts by shared identifiers (`msg_id`,
    `run_id`, `group_id`). It intentionally does not duplicate prompts,
    full tool results, or memory payloads; those stay in llm_debug.log /
    agent_run_log / mcp.log.
    """
    try:
        from .config import settings

        settings.ensure_dirs()
        log_path = settings.data_dir / "events.jsonl"
        entry: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
        }
        entry.update({k: v for k, v in fields.items() if v is not None})
        with _EVENT_LOG_LOCK:
            _maybe_rotate(log_path, _EVENT_LOG_MAX_BYTES)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as e:
        logger.warning("failed to append event log {}: {}", event, e)


_PROCESS_SINK_NAMES: set[str] = set()


def setup_process_log(name: str, *, level: str = "INFO") -> None:
    """Add a rotating file sink to loguru for one long-running process.

    Each process passes its own `name` so concurrent `ingest live` +
    `dispatcher` don't race on the same file. Stderr sink stays in place
    (loguru keeps both). Idempotent: re-calling with the same name is a no-op
    so cli entry points can call it without ordering concerns.
    """
    if name in _PROCESS_SINK_NAMES:
        return
    from .config import settings

    settings.ensure_dirs()
    log_path = settings.data_dir / f"{name}.process.log"
    logger.add(
        str(log_path),
        level=level,
        rotation="10 MB",
        retention=3,
        encoding="utf-8",
        enqueue=True,  # cross-thread queue; safe with worker daemon threads
    )
    _PROCESS_SINK_NAMES.add(name)


_MCP_AUDIT_LOG_LOCK = threading.Lock()
_MCP_AUDIT_MAX_BYTES = 10 * 1024 * 1024
_MAX_AUDIT_PREVIEW = 2000

_OPENCLAW_AUDIT_LOG_LOCK = threading.Lock()
_OPENCLAW_AUDIT_MAX_BYTES = 10 * 1024 * 1024


def append_openclaw_audit(
    log_path: Path,
    *,
    label: str,
    request: dict[str, Any],
    response: dict[str, Any] | None,
    duration_s: float,
    ok: bool,
    error: str | None = None,
) -> None:
    """Append one JSONL entry to data/openclaw.log per chat-completions roundtrip.

    Captures the EXACT payload we POSTed (messages full, not truncated) and the
    FULL response body OpenClaw returned (all choices, finish_reason, usage,
    raw). Bearer token / auth headers are never written.

    Use to debug:
      - did we send what we thought we sent (system / user / model / tools)
      - what did OpenClaw actually return (finish_reason='tool_calls' vs 'stop'
        tells you whether the agent attempted MCP tool use internally)
      - how long round-trips take, and which calls error out
    """
    with _OPENCLAW_AUDIT_LOG_LOCK:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate(log_path, _OPENCLAW_AUDIT_MAX_BYTES)
        entry: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "label": label,
            "dur_s": round(duration_s, 4),
            "ok": ok,
            "request": request,
        }
        if response is not None:
            entry["response"] = response
        if error is not None:
            entry["error"] = error[:2000]
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_mcp_audit(
    log_path: Path,
    *,
    tool: str,
    group_id: str | None,
    args: dict[str, Any] | None,
    duration_s: float,
    ok: bool,
    result: str | None = None,
    error: str | None = None,
) -> None:
    """Append one JSONL line to data/mcp.log per MCP tool invocation.

    JSONL (not prose) so it's grep-friendly and parseable for later metrics.
    `args` is logged in full for read tools (small) and trimmed for writers
    (notes_text / drift_text can be many KB) — the caller decides what to
    pass via `args`.
    """
    with _MCP_AUDIT_LOG_LOCK:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate(log_path, _MCP_AUDIT_MAX_BYTES)
        entry: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "tool": tool,
            "group_id": group_id,
            "args": args or {},
            "dur_s": round(duration_s, 4),
            "ok": ok,
        }
        if ok:
            entry["result_len"] = len(result or "")
            entry["preview"] = (result or "").replace("\n", " ")[:_MAX_AUDIT_PREVIEW]
        else:
            entry["error"] = (error or "")[:_MAX_AUDIT_PREVIEW]
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
