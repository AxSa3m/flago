import asyncio
import base64
import logging
from time import perf_counter
from typing import Any

from google import genai
from google.genai import types

from flago.config import Settings
from flago.logging import redact
from flago.model_providers.echo import EchoModelProvider
from flago.model_providers.prompt import (
    build_assistant_model_request,
    build_assistant_prompt,
    render_text_prompt,
)
from flago.model_providers.types import (
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
    ProviderCapability,
    ProviderConfig,
    ProviderKind,
)
from flago.models import AssistantRequest, AssistantResponse

logger = logging.getLogger(__name__)

__all__ = ["EchoModelProvider", "GeminiProvider"]


class GeminiProvider:
    def __init__(self, settings: Settings) -> None:
        self.provider_config = _gemini_provider_config(settings)
        api_key = (
            self.provider_config.api_key.get_secret_value()
            if self.provider_config.api_key
            else ""
        )
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required")
        http_options: dict[str, Any] = {}
        if self.provider_config.base_url:
            http_options["base_url"] = self.provider_config.base_url
        if self.provider_config.http_proxy:
            http_options["client_args"] = {"proxy": self.provider_config.http_proxy}
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(**http_options) if http_options else None,
        )
        self.name = self.provider_config.name
        self.kind = self.provider_config.kind
        self.capabilities = list(self.provider_config.capabilities)
        self.model = self.provider_config.default_model
        self.timeout = self.provider_config.timeout_seconds
        self.max_output_tokens = self.provider_config.max_output_tokens
        self.thinking_budget = _int_or_none(self.provider_config.extra.get("thinking_budget"))

    async def generate(self, request: AssistantRequest) -> AssistantResponse:
        model_request = build_assistant_model_request(
            request,
            provider=self.name,
            model=self.model,
            max_output_tokens=self.max_output_tokens,
        )
        model_response = await self.generate_model(model_request)
        return AssistantResponse(text=model_response.text)

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        model = request.model or self.model
        contents: Any = _gemini_contents(request)
        started_at = perf_counter()
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.models.generate_content,
                    model=model,
                    contents=contents,
                    config=self._generate_config(request),
                ),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - convert SDK-specific failures for caller
            detail = redact(str(exc))
            raise RuntimeError(f"Gemini request failed: {detail}") from exc
        latency_ms = int((perf_counter() - started_at) * 1000)
        usage = _gemini_usage(response)
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
            text=response.text or "",
            provider=self.name,
            model=model,
            usage=usage,
            tool_calls=_gemini_tool_calls(response),
            raw={"latency_ms": latency_ms},
        )

    def _generate_config(self, request: ModelRequest) -> types.GenerateContentConfig:
        thinking_config = None
        if self.thinking_budget is not None:
            thinking_config = types.ThinkingConfig(
                thinking_budget=self.thinking_budget,
                include_thoughts=False,
            )
        config: dict[str, Any] = {}
        output_limit = request.max_output_tokens or self.max_output_tokens
        if output_limit is not None:
            config["max_output_tokens"] = output_limit
        if request.temperature is not None:
            config["temperature"] = request.temperature
        gemini_tools = _gemini_tools(request.tools)
        if gemini_tools:
            config["tools"] = gemini_tools
            tool_config = _gemini_tool_config(request.tool_choice, request.tools)
            if tool_config is not None:
                config["tool_config"] = tool_config
        if thinking_config is not None:
            config["thinking_config"] = thinking_config
        return types.GenerateContentConfig(**config)

    @staticmethod
    def _build_prompt(request: AssistantRequest) -> str:
        return build_assistant_prompt(request)


def _gemini_provider_config(settings: Settings) -> ProviderConfig:
    return ProviderConfig(
        name="gemini",
        kind=ProviderKind.GEMINI,
        default_model=settings.gemini_model,
        api_key=settings.gemini_api_key,
        base_url=settings.gemini_base_url,
        http_proxy=settings.gemini_http_proxy,
        timeout_seconds=settings.gemini_timeout_seconds,
        max_output_tokens=settings.effective_model_max_output_tokens(
            settings.gemini_max_output_tokens,
        ),
        capabilities=[
            ProviderCapability.CHAT,
            ProviderCapability.JSON_OUTPUT,
            ProviderCapability.TOOL_CALLING,
            ProviderCapability.VISION_INPUT,
            ProviderCapability.AUDIO_INPUT,
            ProviderCapability.VIDEO_INPUT,
            ProviderCapability.LONG_CONTEXT,
        ],
        extra={"thinking_budget": settings.gemini_thinking_budget},
    )


def _gemini_usage(response: Any) -> ModelUsage | None:
    usage_metadata = getattr(response, "usage_metadata", None)
    if usage_metadata is None:
        return None
    input_tokens = _int_or_none(
        getattr(usage_metadata, "prompt_token_count", None)
        or getattr(usage_metadata, "input_token_count", None)
    )
    output_tokens = _int_or_none(
        getattr(usage_metadata, "candidates_token_count", None)
        or getattr(usage_metadata, "output_token_count", None)
    )
    total_tokens = _int_or_none(getattr(usage_metadata, "total_token_count", None))
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _gemini_tools(tool_specs: list[dict[str, Any]]) -> list[types.Tool] | None:
    declarations: list[types.FunctionDeclaration] = []
    for spec in tool_specs:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        description = str(spec.get("description") or "").strip()
        parameters = spec.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        declarations.append(
            types.FunctionDeclaration(
                name=name,
                description=description or None,
                parameters_json_schema=parameters,
            )
        )
    if not declarations:
        return None
    return [types.Tool(function_declarations=declarations)]


def _gemini_tool_config(
    tool_choice: str | None,
    tool_specs: list[dict[str, Any]],
) -> types.ToolConfig | None:
    if not tool_specs or not tool_choice:
        return None
    normalized = tool_choice.strip().lower()
    if normalized == "auto":
        mode = types.FunctionCallingConfigMode.AUTO
    elif normalized == "none":
        mode = types.FunctionCallingConfigMode.NONE
    elif normalized in {"required", "any"}:
        mode = types.FunctionCallingConfigMode.ANY
    else:
        return None
    return types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(mode=mode)
    )


def _gemini_tool_calls(response: Any) -> list[ModelToolCall]:
    raw_calls = getattr(response, "function_calls", None)
    if raw_calls is None:
        raw_calls = _function_calls_from_candidates(response)
    if not isinstance(raw_calls, list):
        return []
    calls: list[ModelToolCall] = []
    for index, raw_call in enumerate(raw_calls):
        name = getattr(raw_call, "name", None)
        if not isinstance(name, str) or not name.strip():
            continue
        args = getattr(raw_call, "args", None)
        calls.append(
            ModelToolCall(
                id=str(getattr(raw_call, "id", None) or f"gemini-call-{index + 1}"),
                name=name.strip(),
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return calls


def _function_calls_from_candidates(response: Any) -> list[Any]:
    calls: list[Any] = []
    candidates = getattr(response, "candidates", None)
    if not isinstance(candidates, list):
        return calls
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None)
        if not isinstance(parts, list):
            continue
        for part in parts:
            function_call = getattr(part, "function_call", None)
            if function_call is not None:
                calls.append(function_call)
    return calls


def _gemini_contents(request: ModelRequest) -> str | list[types.Part]:
    if not any(message.attachments for message in request.messages):
        return render_text_prompt(request)

    parts: list[types.Part] = [types.Part.from_text(text=render_text_prompt(request))]
    for message in request.messages:
        for attachment in message.attachments:
            if attachment.type not in {"image", "audio", "video"}:
                continue
            parts.append(
                types.Part.from_bytes(
                    data=base64.b64decode(attachment.data_base64),
                    mime_type=attachment.media_type,
                )
            )
    return parts


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
