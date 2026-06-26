from __future__ import annotations

import json
from typing import Any

import aiosqlite
import pytest

from fcgo.agent import Assistant
from fcgo.feishu.router import FeishuMessageRouter
from fcgo.model_providers.registry import ModelProviderRegistry, ModelRouter
from fcgo.model_providers.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapability,
    ProviderConfig,
    ProviderKind,
)
from fcgo.models import (
    AuditEventType,
    ConversationType,
    FeishuMessage,
    ResourceReadResult,
    ResourceRef,
)
from fcgo.storage import SQLiteStore


@pytest.mark.asyncio
async def test_mvp_private_message_reaches_model_and_replies_to_feishu(tmp_path) -> None:
    store, provider, client, router = await _acceptance_stack(tmp_path)

    await router.handle_message(_message("om-private", "请给我一个简短回复"))

    assert client.replies == [("oc_private", "MVP 模型回复")]
    assert len(provider.requests) == 1
    assert "请给我一个简短回复" in provider.requests[0].messages[-1].content
    audits = await _audit_details(store, AuditEventType.MODEL_GENERATED.value)
    assert audits[0]["provider"] == "acceptance"
    assert audits[0]["model"] == "acceptance-model"
    assert "请给我一个简短回复" not in json.dumps(audits, ensure_ascii=False)


@pytest.mark.asyncio
async def test_mvp_feishu_link_is_read_before_model_reply(tmp_path) -> None:
    reader = AcceptanceResourceReader()
    _, provider, client, router = await _acceptance_stack(tmp_path, resource_reader=reader)

    await router.handle_message(
        _message(
            "om-resource",
            "请读取 https://my.feishu.cn/docx/docx123 并告诉我测试编号",
        )
    )

    assert reader.calls == [
        (
            "docx123",
            "ou_user",
        )
    ]
    assert len(provider.requests) == 1
    assert "蓝色火箭测试编号 FCGOHJ123" in provider.requests[0].messages[-1].content
    assert client.replies == [("oc_private", "资源中的测试编号是 FCGOHJ123")]


@pytest.mark.asyncio
async def test_mvp_duplicate_message_is_processed_once(tmp_path) -> None:
    _, provider, client, router = await _acceptance_stack(tmp_path)
    message = _message("om-duplicate", "只回复一次")

    await router.handle_message(message)
    await router.handle_message(message)

    assert len(provider.requests) == 1
    assert client.replies == [("oc_private", "MVP 模型回复")]


@pytest.mark.asyncio
async def test_mvp_group_requires_bot_mention(tmp_path) -> None:
    _, provider, client, router = await _acceptance_stack(tmp_path)

    await router.handle_message(
        _message(
            "om-group-ignored",
            "没有艾特机器人",
            conversation_type=ConversationType.GROUP,
            is_bot_mentioned=False,
        )
    )
    await router.handle_message(
        _message(
            "om-group-routed",
            "已经艾特机器人",
            conversation_type=ConversationType.GROUP,
            is_bot_mentioned=True,
        )
    )

    assert len(provider.requests) == 1
    assert "已经艾特机器人" in provider.requests[0].messages[-1].content
    assert client.replies == [("oc_group", "MVP 模型回复")]


async def _acceptance_stack(
    tmp_path,
    *,
    resource_reader: AcceptanceResourceReader | None = None,
) -> tuple[SQLiteStore, AcceptanceProvider, AcceptanceFeishuClient, FeishuMessageRouter]:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    provider = AcceptanceProvider()
    registry = ModelProviderRegistry()
    registry.register(provider)
    model_router = ModelRouter(
        registry,
        default_provider="acceptance",
        audit_recorder=store,
    )
    assistant = Assistant(
        model_router,
        resource_reader=resource_reader,
        audit_recorder=store,
        enable_writeback=False,
    )
    client = AcceptanceFeishuClient()
    router = FeishuMessageRouter(
        assistant,
        client,
        store,
        model_router=model_router,
    )
    return store, provider, client, router


class AcceptanceProvider:
    name = "acceptance"

    def __init__(self) -> None:
        self.provider_config = ProviderConfig(
            name=self.name,
            kind=ProviderKind.ECHO,
            default_model="acceptance-model",
            timeout_seconds=1,
            capabilities=[ProviderCapability.CHAT],
        )
        self.requests: list[ModelRequest] = []

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        prompt = request.messages[-1].content if request.messages else ""
        text = (
            "资源中的测试编号是 FCGOHJ123"
            if "蓝色火箭测试编号 FCGOHJ123" in prompt
            else "MVP 模型回复"
        )
        return ModelResponse(
            text=text,
            provider=self.name,
            model=request.model,
            raw={"latency_ms": 1},
        )


class AcceptanceResourceReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:
        self.calls.append((ref.token, actor_id))
        return ResourceReadResult(
            ref=ref,
            title="测试文档",
            content="蓝色火箭测试编号 FCGOHJ123",
        )


class AcceptanceFeishuClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []

    async def reply_text(self, chat_id: str, text: str) -> None:
        self.replies.append((chat_id, text))


def _message(
    message_id: str,
    text: str,
    *,
    conversation_type: ConversationType = ConversationType.PRIVATE,
    is_bot_mentioned: bool = True,
) -> FeishuMessage:
    chat_id = "oc_private" if conversation_type == ConversationType.PRIVATE else "oc_group"
    return FeishuMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id="ou_user",
        text=text,
        conversation_type=conversation_type,
        conversation_key=f"{conversation_type.value}:{chat_id}",
        is_bot_mentioned=is_bot_mentioned,
    )


async def _audit_details(store: SQLiteStore, event_type: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(store.path) as db:
        rows = await db.execute_fetchall(
            "SELECT detail_json FROM audit_events WHERE event_type = ? ORDER BY id",
            (event_type,),
        )
    return [json.loads(str(row[0])) for row in rows]
