from typing import Protocol

from flgo.config import Settings
from flgo.media_workflows.types import (
    MediaWorkflowProviderConfig,
    MediaWorkflowRequest,
    MediaWorkflowResult,
)
from flgo.model_providers.types import ProviderCapability


class MediaWorkflowProvider(Protocol):
    name: str
    provider_config: MediaWorkflowProviderConfig

    async def submit(self, request: MediaWorkflowRequest) -> MediaWorkflowResult:
        """Submit a media or workflow job."""

    async def poll(self, request_id: str, job_id: str) -> MediaWorkflowResult:
        """Poll a previously submitted media or workflow job."""


class MediaWorkflowRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, MediaWorkflowProvider] = {}

    def register(self, provider: MediaWorkflowProvider) -> None:
        self._providers[_normalize(provider.name)] = provider

    def has(self, provider_name: str) -> bool:
        return _normalize(provider_name) in self._providers

    def names(self) -> list[str]:
        return sorted(self._providers)

    def configs(self) -> list[MediaWorkflowProviderConfig]:
        return [self._providers[name].provider_config for name in self.names()]

    def resolve(
        self,
        provider_name: str,
        capability: ProviderCapability,
    ) -> MediaWorkflowProvider:
        name = _normalize(provider_name)
        try:
            provider = self._providers[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "none"
            raise ValueError(
                f"Media workflow provider '{provider_name}' is not available. "
                f"Available: {available}"
            ) from exc
        if capability not in provider.provider_config.capabilities:
            raise ValueError(
                f"Media workflow provider '{provider.name}' does not support "
                f"required capability: {capability.value}"
            )
        return provider

    async def submit(
        self,
        provider_name: str,
        request: MediaWorkflowRequest,
    ) -> MediaWorkflowResult:
        provider = self.resolve(provider_name, request.capability)
        return await provider.submit(request)


def build_media_workflow_registry(settings: Settings) -> MediaWorkflowRegistry:
    registry = MediaWorkflowRegistry()
    for config in media_workflow_provider_configs(settings):
        registry.register(_ConfiguredUnavailableMediaWorkflowProvider(config))
    return registry


def media_workflow_provider_configs(settings: Settings) -> list[MediaWorkflowProviderConfig]:
    configs: list[MediaWorkflowProviderConfig] = []
    if settings.seedance_api_key.get_secret_value() and settings.seedance_base_url.strip():
        configs.append(
            MediaWorkflowProviderConfig(
                name="seedance",
                base_url=settings.seedance_base_url.strip(),
                api_key=settings.seedance_api_key,
                default_model=settings.seedance_model.strip(),
                capabilities=[ProviderCapability.VIDEO_GENERATION],
            )
        )
    if settings.comfyui_base_url.strip():
        configs.append(
            MediaWorkflowProviderConfig(
                name="comfyui",
                base_url=settings.comfyui_base_url.strip(),
                api_key=settings.comfyui_api_key,
                capabilities=[
                    ProviderCapability.IMAGE_GENERATION,
                    ProviderCapability.VIDEO_GENERATION,
                    ProviderCapability.WORKFLOW_EXECUTION,
                ],
            )
        )
    if settings.coze_api_key.get_secret_value() and settings.coze_base_url.strip():
        configs.append(
            MediaWorkflowProviderConfig(
                name="coze",
                base_url=settings.coze_base_url.strip(),
                api_key=settings.coze_api_key,
                capabilities=[ProviderCapability.WORKFLOW_EXECUTION],
            )
        )
    if settings.dify_api_key.get_secret_value() and settings.dify_base_url.strip():
        configs.append(
            MediaWorkflowProviderConfig(
                name="dify",
                base_url=settings.dify_base_url.strip(),
                api_key=settings.dify_api_key,
                capabilities=[ProviderCapability.WORKFLOW_EXECUTION],
            )
        )
    return configs


class _ConfiguredUnavailableMediaWorkflowProvider:
    def __init__(self, config: MediaWorkflowProviderConfig) -> None:
        self.name = config.name
        self.provider_config = config

    async def submit(self, request: MediaWorkflowRequest) -> MediaWorkflowResult:
        raise NotImplementedError(
            f"Media workflow provider '{self.name}' is configured but its real API "
            "adapter is not implemented yet."
        )

    async def poll(self, request_id: str, job_id: str) -> MediaWorkflowResult:
        raise NotImplementedError(
            f"Media workflow provider '{self.name}' is configured but its real API "
            "adapter is not implemented yet."
        )


def _normalize(value: str) -> str:
    return value.strip().lower()
