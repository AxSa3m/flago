from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from flago.models import (
    ActionProposal,
    AuditEventType,
    ChatContextMessage,
    PendingActionStatus,
    WriteActionType,
)
from flago.storage import SQLiteStore


@pytest.mark.asyncio
async def test_oauth_token_roundtrip(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_oauth_token("user-1", {"access_token": "token"})

    assert await store.get_oauth_token("user-1") == {"access_token": "token"}


@pytest.mark.asyncio
async def test_oauth_state_is_single_use(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_oauth_state(
        state="state-1",
        subject_id="user-1",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    assert await store.consume_oauth_state("state-1") == "user-1"
    assert await store.consume_oauth_state("state-1") is None


@pytest.mark.asyncio
async def test_expired_oauth_state_is_rejected(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_oauth_state(
        state="state-1",
        subject_id="user-1",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert await store.consume_oauth_state("state-1") is None


@pytest.mark.asyncio
async def test_idempotency_key_is_single_use(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    assert await store.remember_idempotency_key("k", "v") is True
    assert await store.remember_idempotency_key("k", "v") is False


@pytest.mark.asyncio
async def test_pending_action_roundtrip(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "chat-1"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    await store.save_pending_action(proposal)
    saved = await store.get_pending_action(proposal.id)

    assert saved is not None
    assert saved["status"] == PendingActionStatus.PENDING.value
    assert saved["payload"] == {"text": "hello"}


@pytest.mark.asyncio
async def test_find_recent_matching_action_matches_confirmed_or_executed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.BITABLE_CREATE_RECORD,
        target={"table_id": "tbl1", "app_token": "app1"},
        payload={"fields": {"内容": "FLAGO 写回测试成功"}},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    same_payload_different_order = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.BITABLE_CREATE_RECORD,
        target={"app_token": "app1", "table_id": "tbl1"},
        payload={"fields": {"内容": "FLAGO 写回测试成功"}},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)
    await store.set_pending_action_status(proposal.id, PendingActionStatus.CONFIRMED)
    await store.set_pending_action_status(proposal.id, PendingActionStatus.EXECUTED)

    match = await store.find_recent_matching_action(
        same_payload_different_order,
        within_seconds=600,
    )

    assert match is not None
    assert match["id"] == proposal.id
    assert match["status"] == PendingActionStatus.EXECUTED.value


@pytest.mark.asyncio
async def test_writeback_execution_tracks_latest_reversible_action(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_writeback_execution(
        action_id="action-1",
        actor_id="user-1",
        action_type=WriteActionType.BITABLE_CREATE_RECORD.value,
        target={"app_token": "app1", "table_id": "tbl1"},
        payload={"fields": {"内容": "A"}},
        result={"record": {"record_id": "rec1"}},
        undo_action_type=WriteActionType.BITABLE_DELETE_RECORD.value,
        undo_target={"app_token": "app1", "table_id": "tbl1", "record_id": "rec1"},
        undo_payload={"undo_of_action_id": "action-1"},
        undo_preview="删除 rec1",
    )

    latest = await store.find_latest_reversible_writeback("user-1")

    assert latest is not None
    assert latest["action_id"] == "action-1"
    assert latest["undo_action_type"] == WriteActionType.BITABLE_DELETE_RECORD.value
    assert latest["undo_target"] == {
        "app_token": "app1",
        "table_id": "tbl1",
        "record_id": "rec1",
    }

    await store.mark_writeback_reverted("action-1")

    assert await store.find_latest_reversible_writeback("user-1") is None


@pytest.mark.asyncio
async def test_model_preference_roundtrip_and_clear(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_model_preference(
        scope="conversation:private:oc_chat",
        provider="gemini",
        model="gemini-2.5-flash",
        updated_by="ou_user",
    )

    saved = await store.get_model_preference("conversation:private:oc_chat")
    assert saved is not None
    assert saved.provider == "gemini"
    assert saved.model == "gemini-2.5-flash"
    assert saved.updated_by == "ou_user"

    await store.clear_model_preference("conversation:private:oc_chat", updated_by="ou_user")

    assert await store.get_model_preference("conversation:private:oc_chat") is None


@pytest.mark.asyncio
async def test_assistant_name_preference_roundtrip_clear_and_audit(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_assistant_name_preference(
        subject_id="ou_user",
        assistant_name="飞灵",
        updated_by="ou_user",
    )
    saved = await store.get_assistant_name_preference("ou_user")

    assert saved is not None
    assert saved.assistant_name == "飞灵"
    assert saved.updated_by == "ou_user"

    await store.save_assistant_name_preference(
        subject_id="ou_user",
        assistant_name="小飞",
        updated_by="ou_user",
    )
    updated = await store.get_assistant_name_preference("ou_user")

    assert updated is not None
    assert updated.assistant_name == "小飞"

    await store.clear_assistant_name_preference("ou_user", updated_by="ou_user")

    assert await store.get_assistant_name_preference("ou_user") is None
    assert await _audit_count(store, AuditEventType.ASSISTANT_NAME_SET.value) == 2
    assert await _audit_count(store, AuditEventType.ASSISTANT_NAME_CLEARED.value) == 1


@pytest.mark.asyncio
async def test_assistant_profile_preference_roundtrip_clear_and_audit(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_assistant_profile_preference(
        subject_id="ou_user",
        assistant_profile="简洁直接，擅长整理飞书文档。",
        updated_by="ou_user",
    )
    saved = await store.get_assistant_profile_preference("ou_user")

    assert saved is not None
    assert saved.assistant_profile == "简洁直接，擅长整理飞书文档。"
    assert saved.updated_by == "ou_user"

    await store.clear_assistant_profile_preference("ou_user", updated_by="ou_user")

    assert await store.get_assistant_profile_preference("ou_user") is None
    assert await _audit_count(store, AuditEventType.ASSISTANT_PROFILE_SET.value) == 1
    assert await _audit_count(store, AuditEventType.ASSISTANT_PROFILE_CLEARED.value) == 1


@pytest.mark.asyncio
async def test_context_preference_roundtrip_and_audit(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.set_context_preference(
        scope="conversation:private:oc_chat",
        enabled=True,
        updated_by="ou_user",
    )
    enabled = await store.get_context_preference("conversation:private:oc_chat")

    assert enabled is not None
    assert enabled.enabled is True

    await store.set_context_preference(
        scope="conversation:private:oc_chat",
        enabled=False,
        updated_by="ou_user",
    )
    disabled = await store.get_context_preference("conversation:private:oc_chat")

    assert disabled is not None
    assert disabled.enabled is False
    assert await _audit_count(store, AuditEventType.CONTEXT_ENABLED.value) == 1
    assert await _audit_count(store, AuditEventType.CONTEXT_DISABLED.value) == 1


@pytest.mark.asyncio
async def test_memory_items_can_be_listed_cleared_disabled_and_enabled(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_memory_item(
        id="memory-1",
        subject_id="ou_user",
        kind="偏好",
        content="输出尽量用表格",
        source="user",
    )

    assert await store.is_memory_enabled("ou_user") is True
    items = await store.list_memory_items("ou_user")
    assert len(items) == 1
    assert items[0].content == "输出尽量用表格"

    deleted_count = await store.clear_memory_items("ou_user", updated_by="ou_user")
    await store.set_memory_enabled(subject_id="ou_user", enabled=False, updated_by="ou_user")

    assert await store.is_memory_enabled("ou_user") is False

    await store.set_memory_enabled(subject_id="ou_user", enabled=True, updated_by="ou_user")

    assert deleted_count == 1
    assert await store.list_memory_items("ou_user") == []
    assert await store.is_memory_enabled("ou_user") is True
    assert await _audit_count(store, AuditEventType.MEMORY_DELETED.value) == 1
    assert await _audit_count(store, AuditEventType.MEMORY_DISABLED.value) == 1
    assert await _audit_count(store, AuditEventType.MEMORY_ENABLED.value) == 1
    assert await _audit_count(store, AuditEventType.MEMORY_UPDATED.value) == 1


@pytest.mark.asyncio
async def test_memory_item_can_be_deleted_by_subject_and_id(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_memory_item(
        id="memory-1",
        subject_id="ou_user",
        kind="语言风格",
        content="简洁中文",
        source="user.command",
    )
    await store.save_memory_item(
        id="memory-2",
        subject_id="ou_other",
        kind="语言风格",
        content="正式中文",
        source="user.command",
    )

    cross_subject_deleted = await store.delete_memory_item(
        subject_id="ou_user",
        item_id="memory-2",
        updated_by="ou_user",
    )
    deleted_count = await store.delete_memory_item(
        subject_id="ou_user",
        item_id="memory-1",
        updated_by="ou_user",
    )

    assert cross_subject_deleted == 0
    assert deleted_count == 1
    assert await store.list_memory_items("ou_user") == []
    other_items = await store.list_memory_items("ou_other")
    assert len(other_items) == 1
    assert other_items[0].content == "正式中文"
    assert await _audit_count(store, AuditEventType.MEMORY_DELETED.value) == 2


@pytest.mark.asyncio
async def test_context_message_cache_roundtrip_and_prune(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()
    await store.upsert_context_messages(
        scope="conversation:private:oc_chat",
        messages=[
            ChatContextMessage(
                message_id="om-1",
                sender_id="ou_user",
                text="项目进度是先做上下文",
                created_at="2026-06-10T09:00:00+00:00",
            ),
            ChatContextMessage(
                message_id="om-2",
                sender_id="ou_other",
                text="下一步减少 token",
                created_at="2026-06-10T09:05:00+00:00",
            ),
        ],
    )

    messages = await store.list_context_messages(
        scope="conversation:private:oc_chat",
        since="2026-06-10T00:00:00+00:00",
        limit=10,
    )

    assert [message.message_id for message in messages] == ["om-1", "om-2"]
    assert await store.latest_context_cache_time("conversation:private:oc_chat") is not None
    assert await store.prune_context_message_cache(older_than="2999-01-01T00:00:00+00:00") == 2


@pytest.mark.asyncio
async def test_conversation_summary_roundtrip(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()

    await store.save_conversation_summary(
        scope="conversation:private:oc_chat",
        summary="- 旧消息：测试关键词是蓝色火箭",
        source_message_count=12,
    )

    assert (
        await store.get_conversation_summary("conversation:private:oc_chat")
        == "- 旧消息：测试关键词是蓝色火箭"
    )


async def _audit_count(store: SQLiteStore, event_type: str) -> int:
    async with aiosqlite.connect(store.path) as db:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = ?",
            (event_type,),
        )
    return int(rows[0][0])
