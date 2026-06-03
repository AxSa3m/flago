from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, SecretStr


class ModelMessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ProviderCapability(StrEnum):
    CHAT = "chat"
    TOOL_CALLING = "tool_calling"
    JSON_OUTPUT = "json_output"
    STREAMING = "streaming"
    VISION_INPUT = "vision_input"
    LONG_CONTEXT = "long_context"
    IMAGE_GENERATION = "image_generation"
    VIDEO_GENERATION = "video_generation"
    WORKFLOW_EXECUTION = "workflow_execution"


class ProviderKind(StrEnum):
    GEMINI = "gemini"
    OPENAI_COMPATIBLE = "openai_compatible"
    CLAUDE = "claude"
    MEDIA_WORKFLOW = "media_workflow"
    ECHO = "echo"


class ModelMessage(BaseModel):
    role: ModelMessageRole
    content: str
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    request_id: str
    messages: list[ModelMessage]
    provider: str | None = None
    model: str | None = None
    required_capabilities: list[ProviderCapability] = Field(
        default_factory=lambda: [ProviderCapability.CHAT]
    )
    max_output_tokens: int | None = None
    temperature: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderConfig(BaseModel):
    name: str
    kind: ProviderKind
    default_model: str
    api_key: SecretStr | None = None
    base_url: str | None = None
    http_proxy: str | None = None
    timeout_seconds: float = 60.0
    max_output_tokens: int | None = None
    capabilities: list[ProviderCapability] = Field(
        default_factory=lambda: [ProviderCapability.CHAT]
    )
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ProviderError(BaseModel):
    provider: str
    message: str
    code: str | None = None
    retryable: bool = False
    raw_status_code: int | None = None


class ModelResponse(BaseModel):
    text: str
    provider: str | None = None
    model: str | None = None
    usage: ModelUsage | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
