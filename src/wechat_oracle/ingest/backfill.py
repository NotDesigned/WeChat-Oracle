"""Historical backfill importers.

Each format adapter is a callable `(Path, Path) -> Iterator[Message]` where the second
arg is the project's `data_dir`. The CLI dispatches by `--format`. Add a new format by
writing a parser function and registering it in FORMATS.

Media handling
--------------
Media-type messages reference files on disk (images/voices/videos/stickers). The source
export (e.g. WeFlow's output folder) is treated as a temporary staging area: this module
copies referenced files into `<data_dir>/media/<group_id>/...` and stores the path
*relative to `data_dir`* in the DB (e.g. `media/12345@chatroom/images/abc.jpg`).
This keeps `data/` self-contained — once import succeeds, the source folder can be
deleted without breaking DB references.
"""

import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from loguru import logger

from ..models import Message, MsgType
from .forwarded import FORWARD_LOCAL_TYPE, base_local_type, parse_record_xml


def read_normalized_jsonl(path: Path, data_dir: Path) -> Iterator[Message]:
    """One Message JSON object per line, already in our canonical schema.

    Useful for testing the pipeline without any third-party export adapter. `data_dir`
    is unused for this format — paths in the JSONL are taken as-is.
    """
    del data_dir  # unused
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                payload.setdefault("source", "backfill")
                yield Message.model_validate(payload)
            except Exception as exc:
                logger.warning("skip line {} of {}: {}", lineno, path.name, exc)


# WeChat localType -> normalized MsgType.
# Codes are the well-known values from WeChat's local SQLite schema, surfaced by WeFlow.
_WEFLOW_LOCAL_TYPE_MAP: dict[int, MsgType] = {
    1: MsgType.TEXT,
    3: MsgType.IMAGE,
    34: MsgType.VOICE,
    43: MsgType.VIDEO,
    47: MsgType.STICKER,
    48: MsgType.TEXT,      # location: WeFlow renders location text into `content`
    49: MsgType.LINK,      # app msg (link/file/transfer/etc); refined when quote present
    10000: MsgType.SYSTEM,
}

_MEDIA_TYPES = {MsgType.IMAGE, MsgType.VOICE, MsgType.VIDEO, MsgType.STICKER}

# Subdir under `data/media/<group_id>/` for each media kind. Decoupled from the source
# export's layout so weird ref paths (e.g. WeFlow's `../images/x.jpg`) can't escape.
_MEDIA_SUBDIR: dict[MsgType, str] = {
    MsgType.IMAGE: "images",
    MsgType.VOICE: "voices",
    MsgType.VIDEO: "videos",
    MsgType.STICKER: "stickers",
}

# Tag written into `content_text` when media_path resolves but the file isn't on disk
# (e.g. friend shared the JSON without the sibling images/voices/ folders).
_MEDIA_MISSING_TAGS: dict[MsgType, str] = {
    MsgType.IMAGE: "[图片缺失]",
    MsgType.VOICE: "[语音缺失]",
    MsgType.VIDEO: "[视频缺失]",
    MsgType.STICKER: "[表情缺失]",
}


def _classify(raw: dict[str, Any]) -> MsgType | None:
    """Quotes/replies override the localType-based mapping.

    WeFlow encodes appmsg subtype in the high 32 bits of localType. We mask
    those off for the type table lookup, but preserve subtype 19 (合并转发)
    by mapping it explicitly to MsgType.FORWARD before the generic 49→LINK fall.
    """
    if raw.get("quotedContent") or raw.get("replyToMessageId"):
        return MsgType.QUOTE
    lt = raw.get("localType")
    if lt == FORWARD_LOCAL_TYPE:
        return MsgType.FORWARD
    return _WEFLOW_LOCAL_TYPE_MAP.get(base_local_type(lt))


def _parse_media_ref(content: str | None) -> Path | None:
    """WeFlow stores media file paths in `content` for media-type messages.

    Placeholder strings like "[图片]" (when media export was disabled) are not paths.
    Returns the raw Path as written in the JSON; relative paths are anchored later.
    """
    if not content:
        return None
    if content.startswith("[") or content.startswith("<"):
        return None
    return Path(content)


def _copy_into_data(
    src_abs: Path,
    msg_type: MsgType,
    group_id: str,
    data_dir: Path,
) -> str:
    """Copy `src_abs` into `<data_dir>/media/<group_id>/<kind>/<filename>`; return
    the path relative to `data_dir` with forward slashes
    (e.g. `media/<group>/images/abc.jpg`).

    Subdir is derived from `msg_type` rather than the source ref path — WeFlow's refs
    can be parent-relative (`../images/x.jpg`), and trusting them lets the file escape
    the per-group folder. Existing targets are skipped (re-import is a no-op).
    """
    sub = _MEDIA_SUBDIR[msg_type]
    target = data_dir / "media" / group_id / sub / src_abs.name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_abs, target)
    return f"media/{group_id}/{sub}/{src_abs.name}"


def _convert_weflow_message(
    raw: dict[str, Any],
    group_id: str,
    group_name: str | None,
    source_root: Path,
    data_dir: Path,
) -> Message | None:
    msg_type = _classify(raw)
    if msg_type is None:
        return None

    content = raw.get("content")
    media_path: str | None = None
    content_text: str | None = None
    if msg_type in _MEDIA_TYPES:
        ref = _parse_media_ref(content)
        if ref is None:
            content_text = content
        else:
            src_abs = ref if ref.is_absolute() else (source_root / ref)
            if src_abs.exists():
                media_path = _copy_into_data(src_abs, msg_type, group_id, data_dir)
            else:
                content_text = _MEDIA_MISSING_TAGS[msg_type]
    else:
        content_text = content

    create_time = raw.get("createTime")
    if create_time is None:
        return None

    forwarded_items = (
        parse_record_xml(raw.get("rawContent"))
        if msg_type is MsgType.FORWARD else []
    )
    if msg_type is MsgType.FORWARD and not content_text:
        # WeFlow's `parsedContent` for record-msg is empty; show a sane preview
        # so the parent row isn't blank in the DB.
        content_text = "[聊天记录]"

    return Message(
        wx_msg_id=str(raw["platformMessageId"]) if raw.get("platformMessageId") else None,
        group_id=group_id,
        group_name=group_name,
        sender_wxid=raw.get("senderUsername"),
        sender_display=raw.get("senderDisplayName"),
        t=int(create_time),
        type=msg_type,
        content_text=content_text,
        media_path=media_path,
        reply_to_wx_msg_id=str(raw["replyToMessageId"]) if raw.get("replyToMessageId") else None,
        quote_text=raw.get("quotedContent"),
        source="backfill",
        forwarded_items=forwarded_items,
    )


def read_weflow(path: Path, data_dir: Path) -> Iterator[Message]:
    """Adapter for WeFlow's `--format json` export (https://github.com/hicccc77/WeFlow).

    Each file holds one session (group or DM). Top-level keys: `weflow`, `session`, `messages`.
    Media files are written next to the JSON; `content` is the relative path for media-type
    messages. Referenced media is copied into `<data_dir>/media/<group_id>/...`.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    session = data.get("session", {}) or {}
    group_id = str(session.get("wxid") or path.stem)
    group_name = session.get("displayName") or session.get("nickname") or session.get("remark")
    source_root = path.parent

    skipped = 0
    missing = 0
    missing_tags = set(_MEDIA_MISSING_TAGS.values())
    for raw in data.get("messages", []) or []:
        msg = _convert_weflow_message(raw, group_id, group_name, source_root, data_dir)
        if msg is None:
            skipped += 1
            continue
        if msg.content_text in missing_tags:
            missing += 1
        yield msg
    if skipped:
        logger.info("skipped {} messages with unmapped type in {}", skipped, path.name)
    if missing:
        logger.warning("{} media files missing on disk in {}", missing, path.name)


FORMATS: dict[str, Callable[[Path, Path], Iterator[Message]]] = {
    "jsonl": read_normalized_jsonl,
    "weflow": read_weflow,
}


def import_file(path: Path, fmt: str, data_dir: Path) -> Iterator[Message]:
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; known: {sorted(FORMATS)}")
    return FORMATS[fmt](path, data_dir)
