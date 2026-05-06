"""Pluggable LLM client boundary for dispatcher calls.

The dispatcher needs four shapes:
  - plain text completion (`complete_text`) for /ask / /sum
  - JSON text completion (`complete_json`) for /find
  - tool-calling completion (`complete_with_tools`) for the @<bot> chat
    agent loop (`agent/runtime.py`)
  - text + image completion (`VisionLLM.complete_with_images`) for
    /explain & /ask single-pass image reads, and the agent's `read_image`
    tool

Provider-specific SDK details live here so command and agent logic stay
vendor-independent.

`VisionLLM` is intentionally a separate Protocol from `LLMClient`:
vision is opt-in (off by default; agent's read_image raises a clean
ToolError when unconfigured) and may use a different provider entirely
(e.g. text=DeepSeek, vision=Qwen-VL).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
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

    def complete_with_tools(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> "ToolingResponse":
        kwargs: dict[str, object] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        tcs_raw = getattr(msg, "tool_calls", None) or []
        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments_json=tc.function.arguments or "",
            )
            for tc in tcs_raw
        ]
        return ToolingResponse(
            content=msg.content,
            tool_calls=tool_calls,
            assistant_message=_coerce_assistant_message(msg),
        )


@dataclass(frozen=True)
class OpenClawChatResponse:
    content: str
    usage: dict[str, Any]
    raw: dict[str, Any]


class OpenClawChatCompletions:
    """Small shared client for OpenClaw's OpenAI-compatible gateway."""

    name = "openclaw-completion"

    def __init__(self, *, gateway_url: str, token: str, agent_id: str):
        self._url = f"{gateway_url.rstrip('/')}/v1/chat/completions"
        self._token = token
        self._model = f"openclaw/{agent_id}"
        self.agent_id = agent_id

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int | None = None,
        label: str = "",
    ) -> OpenClawChatResponse:
        """POST /v1/chat/completions to the configured gateway.

        Every roundtrip is recorded to `data/openclaw.log` (JSONL) — full
        request payload + full response body + duration + ok flag. `label`
        tags the entry (e.g. "agent-chat", "lurk", "ping") so you can grep
        one call site at a time. Auth headers are never logged.
        """
        import time as _time
        from .config import settings
        from .log_utils import append_openclaw_audit
        if not self._token:
            raise RuntimeError("WO_OPENCLAW_TOKEN is empty; set it before using WO_AGENT_BACKEND=openclaw")
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        audit_path = settings.data_dir / "openclaw.log"
        started = _time.time()
        try:
            resp = httpx.post(
                self._url,
                headers={"Authorization": f"Bearer {self._token}"},
                json=payload,
                timeout=180.0,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            snippet = e.response.text[:600] if e.response is not None else ""
            error_msg = (
                f"OpenClaw gateway HTTP {e.response.status_code if e.response is not None else '?'}: {snippet}"
            )
            err_body: dict[str, Any] | None
            try:
                err_body = e.response.json() if e.response is not None else None
            except Exception:
                err_body = {"raw_text": snippet}
            append_openclaw_audit(
                audit_path,
                label=label or "<unlabeled>",
                request=payload,
                response=err_body,
                duration_s=_time.time() - started,
                ok=False,
                error=error_msg,
            )
            raise RuntimeError(error_msg) from e
        except httpx.HTTPError as e:
            error_msg = f"OpenClaw gateway request failed: {e}"
            append_openclaw_audit(
                audit_path,
                label=label or "<unlabeled>",
                request=payload,
                response=None,
                duration_s=_time.time() - started,
                ok=False,
                error=error_msg,
            )
            raise RuntimeError(error_msg) from e

        body = resp.json()
        choices = body.get("choices") if isinstance(body, dict) else None
        content = ""
        if choices:
            message = choices[0].get("message") or {}
            raw_content = message.get("content") or ""
            content = raw_content if isinstance(raw_content, str) else str(raw_content)
        usage = body.get("usage") if isinstance(body, dict) else None

        append_openclaw_audit(
            audit_path,
            label=label or "<unlabeled>",
            request=payload,
            response=body if isinstance(body, dict) else {"raw_text": str(body)[:1000]},
            duration_s=_time.time() - started,
            ok=True,
        )

        return OpenClawChatResponse(
            content=content,
            usage=usage if isinstance(usage, dict) else {},
            raw=body if isinstance(body, dict) else {},
        )


class OpenClawCompletionLLM:
    """Route dispatcher-level text/JSON completions through OpenClaw."""

    name = "openclaw-completion"

    def __init__(
        self,
        *,
        gateway_url: str,
        token: str,
        agent_id: str,
        delegate: Any | None = None,
    ):
        self._client = OpenClawChatCompletions(
            gateway_url=gateway_url, token=token, agent_id=agent_id,
        )
        self._delegate = delegate

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.0,
    ) -> str:
        return self._complete(system=system, user=user, temperature=temperature) or "{}"

    def complete_text(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        return self._complete(system=system, user=user, temperature=temperature, max_tokens=max_tokens).strip()

    def complete_with_tools(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> "ToolingResponse":
        if self._delegate is None:
            raise RuntimeError(
                "OpenClawCompletionLLM does not expose native tool-calling; "
                "route agent/lurk turns through OpenClaw backend instead"
            )
        return self._delegate.complete_with_tools(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )

    def _complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int | None,
    ) -> str:
        return self._client.complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            label="slash-command",
        ).content


# --- tool-calling (multi-turn agent loop) ----------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One function-call request emitted by the model in a tool-calling turn."""
    id: str
    name: str
    arguments_json: str  # raw JSON string the model produced; runtime parses


@dataclass(frozen=True)
class ToolingResponse:
    """Result of one `complete_with_tools` call.

    `assistant_message` is the exact dict the runtime should append to the
    `messages` list before sending tool-result turns back — preserves any
    provider-specific fields (id, refusals, ...) we don't want to model.
    """
    content: str | None
    tool_calls: list[ToolCall]
    assistant_message: dict[str, Any]


class ToolingLLM(Protocol):
    """Tool-calling extension of LLMClient. Separate Protocol so providers can
    opt in independently (some OpenAI-compatible relays reject `tools=[...]`).
    """

    def complete_with_tools(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> ToolingResponse:
        ...


def _coerce_assistant_message(msg: Any) -> dict[str, Any]:
    """Normalize the OpenAI SDK's message object into a plain dict the agent
    runtime can re-feed verbatim. Tool calls keep their string `arguments`
    payload so the model sees the exact text it produced earlier.

    Thinking-mode providers (DeepSeek-V4, Qwen-Thinking, etc.) emit a
    `reasoning_content` field alongside `content`. Their server REQUIRES we
    echo it on the next turn — missing it = HTTP 400
    "reasoning_content must be passed back to the API". The OpenAI SDK
    doesn't have it on its typed schema, so check both attribute access
    and the pydantic model_extra fallback.
    """
    out: dict[str, Any] = {"role": "assistant", "content": msg.content}
    rc = getattr(msg, "reasoning_content", None)
    if rc is None:
        extra = getattr(msg, "model_extra", None) or {}
        rc = extra.get("reasoning_content") if isinstance(extra, dict) else None
    if rc:
        out["reasoning_content"] = rc
    tcs = getattr(msg, "tool_calls", None) or []
    if tcs:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "",
                },
            }
            for tc in tcs
        ]
    return out


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
