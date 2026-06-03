from datetime import UTC, datetime, timedelta

import pytest

from fcgo.models import ActionProposal, PendingActionStatus, WriteActionType
from fcgo.storage import SQLiteStore


@pytest.mark.asyncio
async def test_oauth_token_roundtrip(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()

    await store.save_oauth_token("user-1", {"access_token": "token"})

    assert await store.get_oauth_token("user-1") == {"access_token": "token"}


@pytest.mark.asyncio
async def test_oauth_state_is_single_use(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()

    await store.save_oauth_state(
        state="state-1",
        subject_id="user-1",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert await store.consume_oauth_state("state-1") is None


@pytest.mark.asyncio
async def test_idempotency_key_is_single_use(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()

    assert await store.remember_idempotency_key("k", "v") is True
    assert await store.remember_idempotency_key("k", "v") is False


@pytest.mark.asyncio
async def test_pending_action_roundtrip(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.BITABLE_CREATE_RECORD,
        target={"table_id": "tbl1", "app_token": "app1"},
        payload={"fields": {"内容": "FCGO 写回测试成功"}},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    same_payload_different_order = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.BITABLE_CREATE_RECORD,
        target={"app_token": "app1", "table_id": "tbl1"},
        payload={"fields": {"内容": "FCGO 写回测试成功"}},
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
