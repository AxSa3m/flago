from types import SimpleNamespace

import pytest

from flgo.config import Settings
from flgo.gemini.provider import GeminiProvider
from flgo.model_providers import (
    ModelAttachment,
    ModelMessage,
    ModelMessageRole,
    ModelRequest,
    ProviderCapability,
)
from flgo.models import (
    AssistantRequest,
    ConversationType,
    ResourceReadResult,
    ResourceRef,
    ResourceType,
)


def test_prompt_includes_authorized_resource_content() -> None:
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="总结这个文档",
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

    prompt = GeminiProvider._build_prompt(request)

    assert "Agent observations" in prompt
    assert "kind: resource_result" in prompt
    assert "课程大纲" in prompt
    assert "这个课程包含三个项目。" in prompt


def test_prompt_warns_not_to_treat_sparse_sheet_as_empty() -> None:
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="这个表格里有什么？",
    )

    prompt = GeminiProvider._build_prompt(request)

    assert "不要因为周边空白行列判断表格为空" in prompt


def test_prompt_prefers_bitable_records_over_metadata_warnings() -> None:
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="这个多维表里有什么？",
    )

    prompt = GeminiProvider._build_prompt(request)

    assert "即使字段或视图元数据有警告，也应优先根据记录摘录回答" in prompt


def test_prompt_keeps_writeback_paused_on_read_only_branch() -> None:
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="写一段自我介绍并写入文档",
    )

    prompt = GeminiProvider._build_prompt(request)

    assert "当前写回功能已关闭" in prompt
    assert "不要输出写回 JSON" in prompt
    assert "可复制的草稿或操作建议" in prompt
    assert "flgo_writeback" not in prompt


def test_prompt_enables_confirmed_writeback_when_runtime_switch_is_on() -> None:
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="把测试文字写入文档",
        writeback_enabled=True,
    )

    prompt = GeminiProvider._build_prompt(request)

    assert "写回功能已启用" in prompt
    assert "确认卡片由系统自动生成" in prompt
    assert "不要询问用户是否生成确认卡片" in prompt
    assert "不能声称已经写入" in prompt
    assert "写回功能已关闭" not in prompt


def test_gemini_provider_exposes_unified_provider_config() -> None:
    settings = Settings(
        env="test",
        gemini_api_key="test-key",
        gemini_model="gemini-test",
        gemini_base_url="https://gemini.local",
        gemini_http_proxy="http://127.0.0.1:7890",
        gemini_thinking_budget=0,
    )

    provider = GeminiProvider(settings)

    assert provider.name == "gemini"
    assert provider.model == "gemini-test"
    assert ProviderCapability.CHAT in provider.capabilities
    assert ProviderCapability.VISION_INPUT in provider.capabilities
    assert ProviderCapability.AUDIO_INPUT in provider.capabilities
    assert ProviderCapability.VIDEO_INPUT in provider.capabilities
    assert provider.provider_config.default_model == "gemini-test"
    assert provider.provider_config.base_url == "https://gemini.local"
    assert provider.provider_config.http_proxy == "http://127.0.0.1:7890"
    assert provider.provider_config.extra["thinking_budget"] == 0


@pytest.mark.asyncio
async def test_gemini_provider_generates_unified_model_response() -> None:
    settings = Settings(env="test", gemini_api_key="test-key", gemini_model="gemini-test")
    provider = GeminiProvider(settings)
    fake_models = _FakeGeminiModels()
    provider.client = SimpleNamespace(models=fake_models)
    request = ModelRequest(
        request_id="req-1",
        provider="gemini",
        model="gemini-test",
        max_output_tokens=32,
        messages=[
            ModelMessage(role=ModelMessageRole.SYSTEM, content="system"),
            ModelMessage(role=ModelMessageRole.USER, content="user"),
        ],
    )

    response = await provider.generate_model(request)

    assert response.text == "模型回复"
    assert response.provider == "gemini"
    assert response.model == "gemini-test"
    assert response.usage is not None
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens == 10
    assert fake_models.calls[0]["model"] == "gemini-test"
    assert fake_models.calls[0]["contents"] == "system\n\nuser"
    assert fake_models.calls[0]["config"].max_output_tokens == 32


@pytest.mark.asyncio
async def test_gemini_provider_sends_image_parts() -> None:
    settings = Settings(env="test", gemini_api_key="test-key", gemini_model="gemini-test")
    provider = GeminiProvider(settings)
    fake_models = _FakeGeminiModels()
    provider.client = SimpleNamespace(models=fake_models)
    request = ModelRequest(
        request_id="req-image",
        provider="gemini",
        model="gemini-test",
        required_capabilities=[ProviderCapability.CHAT, ProviderCapability.VISION_INPUT],
        messages=[
            ModelMessage(
                role=ModelMessageRole.USER,
                content="描述图片",
                attachments=[
                    ModelAttachment(
                        media_type="image/png",
                        data_base64="aW1hZ2U=",
                        filename="image.png",
                    )
                ],
            )
        ],
    )

    response = await provider.generate_model(request)

    assert response.text == "模型回复"
    contents = fake_models.calls[0]["contents"]
    assert isinstance(contents, list)
    assert len(contents) == 2


@pytest.mark.asyncio
async def test_gemini_provider_sends_tools_and_adapts_function_calls() -> None:
    settings = Settings(env="test", gemini_api_key="test-key", gemini_model="gemini-test")
    provider = GeminiProvider(settings)
    fake_models = _FakeGeminiModels(
        response=SimpleNamespace(
            text="",
            function_calls=[
                SimpleNamespace(
                    id="gemini-search",
                    name="search_resources",
                    args={"query": "测试文档", "limit": 3},
                )
            ],
            usage_metadata=SimpleNamespace(
                prompt_token_count=8,
                candidates_token_count=2,
                total_token_count=10,
            ),
        )
    )
    provider.client = SimpleNamespace(models=fake_models)
    request = ModelRequest(
        request_id="req-tools",
        provider="gemini",
        model="gemini-test",
        temperature=0,
        messages=[ModelMessage(role=ModelMessageRole.USER, content="帮我找测试文档")],
        tools=[
            {
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
            }
        ],
        tool_choice="auto",
    )

    response = await provider.generate_model(request)

    assert response.text == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "gemini-search"
    assert response.tool_calls[0].name == "search_resources"
    assert response.tool_calls[0].arguments == {"query": "测试文档", "limit": 3}
    config = fake_models.calls[0]["config"]
    assert config.temperature == 0
    assert config.tools
    declarations = config.tools[0].function_declarations
    assert declarations[0].name == "search_resources"
    assert declarations[0].parameters_json_schema["required"] == ["query"]
    assert config.tool_config.function_calling_config.mode.value == "AUTO"


class _FakeGeminiModels:
    def __init__(self, response: SimpleNamespace | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response

    def generate_content(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.response is not None:
            return self.response
        return SimpleNamespace(
            text="模型回复",
            usage_metadata=SimpleNamespace(
                prompt_token_count=7,
                candidates_token_count=3,
                total_token_count=10,
            ),
        )
