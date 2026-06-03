import logging
from typing import Protocol

from fcgo.config import Settings
from fcgo.model_providers.catalog import ModelCatalogItem, build_model_catalog
from fcgo.model_providers.echo import EchoModelProvider
from fcgo.model_providers.openai_compatible import (
    OpenAICompatibleProvider,
    openai_compatible_provider_configs,
)
from fcgo.model_providers.prompt import build_assistant_model_request
from fcgo.model_providers.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapability,
    ProviderConfig,
)
from fcgo.models import AssistantRequest, AssistantResponse

logger = logging.getLogger(__name__)


class UnifiedModelProvider(Protocol):
    name: str
    provider_config: ProviderConfig

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        """Generate a provider-agnostic response."""


class ModelProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, UnifiedModelProvider] = {}

    def register(self, provider: UnifiedModelProvider) -> None:
        name = normalize_provider_name(provider.name)
        self._providers[name] = provider

    def has(self, provider_name: str) -> bool:
        return normalize_provider_name(provider_name) in self._providers

    def get(self, provider_name: str) -> UnifiedModelProvider:
        name = normalize_provider_name(provider_name)
        try:
            return self._providers[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "none"
            raise ValueError(
                f"Model provider '{provider_name}' is not available. Available: {available}"
            ) from exc

    def names(self) -> list[str]:
        return sorted(self._providers)

    def configs(self) -> list[ProviderConfig]:
        return [self._providers[name].provider_config for name in self.names()]

    def resolve(
        self,
        provider_name: str,
        required_capabilities: list[ProviderCapability] | None = None,
    ) -> UnifiedModelProvider:
        provider = self.get(provider_name)
        required = required_capabilities or [ProviderCapability.CHAT]
        supported = set(provider.provider_config.capabilities)
        missing = [capability for capability in required if capability not in supported]
        if missing:
            missing_names = ", ".join(capability.value for capability in missing)
            raise ValueError(
                f"Model provider '{provider.name}' does not support required "
                f"capabilities: {missing_names}"
            )
        return provider


class ModelRouter:
    def __init__(
        self,
        registry: ModelProviderRegistry,
        default_provider: str,
        default_model: str | None = None,
        catalog: list[ModelCatalogItem] | None = None,
    ) -> None:
        self.registry = registry
        self.default_provider = normalize_provider_name(default_provider)
        self.default_model = normalize_model_name(default_model)
        self.catalog = catalog or [
            ModelCatalogItem(
                provider=config.name,
                model=config.default_model,
                configured=True,
            )
            for config in registry.configs()
        ]

    async def generate(self, request: AssistantRequest) -> AssistantResponse:
        provider_name = request.model_provider or self.default_provider
        provider = self.registry.resolve(provider_name, [ProviderCapability.CHAT])
        model_request = build_assistant_model_request(
            request,
            provider=provider.name,
            model=request.model or self._default_model_for(provider),
            max_output_tokens=provider.provider_config.max_output_tokens,
        )
        model_response = await self.generate_model(model_request)
        return AssistantResponse(text=model_response.text)

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        provider_name = request.provider or self.default_provider
        provider = self.registry.resolve(provider_name, request.required_capabilities)
        resolved_request = request.model_copy(
            update={
                "provider": provider.name,
                "model": request.model or self._default_model_for(provider),
            }
        )
        return await provider.generate_model(resolved_request)

    def _default_model_for(self, provider: UnifiedModelProvider) -> str:
        if normalize_provider_name(provider.name) == self.default_provider and self.default_model:
            return self.default_model
        return provider.provider_config.default_model


def build_provider_registry(settings: Settings) -> ModelProviderRegistry:
    from fcgo.gemini.provider import GeminiProvider

    registry = ModelProviderRegistry()
    if settings.gemini_api_key.get_secret_value():
        registry.register(GeminiProvider(settings))
    for config in openai_compatible_provider_configs(settings):
        registry.register(OpenAICompatibleProvider(config))
    registry.register(EchoModelProvider())
    return registry


def build_model_router(settings: Settings) -> ModelRouter:
    registry = build_provider_registry(settings)
    default_provider = normalize_provider_name(settings.default_provider)
    if not registry.has(default_provider):
        can_use_local_echo = settings.env in {"dev", "test"} and registry.has("echo")
        if can_use_local_echo:
            logger.warning(
                "default_model_provider_unavailable provider=%s fallback=echo",
                default_provider,
            )
            default_provider = "echo"
        else:
            available = ", ".join(registry.names()) or "none"
            raise ValueError(
                f"Default model provider '{settings.default_provider}' is not available. "
                f"Available: {available}"
            )
    return ModelRouter(
        registry=registry,
        default_provider=default_provider,
        default_model=settings.default_model,
        catalog=build_model_catalog(settings),
    )


def normalize_provider_name(value: str) -> str:
    return value.strip().lower()


def normalize_model_name(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
