"""Multimedia worker: poll `messages` for unprocessed image/voice rows,
run OCR/ASR, write the result back into `transcript`.

Schema contract (CLAUDE.md F1):
  transcript IS NULL   → not yet processed; this worker picks it up
  transcript = ''      → processed, no text or non-recoverable error;
                         don't retry (otherwise we churn on stuck rows)
  transcript = '<txt>' → success

Path resolution:
  New live/backfill rows store media_path relative to settings.data_dir under
  data/media. Older live rows may still contain absolute WeFlow cache paths;
  run scripts/normalize_media_paths.py to migrate them.

Failure modes (logged, don't kill the worker):
  - file missing on disk → mark transcript='' (we'll never get the bytes back)
  - OCR/ASR engine error → mark transcript='' (suspect file format; bailing
    out without a marker would re-loop forever)
  - DB error → propagate (probably needs human attention)
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from loguru import logger

from ..config import settings
from ..db import get_conn, init_db, transaction


_BATCH = 5
_IDLE_SLEEP = 30.0  # seconds when nothing's queued


def _next_batch(conn: sqlite3.Connection, limit: int = _BATCH) -> list[sqlite3.Row]:
    """Newest-first so recent activity gets transcribed before the long tail.

    `media_path IS NOT NULL` filters out backfill rows that arrived without
    media exports — they're permanently un-OCRable here (would need a fresh
    re-pull from WeFlow with media=1 to recover, which is a separate job).
    """
    return conn.execute(
        """
        SELECT msg_id, type, media_path
          FROM messages
         WHERE type IN ('image', 'voice')
           AND media_path IS NOT NULL
           AND transcript IS NULL
         ORDER BY t DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _resolve_path(media_path: str) -> Path:
    """Resolve data_dir-relative media paths, with legacy absolute fallback."""
    p = Path(media_path)
    if p.is_absolute():
        return p
    return settings.data_dir / media_path


def _save_transcript(
    conn: sqlite3.Connection, msg_id: int, transcript: str
) -> None:
    """`transcript=''` is meaningful (= processed-but-empty); store as-is."""
    with transaction(conn):
        conn.execute(
            "UPDATE messages SET transcript=?, status='mm_done' WHERE msg_id=?",
            (transcript, msg_id),
        )


def transcribe_voice_for_msg(conn: sqlite3.Connection, msg_id: int) -> str:
    """On-demand ASR for one voice msg, callable from outside the worker.

    Used by the agent's `read_voice` tool. Behavior:
      - if `transcript` is already set (incl. empty string for previously-
        processed-but-empty rows), return it as-is — no re-work
      - else load the file and run ASR; persist via `_save_transcript`
      - file missing or engine error → persist '' and return '' so the
        worker doesn't compete to retry it later

    Raises ValueError if the row doesn't exist or isn't a voice row.
    """
    row = conn.execute(
        "SELECT msg_id, type, media_path, transcript FROM messages WHERE msg_id=?",
        (msg_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"msg_id {msg_id} not found")
    if row["type"] != "voice":
        raise ValueError(f"msg_id {msg_id} is type {row['type']!r}, not 'voice'")
    if row["transcript"] is not None:
        return row["transcript"]
    if not row["media_path"]:
        _save_transcript(conn, msg_id, "")
        return ""
    path = _resolve_path(row["media_path"])
    if not path.exists():
        _save_transcript(conn, msg_id, "")
        return ""
    try:
        from .asr import transcribe_voice
        text = transcribe_voice(path)
    except Exception as e:
        logger.exception("on-demand ASR failed on msg_id={} ({})", msg_id, path)
        _save_transcript(conn, msg_id, "")
        return ""
    _save_transcript(conn, msg_id, text)
    return text


def _process_one(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Returns a short status string for logging. Mutates DB."""
    msg_id = row["msg_id"]
    kind = row["type"]
    path = _resolve_path(row["media_path"])
    if not path.exists():
        _save_transcript(conn, msg_id, "")
        return f"missing-file:{path}"

    try:
        if kind == "image":
            from .ocr import ocr_image
            text = ocr_image(path)
        elif kind == "voice":
            from .asr import transcribe_voice
            text = transcribe_voice(path)
        else:
            return f"unsupported:{kind}"
    except ImportError as e:
        # Environmental: missing engine deps (e.g. rapidocr_onnxruntime not
        # installed, faster-whisper missing CUDA). Don't poison transcript —
        # the row may transcribe fine after `uv sync` rebuilds the venv.
        # Re-raise so run_mm_worker's outer loop can back off / surface.
        logger.error(
            "mm worker: engine unavailable ({}); leaving msg_id={} for retry "
            "after deps are restored", e, msg_id,
        )
        raise
    except Exception as e:
        logger.exception("worker: {} failed on msg_id={} ({})", kind, msg_id, path)
        _save_transcript(conn, msg_id, "")  # mark done so we don't loop
        return f"engine-error:{e}"

    _save_transcript(conn, msg_id, text)
    if not text:
        return "empty"
    preview = text.replace("\n", " ")[:60]
    return f"ok len={len(text)} {preview!r}"


_ENGINE_BACKOFF_SLEEP = 600.0  # 10 min after ImportError → don't hammer logs


def run_mm_worker() -> None:
    """Long-running loop. Idle-sleeps when the queue's empty; Ctrl+C to stop."""
    init_db()
    settings.ensure_dirs()
    logger.info(
        "mm worker: batch={} idle_sleep={}s data_dir={}",
        _BATCH, _IDLE_SLEEP, settings.data_dir,
    )

    with get_conn() as conn:
        try:
            while True:
                rows = _next_batch(conn)
                if not rows:
                    time.sleep(_IDLE_SLEEP)
                    continue
                try:
                    for row in rows:
                        status = _process_one(conn, row)
                        logger.info(
                            "msg_id={} type={} → {}",
                            row["msg_id"], row["type"], status,
                        )
                except ImportError:
                    # Engine deps absent — unprocessed rows stay queued.
                    # Sleep longer so the operator's stderr / process.log
                    # isn't spammed every batch while they `uv sync`.
                    logger.warning(
                        "mm worker: engine deps missing; sleeping {}s before retry",
                        _ENGINE_BACKOFF_SLEEP,
                    )
                    time.sleep(_ENGINE_BACKOFF_SLEEP)
        except KeyboardInterrupt:
            logger.info("mm worker stopped by user")
