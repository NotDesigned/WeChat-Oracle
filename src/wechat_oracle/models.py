import hashlib
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
    source: Literal["live", "backfill"]
    status: Status = Status.RAW

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
