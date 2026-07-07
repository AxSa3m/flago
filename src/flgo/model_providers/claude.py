import logging
from time import perf_counter
from typing import Any

import httpx

from flgo.config import Settings
from flgo.logging import redact
from flgo.model_providers.types import (
    ModelMessage,
    ModelMessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapability,
    ProviderConfig,
    ProviderKind,
)

logger = logging.getLogger(__name__)


class ClaudeProvider:
    """Provider for Anthropic Claude Messages API."""

    def __init__(self, provider_config: ProviderConfig) -> None:
        if provider_config.kind != ProviderKind.CLAUDE:
            raise ValueError("ClaudeProvider requires claude config")
        api_key = provider_config.api_key.get_secret_value() if provider_config.api_key else ""
        if not api_key:
            raise ValueError("Anthropic API key is required")
        if not provider_config.base_url:
            raise ValueError("Anthropic base URL is required")
        if not provider_config.default_model:
            raise ValueError("Anthropic model is required")
        if not provider_config.max_output_tokens:
            raise ValueError("Anthropic max output tokens is required")
        self.provider_config = provider_config
        self.name = provider_config.name
        self.kind = provider_config.kind
        self.capabilities = list(provider_config.capabilities)
        self.model = provider_config.default_model
        self.timeout = provider_config.timeout_seconds
        self.max_output_tokens = provider_config.max_output_tokens
        self.version = str(provider_config.extra.get("anthropic_version") or "2023-06-01")
        self.url = _messages_url(provider_config.base_url)

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        model = request.model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens or self.max_output_tokens,
            "messages": _message_payloads(request.messages),
        }
        system = _system_prompt(request.messages)
        if system:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        started_at = perf_counter()
        response_json = await self._post_json(payload)
        latency_ms = int((perf_counter() - started_at) * 1000)
        usage = _usage(response_json.get("usage"))
        logger.info(
            "model_provider_response provider=%s model=%s latency_ms=%s "
            "input_tokens=%s output_tokens=%s",
            self.name,
            model,
            latency_ms,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
        )
        return ModelResponse(
            text=_response_text(response_json),
            provider=self.name,
            model=model,
            usage=usage,
            raw={"latency_ms": latency_ms, "stop_reason": response_json.get("stop_reason")},
        )

    async def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = (
            self.provider_config.api_key.get_secret_value()
            if self.provider_config.api_key
            else ""
        )
        client_kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.provider_config.http_proxy:
            client_kwargs["proxy"] = self.provider_config.http_proxy
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post(
                    self.url,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": self.version,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if response.status_code >= 400:
                raise RuntimeError(_error_message(response))
            return dict(response.json())
        except httpx.HTTPError as exc:
            detail = redact(str(exc))
            raise RuntimeError(f"Claude request failed: {detail}") from exc


def claude_provider_config(settings: Settings) -> ProviderConfig | None:
    if (
        not settings.anthropic_api_key.get_secret_value()
        or not settings.anthropic_base_url.strip()
        or not settings.anthropic_model.strip()
    ):
        return None
    return ProviderConfig(
        name="claude",
        kind=ProviderKind.CLAUDE,
        default_model=settings.anthropic_model.strip(),
        api_key=settings.anthropic_api_key,
        base_url=settings.anthropic_base_url.strip(),
        http_proxy=settings.anthropic_http_proxy,
        timeout_seconds=settings.anthropic_timeout_seconds,
        max_output_tokens=settings.effective_model_max_output_tokens(
            settings.anthropic_max_output_tokens,
        ),
        capabilities=[
            ProviderCapability.CHAT,
            ProviderCapability.JSON_OUTPUT,
            ProviderCapability.LONG_CONTEXT,
        ],
        extra={"anthropic_version": settings.anthropic_version},
    )


def _messages_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1/messages"):
        return normalized
    return f"{normalized}/v1/messages"


def _system_prompt(messages: list[ModelMessage]) -> str:
    return "\n\n".join(
        message.content for message in messages if message.role == ModelMessageRole.SYSTEM
    )


def _message_payloads(messages: list[ModelMessage]) -> list[dict[str, str]]:
    payloads: list[dict[str, str]] = []
    for message in messages:
        if message.role == ModelMessageRole.SYSTEM:
            continue
        role = "assistant" if message.role == ModelMessageRole.ASSISTANT else "user"
        content = message.content
        if not content:
            continue
        if payloads and payloads[-1]["role"] == role:
            payloads[-1]["content"] += f"\n\n{content}"
        else:
            payloads.append({"role": role, "content": content})
    if not payloads:
        payloads.append({"role": "user", "content": ""})
    return payloads


def _response_text(response_json: dict[str, Any]) -> str:
    content = response_json.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _usage(raw_usage: Any) -> ModelUsage | None:
    if not isinstance(raw_usage, dict):
        return None
    input_tokens = _int_or_none(raw_usage.get("input_tokens"))
    output_tokens = _int_or_none(raw_usage.get("output_tokens"))
    total_tokens = None
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _error_message(response: httpx.Response) -> str:
    detail = response.text
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            detail = str(error["message"])
        elif isinstance(payload.get("message"), str):
            detail = str(payload["message"])
    return f"Claude request failed: HTTP {response.status_code}: {redact(detail)}"


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
