from datetime import UTC, datetime

from fcgo.agent.tools import model_tool_specs
from fcgo.models import (
    ActionProposalDraft,
    ResourceReadRequest,
    ResourceRef,
    ResourceType,
    ToolCall,
    ToolName,
    ToolResult,
    WriteActionType,
    WritebackProposalRequest,
)


def test_model_tool_specs_include_resource_read_only() -> None:
    specs = model_tool_specs()

    by_name = {spec.name: spec for spec in specs}

    assert set(by_name) == {ToolName.READ_RESOURCE}
    assert by_name[ToolName.READ_RESOURCE].parameters["properties"]["refs"]["type"] == "array"


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
