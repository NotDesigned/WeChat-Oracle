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

    # WeFlow HTTP API (used by `ingest live`); enable "HTTP API 服务" in WeFlow settings.
    weflow_base_url: str = "http://127.0.0.1:5031"
    weflow_token: str = ""
    weflow_poll_interval: float = 30.0  # deprecated; live uses SSE now

    # Dispatcher: bot's @-mention nickname (its 群昵称 in the watched group).
    # Required for `wechat-oracle dispatcher` to recognize commands.
    bot_name: str = ""

    # DeepSeek API for the dispatcher's LLM step. OpenAI-compatible endpoint.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"

    # Dispatcher loop tunables.
    dispatcher_poll_interval: float = 3.0
    dispatcher_candidate_limit: int = 500   # /find candidates per call
    dispatcher_context_chat: int = 5000     # @<bot> free-text context window

    # Send the dispatcher's result back into the WeChat group via wx4py (UI
    # automation). False = local-only (stdout + log). Requires WeChat main
    # window to be visible (not minimized to tray).
    reply: bool = True

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

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
