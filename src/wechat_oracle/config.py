"""All `WO_*` runtime configuration.

Single source of truth for env-var defaults. `Settings()` is instantiated
once at import (see bottom of file) and importable as `settings` everywhere.
Values come from `.env` in the project root, plus any `WO_*` env vars
overriding it.

When adding a field: also update README.md「配置参考」table — they are paired
in CLAUDE.md「易漂移点 F3」 and the doc-sync hook will remind you.
"""
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("data"))
    db_path: Path = Field(default=Path("data/wechat-oracle.db"))
    media_dir: Path = Field(default=Path("data/media"))

    # Group display names (live) or group_ids (backfill) to ingest. Empty = all groups.
    # Accepts either a JSON list or a plain comma-separated string in the env var.
    # NoDecode stops pydantic-settings from JSON-parsing first; the validator below handles both.
    groups: Annotated[list[str], NoDecode] = Field(default_factory=list)

    log_level: str = "INFO"
    wx4py_log_level: str = "WARNING"

    # WeFlow HTTP API (used by `ingest live`); enable "HTTP API 服务" in WeFlow settings.
    weflow_base_url: str = "http://127.0.0.1:5031"
    weflow_token: str = ""

    # Dispatcher: bot's @-mention nickname (its 群昵称 in the watched group).
    # Required for `wechat-oracle dispatcher` to recognize commands.
    bot_name: str = ""

    # Bot's own wxid. Optional — when empty, dispatcher auto-discovers it from
    # the messages table (looks for the most recent row where sender_display
    # matches WO_BOT_NAME). Discovery only succeeds after WeFlow SSE has
    # echoed at least one of the bot's own messages back into the table.
    # Set this manually to skip the discovery delay (find it once with
    # `SELECT sender_wxid FROM messages WHERE sender_display='<bot_name>' LIMIT 1`
    # after the first reply, or copy from WeChat client settings).
    # When unknown, the reply-to-bot trigger silently degrades; mention
    # and probability triggers still work.
    bot_wxid: str = ""

    # LLM API for dispatcher calls. The endpoint must expose an OpenAI-compatible
    # `/chat/completions` API; include `/v1` if the provider requires it.
    llm_provider: str = "openai-compatible"
    llm_api_key: str = ""
    llm_endpoint: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-pro"
    llm_json_mode: str = "native"  # native=response_format, prompt=prompt-only JSON

    # Dispatcher loop tunables.
    dispatcher_poll_interval: float = 3.0
    dispatcher_worker_threads: int = 4       # global parallel workers; wx4py send is serialized separately
    dispatcher_candidate_limit: int = 500   # /find candidates per call
    dispatcher_context_chat: int = 2500     # legacy candidate cap for summary-style paths

    # LLM output caps. `llm_max_tokens` is the fallback; specialized values let
    # long-context chat/summaries breathe while keeping short utility commands cheap.
    # Memory-write paths (Phase B / lurk) need their own bigger budget because the
    # update_group_memory tool call wraps multi-KB notes_text as a JSON arg, which
    # easily blows a 5K cap mid-string and produces "Unterminated string" tool errors.
    llm_max_tokens: int = 5000
    llm_chat_max_tokens: int | None = None
    llm_sum_max_tokens: int | None = None
    llm_short_max_tokens: int | None = None
    llm_write_max_tokens: int | None = 10_000

    # Vision LLM — optional second-pass for `@<bot>` chat when the text model
    # asks to see original images via `<NEED_IMAGES>` sentinel. Empty api_key
    # disables; chat then runs text-only (transcript / [图片] markers only).
    # Default endpoint/model target Qwen-VL via DashScope's OpenAI-compatible
    # mode; any vendor accepting `image_url` content blocks works.
    vision_provider: str = "openai-compatible"
    vision_api_key: str = ""
    vision_endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_model: str = "qwen-vl-plus"
    vision_max_images: int = 3      # hard cap per request; trims model over-asks
    vision_max_tokens: int | None = 800

    @property
    def chat_max_tokens(self) -> int:
        return self.llm_chat_max_tokens or self.llm_max_tokens

    @property
    def sum_max_tokens(self) -> int:
        return self.llm_sum_max_tokens or self.llm_max_tokens

    @property
    def short_max_tokens(self) -> int:
        return self.llm_short_max_tokens or self.llm_max_tokens

    @property
    def write_max_tokens(self) -> int:
        return self.llm_write_max_tokens or self.llm_max_tokens

    # Agent loop (multi-turn tool-calling chat path). Triggers are classified
    # cheaply in dispatcher: direct @, quote-reply to bot, or optional
    # probability wakeups.
    agent_base_probability: float = 0.25       # per-message ambient wake chance
    agent_proactive_mode: str = "reactive"     # off/reactive/proactive probability posture
    agent_cooldown_seconds: int = 30           # min seconds between bot's own utterances per group
    agent_max_steps: int = 8                   # Phase A read-only loop cap
    agent_reflect_max_steps: int = 3           # Phase B write-only loop cap
    agent_reflection_enabled: bool = True      # off → skip Phase B entirely
    agent_personas_dir: Path = Field(default=Path("data/personas"))
    agent_recent_context_chat: int = 100       # initial recent-msg window for Phase A system prompt
    agent_memory_max_chars: int = 100_000      # group_memory hard cap; agent must compact when full
    agent_max_tool_calls_per_run: int = 20      # Phase A total tool-call budget
    agent_max_tool_calls_per_step: int = 4      # Phase A per-LLM-turn tool-call budget
    agent_max_image_reads_per_run: int = 2      # expensive read_image budget
    agent_max_voice_reads_per_run: int = 2      # expensive read_voice budget

    # Lurk: bot silently reads a watermarked batch of new messages, may use
    # history tools for older context, and decides whether to update
    # group_memory / persona_drift. It never sends a reply.
    agent_lurk_enabled: bool = False          # opt-in background scheduler in dispatcher
    agent_lurk_interval_seconds: int = 1800   # how often dispatcher scans due groups
    agent_lurk_min_new_messages: int = 20     # auto-lurk only after this many new msgs
    agent_lurk_recent_msgs: int = 100          # max new msgs per lurk run
    agent_lurk_max_steps: int = 4              # tool-call rounds for lurk reflection

    # OpenClaw agent runtime — the recommended backend (see WO_AGENT_BACKEND
    # below, added in a later phase). Lets the bot use a Claude subscription
    # via OpenClaw's local gateway instead of paying per-token API rates.
    # Gateway must have its OpenAI-compatible /v1/chat/completions endpoint
    # enabled. Empty token = backend unusable; smoke-test with
    # `wechat-oracle openclaw ping`.
    openclaw_gateway_url: str = "http://127.0.0.1:18789"
    openclaw_token: str = ""
    openclaw_agent_id: str = "wechat-bot"
    openclaw_timeout_seconds: float = 300.0

    # Which agent backend dispatcher uses for chat-trigger turns:
    #   native    — in-process Phase A + Phase B with tools (default; works
    #               with just an LLM API key, no extra component to install)
    #   openclaw  — delegate the whole loop to OpenClaw's wechat-bot agent
    #               via /v1/chat/completions (requires WO_OPENCLAW_*; recommended
    #               for production because of subscription pricing)
    # In openclaw mode, mention/free-chat, slash-command text/JSON completions,
    # and lurk reflection all go through the OpenClaw gateway.
    agent_backend: str = "native"

    # Send the dispatcher's result back into the WeChat group. False = local-
    # only (stdout + log). True = use the backend below.
    reply: bool = True

    # Reply backend choice. See replier.py for trade-offs.
    #   wx4py  — UI automation. Requires Windows + WeChat main window visible.
    #   stdout — No-op. Equivalent to reply=False.
    # (Tencent iLink Bot was prototyped + rejected; can't deliver group msgs.
    #  See README "实验记录" if you're tempted to try again.)
    reply_backend: str = "wx4py"
    reply_mention_policy: str = "explicit"  # always/explicit/never group @ policy

    @field_validator("groups", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            import json
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                return json.loads(s)
            return [item.strip() for item in s.split(",") if item.strip()]
        return v

    @field_validator("agent_proactive_mode")
    @classmethod
    def _validate_agent_proactive_mode(cls, v: str) -> str:
        mode = (v or "reactive").strip().lower()
        if mode not in {"off", "reactive", "proactive"}:
            raise ValueError("WO_AGENT_PROACTIVE_MODE must be one of: off, reactive, proactive")
        return mode

    @field_validator("reply_mention_policy")
    @classmethod
    def _validate_reply_mention_policy(cls, v: str) -> str:
        policy = (v or "explicit").strip().lower()
        if policy not in {"always", "explicit", "never"}:
            raise ValueError("WO_REPLY_MENTION_POLICY must be one of: always, explicit, never")
        return policy

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)



settings = Settings()


def reload_settings() -> Settings:
    """Reload `.env` / WO_* values into the shared settings object.

    Many modules import the `settings` object directly, so replacing the global
    would leave those references stale. Mutating the existing object keeps
    long-running supervisor features, such as the TUI config editor, coherent.
    """
    fresh = Settings()
    for name in Settings.model_fields:
        setattr(settings, name, getattr(fresh, name))
    return settings
