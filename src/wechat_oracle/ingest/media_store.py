"""Project-local media storage for ingested WeChat media files.

All media that downstream tools may read should live under
`<data_dir>/media/<group_id>/<kind>/...`, and `messages.media_path` should
store the path relative to `data_dir`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..models import MsgType


MEDIA_TYPES = {MsgType.IMAGE, MsgType.VOICE, MsgType.VIDEO, MsgType.STICKER}

MEDIA_SUBDIR: dict[MsgType, str] = {
    MsgType.IMAGE: "images",
    MsgType.VOICE: "voices",
    MsgType.VIDEO: "videos",
    MsgType.STICKER: "stickers",
}

MEDIA_MISSING_TAGS: dict[MsgType, str] = {
    MsgType.IMAGE: "[图片缺失]",
    MsgType.VOICE: "[语音缺失]",
    MsgType.VIDEO: "[视频缺失]",
    MsgType.STICKER: "[表情缺失]",
}


def parse_media_ref(value: str | None) -> Path | None:
    """Return a local filesystem path reference, or None for placeholders/URLs."""
    if not value:
        return None
    text = value.strip()
    if not text or text.startswith("[") or text.startswith("<"):
        return None
    if text.startswith(("http://", "https://")):
        return None
    return Path(text)


def copy_into_data(
    src_abs: Path,
    msg_type: MsgType,
    group_id: str,
    data_dir: Path,
) -> str:
    """Copy a media file into data/media and return data_dir-relative path."""
    sub = MEDIA_SUBDIR[msg_type]
    target = data_dir / "media" / group_id / sub / src_abs.name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_abs, target)
    return f"media/{group_id}/{sub}/{src_abs.name}"


def materialize_media_ref(
    ref: str | None,
    msg_type: MsgType,
    group_id: str,
    data_dir: Path,
    *,
    source_root: Path | None = None,
) -> str | None:
    """Copy a local media ref into data/media.

    `ref` may be absolute, or relative to `source_root`. Returns the
    data_dir-relative DB value, or None when the ref is absent, remote, or the
    file is not present on disk.
    """
    parsed = parse_media_ref(ref)
    if parsed is None:
        return None
    src_abs = parsed if parsed.is_absolute() else ((source_root or Path.cwd()) / parsed)
    if not src_abs.exists():
        return None
    return copy_into_data(src_abs, msg_type, group_id, data_dir)
