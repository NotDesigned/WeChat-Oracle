"""Voice transcription via faster-whisper (CTranslate2 backend).

Single-instance, lazy-loaded. Default model is `small` — good Chinese quality
without large RAM footprint (~500 MB). User can override via WO_WHISPER_MODEL.

WeChat voice exports from WeFlow are typically .wav (per the API docs). If
faster-whisper can't decode a file (raises an error), the caller catches and
records '' so it doesn't retry forever; if you discover the file is actually
silk and want to support it, add a `pilk` decode pre-step here.
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Any

from loguru import logger


_model: Any = None
_lock = Lock()


def _get_model() -> Any:
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from faster_whisper import WhisperModel
                size = os.environ.get("WO_WHISPER_MODEL", "small")
                logger.info("loading faster-whisper model={} (first call only)", size)
                _model = WhisperModel(size, device="cpu", compute_type="int8")
    return _model


def transcribe_voice(path: Path, language: str | None = "zh") -> str:
    """Transcribe one voice file. Returns concatenated segment text."""
    if not path.exists():
        raise FileNotFoundError(path)
    model = _get_model()
    segments, _info = model.transcribe(
        str(path),
        language=language,
        # vad_filter trims long silences typical of WeChat voices.
        vad_filter=True,
        # beam_size=1 is fastest; beam=5 is default. Voice messages are short
        # so accuracy gain from larger beams is small here.
        beam_size=1,
    )
    return "".join(seg.text for seg in segments).strip()
