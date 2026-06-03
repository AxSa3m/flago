from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fcgo.config import Settings
from fcgo.feishu.router import FeishuMessageRouter
from fcgo.model_providers.echo import EchoModelProvider
from fcgo.model_providers.registry import ModelProviderRegistry, ModelRouter, build_model_router
from fcgo.models import (
    ActionProposal,
    AssistantResponse,
    ConversationType,
    FeishuBotMenuEvent,
    FeishuMessage,
    PendingActionStatus,
    WriteActionType,
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


class StubOAuth:
    async def create_authorization_url(self, subject_id: str):
        return f"https://auth.example.test/?subject_id={subject_id}", "state-1"


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
async def test_router_sends_writeback_proposal_card_and_saves_pending_action(tmp_path) -> None:
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
    router = FeishuMessageRouter(ProposalAssistant(proposal), client, store)

    await router.handle_message(_message("om_writeback_card", "帮我写回"))

    saved = await store.get_pending_action(proposal.id)
    assert saved is not None
    assert saved["status"] == PendingActionStatus.PENDING.value
    assert client.replies == []
    assert len(client.cards) == 1
    chat_id, card = client.cards[0]
    assert chat_id == "oc_chat"
    assert card["header"]["title"]["content"] == "FCGO 回复"
    assert "我准备写回以下内容" in card["elements"][0]["content"]
    card_text = str(card)
    assert "确认执行" in card_text
    assert "writeback.confirm" in card_text
    assert proposal.id in card_text


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
    router = FeishuMessageRouter(ProposalAssistant(proposal), client, store)

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
    router = FeishuMessageRouter(ProposalAssistant(proposal), client, store)

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
    router = FeishuMessageRouter(ProposalAssistant(proposal), client, store)

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
    router = FeishuMessageRouter(ProposalAssistant(proposal), client, store)

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
    router = FeishuMessageRouter(ProposalAssistant(proposal), client, store)

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
    router = FeishuMessageRouter(ProposalAssistant(proposal), client, store)

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

    assert len(client.replies) == 1
    assert "https://auth.example.test/?subject_id=ou_user" in client.replies[0][1]
    assert assistant.requests == []


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
    router = FeishuMessageRouter(assistant, client, store)

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
async def test_router_replies_when_no_reversible_writeback_exists(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store)

    await router.handle_message(_message("om_undo_empty", "/撤回"))

    assert assistant.requests == []
    assert client.cards == []
    assert "没有找到可撤回" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_replies_to_model_status_command(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())

    await router.handle_message(_message("om_model_status", "/模型 查看"))

    assert len(client.replies) == 1
    assert "当前模型配置" in client.replies[0][1]
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
    assert "[可用] echo/echo" in reply
    assert "[待配置] deepseek/deepseek-chat：缺少 DEEPSEEK_API_KEY" in reply
    assert "[待配置] openai/未设置模型" in reply


@pytest.mark.asyncio
async def test_router_explains_supported_but_unconfigured_provider(tmp_path) -> None:
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

    assert "deepseek 支持但尚未配置完整" in client.replies[0][1]
    assert "DEEPSEEK_API_KEY" in client.replies[0][1]


@pytest.mark.asyncio
async def test_router_applies_conversation_model_preference(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    client = RecordingFeishuClient()
    assistant = SuccessfulAssistant()
    router = FeishuMessageRouter(assistant, client, store, model_router=_model_router())

    await router.handle_message(_message("om_model_set", "/模型 使用 echo/custom-model"))
    await router.handle_message(_message("om_after_model_set", "hello"))

    assert "已将当前会话模型设置为：echo/custom-model" in client.replies[0][1]
    assert assistant.requests[0].model_provider == "echo"
    assert assistant.requests[0].model == "custom-model"


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

    assert "https://auth.example.test/?subject_id=ou_user" in client.sent_texts[0][2]


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
