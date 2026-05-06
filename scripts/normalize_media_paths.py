"""Copy existing absolute media paths into data/media and update the DB.

Run from the project root:
    uv run python scripts/normalize_media_paths.py
"""

from __future__ import annotations

import re
import sqlite3
import os
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MEDIA_SUBDIR = {
    "image": "images",
    "voice": "voices",
    "video": "videos",
    "sticker": "stickers",
}


_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _env_value(name: str, default: str) -> str:
    if name in os.environ:
        return os.environ[name]
    env_path = ROOT / ".env"
    if not env_path.exists():
        return default
    prefix = f"{name}="
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return default


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _looks_absolute(path_text: str) -> bool:
    return Path(path_text).is_absolute() or bool(_WIN_ABS_RE.match(path_text))


def _copy_into_data(src_abs: Path, msg_type: str, group_id: str, data_dir: Path) -> str:
    sub = MEDIA_SUBDIR[msg_type]
    target = data_dir / "media" / group_id / sub / src_abs.name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(src_abs, target)
    return f"media/{group_id}/{sub}/{src_abs.name}"


def _dedupe_key_for_row(row: sqlite3.Row, media_path: str) -> str:
    if row["wx_msg_id"]:
        return f"wx:{row['group_id']}:{row['wx_msg_id']}"
    h = hashlib.sha256()
    for part in (
        row["group_id"],
        row["sender_wxid"] or row["sender_display"] or "",
        str(row["t"]),
        row["type"],
        row["content_text"] or "",
        media_path or "",
    ):
        h.update(str(part).encode("utf-8"))
        h.update(b"\x00")
    return f"h:{h.hexdigest()[:24]}"


def main() -> int:
    data_dir = _project_path(_env_value("WO_DATA_DIR", "data"))
    db_path = _project_path(_env_value("WO_DB_PATH", "data/wechat-oracle.db"))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "media").mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        print(f"db not found: {db_path}")
        return 1
    copied = 0
    missing = 0
    skipped = 0
    failed = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT msg_id, wx_msg_id, group_id, group_name, sender_wxid,
                   sender_display, t, type, content_text, media_path,
                   reply_to_wx_msg_id, quote_text, transcript, source
              FROM messages
             WHERE media_path IS NOT NULL
               AND media_path != ''
               AND type IN ('image', 'voice', 'video', 'sticker')
            """
        ).fetchall()
        for row in rows:
            old = row["media_path"]
            if not old or not _looks_absolute(old):
                skipped += 1
                continue
            src = Path(old)
            if not src.exists():
                missing += 1
                continue
            msg_type = row["type"]
            if msg_type not in MEDIA_SUBDIR:
                skipped += 1
                continue
            new_media_path = _copy_into_data(src, msg_type, row["group_id"], data_dir)
            new_dedupe_key = _dedupe_key_for_row(row, new_media_path)
            try:
                conn.execute("BEGIN")
                conn.execute(
                    """
                    UPDATE messages
                       SET media_path=?,
                           dedupe_key=?
                     WHERE msg_id=?
                    """,
                    (new_media_path, new_dedupe_key, row["msg_id"]),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                conn.rollback()
                failed += 1
                print(f"skip msg_id={row['msg_id']}: dedupe conflict: {e}")
                continue
            copied += 1
    print(
        "normalize-media: "
        f"copied={copied} skipped={skipped} missing={missing} failed={failed}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
