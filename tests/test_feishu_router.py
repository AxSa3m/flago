import json
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import pytest

from fcgo.config import Settings
from fcgo.feishu.oauth import AuthorizationStatus
from fcgo.feishu.openapi import DownloadedFile
from fcgo.feishu.router import FeishuMessageRouter
from fcgo.model_providers.echo import EchoModelProvider
from fcgo.model_providers.registry import ModelProviderRegistry, ModelRouter, build_model_router
from fcgo.models import (
    ActionProposal,
    AssistantResponse,
    AuditEventType,
    ConfirmationResult,
    ConversationType,
    FeishuBotMenuEvent,
    FeishuMessage,
    FeishuMessageAttachment,
    PendingActionStatus,
    WriteActionType,
    WritebackConfirmationMode,
)
from fcgo.storage import SQLiteStore


class SuccessfulAssistant:
    def __init__(self) -> None:
        self.requests = []

    async def handle(self, request):
        self.requests.append(request)
        return AssistantResponse(text=f"ok: {request.text}")


class FailingAssistant:
    async def handle(self, request):
        raise RuntimeError("Gemini request failed: User location is not supported for the API use.")


class ProposalAssistant:
    def __init__(self, proposal: ActionProposal) -> None:
        self.proposal = proposal

    async def handle(self, request):
        return AssistantResponse(
            text="我准备写回以下内容，请确认。",
            action_proposals=[self.proposal],
        )


class FakeWritebackService:
    def __init__(self) -> None:
        self.confirmed: list[tuple[str, str]] = []

    async def confirm(self, action_id: str, actor_id: str):
        self.confirmed.append((action_id, actor_id))
        return ConfirmationResult(status="executed", message="写回已执行")


class RecordingChatHistoryAPI:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data or {"items": []}
        self.calls: list[dict[str, Any]] = []

    async def list_recent_messages(self, **kwargs):
        self.calls.append(kwargs)
        return self.data


class SequencedChatHistoryAPI:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, Any]] = []

    async def list_recent_messages(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        return self.payloads[index]


class FailingChatHistoryAPI:
    async def list_recent_messages(self, **kwargs):
        raise RuntimeError("请先在飞书中发送 /授权 完成授权后再读取聊天上下文")


class FakeMessageResourceAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    async def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        resource_type: str = "image",
        max_bytes: int,
    ) -> DownloadedFile:
        self.calls.append((message_id, file_key, resource_type, max_bytes))
        if file_key.startswith("video"):
            return DownloadedFile(
                content=b"video-bytes",
                content_type="video/mp4",
                filename="视频.mp4",
            )
        if file_key.startswith("audio"):
            return DownloadedFile(
                content=b"audio-bytes",
                content_type="audio/mpeg",
                filename="音频.mp3",
            )
        return DownloadedFile(content=b"image-bytes", content_type="image/png", filename="图.png")


class RecordingFeishuClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.sent_texts: list[tuple[str, str, str]] = []
        self.cards: list[tuple[str, dict[str, Any]]] = []

    async def reply_text(self, chat_id: str, text: str) -> None:
        self.replies.append((chat_id, text))

    async def send_text_to_open_id(self, open_id: str, text: str) -> None:
        self.sent_texts.append(("open_id", open_id, text))

    async def send_text_to_user_id(self, user_id: str, text: str) -> None:
        self.sent_texts.append(("user_id", user_id, text))

    async def send_interactive_card(self, chat_id: str, card: dict[str, Any]) -> None:
        self.cards.append((chat_id, card))

    async def send_interactive_card_to_open_id(
        self,
        open_id: str,
        card: dict[str, Any],
    ) -> None:
        self.cards.append((open_id, card))

    async def send_interactive_card_to_user_id(
        self,
        user_id: str,
        card: dict[str, Any],
    ) -> None:
        self.cards.append((user_id, card))


class StubOAuth:
    def __init__(self, status: AuthorizationStatus | None = None) -> None:
        self.status = status or AuthorizationStatus(
            authorized=False,
            usable=False,
            missing_scopes=["docx:document:readonly"],
            reason="not_authorized",
        )

    async def create_authorization_url(self, subject_id: str):
        return f"https://auth.example.test/?subject_id={subject_id}", "state-1"

    async def authorization_status(self, subject_id: str):
        return self.status


def _writeback_settings() -> Settings:
    return Settings(env="test", writeback_enabled=True)


@pytest.mark.asyncio
async def test_router_replies_with_model_response(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(SuccessfulAssistant(), client, store)

    await router.handle_message(_message("om_ok", "hello"))

    assert client.replies == [("oc_chat", "ok: hello")]
    assert router.assistant.requests[0].conversation_id == "private:oc_chat"


@pytest.mark.asyncio
async def test_router_downloads_current_message_image_for_assistant(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    assistant = SuccessfulAssistant()
    client = RecordingFeishuClient()
    resources = FakeMessageResourceAPI()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        settings=Settings(env="test", attachment_vision_enabled=True),
        message_resource_api=resources,
    )
    message = _message("om_image_question", "这幅图讲了什么？")
    message.attachments.append(FeishuMessageAttachment(key="img_v2_abc", type="image"))

    await router.handle_message(message)

    assert resources.calls == [("om_image_question", "img_v2_abc", "image", 5 * 1024 * 1024)]
    assert len(assistant.requests) == 1
    assert len(assistant.requests[0].attachments) == 1
    assert assistant.requests[0].model_provider == "gemini"
    assert assistant.requests[0].attachments[0].media_type == "image/png"
    assert assistant.requests[0].attachments[0].filename == "图.png"


@pytest.mark.asyncio
async def test_router_downloads_current_message_video_for_assistant(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    assistant = SuccessfulAssistant()
    client = RecordingFeishuClient()
    resources = FakeMessageResourceAPI()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        settings=Settings(env="test", attachment_media_understanding_enabled=True),
        message_resource_api=resources,
    )
    message = _message("om_video_question", "这个视频内容是什么？")
    message.attachments.append(
        FeishuMessageAttachment(key="video_v2_abc", type="video", filename="测试.mp4")
    )

    await router.handle_message(message)

    assert resources.calls == [
        ("om_video_question", "video_v2_abc", "file", 20 * 1024 * 1024)
    ]
    assert len(assistant.requests) == 1
    assert len(assistant.requests[0].attachments) == 1
    assert assistant.requests[0].model_provider == "gemini"
    assert assistant.requests[0].attachments[0].type == "video"
    assert assistant.requests[0].attachments[0].media_type == "video/mp4"
    assert assistant.requests[0].attachments[0].filename == "测试.mp4"


@pytest.mark.asyncio
async def test_router_sends_writeback_proposal_card_and_saves_pending_action(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_assistant_name_preference(
        subject_id="ou_user",
        assistant_name="小飞",
        updated_by="ou_user",
    )
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.DOC_APPEND,
        target={"document_id": "docx123"},
        target_title="测试文档",
        target_url="https://my.feishu.cn/docx/docx123",
        payload={"content": "hello"},
        preview="向文档追加：hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=_writeback_settings(),
    )

    await router.handle_message(_message("om_writeback_card", "帮我写回"))

    saved = await store.get_pending_action(proposal.id)
    assert saved is not None
    assert saved["status"] == PendingActionStatus.PENDING.value
    assert client.replies == []
    assert len(client.cards) == 1
    chat_id, card = client.cards[0]
    assert chat_id == "oc_chat"
    assert card["header"]["title"]["content"] == "小飞 回复"
    assert "我准备写回以下内容" in card["elements"][0]["content"]
    card_text = str(card)
    assert "测试文档" in card_text
    assert "https://my.feishu.cn/docx/docx123" in card_text
    assert "打开目标" in card_text
    assert "- 目标：docx123" not in card["elements"][2]["content"]
    assert "确认执行" in card_text
    assert "writeback.confirm" in card_text
    assert proposal.id in card_text


@pytest.mark.asyncio
async def test_router_drops_writeback_proposals_when_disabled(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "oc_chat"},
        payload={"text": "hello"},
        preview="向当前会话发送：hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=Settings(env="test", writeback_enabled=False),
    )

    await router.handle_message(_message("om_writeback_disabled", "帮我写回"))

    assert await store.get_pending_action(proposal.id) is None
    assert client.cards == []
    assert "写入功能当前已暂停" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_draft_only_writeback_policy_does_not_save_pending_action(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.DOC_APPEND,
        target={"document_id": "docx123"},
        target_title="测试文档",
        payload={"content": "hello"},
        preview="向文档追加：hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=Settings(
            env="test",
            writeback_enabled=True,
            writeback_confirmation_mode=WritebackConfirmationMode.DRAFT_ONLY,
        ),
    )

    await router.handle_message(_message("om_writeback_draft_only", "帮我写回"))

    assert await store.get_pending_action(proposal.id) is None
    assert client.cards == []
    assert "只生成草稿" in client.replies[0][1]
    assert "向文档追加：hello" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_low_risk_direct_writeback_policy_executes_doc_append(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.DOC_APPEND,
        target={"document_id": "docx123"},
        target_title="测试文档",
        payload={"content": "hello"},
        preview="向文档追加：hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    writeback_service = FakeWritebackService()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=Settings(
            env="test",
            writeback_enabled=True,
            writeback_confirmation_mode=WritebackConfirmationMode.LOW_RISK_DIRECT,
        ),
        writeback_service=writeback_service,
    )

    await router.handle_message(_message("om_writeback_direct", "帮我写回"))

    assert await store.get_pending_action(proposal.id) is not None
    assert writeback_service.confirmed == [(proposal.id, "ou_user")]
    assert client.cards == []
    assert "写回已执行" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_writeback_auto_command_enables_low_risk_direct(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.DOC_APPEND,
        target={"document_id": "docx123"},
        target_title="测试文档",
        payload={"content": "hello"},
        preview="向文档追加：hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    writeback_service = FakeWritebackService()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=Settings(
            env="test",
            writeback_enabled=True,
            writeback_auto_execute_enabled=True,
            writeback_confirmation_mode=WritebackConfirmationMode.ALWAYS,
        ),
        writeback_service=writeback_service,
    )

    await router.handle_message(_message("om_writeback_auto_on", "/写回 自动开启"))
    await router.handle_message(_message("om_writeback_auto_run", "帮我写回"))

    preference = await store.get_writeback_auto_execute("ou_user")
    assert preference is not None
    assert preference.enabled is True
    assert writeback_service.confirmed == [(proposal.id, "ou_user")]
    assert client.cards == []
    assert "已开启你的个人自动写入偏好" in client.replies[0][1]
    assert "写回已执行" in client.replies[1][1]


@pytest.mark.asyncio
async def test_router_writeback_auto_command_keeps_high_risk_on_card(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.BITABLE_UPDATE_RECORD,
        target={"app_token": "app1", "table_id": "tbl1", "record_id": "rec1"},
        target_title="测试多维表",
        payload={"fields": {"状态": "完成"}},
        preview="更新多维表记录",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    writeback_service = FakeWritebackService()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=Settings(
            env="test",
            writeback_enabled=True,
            writeback_auto_execute_enabled=True,
            writeback_confirmation_mode=WritebackConfirmationMode.ALWAYS,
        ),
        writeback_service=writeback_service,
    )

    await router.handle_message(_message("om_writeback_auto_high_on", "/写回 自动开启"))
    await router.handle_message(_message("om_writeback_auto_high_run", "帮我更新记录"))

    assert writeback_service.confirmed == []
    assert len(client.cards) == 1
    assert "测试多维表" in str(client.cards[0][1])


@pytest.mark.asyncio
async def test_router_writeback_auto_command_disabled_restores_cards(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.DOC_APPEND,
        target={"document_id": "docx123"},
        target_title="测试文档",
        payload={"content": "hello"},
        preview="向文档追加：hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    writeback_service = FakeWritebackService()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=Settings(
            env="test",
            writeback_enabled=True,
            writeback_auto_execute_enabled=True,
            writeback_confirmation_mode=WritebackConfirmationMode.ALWAYS,
        ),
        writeback_service=writeback_service,
    )

    await router.handle_message(_message("om_writeback_auto_disable", "/写回 自动关闭"))
    await router.handle_message(_message("om_writeback_auto_card", "帮我写回"))

    preference = await store.get_writeback_auto_execute("ou_user")
    assert preference is not None
    assert preference.enabled is False
    assert writeback_service.confirmed == []
    assert len(client.cards) == 1
    assert "已关闭你的个人自动写入偏好" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_writeback_auto_command_requires_feature_flag(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        SuccessfulAssistant(),
        client,
        store,
        settings=Settings(
            env="test",
            writeback_enabled=True,
            writeback_auto_execute_enabled=False,
        ),
    )

    await router.handle_message(_message("om_writeback_auto_flag", "/写回 自动开启"))

    assert await store.get_writeback_auto_execute("ou_user") is None
    assert "FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED=true" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_warns_but_allows_recent_duplicate_writeback(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.BITABLE_CREATE_RECORD,
        target={"app_token": "app1", "table_id": "tbl1"},
        payload={"fields": {"内容": "FCGO 写回测试成功"}},
        preview="向多维表格新增记录",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    previous = proposal.model_copy(update={"id": "previous-action"})
    await store.save_pending_action(previous)
    await store.set_pending_action_status(previous.id, PendingActionStatus.CONFIRMED)
    await store.set_pending_action_status(previous.id, PendingActionStatus.EXECUTED)
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=_writeback_settings(),
    )

    await router.handle_message(_message("om_duplicate_writeback", "请写回"))

    saved = await store.get_pending_action(proposal.id)
    assert saved is not None
    assert saved["status"] == PendingActionStatus.PENDING.value
    assert client.replies == []
    assert len(client.cards) == 1
    card_text = str(client.cards[0][1])
    assert "系统检测到近期有相同目标和相同内容的写回记录" in card_text
    assert proposal.id in card_text


@pytest.mark.asyncio
async def test_router_trusts_assistant_action_proposals_without_keyword_gate(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "oc_chat"},
        payload={"text": "hello"},
        preview="向当前会话发送：hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=_writeback_settings(),
    )

    await router.handle_message(_message("om_model_planned_writeback", "请总结这个文档"))

    assert await store.get_pending_action(proposal.id) is not None
    assert client.replies == []
    assert len(client.cards) == 1


@pytest.mark.asyncio
async def test_router_allows_writeback_card_for_join_sheet_intent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.SHEET_WRITE_RANGE,
        target={"spreadsheet_token": "sht123", "range": "sheet1!C4:C4"},
        payload={"values": [["hello"]]},
        preview="向电子表格 sheet1!C4:C4 写入：hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=_writeback_settings(),
    )

    await router.handle_message(
        _message("om_join_sheet_writeback", "请把“hello”加入这个表格")
    )

    saved = await store.get_pending_action(proposal.id)
    assert saved is not None
    assert client.replies == []
    assert len(client.cards) == 1


@pytest.mark.asyncio
async def test_router_allows_writeback_card_for_add_bitable_intent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.BITABLE_UPDATE_RECORD,
        target={"app_token": "app123", "table_id": "tbl1", "record_id": "rec10"},
        payload={"fields": {"测试列2": "测试数据"}},
        preview="更新多维表格记录 rec10：测试列2 = 测试数据",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=_writeback_settings(),
    )

    await router.handle_message(
        _message("om_add_bitable_writeback", "把“测试数据”添加到这个多维表格")
    )

    saved = await store.get_pending_action(proposal.id)
    assert saved is not None
    assert client.replies == []
    assert len(client.cards) == 1


@pytest.mark.asyncio
async def test_router_allows_writeback_card_for_increase_bitable_intent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.BITABLE_CREATE_RECORD,
        target={"app_token": "app123", "table_id": "tbl1"},
        payload={"fields": {"测试列2": "总结结果"}},
        preview="向多维表格新增记录：测试列2 = 总结结果",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=_writeback_settings(),
    )

    await router.handle_message(
        _message("om_increase_bitable_writeback", "把总结结果增加到测试列2的最后一行")
    )

    saved = await store.get_pending_action(proposal.id)
    assert saved is not None
    assert client.replies == []
    assert len(client.cards) == 1


@pytest.mark.asyncio
async def test_router_allows_writeback_card_for_replace_bitable_intent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.BITABLE_UPDATE_RECORD,
        target={"app_token": "app123", "table_id": "tbl1", "record_id": "rec10"},
        payload={"fields": {"测试列2": "总结结果"}},
        preview="更新多维表格记录 rec10：测试列2 = 总结结果",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(
        ProposalAssistant(proposal),
        client,
        store,
        settings=_writeback_settings(),
    )

    await router.handle_message(
        _message("om_replace_bitable_writeback", "把总结结果替换掉测试列2的最后一行")
    )

    saved = await store.get_pending_action(proposal.id)
    assert saved is not None
    assert client.replies == []
    assert len(client.cards) == 1


@pytest.mark.asyncio
async def test_router_replies_to_bot_mentioned_group_message(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(
        _message(
            "om_group_ok",
            "hello group",
            conversation_type=ConversationType.GROUP,
            conversation_key="group:oc_group",
            is_bot_mentioned=True,
        )
    )

    assert client.replies == [("oc_chat", "ok: hello group")]
    assert assistant.requests[0].conversation_id == "group:oc_group"


@pytest.mark.asyncio
async def test_router_ignores_group_message_without_bot_mention(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(
        _message(
            "om_group_ignore",
            "hello group",
            conversation_type=ConversationType.GROUP,
            conversation_key="group:oc_group",
            is_bot_mentioned=False,
        )
    )

    assert client.replies == []
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_replies_with_user_facing_model_error(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    router = FeishuMessageRouter(FailingAssistant(), client, store)

    await router.handle_message(_message("om_fail", "hello"))

    assert len(client.replies) == 1
    assert client.replies[0][0] == "oc_chat"
    assert "当前网络或地区" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_rejects_oversized_message_before_model_call(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        settings=Settings(env="test", max_message_chars=5),
    )

    await router.handle_message(_message("om_too_large", "x" * 20))

    assert "消息太长" in client.replies[0][1]
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_replies_to_authorization_command(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, StubOAuth())

    await router.handle_message(_message("om_auth", "/授权"))

    assert client.replies == []
    assert len(client.cards) == 1
    card = client.cards[0][1]
    assert "点击授权" in str(card)
    assert "https://auth.example.test/?subject_id=ou_user" in str(card)
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_replies_to_authorization_status_command(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, StubOAuth())

    await router.handle_message(_message("om_auth_status", "/授权 状态"))

    assert client.replies == []
    assert len(client.cards) == 1
    card_text = str(client.cards[0][1])
    assert "需要飞书授权" in card_text
    assert "点击授权" in card_text
    assert "https://auth.example.test/?subject_id=ou_user" in card_text
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_reports_usable_authorization_status(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        StubOAuth(
            AuthorizationStatus(
                authorized=True,
                usable=True,
                missing_scopes=[],
                expires_at="2026-06-12T00:00:00+08:00",
            )
        ),
    )

    await router.handle_message(_message("om_auth_status_ok", "/授权 状态"))

    assert client.replies == []
    assert len(client.cards) == 1
    card_text = str(client.cards[0][1])
    assert "飞书授权可用" in card_text
    assert "点击授权" not in card_text
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_context_command_reports_default_policy(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, StubOAuth())

    await router.handle_message(_message("om_context_auth_required", "/上下文 开启"))
    await router.handle_message(_message("om_context_status", "/上下文 查看"))
    await router.handle_message(_message("om_context_disable", "/上下文 关闭"))

    assert all("上下文默认开启" in reply for _, reply in client.replies)
    assert all("不会保存完整聊天原文" in reply for _, reply in client.replies)
    assert all("不会出现在“查看记忆”" not in reply for _, reply in client.replies)
    assert await store.get_context_preference("conversation:private:oc_chat") is None
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_reads_chat_history_by_default(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    history = RecordingChatHistoryAPI(_chat_history_payload())
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        chat_history_api=history,
        settings=Settings(env="test"),
    )

    await router.handle_message(_message("om_context_default", "结合上文回答"))

    assert len(history.calls) == 1
    assert assistant.requests[0].chat_context_messages != []


@pytest.mark.asyncio
async def test_router_reads_and_injects_recent_full_chat_context(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    history = RecordingChatHistoryAPI(_chat_history_payload())
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        chat_history_api=history,
        settings=Settings(
            env="test",
            context_inject_message_limit=2,
            context_max_chars=500,
        ),
    )

    await router.handle_message(_message("om_context_enabled", "项目进度如何"))

    assert history.calls[0]["container_id_type"] == "chat"
    assert history.calls[0]["container_id"] == "oc_chat"
    injected = assistant.requests[0].chat_context_messages
    assert [item.message_id for item in injected] == ["om_old_1", "om_old_2"]
    assert "项目进度" in injected[0].text
    assert assistant.requests[0].chat_context_omitted_count == 0


@pytest.mark.asyncio
async def test_router_summarizes_messages_outside_recent_full_window(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    history = RecordingChatHistoryAPI(
        {
            "items": [
                {
                    "message_id": "om_old_1",
                    "sender": {"id": {"open_id": "ou_user"}},
                    "create_time": "1791359900000",
                    "body": {"content": "{\"text\":\"旧消息里说测试关键词是蓝色火箭\"}"},
                },
                {
                    "message_id": "om_old_2",
                    "sender": {"id": {"open_id": "ou_user"}},
                    "create_time": "1791359960000",
                    "body": {"content": "{\"text\":\"旧消息里又补充项目叫 FCGO\"}"},
                },
                {
                    "message_id": "om_recent",
                    "sender": {"id": {"open_id": "ou_user"}},
                    "create_time": "1791360020000",
                    "body": {"content": "{\"text\":\"最近消息说继续测试\"}"},
                },
            ]
        }
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        chat_history_api=history,
        settings=Settings(env="test", context_inject_message_limit=1),
    )

    await router.handle_message(_message("om_context_summary", "刚才说什么？"))

    request = assistant.requests[0]
    assert [item.message_id for item in request.chat_context_messages] == ["om_recent"]
    assert "测试关键词是蓝色火箭" in request.chat_context_summary
    assert "项目叫 FCGO" in request.chat_context_summary
    assert request.chat_context_omitted_count == 2
    assert request.memory_items == []

    memory_items = await store.list_memory_items("ou_user")
    assert len(memory_items) == 1
    assert memory_items[0].kind == "会话摘要"
    assert memory_items[0].source == "fcgo.context.summary:conversation:private:oc_chat"
    assert "测试关键词是蓝色火箭" in memory_items[0].content
    assert "项目叫 FCGO" in memory_items[0].content
    assert "om_old_1" not in memory_items[0].content
    assert "ou_user:" not in memory_items[0].content
    assert "1791359900" not in memory_items[0].content


@pytest.mark.asyncio
async def test_router_uses_cached_chat_context_within_refresh_window(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    history = RecordingChatHistoryAPI(_chat_history_payload())
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        chat_history_api=history,
        settings=Settings(env="test", context_cache_refresh_seconds=3600),
    )

    await router.handle_message(_message("om_context_cache_1", "项目进度如何"))
    await router.handle_message(_message("om_context_cache_2", "继续"))

    assert len(history.calls) == 1
    assert assistant.requests[1].chat_context_messages != []


@pytest.mark.asyncio
async def test_router_caches_current_message_for_next_followup_within_refresh_window(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    history = RecordingChatHistoryAPI({"items": []})
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        chat_history_api=history,
        settings=Settings(env="test", context_cache_refresh_seconds=3600),
    )

    await router.handle_message(
        _message(
            "om_middle_writeback",
            "帮我写一句“中间位置测试agents”到测试文档里的多维表格之前",
        )
    )
    await router.handle_message(_message("om_location_followup", "帮我写在开头吧"))

    injected = assistant.requests[1].chat_context_messages
    assert len(history.calls) == 2
    assert any(
        item.message_id == "om_middle_writeback"
        and "中间位置测试agents" in item.text
        for item in injected
    )


@pytest.mark.asyncio
async def test_router_refreshes_chat_context_for_context_dependent_query(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    history = SequencedChatHistoryAPI(
        [
            {"items": []},
            {
                "items": [
                    {
                        "message_id": "om_keyword",
                        "sender": {"id": {"open_id": "ou_user"}},
                        "create_time": "1791359900000",
                        "body": {"content": "{\"text\":\"我们的测试关键词是蓝色火箭\"}"},
                    }
                ]
            },
        ]
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        chat_history_api=history,
        settings=Settings(env="test", context_cache_refresh_seconds=3600),
    )

    await router.handle_message(_message("om_context_cache_first", "继续"))
    await router.handle_message(_message("om_context_cache_second", "刚才的测试关键词是什么？"))

    assert len(history.calls) == 2
    injected = assistant.requests[1].chat_context_messages
    assert [item.message_id for item in injected][-1:] == ["om_keyword"]


@pytest.mark.asyncio
async def test_router_refreshes_chat_context_for_document_reference_query(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    history = SequencedChatHistoryAPI(
        [
            {"items": []},
            {
                "items": [
                    {
                        "message_id": "om_user_lookup",
                        "sender": {"id": {"open_id": "ou_user"}},
                        "create_time": "1791359900000",
                        "body": {
                            "content": (
                                '{"text":"帮我某一篇飞书文档里 有个 蓝色火箭测试 的编码是多少"}'
                            )
                        },
                    },
                    {
                        "message_id": "om_bot_answer",
                        "sender": {"id": {"open_id": "ou_bot"}},
                        "create_time": "1791359960000",
                        "body": {
                            "content": (
                                '{"text":"在已读取的测试文档中，蓝色火箭测试的编码是 FCGOHJ123。"}'
                            )
                        },
                    },
                ]
            },
        ]
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        chat_history_api=history,
        settings=Settings(
            env="test",
            feishu_bot_open_id="ou_bot",
            context_cache_refresh_seconds=3600,
        ),
    )

    await router.handle_message(_message("om_first", "继续"))
    await router.handle_message(_message("om_second", "帮我找到这篇文档 发链接给我"))

    assert len(history.calls) == 2
    injected = assistant.requests[1].chat_context_messages
    assert [item.message_id for item in injected][-2:] == ["om_user_lookup", "om_bot_answer"]
    assert "蓝色火箭测试的编码" in injected[-1].text


@pytest.mark.asyncio
async def test_router_parses_nested_post_text_for_chat_context(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    history = RecordingChatHistoryAPI(
        {
            "items": [
                {
                    "message_id": "om_post_context",
                    "sender": {"id": {"open_id": "ou_user"}},
                    "create_time": "1791359900000",
                    "body": {
                        "content": (
                            '{"content":[[{"tag":"text","text":"帮我搜索飞书文档 "},'
                            '{"tag":"text","text":"蓝色火箭资料测试"}]]}'
                        )
                    },
                }
            ]
        }
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        chat_history_api=history,
        settings=Settings(env="test"),
    )

    await router.handle_message(_message("om_nested_context", "结合上文回答"))

    injected = assistant.requests[0].chat_context_messages
    assert injected[0].text == "帮我搜索飞书文档 蓝色火箭资料测试"


@pytest.mark.asyncio
async def test_router_degrades_when_chat_context_read_fails(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        chat_history_api=FailingChatHistoryAPI(),
        settings=Settings(env="test"),
    )

    await router.handle_message(_message("om_context_auth_failure", "结合上文回答"))

    assert client.replies == [("oc_chat", "ok: 结合上文回答")]
    assert assistant.requests[0].chat_context_messages == []
    audit_details = await _audit_details(store, AuditEventType.ERROR.value)
    assert len(audit_details) == 1
    assert audit_details[0]["kind"] == "chat_context_load_failed"
    assert audit_details[0]["message_id"] == "om_context_auth_failure"
    assert audit_details[0]["conversation_type"] == "private"
    assert "结合上文回答" not in str(audit_details[0])


@pytest.mark.asyncio
async def test_router_memory_view_delete_and_disable(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_memory_item(
        id="memory-1",
        subject_id="ou_user",
        kind="偏好",
        content="输出尽量用表格",
        source="user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_memory_view", "/记忆 查看"))
    await router.handle_message(_message("om_memory_delete", "/记忆 删除"))
    await router.confirm_memory_delete("ou_user")
    await router.handle_message(_message("om_memory_empty", "/记忆 查看"))
    await store.save_memory_item(
        id="memory-2",
        subject_id="ou_user",
        kind="称呼",
        content="称呼我为 Alex",
        source="user",
    )
    await router.handle_message(_message("om_memory_disable", "/记忆 关闭"))
    await router.handle_message(_message("om_memory_disabled", "/记忆 查看"))
    await router.handle_message(_message("om_memory_enable", "/记忆 开启"))
    await router.handle_message(_message("om_memory_enabled", "/记忆 查看"))

    assert "输出尽量用表格" in client.replies[0][1]
    assert len(client.cards) == 1
    assert "确认删除长期记忆" in str(client.cards[0][1])
    assert "暂时没有保存你的长期记忆" in client.replies[1][1]
    assert "已关闭长期记忆" in client.replies[2][1]
    assert "长期记忆已关闭" in client.replies[3][1]
    assert "已开启长期记忆" in client.replies[4][1]
    assert "称呼我为 Alex" in client.replies[5][1]
    assert len(await store.list_memory_items("ou_user")) == 1
    assert await store.is_memory_enabled("ou_user") is True
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_memory_command_crud_by_key(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_memory_remember", "/记忆 记住 输出格式=优先表格"))
    await router.handle_message(_message("om_memory_update", "/记忆 修改 语言风格=简洁中文"))
    await router.handle_message(_message("om_memory_view_after_crud", "/记忆 查看"))
    await router.handle_message(_message("om_memory_delete_one", "/记忆 删除 语言风格"))
    await router.handle_message(_message("om_memory_view_after_delete_one", "/记忆 查看"))

    assert "已记录：输出格式：优先表格" in client.replies[0][1]
    assert "已修改：语言风格：简洁中文" in client.replies[1][1]
    assert "- 语言风格：简洁中文" in client.replies[2][1]
    assert "- 输出格式：优先表格" in client.replies[2][1]
    assert "已删除记忆：语言风格" in client.replies[3][1]
    assert "语言风格：简洁中文" not in client.replies[4][1]
    assert "输出格式：优先表格" in client.replies[4][1]
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_memory_command_generic_preference_can_be_deleted(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_memory_generic", "/记忆 记住 输出尽量用表格"))
    await router.handle_message(_message("om_memory_generic_view", "/记忆 查看"))
    await router.handle_message(_message("om_memory_generic_delete", "/记忆 删除 偏好"))
    await router.handle_message(_message("om_memory_generic_empty", "/记忆 查看"))

    assert "已记录：偏好：输出尽量用表格。" in client.replies[0][1]
    assert "- 偏好：输出尽量用表格。" in client.replies[1][1]
    assert "已删除记忆：偏好" in client.replies[2][1]
    assert "暂时没有保存你的长期记忆" in client.replies[3][1]
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_memory_command_rejects_write_when_disabled_and_group_management(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.set_memory_enabled(subject_id="ou_user", enabled=False, updated_by="ou_user")
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_memory_disabled_write", "/记忆 记住 输出格式=表格"))
    await store.set_memory_enabled(subject_id="ou_user", enabled=True, updated_by="ou_user")
    await router.handle_message(
        _message(
            "om_memory_group_view",
            "/记忆 查看",
            conversation_type=ConversationType.GROUP,
            conversation_key="group:oc_group",
            is_bot_mentioned=True,
        )
    )

    assert "长期记忆已关闭，未保存这条内容" in client.replies[0][1]
    assert "记忆管理请在私聊中操作" in client.replies[1][1]
    assert await store.list_memory_items("ou_user") == []
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_injects_enabled_private_user_memory(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_memory_item(
        id="memory-1",
        subject_id="ou_user",
        kind="偏好",
        content="输出尽量用表格",
        source="user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_memory_context", "总结一下"))

    assert len(assistant.requests[0].memory_items) == 1
    assert assistant.requests[0].memory_items[0].content == "输出尽量用表格"


@pytest.mark.asyncio
async def test_router_requires_confirmation_for_explicit_memory_statement(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_memory_name", "记住我叫Sa3m"))
    await router.handle_message(_message("om_memory_project", "我的项目代号是：空杯"))
    await router.handle_message(_message("om_memory_preference", "输出偏好是简短直接"))

    assert await store.list_memory_items("ou_user") == []
    assert len(client.cards) == 3
    for _, card in client.cards:
        assert card["header"]["title"]["content"] == "确认保存长期记忆"

    for _, card in client.cards:
        value = _card_action_values(card)[0]
        await router.confirm_memory_save(
            "ou_user",
            key=str(value["memory_key"]),
            kind=str(value["memory_kind"]),
            content=str(value["memory_content"]),
        )

    await router.handle_bot_menu(_menu_event("evt_memory_view_after_explicit", "fcgo.memory.view"))

    items = await store.list_memory_items("ou_user")
    contents = {item.content for item in items}
    assert "你叫Sa3m。" in contents
    assert "你的项目代号是“空杯”。" in contents
    assert "输出偏好是简短直接。" in contents
    assert assistant.requests == []
    assert "你叫Sa3m。" in client.sent_texts[0][2]
    assert "你的项目代号是“空杯”。" in client.sent_texts[0][2]
    assert "输出偏好是简短直接。" in client.sent_texts[0][2]


@pytest.mark.asyncio
async def test_router_limits_explicit_memory_item_length(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        settings=Settings(env="test", memory_item_max_chars=24),
    )

    await router.handle_message(_message("om_memory_long", "记住：" + "很长" * 30))

    assert await store.list_memory_items("ou_user") == []
    assert len(client.cards) == 1
    value = _card_action_values(client.cards[0][1])[0]
    assert len(value["memory_content"]) <= 24
    assert value["memory_content"].endswith("...")

    await router.confirm_memory_save(
        "ou_user",
        key=str(value["memory_key"]),
        kind=str(value["memory_kind"]),
        content=str(value["memory_content"]),
    )

    items = await store.list_memory_items("ou_user")
    assert len(items) == 1
    assert len(items[0].content) <= 24
    assert items[0].content.endswith("...")
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_limits_memory_context_budget(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_memory_item(
        id="memory-1",
        subject_id="ou_user",
        kind="偏好",
        content="输出偏好是简短直接",
        source="user",
    )
    await store.save_memory_item(
        id="memory-2",
        subject_id="ou_user",
        kind="项目",
        content="项目代号是空杯",
        source="user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        settings=Settings(env="test", memory_context_max_chars=10),
    )

    await router.handle_message(_message("om_memory_budget", "按记忆回答"))

    assert len(assistant.requests[0].memory_items) == 1
    assert len(assistant.requests[0].memory_items[0].content) <= 5
    assert assistant.requests[0].memory_items[0].content.endswith("...")


@pytest.mark.asyncio
async def test_router_does_not_persist_explicit_memory_when_disabled(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.set_memory_enabled(subject_id="ou_user", enabled=False, updated_by="ou_user")
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_memory_disabled_explicit", "记住我叫Sa3m"))

    assert await store.list_memory_items("ou_user") == []
    assert assistant.requests == []
    assert "长期记忆已关闭" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_does_not_inject_disabled_or_group_user_memory(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_memory_item(
        id="memory-1",
        subject_id="ou_user",
        kind="偏好",
        content="输出尽量用表格",
        source="user",
    )
    await store.set_memory_enabled(subject_id="ou_user", enabled=False, updated_by="ou_user")
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_memory_disabled_context", "总结一下"))
    await store.set_memory_enabled(subject_id="ou_user", enabled=True, updated_by="ou_user")
    await router.handle_message(
        _message(
            "om_group_memory_context",
            "总结一下",
            conversation_type=ConversationType.GROUP,
            conversation_key="group:oc_chat",
            is_bot_mentioned=True,
        )
    )

    assert assistant.requests[0].memory_items == []
    assert assistant.requests[1].memory_items == []


@pytest.mark.asyncio
async def test_router_sends_undo_card_for_latest_reversible_writeback(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_writeback_execution(
        action_id="original-action",
        actor_id="ou_user",
        action_type=WriteActionType.BITABLE_CREATE_RECORD.value,
        target={"app_token": "app1", "table_id": "tbl1"},
        payload={"fields": {"内容": "FCGO 写回测试成功"}},
        result={"record": {"record_id": "rec1"}},
        undo_action_type=WriteActionType.BITABLE_DELETE_RECORD.value,
        undo_target={"app_token": "app1", "table_id": "tbl1", "record_id": "rec1"},
        undo_payload={"undo_of_action_id": "original-action"},
        undo_preview="撤回上一次写回：删除多维表格记录 rec1",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, settings=_writeback_settings())

    await router.handle_message(_message("om_undo", "/撤回"))

    assert assistant.requests == []
    assert client.replies == []
    assert len(client.cards) == 1
    saved_actions = [
        await store.get_pending_action(value["action_id"])
        for value in _card_action_values(client.cards[0][1])
        if value.get("fcgo_action") == "writeback.confirm"
    ]
    assert saved_actions[0] is not None
    assert saved_actions[0]["action_type"] == WriteActionType.BITABLE_DELETE_RECORD.value
    assert saved_actions[0]["target"] == {
        "app_token": "app1",
        "table_id": "tbl1",
        "record_id": "rec1",
    }
    assert "删除多维表格记录" in str(client.cards[0][1])


@pytest.mark.asyncio
async def test_router_sends_undo_card_from_writeback_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_writeback_execution(
        action_id="original-action",
        actor_id="ou_user",
        action_type=WriteActionType.BITABLE_CREATE_RECORD.value,
        target={"app_token": "app1", "table_id": "tbl1"},
        payload={"fields": {"内容": "FCGO 写入测试成功"}},
        result={"record": {"record_id": "rec1"}},
        undo_action_type=WriteActionType.BITABLE_DELETE_RECORD.value,
        undo_target={"app_token": "app1", "table_id": "tbl1", "record_id": "rec1"},
        undo_payload={"undo_of_action_id": "original-action"},
        undo_preview="撤回上一次写入：删除多维表格记录 rec1",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, settings=_writeback_settings())

    await router.handle_bot_menu(_menu_event("evt_writeback_undo", "fcgo.writeback.undo"))

    assert assistant.requests == []
    assert client.sent_texts == []
    assert len(client.cards) == 1
    assert client.cards[0][0] == "ou_user"
    assert "可撤回的写入记录" in str(client.cards[0][1])
    saved_actions = [
        await store.get_pending_action(value["action_id"])
        for value in _card_action_values(client.cards[0][1])
        if value.get("fcgo_action") == "writeback.confirm"
    ]
    assert saved_actions[0] is not None
    assert saved_actions[0]["action_type"] == WriteActionType.BITABLE_DELETE_RECORD.value


@pytest.mark.asyncio
async def test_router_undo_prefers_latest_doc_append_over_older_bitable(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_writeback_execution(
        action_id="older-bitable",
        actor_id="ou_user",
        action_type=WriteActionType.BITABLE_CREATE_RECORD.value,
        target={"app_token": "app1", "table_id": "tbl1"},
        payload={"fields": {"内容": "旧记录"}},
        result={"record": {"record_id": "rec1"}},
        undo_action_type=WriteActionType.BITABLE_DELETE_RECORD.value,
        undo_target={"app_token": "app1", "table_id": "tbl1", "record_id": "rec1"},
        undo_payload={"undo_of_action_id": "older-bitable"},
        undo_preview="撤回旧多维表记录",
    )
    await store.save_writeback_execution(
        action_id="latest-doc",
        actor_id="ou_user",
        action_type=WriteActionType.DOC_APPEND.value,
        target={
            "document_id": "docx123",
            "title": "测试文档",
            "url": "https://my.feishu.cn/docx/docx123",
        },
        payload={"content": "卡片测试成功"},
        result={"children": [{"block_id": "blk1"}]},
        undo_action_type=WriteActionType.DOC_DELETE_BLOCK.value,
        undo_target={"document_id": "docx123"},
        undo_payload={"block_ids": ["blk1"], "undo_of_action_id": "latest-doc"},
        undo_preview="撤回上一次写回：删除文档中新追加的 1 个内容块",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, settings=_writeback_settings())

    await router.handle_message(_message("om_undo_latest_doc", "/撤回"))

    assert len(client.cards) == 1
    saved_actions = [
        await store.get_pending_action(value["action_id"])
        for value in _card_action_values(client.cards[0][1])
        if value.get("fcgo_action") == "writeback.confirm"
    ]
    assert saved_actions[0] is not None
    assert saved_actions[0]["action_type"] == WriteActionType.DOC_DELETE_BLOCK.value
    assert saved_actions[0]["target"] == {
        "document_id": "docx123",
        "title": "测试文档",
        "url": "https://my.feishu.cn/docx/docx123",
    }
    assert saved_actions[0]["payload"]["block_ids"] == ["blk1"]
    assert "测试文档" in str(client.cards[0][1])
    assert "卡片测试成功" in str(client.cards[0][1])
    assert "多维表" not in str(client.cards[0][1])


@pytest.mark.asyncio
async def test_router_replies_when_no_reversible_writeback_exists(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, settings=_writeback_settings())

    await router.handle_message(_message("om_undo_empty", "/撤回"))

    assert assistant.requests == []
    assert client.cards == []
    assert "没有找到可撤回" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_lists_recent_writeback_statuses(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_writeback_execution(
        action_id="sheet-action",
        actor_id="ou_user",
        action_type=WriteActionType.SHEET_WRITE_RANGE.value,
        target={"spreadsheet_token": "sht1", "range": "Sheet1!A1:B2"},
        payload={"values": [["A"]]},
        result={"previous_values": [["旧值"]]},
        undo_action_type=WriteActionType.SHEET_WRITE_RANGE.value,
        undo_target={"spreadsheet_token": "sht1", "range": "Sheet1!A1:B2"},
        undo_payload={"values": [["旧值"]], "undo_of_action_id": "sheet-action"},
        undo_preview="restore sheet",
    )
    await store.save_writeback_execution(
        action_id="message-action",
        actor_id="ou_user",
        action_type=WriteActionType.MESSAGE_SEND.value,
        target={"chat_id": "oc_chat"},
        payload={"text": "hello"},
        result={"ok": True},
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_write_history", "/查看最近写入"))

    reply = client.replies[0][1]
    assert "最近写入" in reply
    assert "写入电子表格 · 电子表格 Sheet1!A1:B2 · 可撤回" in reply
    assert "发送消息 · 飞书消息 · 不可撤回" in reply
    assert "飞书消息默认不自动撤回" in reply
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_replies_to_model_status_command(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())

    await router.handle_message(_message("om_model_status", "/模型 查看"))

    assert len(client.replies) == 1
    assert "当前使用的模型：echo/echo" in client.replies[0][1]
    assert "echo/echo" in client.replies[0][1]
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_model_status_shows_supported_unconfigured_providers(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    model_router = build_model_router(
        Settings(
            env="test",
            gemini_api_key="",
            default_provider="deepseek",
            deepseek_api_key="",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-chat",
        )
    )
    router = FeishuMessageRouter(assistant, client, store, model_router=model_router)

    await router.handle_message(_message("om_model_catalog", "/模型 查看"))

    reply = client.replies[0][1]
    assert "- echo/echo（默认）" in reply
    assert "未启用：" in reply
    assert "deepseek" in reply
    assert "缺少 DEEPSEEK_API_KEY" not in reply
    assert "未设置模型" not in reply


@pytest.mark.asyncio
async def test_router_declines_text_model_switching(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    model_router = build_model_router(
        Settings(
            env="test",
            gemini_api_key="",
            default_provider="deepseek",
            deepseek_api_key="",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-chat",
        )
    )
    router = FeishuMessageRouter(assistant, client, store, model_router=model_router)

    await router.handle_message(
        _message("om_model_unconfigured", "/模型 使用 deepseek/deepseek-chat")
    )

    assert "模型切换请通过飞书机器人自定义菜单操作" in client.replies[0][1]
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_does_not_set_conversation_model_from_text_command(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())

    await router.handle_message(_message("om_model_set", "/模型 使用 echo/custom-model"))
    await router.handle_message(_message("om_after_model_set", "hello"))

    assert "模型切换请通过飞书机器人自定义菜单操作" in client.replies[0][1]
    assert await store.get_model_preference("conversation:private:oc_chat") is None
    assert assistant.requests[0].model_provider is None
    assert assistant.requests[0].model is None


@pytest.mark.asyncio
async def test_router_treats_inline_model_words_as_normal_message(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_model_preference(
        scope="conversation:private:oc_chat",
        provider="echo",
        model="conversation-model",
        updated_by="ou_user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())

    await router.handle_message(_message("om_inline_model_words", "本次用 echo/inline-model 回答"))

    assert assistant.requests[0].text == "本次用 echo/inline-model 回答"
    assert assistant.requests[0].model_provider == "echo"
    assert assistant.requests[0].model == "conversation-model"


@pytest.mark.asyncio
async def test_router_private_user_model_preference_overrides_legacy_conversation_preference(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_model_preference(
        scope="conversation:private:oc_chat",
        provider="echo",
        model="legacy-conversation-model",
        updated_by="ou_user",
    )
    await store.save_model_preference(
        scope="user:ou_user",
        provider="echo",
        model="menu-selected-model",
        updated_by="ou_user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())

    await router.handle_message(_message("om_user_pref_overrides_legacy_conversation", "hello"))

    assert assistant.requests[0].model_provider == "echo"
    assert assistant.requests[0].model == "menu-selected-model"


@pytest.mark.asyncio
async def test_router_does_not_apply_user_model_preference_to_group_chat(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_model_preference(
        scope="user:ou_user",
        provider="echo",
        model="personal-model",
        updated_by="ou_user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())

    await router.handle_message(
        _message(
            "om_group_user_pref",
            "hello group",
            conversation_type=ConversationType.GROUP,
            conversation_key="group:oc_group",
            is_bot_mentioned=True,
        )
    )

    assert assistant.requests[0].model_provider is None
    assert assistant.requests[0].model is None


@pytest.mark.asyncio
async def test_router_replies_to_model_menu_status(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())

    await router.handle_bot_menu(_menu_event("evt_menu_status", "fcgo.model.view"))

    assert len(client.sent_texts) == 1
    assert client.sent_texts[0][0] == "open_id"
    assert client.sent_texts[0][1] == "ou_user"
    assert "支持的模型" in client.sent_texts[0][2]


@pytest.mark.asyncio
async def test_router_sets_user_model_from_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())

    await router.handle_bot_menu(_menu_event("evt_menu_use_echo", "fcgo.model.use.echo"))

    preference = await store.get_model_preference("user:ou_user")
    assert preference is not None
    assert preference.provider == "echo"
    assert preference.model == "echo"
    assert "echo/echo" in client.sent_texts[0][2]


@pytest.mark.asyncio
async def test_router_replies_to_auth_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, StubOAuth(), _model_router())

    await router.handle_bot_menu(_menu_event("evt_menu_auth", "fcgo.auth.start"))

    assert client.sent_texts == []
    assert len(client.cards) == 1
    assert client.cards[0][0] == "ou_user"
    assert "点击授权" in str(client.cards[0][1])


@pytest.mark.asyncio
async def test_router_replies_to_auth_status_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, StubOAuth(), _model_router())

    await router.handle_bot_menu(_menu_event("evt_menu_auth_status", "fcgo.auth.status"))

    assert client.sent_texts == []
    assert len(client.cards) == 1
    assert "需要飞书授权" in str(client.cards[0][1])
    assert "点击授权" in str(client.cards[0][1])


@pytest.mark.asyncio
async def test_router_context_menu_reports_default_policy(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_oauth_token("ou_user", {"access_token": "token"})
    history = RecordingChatHistoryAPI(_chat_history_payload())
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        StubOAuth(),
        _model_router(),
        chat_history_api=history,
        settings=Settings(env="test"),
    )

    await router.handle_bot_menu(_menu_event("evt_menu_context_enable", "fcgo.context.enable"))
    await router.handle_bot_menu(_menu_event("evt_menu_context_view", "fcgo.context.view"))
    await router.handle_message(_message("om_menu_context_enabled", "项目进度如何"))

    preference = await store.get_context_preference("user:ou_user")
    assert preference is None
    assert "上下文默认开启" in client.sent_texts[0][2]
    assert "会话摘要" in client.sent_texts[1][2]
    assert "不会保存完整聊天原文" in client.sent_texts[1][2]
    assert len(history.calls) == 1
    assert assistant.requests[0].chat_context_messages != []


@pytest.mark.asyncio
async def test_router_context_disable_menu_no_longer_disables_context(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.set_context_preference(
        scope="user:ou_user",
        enabled=True,
        updated_by="ou_user",
    )
    history = RecordingChatHistoryAPI(_chat_history_payload())
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        StubOAuth(),
        _model_router(),
        chat_history_api=history,
        settings=Settings(env="test"),
    )

    await router.handle_bot_menu(_menu_event("evt_menu_context_disable", "fcgo.context.disable"))
    await router.handle_message(_message("om_menu_context_disabled", "项目进度如何"))

    preference = await store.get_context_preference("user:ou_user")
    assert preference is not None
    assert preference.enabled is True
    assert "上下文默认开启" in client.sent_texts[0][2]
    assert len(history.calls) == 1
    assert assistant.requests[0].chat_context_messages != []


@pytest.mark.asyncio
async def test_router_writeback_auto_menus_roundtrip(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        settings=Settings(
            env="test",
            writeback_enabled=True,
            writeback_auto_execute_enabled=True,
            writeback_confirmation_mode=WritebackConfirmationMode.ALWAYS,
        ),
    )

    await router.handle_bot_menu(
        _menu_event("evt_writeback_auto_enable", "fcgo.writeback.auto.enable")
    )
    enabled = await store.get_writeback_auto_execute("ou_user")
    await router.handle_bot_menu(_menu_event("evt_writeback_status", "fcgo.writeback.status"))
    await router.handle_bot_menu(
        _menu_event("evt_writeback_auto_disable", "fcgo.writeback.auto.disable")
    )
    disabled = await store.get_writeback_auto_execute("ou_user")
    await router.handle_bot_menu(
        _menu_event("evt_writeback_auto_clear", "fcgo.writeback.auto.clear")
    )

    assert enabled is not None
    assert enabled.enabled is True
    assert "已开启你的个人自动写入偏好" in client.sent_texts[0][2]
    assert "你的自动写入偏好：已开启" in client.sent_texts[1][2]
    assert disabled is not None
    assert disabled.enabled is False
    assert "已关闭你的个人自动写入偏好" in client.sent_texts[2][2]
    assert await store.get_writeback_auto_execute("ou_user") is None
    assert "已清除你的自动写入偏好" in client.sent_texts[3][2]


@pytest.mark.asyncio
async def test_router_writeback_history_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_writeback_execution(
        action_id="doc-action",
        actor_id="ou_user",
        action_type=WriteActionType.DOC_APPEND.value,
        target={"document_id": "docx1", "title": "测试文档"},
        payload={"content": "hello"},
        result={"ok": True},
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_bot_menu(_menu_event("evt_writeback_history", "fcgo.writeback.history"))

    assert len(client.sent_texts) == 1
    assert "最近写入" in client.sent_texts[0][2]
    assert "追加文档 · 文档 docx1" in client.sent_texts[0][2]


@pytest.mark.asyncio
async def test_router_replies_to_memory_view_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_memory_item(
        id="memory-1",
        subject_id="ou_user",
        kind="偏好",
        content="输出尽量用表格",
        source="user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_bot_menu(_menu_event("evt_menu_memory_view", "fcgo.memory.view"))

    assert len(client.sent_texts) == 1
    assert client.sent_texts[0][0] == "open_id"
    assert client.sent_texts[0][1] == "ou_user"
    assert "输出尽量用表格" in client.sent_texts[0][2]
    assert await store.is_memory_enabled("ou_user") is True


@pytest.mark.asyncio
async def test_router_deletes_memory_from_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_memory_item(
        id="memory-1",
        subject_id="ou_user",
        kind="偏好",
        content="输出尽量用表格",
        source="user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_bot_menu(_menu_event("evt_menu_memory_delete", "fcgo.memory.delete"))

    assert client.sent_texts == []
    assert len(client.cards) == 1
    assert client.cards[0][0] == "ou_user"
    assert "确认删除长期记忆" in str(client.cards[0][1])
    assert await store.list_memory_items("ou_user") != []
    assert await store.is_memory_enabled("ou_user") is True


@pytest.mark.asyncio
async def test_router_disables_memory_from_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_memory_item(
        id="memory-1",
        subject_id="ou_user",
        kind="偏好",
        content="输出尽量用表格",
        source="user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_bot_menu(_menu_event("evt_menu_memory_disable", "fcgo.memory.disable"))

    assert "已关闭长期记忆" in client.sent_texts[0][2]
    assert len(await store.list_memory_items("ou_user")) == 1
    assert await store.is_memory_enabled("ou_user") is False


@pytest.mark.asyncio
async def test_router_enables_memory_from_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.set_memory_enabled(subject_id="ou_user", enabled=False, updated_by="ou_user")
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_bot_menu(_menu_event("evt_menu_memory_enable", "fcgo.memory.enable"))

    assert "已开启长期记忆" in client.sent_texts[0][2]
    assert await store.is_memory_enabled("ou_user") is True


@pytest.mark.asyncio
async def test_router_assistant_name_commands_roundtrip(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_assistant_default", "/助手 名称"))
    await router.handle_message(_message("om_assistant_set", "/助手 名称 小智"))
    await router.handle_message(
        _message("om_assistant_profile", "/助手 简介 简洁直接，擅长整理飞书文档。")
    )
    await router.handle_message(_message("om_assistant_view", "/助手 名称"))
    await router.handle_message(_message("om_assistant_reset", "/助手 恢复默认"))
    await router.handle_message(_message("om_assistant_view_reset", "/助手 名称"))

    assert "当前助手信息" in client.replies[0][1]
    assert "名称：小智" in client.replies[0][1]
    assert "简介：" in client.replies[0][1]
    assert "已将你的助手名称设置为：小智" in client.replies[1][1]
    assert "已更新你的助手简介" in client.replies[2][1]
    assert "名称：小智" in client.replies[3][1]
    assert "简介：简洁直接，擅长整理飞书文档。" in client.replies[3][1]
    assert "已恢复默认助手名称和简介" in client.replies[4][1]
    assert "名称：小智" in client.replies[5][1]
    assert await store.get_assistant_name_preference("ou_user") is None
    assert await store.get_assistant_profile_preference("ou_user") is None

    audit_set = await _audit_details(store, AuditEventType.ASSISTANT_NAME_SET.value)
    audit_cleared = await _audit_details(
        store,
        AuditEventType.ASSISTANT_NAME_CLEARED.value,
    )
    assert audit_set == [{"subject_id": "ou_user", "assistant_name_length": 2}]
    assert audit_cleared == [{"subject_id": "ou_user"}]


@pytest.mark.asyncio
async def test_router_rejects_invalid_assistant_name(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_assistant_empty", "/助手 命名"))
    await router.handle_message(
        _message("om_assistant_long", "/助手 命名 " + "很长" * 20)
    )

    assert "助手名称不能为空" in client.replies[0][1]
    assert "助手名称太长了" in client.replies[1][1]
    assert await store.get_assistant_name_preference("ou_user") is None


@pytest.mark.asyncio
async def test_router_injects_assistant_name_without_leaking_to_group_chat(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_assistant_name_preference(
        subject_id="ou_user",
        assistant_name="小飞",
        updated_by="ou_user",
    )
    await store.save_assistant_profile_preference(
        subject_id="ou_user",
        assistant_profile="像项目经理一样简洁。",
        updated_by="ou_user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, settings=Settings(env="test"))

    await router.handle_message(_message("om_private_named", "你好"))
    await router.handle_message(
        _message(
            "om_group_named",
            "群里也问一下",
            conversation_type=ConversationType.GROUP,
            conversation_key="group:oc_group",
            is_bot_mentioned=True,
        )
    )

    assert assistant.requests[0].assistant_name == "小飞"
    assert assistant.requests[0].assistant_profile == "像项目经理一样简洁。"
    assert assistant.requests[1].assistant_name == "小智"
    assert assistant.requests[1].assistant_profile == Settings(env="test").assistant_default_profile


@pytest.mark.asyncio
async def test_router_uses_assistant_name_in_menu_model_and_auth_text(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_assistant_name_preference(
        subject_id="ou_user",
        assistant_name="小飞",
        updated_by="ou_user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, StubOAuth(), _model_router())

    await router.handle_bot_menu(_menu_event("evt_named_help", "fcgo.help"))
    await router.handle_bot_menu(_menu_event("evt_named_model", "fcgo.model.view"))
    await router.handle_bot_menu(_menu_event("evt_named_auth", "fcgo.auth.status"))

    assert "小飞 菜单入口" in client.sent_texts[0][2]
    assert "小飞 当前使用的模型" in client.sent_texts[1][2]
    assert "小飞 才能按你的权限读取飞书文档" in str(client.cards[0][1])


@pytest.mark.asyncio
async def test_router_replies_to_assistant_name_view_menu(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_assistant_name_preference(
        subject_id="ou_user",
        assistant_name="小飞",
        updated_by="ou_user",
    )
    await store.save_assistant_profile_preference(
        subject_id="ou_user",
        assistant_profile="简洁直接，擅长整理飞书文档。",
        updated_by="ou_user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_bot_menu(
        _menu_event("evt_assistant_name_view", "fcgo.assistant.name.view")
    )

    assert len(client.sent_texts) == 1
    assert "当前助手信息" in client.sent_texts[0][2]
    assert "名称：小飞" in client.sent_texts[0][2]
    assert "简介：简洁直接，擅长整理飞书文档。" in client.sent_texts[0][2]
    assert "作用范围" not in client.sent_texts[0][2]
    assert "Agent + Tools" not in client.sent_texts[0][2]
    assert "安全边界" not in client.sent_texts[0][2]
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_help_command_uses_assistant_name_without_group_leak(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_assistant_name_preference(
        subject_id="ou_user",
        assistant_name="小飞",
        updated_by="ou_user",
    )
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, settings=Settings(env="test"))

    await router.handle_message(_message("om_help_private", "/帮助"))
    await router.handle_message(
        _message(
            "om_help_group",
            "/帮助",
            conversation_type=ConversationType.GROUP,
            conversation_key="group:oc_group",
            is_bot_mentioned=True,
        )
    )

    assert "小飞 菜单入口" in client.replies[0][1]
    assert "/助手 名称 小飞" in client.replies[0][1]
    assert "/助手 简介" in client.replies[0][1]
    assert "/助手 恢复默认" in client.replies[0][1]
    assert "小智 菜单入口" in client.replies[1][1]
    assert "小飞 菜单入口" not in client.replies[1][1]
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_opens_admin_page_from_menu_and_command(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        settings=Settings(env="test", base_url="https://fcgo.example.test"),
    )

    await router.handle_bot_menu(_menu_event("evt_admin_open", "fcgo.admin.open"))
    await router.handle_message(_message("om_admin_open", "/配置"))

    assert len(client.cards) == 2
    assert "本地配置网页" in str(client.cards[0][1])
    assert "https://fcgo.example.test/admin" in str(client.cards[0][1])
    assert "打开配置后台" in str(client.cards[1][1])
    assert assistant.requests == []


@pytest.mark.asyncio
async def test_router_ignores_duplicate_menu_event(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())
    event = _menu_event("evt_menu_duplicate", "fcgo.model.view")

    await router.handle_bot_menu(event)
    await router.handle_bot_menu(event)

    assert len(client.sent_texts) == 1


def _message(
    message_id: str,
    text: str,
    *,
    conversation_type: ConversationType = ConversationType.PRIVATE,
    conversation_key: str = "private:oc_chat",
    is_bot_mentioned: bool = True,
) -> FeishuMessage:
    return FeishuMessage(
        message_id=message_id,
        chat_id="oc_chat",
        sender_id="ou_user",
        text=text,
        conversation_type=conversation_type,
        conversation_key=conversation_key,
        is_bot_mentioned=is_bot_mentioned,
    )


def _menu_event(event_id: str, event_key: str) -> FeishuBotMenuEvent:
    return FeishuBotMenuEvent(
        event_id=event_id,
        event_key=event_key,
        operator_open_id="ou_user",
    )


def _chat_history_payload() -> dict[str, Any]:
    return {
        "items": [
            {
                "message_id": "om_old_1",
                "sender": {"id": {"open_id": "ou_user"}},
                "create_time": "1791359900000",
                "body": {"content": "{\"text\":\"昨天讨论的项目进度是上下文读取先做。\"}"},
            },
            {
                "message_id": "om_old_2",
                "sender": {"id": {"open_id": "ou_other"}},
                "create_time": "1791359960000",
                "body": {"content": "{\"text\":\"下一步需要减少 token 消耗。\"}"},
            },
        ]
    }


async def _audit_details(store: SQLiteStore, event_type: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(store.path) as db:
        rows = await db.execute_fetchall(
            "SELECT detail_json FROM audit_events WHERE event_type = ? ORDER BY id",
            (event_type,),
        )
    return [json.loads(str(row[0])) for row in rows]


def _model_router() -> ModelRouter:
    registry = ModelProviderRegistry()
    registry.register(EchoModelProvider())
    return ModelRouter(registry, default_provider="echo")


def _card_action_values(card: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for element in card.get("elements", []):
        if not isinstance(element, dict):
            continue
        actions = element.get("actions")
        if not isinstance(actions, list):
            continue
        for action in actions:
            if isinstance(action, dict) and isinstance(action.get("value"), dict):
                values.append(action["value"])
    return values
