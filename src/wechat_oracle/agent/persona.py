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


_PHASE_A_OPS_RULES = """\
你能调用的工具：
 - recall_group_history(query, ...)：在本群历史里搜过往发言（substring 匹配）
 - view_quoted_chain(msg_id)：跟随某条消息的引用链上溯
 - expand_forward_bundle(msg_id)：展开合并转发的子消息
 - read_image(msg_id, prompt?)：让视觉模型直接看一张图
 - read_voice(msg_id)：拿语音转写（已转写的秒返）
 - who_is(sender_wxid)：看某成员的笔记 + 最近发言
 - read_member_notes(sender_wxid) / read_group_notes(topic?)：取笔记
 - stay_silent(reason)：判断这次不该回应，闭嘴

调用约定：所有 msg_id 是整数（context 里方括号 `[123]` 的数字）；群 ID 不需要传——工具内部已经锁定本群。

回答风格：
 - 中文，2–6 句，像在打字而不是在写文章
 - 不要 markdown，不要 @ 任何人，不要打招呼问候
 - 不必每次都查工具——能直接答的就答；要查就查清楚再答
 - 触发不值得回应（别人在聊天恰好@你别的意思、问题完全跟你无关），调 stay_silent
"""


_DEFAULT_VOICE = (
    "用群友视角说话，简洁、不卑不亢；不要 ai 助手腔（不要「我可以帮你」「请告诉我」之类）。"
    " 群里有人发图发语音可以主动看一看再答；引用的消息记得读完再回。"
)


def _identity_block(
    persona: dict[str, Any], bot_name: str, group_name: str | None
) -> str:
    """First paragraph: who the bot is here."""
    group = group_name or "（未命名群）"
    identity = persona.get("identity")
    if isinstance(identity, str) and identity.strip():
        return identity.strip()
    return (
        f"你是微信群「{group}」里的成员，群昵称叫「{bot_name}」。"
        " 用户 @ 了你或对你说话，请像群友一样自然回应。"
    )


def _voice_block(persona: dict[str, Any]) -> str:
    voice = persona.get("voice")
    if isinstance(voice, str) and voice.strip():
        return voice.strip()
    return _DEFAULT_VOICE


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


def build_phase_a_system(
    *,
    persona: dict[str, Any],
    drift_text: str,
    bot_name: str,
    group_name: str | None,
) -> str:
    """Compose: identity + voice + knows/avoid + drift + ops rules + tool
    inventory. The result goes straight into the agent's system role."""
    parts = [
        _identity_block(persona, bot_name, group_name),
        "",
        _voice_block(persona),
    ]
    parts.append(_list_block(persona, "knows_about", "你对以下话题有立场或上下文"))
    parts.append(_list_block(persona, "avoid", "请避免的话题或语气"))
    parts.append(_drift_block(drift_text, persona))
    parts.append("")
    parts.append("---")
    parts.append(_PHASE_A_OPS_RULES)
    return "\n".join(p for p in parts if p is not None).strip()


def build_phase_b_system(
    *,
    persona: dict[str, Any],
    drift_text: str,
    bot_name: str,
    group_name: str | None,
    base_phase_b_prompt: str,
) -> str:
    """Phase B keeps voice/identity (so update_persona_drift writes in a
    consistent voice) but appends the reflection-specific rules instead
    of the chat ops rules. `base_phase_b_prompt` is the static reflection
    instructions (from `tools_write.phase_b_system_prompt()`)."""
    parts = [
        _identity_block(persona, bot_name, group_name),
        "",
        _voice_block(persona),
    ]
    parts.append(_drift_block(drift_text, persona))
    parts.append("")
    parts.append("---")
    parts.append(base_phase_b_prompt)
    return "\n".join(p for p in parts if p is not None).strip()


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
