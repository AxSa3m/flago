from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from flgo.model_providers.types import ProviderCapability


class MediaJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MediaAsset(BaseModel):
    type: str
    media_type: str
    url: str | None = None
    data_base64: str | None = None
    filename: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MediaWorkflowRequest(BaseModel):
    request_id: str
    capability: ProviderCapability
    prompt: str = ""
    input_assets: list[MediaAsset] = Field(default_factory=list)
    workflow: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MediaWorkflowResult(BaseModel):
    request_id: str
    provider: str
    status: MediaJobStatus
    job_id: str | None = None
    assets: list[MediaAsset] = Field(default_factory=list)
    text: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MediaWorkflowProviderConfig(BaseModel):
    name: str
    base_url: str | None = None
    api_key: SecretStr | None = None
    default_model: str = ""
    capabilities: list[ProviderCapability] = Field(default_factory=list)
    timeout_seconds: float = 300.0
    poll_interval_seconds: float = 5.0
    extra: dict[str, Any] = Field(default_factory=dict)
