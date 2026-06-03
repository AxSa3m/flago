from fcgo.config import Settings
from fcgo.model_providers import (
    ModelMessageRole,
    ProviderCapability,
    build_assistant_model_request,
    build_assistant_prompt,
)
from fcgo.model_providers.echo import EchoModelProvider
from fcgo.model_providers.registry import (
    ModelProviderRegistry,
    ModelRouter,
    build_model_router,
)
from fcgo.models import (
    AssistantRequest,
    ConversationType,
    ResourceReadResult,
    ResourceRef,
    ResourceType,
)


def test_build_assistant_model_request_uses_system_and_user_messages() -> None:
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="总结这个文档",
    )

    model_request = build_assistant_model_request(
        request,
        provider="gemini",
        model="gemini-2.5-flash",
        max_output_tokens=1024,
    )

    assert model_request.provider == "gemini"
    assert model_request.model == "gemini-2.5-flash"
    assert model_request.max_output_tokens == 1024
    assert model_request.required_capabilities == [ProviderCapability.CHAT]
    assert [message.role for message in model_request.messages] == [
        ModelMessageRole.SYSTEM,
        ModelMessageRole.USER,
    ]
    assert model_request.metadata["conversation_type"] == "private"


def test_build_assistant_prompt_includes_resource_context_without_provider_sdk_details() -> None:
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="这个文档说了什么？",
        resource_urls=[ref.url],
        resource_refs=[ref],
        resource_results=[
            ResourceReadResult(
                ref=ref,
                title="课程大纲",
                content="这个课程包含三个项目。",
            )
        ],
    )

    prompt = build_assistant_prompt(request)

    assert "你是 FCGO" in prompt
    assert "用户输入：这个文档说了什么？" in prompt
    assert "已按用户授权读取的资源内容" in prompt
    assert "课程大纲" in prompt
    assert "这个课程包含三个项目。" in prompt


def test_model_router_uses_configured_default_provider_and_model() -> None:
    registry = ModelProviderRegistry()
    registry.register(EchoModelProvider())
    router = ModelRouter(registry, default_provider="echo", default_model="echo-custom")
    request = build_assistant_model_request(
        AssistantRequest(
            actor_id="ou_user",
            conversation_id="chat-1",
            conversation_type=ConversationType.PRIVATE,
            text="测试",
        ),
        provider=None,
        model=None,
    )

    resolved_provider = router.registry.resolve(router.default_provider)

    assert resolved_provider.name == "echo"
    assert router.default_model == "echo-custom"
    assert router._default_model_for(resolved_provider) == "echo-custom"
    assert request.provider is None
    assert request.model is None


def test_model_router_rejects_provider_without_required_capability() -> None:
    registry = ModelProviderRegistry()
    registry.register(EchoModelProvider())
    router = ModelRouter(registry, default_provider="echo")

    try:
        router.registry.resolve("echo", [ProviderCapability.IMAGE_GENERATION])
    except ValueError as exc:
        assert "image_generation" in str(exc)
    else:
        raise AssertionError("expected unsupported capability to fail")


def test_build_model_router_falls_back_to_echo_for_local_dev_without_gemini_key() -> None:
    settings = Settings(
        env="test",
        gemini_api_key="",
        deepseek_api_key="",
        default_provider="gemini",
    )

    router = build_model_router(settings)

    assert router.default_provider == "echo"
    assert router.registry.names() == ["echo"]


def test_build_model_router_raises_for_unavailable_provider() -> None:
    settings = Settings(env="prod", gemini_api_key="", default_provider="openai")

    try:
        build_model_router(settings)
    except ValueError as exc:
        assert "Default model provider 'openai' is not available" in str(exc)
    else:
        raise AssertionError("expected unavailable provider to fail")
