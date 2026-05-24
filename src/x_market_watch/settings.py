from __future__ import annotations

from pathlib import Path

from pydantic import Field, HttpUrl, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    x_bearer_token: str = Field(..., alias="X_BEARER_TOKEN")
    x_auth_mode: str = Field("bearer", alias="X_AUTH_MODE")
    x_api_key: str | None = Field(None, alias="X_API_KEY")
    x_api_key_secret: str | None = Field(None, alias="X_API_KEY_SECRET")
    x_access_token: str | None = Field(None, alias="X_ACCESS_TOKEN")
    x_access_token_secret: str | None = Field(None, alias="X_ACCESS_TOKEN_SECRET")
    x_list_id: str = Field(..., alias="X_LIST_ID")
    x_api_base: HttpUrl = Field("https://api.x.com/2", alias="X_API_BASE")
    x_max_results: int = Field(50, ge=1, le=100, alias="X_MAX_RESULTS")
    x_max_pages: int = Field(1, ge=1, le=8, alias="X_MAX_PAGES")

    llm_api_key: str = Field(..., alias="LLM_API_KEY")
    llm_api_base: HttpUrl = Field("https://api.openai.com/v1", alias="LLM_API_BASE")
    llm_model: str = Field("gpt-4o-mini", alias="LLM_MODEL")
    llm_timeout_seconds: PositiveInt = Field(60, alias="LLM_TIMEOUT_SECONDS")
    llm_min_importance: int = Field(3, ge=1, le=5, alias="LLM_MIN_IMPORTANCE")

    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(..., alias="TELEGRAM_CHAT_ID")

    poll_interval_seconds: PositiveInt = Field(300, alias="POLL_INTERVAL_SECONDS")
    state_path: Path = Field(Path("data/state.json"), alias="STATE_PATH")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
