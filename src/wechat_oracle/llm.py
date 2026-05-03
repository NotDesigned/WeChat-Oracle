"""Pluggable LLM client boundary for dispatcher calls.

The dispatcher needs only two shapes: plain text completion for chat-style
answers, and JSON text completion for `/find`. Keep provider-specific SDK
details here so command logic stays independent from any one vendor.
"""
from __future__ import annotations

from typing import Literal, Protocol

from openai import OpenAI


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
