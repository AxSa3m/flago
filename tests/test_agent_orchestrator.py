import json
from typing import Any

import pytest

from fcgo.agent.orchestrator import AgentOrchestrator
from fcgo.model_providers.types import ModelRequest, ModelResponse
from fcgo.models import (
    AssistantRequest,
    ConversationType,
    ResourceReadResult,
    ResourceRef,
    ResourceType,
    ToolName,
    WriteActionType,
    WritebackConfirmationMode,
)


class FakeAgentModel:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(text=self.outputs.pop(0), provider="fake", model="fake-model")


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
async def test_agent_repairs_invalid_json_once() -> None:
    model = FakeAgentModel(["不是 JSON", _decision(final_response="修复成功。")])
    agent = AgentOrchestrator(model)

    response = await agent.handle(_request("测试"))

    assert response.text == "修复成功。"
    assert len(model.requests) == 2
    assert model.requests[1].metadata["agent_mode"] == "json_repair"


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
