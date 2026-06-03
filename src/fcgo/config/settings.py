from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    env: Literal["dev", "test", "prod"] = Field(default="dev", alias="FCGO_ENV")
    log_level: str = Field(default="INFO", alias="FCGO_LOG_LEVEL")
    host: str = Field(default="127.0.0.1", alias="FCGO_HOST")
    port: int = Field(default=8000, alias="FCGO_PORT")
    base_url: str = Field(default="http://127.0.0.1:8000", alias="FCGO_BASE_URL")
    sqlite_path: Path = Field(default=Path("data/fcgo.sqlite3"), alias="FCGO_SQLITE_PATH")
    start_long_connection: bool = Field(default=False, alias="FCGO_START_LONG_CONNECTION")

    feishu_app_id: str = Field(default="", alias="FEISHU_APP_ID")
    feishu_app_secret: SecretStr = Field(default=SecretStr(""), alias="FEISHU_APP_SECRET")
    feishu_verification_token: SecretStr = Field(
        default=SecretStr(""), alias="FEISHU_VERIFICATION_TOKEN"
    )
    feishu_encrypt_key: SecretStr = Field(default=SecretStr(""), alias="FEISHU_ENCRYPT_KEY")
    feishu_bot_open_id: str = Field(default="", alias="FEISHU_BOT_OPEN_ID")
    feishu_bot_name: str = Field(default="", alias="FEISHU_BOT_NAME")
    feishu_base_url: str = Field(default="https://open.feishu.cn", alias="FEISHU_BASE_URL")
    feishu_auth_base_url: str = Field(
        default="https://accounts.feishu.cn", alias="FEISHU_AUTH_BASE_URL"
    )
    feishu_oauth_scopes: str = Field(
        default=(
            "auth:user.id:read "
            "docx:document:readonly "
            "docx:document "
            "wiki:node:read "
            "sheets:spreadsheet:readonly "
            "sheets:spreadsheet "
            "bitable:app:readonly "
            "bitable:app "
            "base:record:read "
            "base:record:create "
            "base:record:update "
            "base:record:delete "
            "base:field:read "
            "base:view:read"
        ),
        alias="FEISHU_OAUTH_SCOPES",
    )
    oauth_state_ttl_seconds: int = Field(default=600, alias="FCGO_OAUTH_STATE_TTL_SECONDS")

    default_provider: str = Field(default="gemini", alias="FCGO_DEFAULT_PROVIDER")
    default_model: str | None = Field(default=None, alias="FCGO_DEFAULT_MODEL")

    gemini_api_key: SecretStr = Field(default=SecretStr(""), alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    gemini_timeout_seconds: float = Field(default=60.0, alias="GEMINI_TIMEOUT_SECONDS")
    gemini_max_output_tokens: int = Field(default=4096, alias="GEMINI_MAX_OUTPUT_TOKENS")
    gemini_thinking_budget: int | None = Field(default=None, alias="GEMINI_THINKING_BUDGET")
    gemini_base_url: str | None = Field(default=None, alias="GEMINI_BASE_URL")
    gemini_http_proxy: str | None = Field(default=None, alias="GEMINI_HTTP_PROXY")

    openai_compatible_timeout_seconds: float = Field(
        default=60.0,
        alias="FCGO_OPENAI_COMPATIBLE_TIMEOUT_SECONDS",
    )
    openai_compatible_max_output_tokens: int = Field(
        default=4096,
        alias="FCGO_OPENAI_COMPATIBLE_MAX_OUTPUT_TOKENS",
    )
    openai_compatible_http_proxy: str | None = Field(
        default=None,
        alias="FCGO_OPENAI_COMPATIBLE_HTTP_PROXY",
    )

    openai_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="", alias="OPENAI_MODEL")

    deepseek_api_key: SecretStr = Field(default=SecretStr(""), alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        alias="DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")

    qwen_api_key: SecretStr = Field(default=SecretStr(""), alias="QWEN_API_KEY")
    qwen_base_url: str = Field(default="", alias="QWEN_BASE_URL")
    qwen_model: str = Field(default="", alias="QWEN_MODEL")

    doubao_api_key: SecretStr = Field(default=SecretStr(""), alias="DOUBAO_API_KEY")
    doubao_base_url: str = Field(default="", alias="DOUBAO_BASE_URL")
    doubao_model: str = Field(default="", alias="DOUBAO_MODEL")

    minimax_api_key: SecretStr = Field(default=SecretStr(""), alias="MINIMAX_API_KEY")
    minimax_base_url: str = Field(default="", alias="MINIMAX_BASE_URL")
    minimax_model: str = Field(default="", alias="MINIMAX_MODEL")

    max_resource_chars: int = Field(default=120_000, alias="FCGO_MAX_RESOURCE_CHARS")
    max_message_chars: int = Field(default=20_000, alias="FCGO_MAX_MESSAGE_CHARS")
    max_sheet_rows: int = Field(default=200, alias="FCGO_MAX_SHEET_ROWS")
    max_sheet_columns: int = Field(default=26, alias="FCGO_MAX_SHEET_COLUMNS")
    max_bitable_records: int = Field(default=200, alias="FCGO_MAX_BITABLE_RECORDS")
    web_timeout_seconds: float = Field(default=20.0, alias="FCGO_WEB_TIMEOUT_SECONDS")
    pending_action_ttl_seconds: int = Field(
        default=1800, alias="FCGO_PENDING_ACTION_TTL_SECONDS"
    )
    writeback_dedupe_window_seconds: int = Field(
        default=600,
        alias="FCGO_WRITEBACK_DEDUPE_WINDOW_SECONDS",
    )
    card_action_ack_timeout_seconds: float = Field(
        default=2.0,
        alias="FCGO_CARD_ACTION_ACK_TIMEOUT_SECONDS",
    )
    max_writeback_chars: int = Field(default=20_000, alias="FCGO_MAX_WRITEBACK_CHARS")
    max_writeback_cells: int = Field(default=1_000, alias="FCGO_MAX_WRITEBACK_CELLS")

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.base_url.rstrip('/')}/oauth/feishu/callback"

    @property
    def oauth_scope_list(self) -> list[str]:
        return [
            scope
            for scope in self.feishu_oauth_scopes.replace(",", " ").split()
            if scope.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
