from datetime import UTC, datetime

import pytest

from flgo.agent.tools import ToolExecutionContext, default_tool_registry, model_tool_specs
from flgo.models import (
    ActionProposalDraft,
    AssistantRequest,
    ConversationType,
    ResourceReadRequest,
    ResourceRef,
    ResourceType,
    ToolCall,
    ToolName,
    ToolResult,
    WriteActionType,
    WritebackConfirmationMode,
    WritebackProposalRequest,
)


def test_model_tool_specs_include_resource_read_only() -> None:
    specs = model_tool_specs()

    by_name = {spec.name: spec for spec in specs}

    assert {
        ToolName.SEARCH_RESOURCES,
        ToolName.READ_RESOURCE,
        ToolName.INSPECT_DOC_STRUCTURE,
        ToolName.PREPARE_WRITEBACK,
        ToolName.GET_WRITEBACK_POLICY,
        ToolName.LIST_MEMORY,
        ToolName.UPSERT_MEMORY_DRAFT,
        ToolName.GET_MODEL_STATUS,
        ToolName.WEB_READ,
    } <= set(by_name)
    assert by_name[ToolName.READ_RESOURCE].parameters["properties"]["refs"]["type"] == "array"
    assert by_name[ToolName.PREPARE_WRITEBACK].read_only is False


def test_resource_read_request_validates_resource_refs() -> None:
    request = ResourceReadRequest(
        refs=[
            ResourceRef(
                type=ResourceType.FEISHU_DOC,
                url="https://docs.feishu.cn/docx/abc",
                token="abc",
            )
        ],
        reason="summarize linked doc",
    )

    assert request.refs[0].type == ResourceType.FEISHU_DOC
    assert request.reason == "summarize linked doc"


def test_writeback_draft_converts_to_expiring_action_proposal() -> None:
    now = datetime(2026, 5, 27, 8, 0, tzinfo=UTC)
    draft = ActionProposalDraft(
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "oc_chat"},
        payload={"text": "hello"},
        preview="Send hello to chat",
    )

    proposal = draft.to_proposal(actor_id="ou_user", ttl_seconds=300, now=now)

    assert proposal.actor_id == "ou_user"
    assert proposal.action_type == WriteActionType.MESSAGE_SEND
    assert proposal.target == {"chat_id": "oc_chat"}
    assert proposal.payload == {"text": "hello"}
    assert proposal.preview == "Send hello to chat"
    assert proposal.created_at == now
    assert (proposal.expires_at - proposal.created_at).total_seconds() == 300


def test_tool_call_and_result_shapes() -> None:
    call = ToolCall(
        name=ToolName.PROPOSE_WRITEBACK,
        arguments=WritebackProposalRequest(
            proposals=[
                ActionProposalDraft(
                    action_type=WriteActionType.MESSAGE_SEND,
                    target={"chat_id": "oc_chat"},
                    payload={"text": "hello"},
                    preview="Send hello",
                )
            ]
        ).model_dump(mode="json"),
    )
    result = ToolResult(
        call_id=call.id,
        name=call.name,
        ok=False,
        error="user confirmation required",
    )

    assert call.name == ToolName.PROPOSE_WRITEBACK
    assert result.call_id == call.id
    assert result.ok is False
    assert result.error == "user confirmation required"


@pytest.mark.asyncio
async def test_tool_registry_rejects_unknown_tool_name() -> None:
    registry = default_tool_registry()

    result = await registry.execute(
        call_id="call-1",
        name=ToolName.PROPOSE_WRITEBACK,
        arguments={},
        context=_tool_context(writeback_enabled=True),
    )

    assert result.call_id == "call-1"
    assert result.ok is False
    assert "未知工具" in (result.error or "")


@pytest.mark.asyncio
async def test_tool_registry_rejects_write_tool_when_writeback_disabled() -> None:
    registry = default_tool_registry()
    request = WritebackProposalRequest(
        proposals=[
            ActionProposalDraft(
                action_type=WriteActionType.DOC_APPEND,
                target={"document_id": "docx1"},
                payload={"content": "hello"},
                preview="向文档追加：hello",
            )
        ]
    )

    result = await registry.execute(
        call_id="call-2",
        name=ToolName.PREPARE_WRITEBACK,
        arguments=request.model_dump(mode="json"),
        context=_tool_context(writeback_enabled=False),
    )

    assert result.call_id == "call-2"
    assert result.ok is False
    assert result.error == "writeback_disabled"


@pytest.mark.asyncio
async def test_prepare_writeback_rejects_document_middle_insert() -> None:
    registry = default_tool_registry()
    request = WritebackProposalRequest(
        proposals=[
            ActionProposalDraft(
                action_type=WriteActionType.DOC_APPEND,
                target={"document_id": "docx1", "block_id": "block-mid", "index": 1},
                payload={"content": "hello"},
                preview="在中间写入：hello",
            )
        ]
    )

    result = await registry.execute(
        call_id="call-3",
        name=ToolName.PREPARE_WRITEBACK,
        arguments=request.model_dump(mode="json"),
        context=_tool_context(writeback_enabled=True),
    )

    assert result.call_id == "call-3"
    assert result.ok is False
    assert result.error == "unsupported_doc_middle_insert"
    assert "文档中间位置" in (result.user_message or "")


@pytest.mark.asyncio
async def test_prepare_writeback_normalizes_agent_json_doc_append_shape() -> None:
    registry = default_tool_registry()
    request = WritebackProposalRequest(
        proposals=[
            ActionProposalDraft(
                action_type=WriteActionType.DOC_APPEND,
                target={
                    "type": ResourceType.FEISHU_DOC.value,
                    "token": "docx1",
                    "url": "https://my.feishu.cn/docx/docx1",
                    "position": "start",
                },
                payload={"text": "hello"},
                preview="向文档开头插入文本：hello",
            )
        ]
    )

    result = await registry.execute(
        call_id="call-4",
        name=ToolName.PREPARE_WRITEBACK,
        arguments=request.model_dump(mode="json"),
        context=_tool_context(
            writeback_enabled=True,
            request=AssistantRequest(
                actor_id="ou_user",
                conversation_id="private:oc_chat",
                conversation_type=ConversationType.PRIVATE,
                text="帮我写一句“hello”到测试文档开头",
            ),
        ),
    )

    draft = result.content["writeback_drafts"][0]
    assert result.ok is True
    assert draft["target"]["document_id"] == "docx1"
    assert draft["target"]["block_id"] == "docx1"
    assert draft["target"]["index"] == 0
    assert draft["target"]["title"] == "测试文档"
    assert draft["payload"] == {"content": "hello"}


def _tool_context(
    *,
    writeback_enabled: bool,
    request: AssistantRequest | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        actor_id="ou_user",
        conversation_type=ConversationType.PRIVATE,
        writeback_enabled=writeback_enabled,
        writeback_confirmation_mode=WritebackConfirmationMode.ALWAYS,
        request=request,
    )
