import asyncio
import logging
from time import perf_counter
from typing import Any, Protocol

from flgo.config import Settings
from flgo.logging import redact
from flgo.model_providers.catalog import ModelCatalogItem, build_model_catalog
from flgo.model_providers.claude import ClaudeProvider, claude_provider_config
from flgo.model_providers.echo import EchoModelProvider
from flgo.model_providers.openai_compatible import (
    OpenAICompatibleProvider,
    openai_compatible_provider_configs,
)
from flgo.model_providers.prompt import build_assistant_model_request
from flgo.model_providers.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapability,
    ProviderConfig,
)
from flgo.models import AssistantRequest, AssistantResponse, AuditEventType

logger = logging.getLogger(__name__)


class ModelAuditRecorder(Protocol):
    async def audit(
        self,
        event_type: AuditEventType,
        *,
        actor_id: str | None = None,
        action_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Persist metadata-only model audit events."""


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
        audit_recorder: ModelAuditRecorder | None = None,
        provider_concurrency_limit: int = 0,
    ) -> None:
        self.registry = registry
        self.default_provider = normalize_provider_name(default_provider)
        self.default_model = normalize_model_name(default_model)
        self.audit_recorder = audit_recorder
        self.provider_concurrency_limit = max(provider_concurrency_limit, 0)
        self._provider_semaphores: dict[str, asyncio.Semaphore] = {
            name: asyncio.Semaphore(self.provider_concurrency_limit)
            for name in registry.names()
            if self.provider_concurrency_limit > 0
        }
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
        return await self._generate_with_guards(provider, resolved_request)

    def _default_model_for(self, provider: UnifiedModelProvider) -> str:
        if normalize_provider_name(provider.name) == self.default_provider and self.default_model:
            return self.default_model
        return provider.provider_config.default_model

    async def _generate_with_guards(
        self,
        provider: UnifiedModelProvider,
        request: ModelRequest,
    ) -> ModelResponse:
        started_at = perf_counter()
        try:
            semaphore = self._provider_semaphores.get(provider.name)
            if semaphore is None:
                response = await asyncio.wait_for(
                    provider.generate_model(request),
                    timeout=provider.provider_config.timeout_seconds,
                )
            else:
                async with semaphore:
                    response = await asyncio.wait_for(
                        provider.generate_model(request),
                        timeout=provider.provider_config.timeout_seconds,
                    )
        except Exception as exc:
            await self._audit_model_failure(provider, request, exc)
            raise
        latency_ms = _response_latency_ms(response, started_at)
        await self._audit_model_success(provider, request, response, latency_ms)
        return response

    async def _audit_model_success(
        self,
        provider: UnifiedModelProvider,
        request: ModelRequest,
        response: ModelResponse,
        latency_ms: int,
    ) -> None:
        if self.audit_recorder is None:
            return
        detail: dict[str, Any] = {
            "provider": provider.name,
            "model": request.model,
            "latency_ms": latency_ms,
            "message_count": len(request.messages),
            "max_output_tokens": request.max_output_tokens,
            "required_capabilities": [
                capability.value for capability in request.required_capabilities
            ],
        }
        if response.usage is not None:
            detail["usage"] = response.usage.model_dump(exclude_none=True)
        await self.audit_recorder.audit(
            AuditEventType.MODEL_GENERATED,
            actor_id=_request_actor_id(request),
            detail=detail,
        )

    async def _audit_model_failure(
        self,
        provider: UnifiedModelProvider,
        request: ModelRequest,
        exc: Exception,
    ) -> None:
        if self.audit_recorder is None:
            return
        await self.audit_recorder.audit(
            AuditEventType.ERROR,
            actor_id=_request_actor_id(request),
            detail={
                "kind": "model_provider_failed",
                "provider": provider.name,
                "model": request.model,
                "error_type": type(exc).__name__,
                "error": redact(str(exc)),
            },
        )


def build_provider_registry(settings: Settings) -> ModelProviderRegistry:
    from flgo.gemini.provider import GeminiProvider

    registry = ModelProviderRegistry()
    if settings.gemini_api_key.get_secret_value():
        registry.register(GeminiProvider(settings))
    for config in openai_compatible_provider_configs(settings):
        registry.register(OpenAICompatibleProvider(config))
    claude_config = claude_provider_config(settings)
    if claude_config is not None:
        registry.register(ClaudeProvider(claude_config))
    registry.register(EchoModelProvider())
    return registry


def build_model_router(
    settings: Settings,
    audit_recorder: ModelAuditRecorder | None = None,
) -> ModelRouter:
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
        audit_recorder=audit_recorder,
        provider_concurrency_limit=settings.model_provider_concurrency_limit,
    )


def normalize_provider_name(value: str) -> str:
    return value.strip().lower()


def normalize_model_name(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _response_latency_ms(response: ModelResponse, started_at: float) -> int:
    raw_latency = response.raw.get("latency_ms")
    if isinstance(raw_latency, int):
        return raw_latency
    if isinstance(raw_latency, str):
        try:
            return int(raw_latency)
        except ValueError:
            pass
    return int((perf_counter() - started_at) * 1000)


def _request_actor_id(request: ModelRequest) -> str | None:
    actor_id = request.metadata.get("actor_id")
    return str(actor_id) if actor_id else None
