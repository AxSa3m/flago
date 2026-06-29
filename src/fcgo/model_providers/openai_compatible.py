import json
import logging
from time import perf_counter
from typing import Any

import httpx

from fcgo.config import Settings
from fcgo.logging import redact
from fcgo.model_providers.types import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
    ProviderCapability,
    ProviderConfig,
    ProviderKind,
)

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider:
    """Provider for APIs that expose an OpenAI-compatible chat completions endpoint."""

    def __init__(self, provider_config: ProviderConfig) -> None:
        if provider_config.kind != ProviderKind.OPENAI_COMPATIBLE:
            raise ValueError("OpenAICompatibleProvider requires openai_compatible config")
        api_key = provider_config.api_key.get_secret_value() if provider_config.api_key else ""
        if not api_key:
            raise ValueError(f"{provider_config.name} API key is required")
        if not provider_config.base_url:
            raise ValueError(f"{provider_config.name} base URL is required")
        if not provider_config.default_model:
            raise ValueError(f"{provider_config.name} model is required")
        self.provider_config = provider_config
        self.name = provider_config.name
        self.kind = provider_config.kind
        self.capabilities = list(provider_config.capabilities)
        self.model = provider_config.default_model
        self.timeout = provider_config.timeout_seconds
        self.max_output_tokens = provider_config.max_output_tokens
        self.url = _chat_completions_url(provider_config.base_url)

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        model = request.model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_message_payload(message) for message in request.messages],
        }
        max_tokens = request.max_output_tokens or self.max_output_tokens
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [_tool_payload(tool) for tool in request.tools]
        if request.tool_choice:
            payload["tool_choice"] = request.tool_choice
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
            tool_calls=_response_tool_calls(response_json),
            raw={"latency_ms": latency_ms},
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
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if response.status_code >= 400:
                raise RuntimeError(_error_message(self.name, response))
            return dict(response.json())
        except httpx.HTTPError as exc:
            detail = redact(str(exc))
            raise RuntimeError(f"{self.name} request failed: {detail}") from exc


def openai_compatible_provider_configs(settings: Settings) -> list[ProviderConfig]:
    specs = [
        (
            "openai",
            settings.openai_api_key,
            settings.openai_base_url,
            settings.openai_model,
        ),
        (
            "deepseek",
            settings.deepseek_api_key,
            settings.deepseek_base_url,
            settings.deepseek_model,
        ),
        (
            "qwen",
            settings.qwen_api_key,
            settings.qwen_base_url,
            settings.qwen_model,
        ),
        (
            "doubao",
            settings.doubao_api_key,
            settings.doubao_base_url,
            settings.doubao_model,
        ),
        (
            "minimax",
            settings.minimax_api_key,
            settings.minimax_base_url,
            settings.minimax_model,
        ),
    ]
    configs: list[ProviderConfig] = []
    for name, api_key, base_url, model in specs:
        if not api_key.get_secret_value() or not base_url.strip() or not model.strip():
            continue
        configs.append(
            ProviderConfig(
                name=name,
                kind=ProviderKind.OPENAI_COMPATIBLE,
                default_model=model.strip(),
                api_key=api_key,
                base_url=base_url.strip(),
                http_proxy=settings.openai_compatible_http_proxy,
                timeout_seconds=settings.openai_compatible_timeout_seconds,
                max_output_tokens=settings.effective_model_max_output_tokens(
                    settings.openai_compatible_max_output_tokens,
                ),
                capabilities=[
                    ProviderCapability.CHAT,
                    ProviderCapability.JSON_OUTPUT,
                    ProviderCapability.TOOL_CALLING,
                ],
            )
        )
    return configs


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.name:
        payload["name"] = message.name
    tool_call_id = message.metadata.get("tool_call_id")
    if message.role.value == "tool" and isinstance(tool_call_id, str) and tool_call_id:
        payload["tool_call_id"] = tool_call_id
    return payload


def _tool_payload(tool: dict[str, Any]) -> dict[str, Any]:
    name = str(tool.get("name") or "").strip()
    description = str(tool.get("description") or "").strip()
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def _response_text(response_json: dict[str, Any]) -> str:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _response_tool_calls(response_json: dict[str, Any]) -> list[ModelToolCall]:
    message = _first_choice_message(response_json)
    if message is None:
        return []
    raw_tool_calls = message.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []
    calls: list[ModelToolCall] = []
    for index, item in enumerate(raw_tool_calls):
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        calls.append(
            ModelToolCall(
                id=str(item.get("id") or f"tool-call-{index + 1}"),
                name=name.strip(),
                arguments=_tool_arguments(function.get("arguments")),
            )
        )
    return calls


def _first_choice_message(response_json: dict[str, Any]) -> dict[str, Any] | None:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    return message if isinstance(message, dict) else None


def _tool_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str) or not raw_arguments.strip():
        return {}
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _usage(raw_usage: Any) -> ModelUsage | None:
    if not isinstance(raw_usage, dict):
        return None
    return ModelUsage(
        input_tokens=_int_or_none(raw_usage.get("prompt_tokens")),
        output_tokens=_int_or_none(raw_usage.get("completion_tokens")),
        total_tokens=_int_or_none(raw_usage.get("total_tokens")),
    )


def _error_message(provider_name: str, response: httpx.Response) -> str:
    detail = response.text
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                detail = message
        elif isinstance(payload.get("message"), str):
            detail = str(payload["message"])
    return (
        f"{provider_name} request failed: HTTP {response.status_code}: "
        f"{redact(detail)}"
    )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
