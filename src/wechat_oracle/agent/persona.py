"""Persona assembly: static yaml core + evolvable drift table → system prompts.

Layered persona model:
  1. Static core (per-group yaml at `data/personas/<group_id>.yaml`)
     — voice, style, identity. Hand-edited; agent does NOT modify.
  2. Drift (`persona_drift` table) — agent-writable supplement; replaces
     wholesale on each `update_persona_drift` call.
  3. Operational rules (this module's hard-coded `_OPS_RULES`) — what the
     agent CAN do (tool list, msg_id format, terseness rules). Identical
     across groups; not user-editable.

Phase A and Phase B compose these layers slightly differently:
  - Phase A: core + drift + ops_rules + tool inventory
  - Phase B: core + drift + reflection-specific rules (the model is
    deciding whether/what to write, not chatting)

If the yaml file is missing or malformed, we fall back to a generic
default — the agent stays usable on a fresh install before personas
are authored.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from .memory import get_persona_drift


# --- yaml loading ----------------------------------------------------------


def load_persona_yaml(personas_dir: Path, group_id: str) -> dict[str, Any]:
    """Load `<personas_dir>/<group_id>.yaml`. Empty dict on missing/bad file.

    Recognized keys (all optional):
      identity:         one-line "you are X in this group"
      voice:            multi-line voice / style description
      knows_about:      list[str] — topics the bot has standing context on
      avoid:            list[str] — topics / tones to avoid
      default_drift:    string seed used as fallback when persona_drift table
                        is empty for this group (still appears in prompts)
    """
    path = personas_dir / f"{group_id}.yaml"
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        logger.warning("persona yaml {} malformed; using defaults: {}", path, e)
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("persona yaml {} root is not a mapping; using defaults", path)
        return {}
    return raw


# --- prompt assembly -------------------------------------------------------


# Minimal-as-possible operational rules. Tool signatures are passed via the
# OpenAI tools= parameter — re-listing them here would be redundant and waste
# tokens. Style prescriptions deliberately removed; let `persona_drift` learn
# what fits this group rather than baking in a prior.
_PHASE_A_OPS_RULES = """\
约定：context 里方括号 [N] 的数字就是 msg_id（整数）；群 ID 不用传，工具内部已锁定本群。

回答只写正文，不要 @ 任何人；发送层会自动 @ 触发者。不要使用 markdown。

不知道该不该说话就调 stay_silent。群友的对话不必每条都接。"""


def _identity_block(
    persona: dict[str, Any], bot_name: str, group_name: str | None
) -> str:
    """First paragraph: who the bot is here. Defaults are deliberately bare —
    users opt into a richer identity by writing `data/personas/<gid>.yaml`."""
    identity = persona.get("identity")
    if isinstance(identity, str) and identity.strip():
        return identity.strip()
    group = group_name or "（未命名群）"
    return f"你是微信群「{group}」里的某个成员，群昵称叫「{bot_name}」。"


def _voice_block(persona: dict[str, Any]) -> str:
    voice = persona.get("voice")
    if isinstance(voice, str) and voice.strip():
        return voice.strip()
    return ""  # no default voice — let the model + drift settle naturally


def _list_block(persona: dict[str, Any], key: str, label: str) -> str:
    """Render `knows_about` / `avoid` style lists. Empty/missing → empty."""
    items = persona.get(key)
    if not isinstance(items, list) or not items:
        return ""
    bullets = "\n".join(f" - {it}" for it in items if isinstance(it, str) and it.strip())
    if not bullets:
        return ""
    return f"\n\n{label}：\n{bullets}"


def _drift_block(drift_text: str, persona: dict[str, Any]) -> str:
    """Drift goes after core voice. Falls back to yaml `default_drift` if
    the table is empty — lets users seed an initial drift in yaml without
    pre-populating the table."""
    if drift_text.strip():
        return f"\n\n# 人格补充（agent 自己维护，会随时间更新）\n{drift_text.strip()}"
    seed = persona.get("default_drift")
    if isinstance(seed, str) and seed.strip():
        return f"\n\n# 人格补充（默认种子，尚未演化）\n{seed.strip()}"
    return ""


def _join_nonempty(*parts: str, sep: str = "\n\n") -> str:
    """Drop any empty / whitespace-only parts before joining. Keeps the
    output tight when persona is mostly default (no yaml + no drift)."""
    return sep.join(p.strip() for p in parts if p and p.strip())


def build_phase_a_system(
    *,
    persona: dict[str, Any],
    drift_text: str,
    bot_name: str,
    group_name: str | None,
) -> str:
    """Compose persona layers (identity → voice → knows/avoid → drift) and
    operational rules into one system prompt. Empty layers drop out clean —
    bare default persona renders to just identity + ops_rules."""
    persona_block = _join_nonempty(
        _identity_block(persona, bot_name, group_name),
        _voice_block(persona),
        _list_block(persona, "knows_about", "你对以下话题有立场或上下文"),
        _list_block(persona, "avoid", "请避免的话题或语气"),
        _drift_block(drift_text, persona),
    )
    return _join_nonempty(persona_block, "---", _PHASE_A_OPS_RULES)


def build_phase_b_system(
    *,
    persona: dict[str, Any],
    drift_text: str,
    bot_name: str,
    group_name: str | None,
    base_phase_b_prompt: str,
) -> str:
    """Phase B keeps the persona stack (so writes to drift/memory stay in
    voice) and swaps the chat ops rules for reflection rules."""
    persona_block = _join_nonempty(
        _identity_block(persona, bot_name, group_name),
        _voice_block(persona),
        _drift_block(drift_text, persona),
    )
    return _join_nonempty(persona_block, "---", base_phase_b_prompt)


# --- top-level convenience -------------------------------------------------


def assemble_system_prompts(
    *,
    conn: sqlite3.Connection,
    group_id: str,
    group_name: str | None,
    bot_name: str,
    personas_dir: Path,
    base_phase_b_prompt: str,
) -> tuple[str, str]:
    """One-shot: load yaml + drift + render both phase prompts.

    Returns `(phase_a_system, phase_b_system)`. Caller passes phase_b
    through to `run_agent` whether or not reflection is enabled — the
    runtime ignores it when reflection is off.
    """
    persona = load_persona_yaml(personas_dir, group_id)
    drift = get_persona_drift(conn, group_id)
    return (
        build_phase_a_system(
            persona=persona, drift_text=drift,
            bot_name=bot_name, group_name=group_name,
        ),
        build_phase_b_system(
            persona=persona, drift_text=drift,
            bot_name=bot_name, group_name=group_name,
            base_phase_b_prompt=base_phase_b_prompt,
        ),
    )
