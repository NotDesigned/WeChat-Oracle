"""Normalized data shapes that every importer + writer must produce / consume.

`Message` is the canonical row for the `messages` table; `ForwardedItem` is
the canonical row for `forwarded_records` (children of 合并转发 wrappers).
Field semantics are paired with `schema.sql` (DDL comments) and with
`ingest/writer.py` (INSERT_SQL). See CLAUDE.md「易漂移点 F1/F2」.

`MsgType` / `Status` enums also appear in `schema.sql` CHECK constraints
and column comments — keep in sync (CLAUDE.md F7/F8).
"""
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class MsgType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    LINK = "link"
    FORWARD = "forward"
    QUOTE = "quote"
    STICKER = "sticker"
    SYSTEM = "system"


class Status(StrEnum):
    RAW = "raw"
    MM_PENDING = "mm_pending"
    MM_DONE = "mm_done"
    ASSIGNED = "assigned"
    INDEXED = "indexed"


@dataclass
class ForwardedItem:
    """One child message inside a 合并转发 (merged-forward) bundle.

    Carried alongside the parent `Message` from importer to writer; written into
    `forwarded_records` after the parent row's msg_id is known.

    `sender_display` is the original author's display name (`<sourcename>` from
    the WeChat XML) — no wxid is available because `<hashusername>` is a sha256.
    `t` is `<srcMsgCreateTime>` of the original message in its source group, so
    forwarded items can be older than the parent message they're packaged in.
    `datatype` is the WeChat dataitem type: 1=text (we keep `content`), other
    values get a placeholder string. `media_path` is used when a non-text
    child's media bytes are available locally (most commonly datatype=2 image).
    Nested forwards (datatype=17) are NOT recursed — placeholder only.
    """
    seq: int
    sender_display: str | None
    t: int
    datatype: int
    content: str | None
    src_msg_id: str | None
    media_path: str | None = None


class Message(BaseModel):
    """Normalized chat message. The single shape that every importer must produce."""

    wx_msg_id: str | None = None
    group_id: str
    group_name: str | None = None
    sender_wxid: str | None = None
    sender_display: str | None = None
    t: int  # unix seconds, UTC
    type: MsgType
    content_text: str | None = None
    media_path: str | None = None
    reply_to_wx_msg_id: str | None = None
    quote_text: str | None = None
    # OCR/ASR output, populated by the `worker mm` job. NULL = not yet processed
    # (worker will pick it up); '' = processed but no text was found (don't
    # reprocess); '<text>' = the actual transcript.
    transcript: str | None = None
    source: Literal["live", "backfill"]
    status: Status = Status.RAW

    # Out-of-band side payload for type='forward' messages. Excluded from
    # serialization; the writer reads this attribute directly and persists into
    # `forwarded_records` after the parent row is inserted.
    forwarded_items: list[ForwardedItem] = Field(default_factory=list, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    def compute_dedupe_key(self) -> str:
        """A stable per-source key.

        Backfill messages always carry a wx_msg_id; we prefer it.
        Live messages typically do not, so we fall back to a content hash.
        """
        if self.wx_msg_id:
            return f"wx:{self.group_id}:{self.wx_msg_id}"
        h = hashlib.sha256()
        for part in (
            self.group_id,
            self.sender_wxid or self.sender_display or "",
            str(self.t),
            self.type.value,
            self.content_text or "",
            self.media_path or "",
        ):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return f"h:{h.hexdigest()[:24]}"
