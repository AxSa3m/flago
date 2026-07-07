import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from flago.config import Settings
from flago.model_providers import ModelMessage, ModelMessageRole, ModelRequest
from flago.model_providers.claude import ClaudeProvider
from flago.model_providers.registry import build_model_router
from flago.model_providers.types import ProviderCapability, ProviderConfig, ProviderKind


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_posts_messages_request() -> None:
    route = respx.post("https://api.anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Claude 回复"}],
                "usage": {"input_tokens": 12, "output_tokens": 6},
                "stop_reason": "end_turn",
            },
        )
    )
    provider = ClaudeProvider(
        ProviderConfig(
            name="claude",
            kind=ProviderKind.CLAUDE,
            default_model="claude-test",
            api_key=SecretStr("sk-ant-test-secret"),
            base_url="https://api.anthropic.test",
            timeout_seconds=30,
            max_output_tokens=64,
            capabilities=[ProviderCapability.CHAT],
            extra={"anthropic_version": "2023-06-01"},
        )
    )
    request = ModelRequest(
        request_id="req-1",
        provider="claude",
        model="claude-test",
        temperature=0.2,
        max_output_tokens=32,
        messages=[
            ModelMessage(role=ModelMessageRole.SYSTEM, content="system"),
            ModelMessage(role=ModelMessageRole.USER, content="user"),
        ],
    )

    response = await provider.generate_model(request)

    assert response.text == "Claude 回复"
    assert response.provider == "claude"
    assert response.model == "claude-test"
    assert response.usage is not None
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 6
    assert response.usage.total_tokens == 18
    payload = json.loads(route.calls.last.request.content)
    assert payload == {
        "model": "claude-test",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "user"}],
        "system": "system",
        "temperature": 0.2,
    }
    headers = route.calls.last.request.headers
    assert headers["x-api-key"] == "sk-ant-test-secret"
    assert headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
@respx.mock
async def test_claude_provider_reports_redacted_http_errors() -> None:
    respx.post("https://api.anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            401,
            json={
                "type": "error",
                "error": {
                    "type": "authentication_error",
                    "message": "bad key sk-ant-test-secret",
                },
            },
        )
    )
    provider = ClaudeProvider(
        ProviderConfig(
            name="claude",
            kind=ProviderKind.CLAUDE,
            default_model="claude-test",
            api_key=SecretStr("sk-ant-test-secret"),
            base_url="https://api.anthropic.test",
            max_output_tokens=64,
        )
    )

    with pytest.raises(RuntimeError) as exc_info:
        await provider.generate_model(
            ModelRequest(
                request_id="req-1",
                provider="claude",
                model="claude-test",
                messages=[ModelMessage(role=ModelMessageRole.USER, content="hello")],
            )
        )

    assert "HTTP 401" in str(exc_info.value)
    assert "sk-ant-test-secret" not in str(exc_info.value)
    assert "***REDACTED***" in str(exc_info.value)


def test_build_model_router_registers_configured_claude_provider() -> None:
    settings = Settings(
        env="test",
        default_provider="claude",
        gemini_api_key="",
        anthropic_api_key="sk-ant-test-secret",
        anthropic_base_url="https://api.anthropic.test",
        anthropic_model="claude-test",
        anthropic_max_output_tokens=512,
    )

    router = build_model_router(settings)

    assert router.default_provider == "claude"
    assert "claude" in router.registry.names()
    claude = router.registry.resolve("claude")
    assert claude.provider_config.default_model == "claude-test"
    assert claude.provider_config.max_output_tokens == 512
    assert ProviderCapability.CHAT in claude.provider_config.capabilities
    assert ProviderCapability.TOOL_CALLING not in claude.provider_config.capabilities

