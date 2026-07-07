from time import perf_counter

from flago.model_providers.prompt import build_assistant_model_request
from flago.model_providers.types import (
    ModelMessageRole,
    ModelRequest,
    ModelResponse,
    ProviderCapability,
    ProviderConfig,
    ProviderKind,
)
from flago.models import AssistantRequest, AssistantResponse


class EchoModelProvider:
    """Dry-run provider used when no remote model is configured in local development."""

    def __init__(self, default_model: str = "echo") -> None:
        self.provider_config = ProviderConfig(
            name="echo",
            kind=ProviderKind.ECHO,
            default_model=default_model,
            capabilities=[ProviderCapability.CHAT],
        )
        self.name = self.provider_config.name
        self.kind = self.provider_config.kind
        self.capabilities = list(self.provider_config.capabilities)
        self.model = self.provider_config.default_model

    async def generate(self, request: AssistantRequest) -> AssistantResponse:
        model_request = build_assistant_model_request(
            request,
            provider=self.name,
            model=self.model,
        )
        model_response = await self.generate_model(model_request)
        return AssistantResponse(text=model_response.text)

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        started_at = perf_counter()
        text = _last_user_text(request) or request.messages[-1].content if request.messages else ""
        latency_ms = int((perf_counter() - started_at) * 1000)
        return ModelResponse(
            text=f"收到：{text}",
            provider=self.name,
            model=request.model or self.model,
            raw={"latency_ms": latency_ms},
        )


def _last_user_text(request: ModelRequest) -> str:
    for message in reversed(request.messages):
        if message.role == ModelMessageRole.USER:
            return message.content
    return ""
