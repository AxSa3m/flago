import pytest

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
from fcgo.model_providers.types import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderKind,
)
from fcgo.models import (
    AssistantRequest,
    AuditEventType,
    ChatContextMessage,
    ConversationType,
    MemoryItem,
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

    assert "你是 小智" in prompt
    assert "Agent observations" in prompt
    assert "当前用户消息" in prompt
    assert "这个文档说了什么？" in prompt
    assert "kind: resource_result" in prompt
    assert "资源 1: 课程大纲" in prompt
    assert "课程大纲" in prompt
    assert "这个课程包含三个项目。" in prompt


def test_build_assistant_prompt_includes_assistant_profile() -> None:
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我总结",
        assistant_name="小飞",
        assistant_profile="简洁直接，擅长整理飞书文档。",
    )

    prompt = build_assistant_prompt(request)

    assert "你是 小飞" in prompt
    assert "你的语言风格和任务角色简介：简洁直接，擅长整理飞书文档。" in prompt


def test_build_assistant_prompt_includes_budgeted_chat_context() -> None:
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="继续推进",
        chat_context_messages=[
            ChatContextMessage(
                message_id="om-1",
                sender_id="ou_user",
                text="上文说先做 UXS-25。",
                created_at="2026-06-10T09:00:00+00:00",
            )
        ],
        chat_context_summary="- 旧消息摘要：此前决定先做上下文默认开启。",
        chat_context_omitted_count=2,
    )

    prompt = build_assistant_prompt(request)

    assert "kind: chat_summary" in prompt
    assert "persisted_as_long_term_memory: False" in prompt
    assert "旧消息摘要" in prompt
    assert "kind: recent_chat" in prompt
    assert "message_count: 1" in prompt
    assert "上文说先做 UXS-25。" in prompt
    assert "另有 2 条" not in prompt
    assert "预算限制" not in prompt


def test_build_assistant_prompt_includes_user_memory_observation() -> None:
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="按我的偏好总结",
        memory_items=[
            MemoryItem(
                id="memory-1",
                subject_id="ou_user",
                kind="偏好",
                content="输出尽量用表格",
                source="user",
                created_at="2026-06-10T09:00:00+00:00",
                updated_at="2026-06-10T09:00:00+00:00",
            )
        ],
    )

    prompt = build_assistant_prompt(request)

    assert "kind: user_memory" in prompt
    assert "raw_chat_text_stored: False" in prompt
    assert "- 偏好: 输出尽量用表格" in prompt


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


def test_test_env_adds_default_model_output_limit_without_provider_config() -> None:
    settings = Settings(
        env="test",
        gemini_api_key="test-key",
        gemini_max_output_tokens=None,
    )

    router = build_model_router(settings)
    gemini = router.registry.resolve("gemini")

    assert gemini.provider_config.max_output_tokens == 1024


def test_prod_env_does_not_add_default_model_output_limit() -> None:
    settings = Settings(
        env="prod",
        gemini_api_key="test-key",
        gemini_max_output_tokens=None,
    )

    router = build_model_router(settings)
    gemini = router.registry.resolve("gemini")

    assert gemini.provider_config.max_output_tokens is None


def test_configured_model_output_limit_overrides_env_default() -> None:
    settings = Settings(
        env="prod",
        gemini_api_key="test-key",
        gemini_max_output_tokens=2048,
    )

    router = build_model_router(settings)
    gemini = router.registry.resolve("gemini")

    assert gemini.provider_config.max_output_tokens == 2048


@pytest.mark.asyncio
async def test_model_router_audits_generation_metadata_without_prompt_text() -> None:
    registry = ModelProviderRegistry()
    provider = RecordingUnifiedProvider()
    registry.register(provider)
    audit = RecordingModelAudit()
    router = ModelRouter(
        registry,
        default_provider="mock",
        audit_recorder=audit,
        provider_concurrency_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="这是不应进入审计的用户正文",
    )

    response = await router.generate(request)

    assert response.text == "模型回复"
    assert provider.requests[0].max_output_tokens is None
    assert audit.events == [
        (
            AuditEventType.MODEL_GENERATED,
            "ou_user",
            {
                "provider": "mock",
                "model": "mock-model",
                "latency_ms": 12,
                "message_count": 2,
                "max_output_tokens": None,
                "required_capabilities": ["chat"],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )
    ]
    assert "这是不应进入审计的用户正文" not in str(audit.events)


@pytest.mark.asyncio
async def test_model_router_audits_redacted_provider_failure_without_prompt_text() -> None:
    registry = ModelProviderRegistry()
    provider = FailingUnifiedProvider()
    registry.register(provider)
    audit = RecordingModelAudit()
    router = ModelRouter(registry, default_provider="mock", audit_recorder=audit)
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="正文不要进审计",
    )

    with pytest.raises(RuntimeError):
        await router.generate(request)

    assert audit.events[0][0] == AuditEventType.ERROR
    assert audit.events[0][1] == "ou_user"
    detail = audit.events[0][2]
    assert detail["kind"] == "model_provider_failed"
    assert detail["provider"] == "mock"
    assert detail["model"] == "mock-model"
    assert detail["error_type"] == "RuntimeError"
    assert "sk-test-secret" not in str(detail)
    assert "***REDACTED***" in str(detail)
    assert "正文不要进审计" not in str(detail)


class RecordingUnifiedProvider:
    name = "mock"

    def __init__(self) -> None:
        self.provider_config = ProviderConfig(
            name="mock",
            kind=ProviderKind.ECHO,
            default_model="mock-model",
            timeout_seconds=1,
            capabilities=[ProviderCapability.CHAT],
        )
        self.requests: list[ModelRequest] = []

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            text="模型回复",
            provider="mock",
            model=request.model,
            usage=ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14),
            raw={"latency_ms": 12},
        )


class FailingUnifiedProvider(RecordingUnifiedProvider):
    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        raise RuntimeError("provider failed with key sk-test-secret")


class RecordingModelAudit:
    def __init__(self) -> None:
        self.events: list[tuple[AuditEventType, str | None, dict[str, object]]] = []

    async def audit(
        self,
        event_type: AuditEventType,
        *,
        actor_id: str | None = None,
        action_id: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        self.events.append((event_type, actor_id, detail or {}))
