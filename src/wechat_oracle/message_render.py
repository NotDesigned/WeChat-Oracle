"""Shared LLM-visible rendering for normalized chat messages.

Keep the semantic parts of message rendering in one place: OCR/ASR prefixes,
media placeholders, quote suffixes, and one-line normalization. Callers can
still choose their own outer line format because `/find`, agent recent context,
and human-facing `/recent` have different ID and layout needs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol


class RowLike(Protocol):
    def __getitem__(self, key: str) -> Any: ...


TRANSCRIPT_PREFIX: dict[str, str] = {
    "image": "[图片·OCR] ",
    "voice": "[语音·ASR] ",
    "video": "[视频·识别] ",
    "sticker": "[表情·OCR] ",
}

MEDIA_PLACEHOLDER: dict[str, str] = {
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "sticker": "[表情]",
}


def _get(row: RowLike | dict[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        if isinstance(row, dict):
            return row.get(key, default)
        return default


def one_line(text: object) -> str:
    return str(text).replace("\n", " ").strip()


def format_time(t: int | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if t is None:
        return "?"
    return datetime.fromtimestamp(int(t)).strftime(fmt)


def message_ref(msg_id: object, *, kind: str = "m", id_style: Literal["bare", "prefixed"] = "bare") -> str:
    if msg_id is None:
        return "?"
    if id_style == "prefixed":
        return f"{kind}:{msg_id}"
    return str(msg_id)


def render_message_body(row: RowLike | dict[str, Any]) -> str:
    """Render only the semantic body of one `messages` row.

    Priority mirrors dispatcher.fetch_candidates:
      transcript with type prefix > content_text > typed media placeholder >
      generic `[type]`.

    This makes `[图片·OCR] ...` / `[语音·ASR] ...` visible consistently across
    agent recent context and read tools, instead of hiding recognized media
    behind an older `[图片]` placeholder.
    """
    msg_type = str(_get(row, "type", "?") or "?")
    transcript = _get(row, "transcript")
    if transcript:
        prefix = TRANSCRIPT_PREFIX.get(msg_type, f"[{msg_type}·识别] ")
        return prefix + one_line(transcript)

    content = _get(row, "content_text")
    if content:
        return one_line(content)

    return MEDIA_PLACEHOLDER.get(msg_type, f"[{msg_type}]")


def render_sender(row: RowLike | dict[str, Any]) -> str:
    return str(_get(row, "sender_display") or _get(row, "sender_wxid") or "?")


def render_quote_suffix(
    row: RowLike | dict[str, Any],
    *,
    style: Literal["agent", "inline"] = "agent",
) -> str:
    """Render quote metadata for a `messages` row.

    `agent` is navigational and includes parent ref/type when available:
    `[引用→m:122 image Alice：snippet]`.

    `inline` is compact for search/summarization:
    `[引用 m:122 Alice：snippet]`.
    """
    if _get(row, "type") != "quote":
        return ""
    content = _get(row, "content_text") or ""
    if isinstance(content, str) and "[引用" in content:
        return ""

    parent_msg_id = _get(row, "parent_msg_id")
    parent_type = _get(row, "parent_type")
    parent_sender = _get(row, "parent_sender") or _get(row, "parent_sender_wxid")
    snippet = one_line(_get(row, "quote_text") or "")

    if parent_msg_id is None:
        if style == "inline":
            return f" [引用 未入库：{snippet}]" if snippet else " [引用 未入库]"
        return f" [引用→未入库：{snippet}]" if snippet else " [引用→未入库]"

    ref = message_ref(parent_msg_id, id_style="prefixed")
    who = f" {parent_sender}" if parent_sender else ""
    typ = f" {parent_type}" if parent_type and style == "agent" else ""
    sep = "：" if snippet else ""
    arrow = "→" if style == "agent" else " "
    return f" [引用{arrow}{ref}{typ}{who}{sep}{snippet}]"


def render_message_line(
    row: RowLike | dict[str, Any],
    *,
    style: Literal["agent", "tool", "human"] = "tool",
    id_style: Literal["bare", "prefixed"] = "bare",
    include_wxid: bool = False,
    self_wxid: str | None = None,
    time_fmt: str = "%Y-%m-%d %H:%M",
) -> str:
    msg_id = _get(row, "msg_id")
    ref = message_ref(msg_id, id_style=id_style)
    ts = format_time(_get(row, "t"), time_fmt)
    sender = render_sender(row)
    wxid = _get(row, "sender_wxid") or "?"
    self_tag = " [自己]" if self_wxid and wxid == self_wxid else ""
    wxid_part = f" ({wxid})" if include_wxid else ""
    quote = render_quote_suffix(row, style="agent" if style == "agent" else "inline")
    return f"[{ref}] {ts}{self_tag} {sender}{wxid_part}: {render_message_body(row)}{quote}"
