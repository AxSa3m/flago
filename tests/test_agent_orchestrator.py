import json
from typing import Any

import pytest

from fcgo.agent.orchestrator import AgentOrchestrator
from fcgo.model_providers.types import ModelRequest, ModelResponse, ModelToolCall
from fcgo.models import (
    AssistantRequest,
    ChatContextMessage,
    ConversationType,
    ResourceReadResult,
    ResourceRef,
    ResourceType,
    ToolName,
    WriteActionType,
    WritebackConfirmationMode,
)


class FakeAgentModel:
    def __init__(self, outputs: list[str | ModelResponse]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        output = self.outputs.pop(0)
        if isinstance(output, ModelResponse):
            return output
        return ModelResponse(text=output, provider="fake", model="fake-model")


class FakeSearcher:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, actor_id: str, *, limit: int) -> list[ResourceRef]:
        self.queries.append(query)
        return [
            ResourceRef(
                type=ResourceType.FEISHU_DOC,
                url="https://my.feishu.cn/docx/docx1",
                title="情感调优",
                token="docx1",
                source_kind="search:drive",
            )
        ][:limit]


class QueryMatchingSearcher:
    def __init__(self, match_query: str) -> None:
        self.match_query = match_query
        self.queries: list[str] = []

    async def search(self, query: str, actor_id: str, *, limit: int) -> list[ResourceRef]:
        self.queries.append(query)
        if query != self.match_query:
            return []
        return [
            ResourceRef(
                type=ResourceType.FEISHU_DOC,
                url="https://my.feishu.cn/docx/test-doc",
                title="测试文档",
                token="test-doc",
                source_kind="search:drive",
            )
        ][:limit]


class FakeReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:
        return ResourceReadResult(
            ref=ref,
            title=ref.title,
            content="这是一份真实读取到的文档内容。",
        )


@pytest.mark.asyncio
async def test_agent_returns_final_response_without_tools() -> None:
    model = FakeAgentModel([_decision(final_response="你好，我在。")])
    agent = AgentOrchestrator(model)

    response = await agent.handle(_request("你好"))

    assert response.text == "你好，我在。"
    assert response.action_proposals == []


@pytest.mark.asyncio
async def test_agent_system_prompt_includes_assistant_profile() -> None:
    model = FakeAgentModel([_decision(final_response="收到。")])
    agent = AgentOrchestrator(model)
    request = _request("你好")
    request.assistant_name = "小飞"
    request.assistant_profile = "简洁直接，擅长整理飞书文档。"

    await agent.handle(request)

    system_prompt = model.requests[0].messages[0].content
    assert "你是 小飞" in system_prompt
    assert "你的语言风格和任务角色简介：简洁直接，擅长整理飞书文档。" in system_prompt


@pytest.mark.asyncio
async def test_agent_executes_search_tool_then_returns_final_response() -> None:
    searcher = FakeSearcher()
    model = FakeAgentModel(
        [
            _decision(
                tool_calls=[
                    {
                        "id": "search-1",
                        "name": ToolName.SEARCH_RESOURCES.value,
                        "arguments": {"query": "情感调优", "limit": 3},
                    }
                ]
            ),
            _decision(final_response="找到了：情感调优。"),
        ]
    )
    agent = AgentOrchestrator(model, resource_searcher=searcher)

    response = await agent.handle(_request("帮我找情感调优相关文档"))

    assert searcher.queries == ["情感调优"]
    assert response.text == "找到了：情感调优。"
    assert len(model.requests) == 2
    assert "Tool results" in model.requests[1].messages[1].content


@pytest.mark.asyncio
async def test_agent_search_tool_expands_over_specific_feishu_queries() -> None:
    searcher = QueryMatchingSearcher("蓝色火箭测试")
    model = FakeAgentModel(
        [
            _decision(
                tool_calls=[
                    {
                        "id": "search-1",
                        "name": ToolName.SEARCH_RESOURCES.value,
                        "arguments": {"query": "飞书文档 蓝色火箭资料测试", "limit": 3},
                    }
                ]
            ),
            _decision(final_response="找到了：测试文档。"),
        ]
    )
    agent = AgentOrchestrator(model, resource_searcher=searcher)

    response = await agent.handle(_request("帮我搜索 飞书文档 蓝色火箭资料测试"))

    assert searcher.queries[:2] == ["飞书文档 蓝色火箭资料测试", "蓝色火箭测试"]
    assert response.text == "找到了：测试文档。"


@pytest.mark.asyncio
async def test_agent_executes_native_model_tool_calls() -> None:
    searcher = FakeSearcher()
    model = FakeAgentModel(
        [
            ModelResponse(
                text="",
                provider="fake",
                model="fake-model",
                tool_calls=[
                    ModelToolCall(
                        id="native-search",
                        name=ToolName.SEARCH_RESOURCES.value,
                        arguments={"query": "情感调优", "limit": 3},
                    )
                ],
            ),
            _decision(final_response="原生工具调用已执行。"),
        ]
    )
    agent = AgentOrchestrator(model, resource_searcher=searcher)

    response = await agent.handle(_request("帮我找情感调优相关文档"))

    assert searcher.queries == ["情感调优"]
    assert response.text == "原生工具调用已执行。"
    assert len(model.requests) == 2
    assert model.requests[0].tools
    assert model.requests[0].tool_choice == "auto"
    assert "Tool results" in model.requests[1].messages[1].content


@pytest.mark.asyncio
async def test_agent_repairs_invalid_json_once() -> None:
    model = FakeAgentModel(["不是 JSON", _decision(final_response="修复成功。")])
    agent = AgentOrchestrator(model)

    response = await agent.handle(_request("测试"))

    assert response.text == "修复成功。"
    assert len(model.requests) == 2
    assert model.requests[1].metadata["agent_mode"] == "json_repair"


@pytest.mark.asyncio
async def test_agent_repairs_invalid_json_after_tool_results() -> None:
    searcher = FakeSearcher()
    model = FakeAgentModel(
        [
            _decision(
                tool_calls=[
                    {
                        "id": "search-1",
                        "name": ToolName.SEARCH_RESOURCES.value,
                        "arguments": {"query": "测试文档", "limit": 3},
                    }
                ]
            ),
            "不是 JSON",
            _decision(final_response="已根据搜索结果继续处理。"),
        ]
    )
    agent = AgentOrchestrator(model, resource_searcher=searcher)

    response = await agent.handle(_request("帮我搜索测试文档"))

    assert response.text == "已根据搜索结果继续处理。"
    assert len(model.requests) == 3
    assert model.requests[2].metadata["agent_mode"] == "json_repair"
    assert "Tool results" in model.requests[2].messages[1].content


@pytest.mark.asyncio
async def test_agent_accepts_null_decision_lists() -> None:
    model = FakeAgentModel(
        [
            json.dumps(
                {
                    "final_response": "空列表字段已兼容。",
                    "tool_calls": None,
                    "writeback_drafts": None,
                },
                ensure_ascii=False,
            )
        ]
    )
    agent = AgentOrchestrator(model)

    response = await agent.handle(_request("测试 null 列表"))

    assert response.text == "空列表字段已兼容。"
    assert response.action_proposals == []


@pytest.mark.asyncio
async def test_agent_converts_writeback_draft_to_action_proposal() -> None:
    model = FakeAgentModel(
        [
            _decision(
                final_response="我已准备好写回预览，请在卡片中确认后执行。",
                writeback_drafts=[
                    {
                        "action_type": WriteActionType.DOC_APPEND.value,
                        "target": {
                            "document_id": "docx1",
                            "title": "测试文档",
                            "url": "https://my.feishu.cn/docx/docx1",
                        },
                        "payload": {"content": "卡片测试成功"},
                        "preview": "向文档追加文本：\n卡片测试成功",
                    }
                ],
            )
        ]
    )
    agent = AgentOrchestrator(model, enable_writeback=True)

    response = await agent.handle(_request("写一句卡片测试成功到测试文档末尾"))

    assert response.action_proposals
    proposal = response.action_proposals[0]
    assert proposal.target_title == "测试文档"
    assert proposal.target_url == "https://my.feishu.cn/docx/docx1"
    assert proposal.payload == {"content": "卡片测试成功"}


@pytest.mark.asyncio
async def test_agent_normalizes_json_writeback_draft_to_executable_doc_append() -> None:
    model = FakeAgentModel(
        [
            _decision(
                final_response="我已准备好写回预览，请在卡片中确认后执行。",
                writeback_drafts=[
                    {
                        "action_type": WriteActionType.DOC_APPEND.value,
                        "target": {
                            "type": ResourceType.FEISHU_DOC.value,
                            "token": "docx1",
                            "url": "https://my.feishu.cn/docx/docx1",
                            "position": "start",
                        },
                        "payload": {"text": "Agent测试Agents"},
                        "preview": "向文档开头插入文本：Agent测试Agents",
                    }
                ],
            )
        ]
    )
    agent = AgentOrchestrator(model, enable_writeback=True)

    response = await agent.handle(_request("帮我写一句“Agent测试Agents”到测试文档开头"))

    proposal = response.action_proposals[0]
    assert proposal.target == {
        "type": ResourceType.FEISHU_DOC.value,
        "token": "docx1",
        "url": "https://my.feishu.cn/docx/docx1",
        "document_id": "docx1",
        "title": "测试文档",
        "block_id": "docx1",
        "index": 0,
    }
    assert proposal.target_title == "测试文档"
    assert proposal.payload == {"content": "Agent测试Agents"}
    assert proposal.preview == "向文档开头插入文本：\nAgent测试Agents"


@pytest.mark.asyncio
async def test_agent_prompt_marks_recent_writeback_request_for_location_followup() -> None:
    model = FakeAgentModel([_decision(final_response="ok")])
    agent = AgentOrchestrator(model, enable_writeback=True)
    request = _request("帮我写在开头吧").model_copy(
        update={
            "chat_context_messages": [
                ChatContextMessage(
                    message_id="m1",
                    sender_id="ou_user",
                    text="帮我写一句“旧内容”到测试文档开头",
                    created_at="2026-06-26T06:00:00+00:00",
                ),
                ChatContextMessage(
                    message_id="m2",
                    sender_id="ou_user",
                    text="帮我写一句“新内容”到测试文档里的多维表格之前",
                    created_at="2026-06-26T06:01:00+00:00",
                ),
            ]
        }
    )

    await agent.handle(request)

    user_prompt = model.requests[0].messages[1].content
    assert "最近待补充位置的写回请求" in user_prompt
    assert "新内容" in user_prompt


@pytest.mark.asyncio
async def test_agent_overrides_stale_model_content_for_location_followup() -> None:
    model = FakeAgentModel(
        [
            _decision(
                writeback_drafts=[
                    {
                        "action_type": WriteActionType.DOC_APPEND.value,
                        "target": {
                            "token": "docx1",
                            "url": "https://my.feishu.cn/docx/docx1",
                            "position": "start",
                        },
                        "payload": {"text": "旧内容"},
                        "preview": "向文档开头插入文本：旧内容",
                    }
                ],
            )
        ]
    )
    agent = AgentOrchestrator(model, enable_writeback=True)
    request = _request("帮我写在开头吧").model_copy(
        update={
            "chat_context_messages": [
                ChatContextMessage(
                    message_id="m1",
                    sender_id="ou_user",
                    text="帮我写一句“旧内容”到测试文档开头",
                    created_at="2026-06-26T06:00:00+00:00",
                ),
                ChatContextMessage(
                    message_id="m2",
                    sender_id="ou_user",
                    text="帮我写一句“新内容”到测试文档里的多维表格之前",
                    created_at="2026-06-26T06:01:00+00:00",
                ),
            ]
        }
    )

    response = await agent.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.payload == {"content": "新内容"}
    assert proposal.preview == "向文档开头插入文本：\n新内容"


@pytest.mark.asyncio
async def test_agent_uses_recent_bare_user_text_for_location_followup() -> None:
    model = FakeAgentModel(
        [
            _decision(
                writeback_drafts=[
                    {
                        "action_type": WriteActionType.DOC_APPEND.value,
                        "target": {
                            "token": "docx1",
                            "url": "https://my.feishu.cn/docx/docx1",
                            "position": "end",
                        },
                        "payload": {"text": "Agent测试Agents"},
                        "preview": "向文档追加文本：Agent测试Agents",
                    }
                ],
            )
        ]
    )
    agent = AgentOrchestrator(model, enable_writeback=True)
    request = _request("写在结尾吧").model_copy(
        update={
            "chat_context_messages": [
                ChatContextMessage(
                    message_id="m1",
                    sender_id="ou_user",
                    text="帮我写一句“Agent测试Agents”到测试文档开头",
                    created_at="2026-06-26T06:00:00+00:00",
                ),
                ChatContextMessage(
                    message_id="m2",
                    sender_id="ou_bot",
                    text="你想把这句话写到测试文档的开头还是末尾？",
                    created_at="2026-06-26T06:01:00+00:00",
                ),
                ChatContextMessage(
                    message_id="m3",
                    sender_id="ou_user",
                    text="中间位置测试agents",
                    created_at="2026-06-26T06:02:00+00:00",
                ),
            ]
        }
    )

    response = await agent.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.payload == {"content": "中间位置测试agents"}
    assert proposal.preview == "向文档追加文本：\n中间位置测试agents"


@pytest.mark.asyncio
async def test_agent_rejects_unsupported_middle_writeback_draft() -> None:
    model = FakeAgentModel(
        [
            _decision(
                writeback_drafts=[
                    {
                        "action_type": WriteActionType.DOC_APPEND.value,
                        "target": {
                            "document_id": "docx1",
                            "block_id": "file-block",
                            "index": 1,
                        },
                        "payload": {"content": "hello"},
                        "preview": "在附件前插入：hello",
                    }
                ],
            )
        ]
    )
    agent = AgentOrchestrator(
        model,
        enable_writeback=True,
        writeback_confirmation_mode=WritebackConfirmationMode.ALWAYS,
    )

    response = await agent.handle(_request("写到附件前面"))

    assert response.action_proposals == []
    assert "文档中间位置" in response.text


def _request(text: str) -> AssistantRequest:
    return AssistantRequest(
        actor_id="ou_user",
        conversation_id="private:oc_chat",
        conversation_type=ConversationType.PRIVATE,
        text=text,
        assistant_name="小智",
    )


def _decision(**values: Any) -> str:
    return json.dumps(values, ensure_ascii=False)
