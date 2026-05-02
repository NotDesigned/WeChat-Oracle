"""Image OCR via rapidocr-onnxruntime (PP-OCRv4 ONNX models, Chinese-strong).

Single-instance, lazy-loaded. The ONNX runtime keeps the model in memory once
warmed; the first image takes ~3–5s, subsequent ones ~0.3–1.5s on CPU.

Output is a single string: detected lines joined by `\\n` in reading order
(rapidocr returns them roughly top-to-bottom). Empty string when no text was
found — caller treats that as a permanent "no content" marker so the worker
doesn't retry.
"""
from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any

from loguru import logger


_engine: Any = None
_lock = Lock()


def _get_engine() -> Any:
    """Lazy-load the OCR engine. Thread-safe (worker is single-threaded today
    but cheap to be defensive)."""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR
                logger.info("loading RapidOCR (first call only — model warm-up)")
                _engine = RapidOCR()
    return _engine


def ocr_image(path: Path) -> str:
    """Run OCR on `path`. Returns text (empty string if nothing recognised).

    Raises FileNotFoundError if the file is missing — caller decides whether
    to mark mm_done with empty transcript or leave it unprocessed.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    engine = _get_engine()
    result, _elapsed = engine(str(path))
    # result is list[ [box, text, score] ] or None.
    if not result:
        return ""
    lines = [item[1].strip() for item in result if item and len(item) >= 2 and item[1]]
    return "\n".join(lines)
