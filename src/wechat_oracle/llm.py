"""Pluggable LLM client boundary for dispatcher calls.

The dispatcher needs three shapes: plain text completion for chat-style
answers, JSON text completion for `/find`, and (optional) text+image
completion for the vision second-pass in `chat_assistant`. Provider-
specific SDK details live here so command logic stays vendor-independent.

`VisionLLM` is intentionally a separate Protocol from `LLMClient`: vision
is opt-in (off by default; chat falls back to text-only) and may use a
different provider entirely (e.g. text=DeepSeek, vision=Qwen-VL).
"""
from __future__ import annotations

import base64
from typing import Literal, Protocol

from openai import OpenAI


def _sniff_image_mime(data: bytes) -> str:
    """Detect Content-Type from image magic bytes.

    DashScope (and most vision APIs) reject mismatched MIME — e.g. PNG
    bytes labeled `image/jpeg` may be silently downsampled or rejected.
    Per DashScope docs, supported types are bmp/jpeg/png/tiff/webp/heic;
    we cover the WeChat-common five and fall back to jpeg.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1"):
        return "image/heic"
    return "image/jpeg"


JsonMode = Literal["native", "prompt"]


class LLMClient(Protocol):
    """Minimal interface consumed by dispatcher commands."""

    name: str

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.0,
    ) -> str:
        ...

    def complete_text(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        ...


class OpenAICompatLLM:
    """OpenAI-compatible `/v1/chat/completions` adapter.

    `json_mode` exists because some "compatible" relays reject OpenAI's
    response_format even though normal chat completions work.
    """

    name = "openai-compatible"

    def __init__(self, *, api_key: str, endpoint: str, json_mode: JsonMode = "native"):
        self._client = OpenAI(api_key=api_key, base_url=endpoint)
        self._json_mode = json_mode

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.0,
    ) -> str:
        kwargs: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if self._json_mode == "native":
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or "{}"

    def complete_text(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        kwargs: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        resp = self._client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()


def build_llm_client(
    *,
    provider: str,
    api_key: str,
    endpoint: str,
    json_mode: str,
) -> LLMClient:
    if not api_key:
        raise RuntimeError("WO_LLM_API_KEY is empty; set it in .env")
    if provider != "openai-compatible":
        raise RuntimeError(
            f"Unsupported WO_LLM_PROVIDER={provider!r}; only 'openai-compatible' is implemented"
        )
    if json_mode not in ("native", "prompt"):
        raise RuntimeError("WO_LLM_JSON_MODE must be 'native' or 'prompt'")
    return OpenAICompatLLM(
        api_key=api_key,
        endpoint=endpoint,
        json_mode=json_mode,  # type: ignore[arg-type]
    )


# --- vision (optional second-pass for image-heavy chat questions) ----------


class VisionLLM(Protocol):
    """Text + image completion. `images` is raw bytes; the adapter handles
    base64 / data URI / multipart wrapping per provider."""

    name: str

    def complete_with_images(
        self,
        *,
        model: str,
        system: str,
        user: str,
        images: list[bytes],
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        ...


class OpenAICompatVisionLLM:
    """OpenAI-compatible vision adapter — works with Qwen-VL (DashScope
    `compatible-mode/v1`), GPT-4o, and any provider that accepts the
    standard `image_url` content shape with a base64 data URI."""

    name = "openai-compatible-vision"

    def __init__(self, *, api_key: str, endpoint: str):
        self._client = OpenAI(api_key=api_key, base_url=endpoint)

    def complete_with_images(
        self,
        *,
        model: str,
        system: str,
        user: str,
        images: list[bytes],
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        content: list[dict[str, object]] = [{"type": "text", "text": user}]
        for img in images:
            mime = _sniff_image_mime(img)
            b64 = base64.b64encode(img).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        kwargs: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        resp = self._client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()


def build_vision_client(
    *,
    provider: str,
    api_key: str,
    endpoint: str,
) -> VisionLLM | None:
    """Returns None when api_key is empty — vision is off, callers fall
    back to text-only chat. Non-empty key + unknown provider raises."""
    if not api_key:
        return None
    if provider != "openai-compatible":
        raise RuntimeError(
            f"Unsupported WO_VISION_PROVIDER={provider!r}; only 'openai-compatible' is implemented"
        )
    return OpenAICompatVisionLLM(api_key=api_key, endpoint=endpoint)
