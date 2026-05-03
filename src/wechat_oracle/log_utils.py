"""Shared file-log writers used by both dispatcher and agent orchestrator.

Two sinks live here so `dispatcher.py` and `agent/orchestrator.py` can both
write without importing each other (would be circular):

  - `dispatcher.log`  — human-readable per-command stdout dump.
                        Written by slash commands AND by the agent path.
                        `append_log(path, t, block)`
  - `llm_debug.log`   — full LLM round-trip dump (system / user / raw /
                        parsed) for offline post-mortem. Single-backup
                        rotation when the file gets large.
                        `dump_llm_call(path, label, system, user, raw,
                                       parsed, note="")`

Both helpers acquire a module-level lock; multiple threads in the
dispatcher pool can call them concurrently.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


_DISPATCHER_LOG_LOCK = threading.Lock()
_LLM_DEBUG_LOG_LOCK = threading.Lock()
_LLM_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB rotate threshold


def append_log(log_path: Path, command_t: int, block: str) -> None:
    """Append one timestamped block to dispatcher.log."""
    with _DISPATCHER_LOG_LOCK:
        log_path.parent.mkdir(parents=True, exist_ok=True)
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
