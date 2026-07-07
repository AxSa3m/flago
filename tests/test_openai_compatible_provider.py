import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from flago.config import Settings
from flago.model_providers import ModelMessage, ModelMessageRole, ModelRequest
from flago.model_providers.openai_compatible import OpenAICompatibleProvider
from flago.model_providers.registry import build_model_router
from flago.model_providers.types import ProviderCapability, ProviderConfig, ProviderKind


@pytest.mark.asyncio
@respx.mock
async def test_openai_compatible_provider_posts_chat_completion_request() -> None:
    route = respx.post("https://api.example.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "兼容模型回复"}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 5,
                    "total_tokens": 16,
                },
            },
        )
    )
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            name="openai",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            default_model="chat-test",
            api_key=SecretStr("sk-test-openai-compatible"),
            base_url="https://api.example.test/v1",
            max_output_tokens=64,
            capabilities=[ProviderCapability.CHAT],
        )
    )
    request = ModelRequest(
        request_id="req-1",
        provider="openai",
        model="chat-test",
        temperature=0.2,
        messages=[
            ModelMessage(role=ModelMessageRole.SYSTEM, content="system"),
            ModelMessage(role=ModelMessageRole.USER, content="user"),
        ],
    )

    response = await provider.generate_model(request)

    assert response.text == "兼容模型回复"
    assert response.provider == "openai"
    assert response.model == "chat-test"
    assert response.usage is not None
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 5
    assert response.usage.total_tokens == 16
    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == "chat-test"
    assert payload["max_tokens"] == 64
    assert payload["temperature"] == 0.2
    assert payload["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert route.calls.last.request.headers["Authorization"] == (
        "Bearer sk-test-openai-compatible"
    )


@pytest.mark.asyncio
@respx.mock
async def test_openai_compatible_provider_adapts_native_tool_calls() -> None:
    route = respx.post("https://api.example.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-search",
                                    "type": "function",
                                    "function": {
                                        "name": "search_resources",
                                        "arguments": "{\"query\":\"测试文档\",\"limit\":3}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )
    )
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            name="deepseek",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            default_model="deepseek-test",
            api_key=SecretStr("sk-test-openai-compatible"),
            base_url="https://api.example.test/v1",
            capabilities=[ProviderCapability.CHAT, ProviderCapability.TOOL_CALLING],
        )
    )
    request = ModelRequest(
        request_id="req-tools",
        provider="deepseek",
        model="deepseek-test",
        messages=[ModelMessage(role=ModelMessageRole.USER, content="帮我找测试文档")],
        tools=[
            {
                "name": "search_resources",
                "description": "Search resources.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                },
            }
        ],
        tool_choice="auto",
    )

    response = await provider.generate_model(request)

    assert response.text == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call-search"
    assert response.tool_calls[0].name == "search_resources"
    assert response.tool_calls[0].arguments == {"query": "测试文档", "limit": 3}
    payload = json.loads(route.calls.last.request.content)
    assert payload["tool_choice"] == "auto"
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search_resources",
                "description": "Search resources.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        }
    ]


@pytest.mark.asyncio
@respx.mock
async def test_openai_compatible_provider_reports_redacted_http_errors() -> None:
    respx.post("https://api.example.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": "bad key sk-test-openai-compatible"}},
        )
    )
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            name="deepseek",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            default_model="deepseek-test",
            api_key=SecretStr("sk-test-openai-compatible"),
            base_url="https://api.example.test/v1",
        )
    )

    with pytest.raises(RuntimeError) as exc_info:
        await provider.generate_model(
            ModelRequest(
                request_id="req-1",
                provider="deepseek",
                model="deepseek-test",
                messages=[ModelMessage(role=ModelMessageRole.USER, content="hello")],
            )
        )

    assert "HTTP 401" in str(exc_info.value)
    assert "sk-test-openai-compatible" not in str(exc_info.value)
    assert "***REDACTED***" in str(exc_info.value)


def test_build_model_router_registers_configured_openai_compatible_providers() -> None:
    settings = Settings(
        env="test",
        default_provider="deepseek",
        gemini_api_key="",
        openai_api_key="sk-test-openai",
        openai_base_url="https://api.openai.test/v1",
        openai_model="openai-test",
        deepseek_api_key="sk-test-deepseek",
        deepseek_base_url="https://api.deepseek.test/v1",
        deepseek_model="deepseek-test",
        qwen_api_key="sk-test-qwen",
        qwen_base_url="https://api.qwen.test/v1",
        qwen_model="qwen-test",
        doubao_api_key="sk-test-doubao",
        doubao_base_url="https://api.doubao.test/v1",
        doubao_model="doubao-test",
        minimax_api_key="sk-test-minimax",
        minimax_base_url="https://api.minimax.test/v1",
        minimax_model="minimax-test",
    )

    router = build_model_router(settings)

    assert router.default_provider == "deepseek"
    assert router.registry.names() == [
        "deepseek",
        "doubao",
        "echo",
        "minimax",
        "openai",
        "qwen",
    ]
    deepseek = router.registry.resolve("deepseek")
    assert deepseek.provider_config.default_model == "deepseek-test"
    assert ProviderCapability.TOOL_CALLING in deepseek.provider_config.capabilities
