import json
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import pytest

from fcgo.config import Settings
from fcgo.models import ActionProposal, AuditEventType, PendingActionStatus, WriteActionType
from fcgo.storage import SQLiteStore
from fcgo.writeback.service import WritebackService


class FakeExecutor:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or {"ok": True}

    async def execute(
        self,
        *,
        action_type: WriteActionType,
        actor_id: str,
        target: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "action_type": action_type,
                "actor_id": actor_id,
                "target": target,
                "payload": payload,
            }
        )
        return self.result


class FailingExecutor:
    async def execute(
        self,
        *,
        action_type: WriteActionType,
        actor_id: str,
        target: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise RuntimeError("Feishu API error 99991679: token=secret-token")


@pytest.mark.asyncio
async def test_confirm_executes_once(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    executor = FakeExecutor()
    service = WritebackService(store, executor)
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "chat-1"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)

    first = await service.confirm(proposal.id, "user-1")
    second = await service.confirm(proposal.id, "user-1")

    assert first.status == "executed"
    assert second.status == "duplicate"
    assert len(executor.calls) == 1
    saved = await store.get_pending_action(proposal.id)
    assert saved is not None
    assert saved["status"] == PendingActionStatus.EXECUTED.value


@pytest.mark.asyncio
async def test_confirm_rejects_wrong_actor(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    service = WritebackService(store, FakeExecutor())
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "chat-1"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)

    result = await service.confirm(proposal.id, "user-2")

    assert result.status == "forbidden"


@pytest.mark.asyncio
async def test_confirm_expires_pending_action(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    executor = FakeExecutor()
    service = WritebackService(store, executor)
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "chat-1"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await store.save_pending_action(proposal)

    result = await service.confirm(proposal.id, "user-1")
    saved = await store.get_pending_action(proposal.id)

    assert result.status == "expired"
    assert saved is not None
    assert saved["status"] == "expired"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_cancel_is_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    service = WritebackService(store, FakeExecutor())
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "chat-1"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)

    first = await service.cancel(proposal.id, "user-1")
    second = await service.cancel(proposal.id, "user-1")

    assert first.status == "canceled"
    assert second.status == "duplicate"


@pytest.mark.asyncio
async def test_confirm_returns_failed_and_audits_redacted_executor_error(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    service = WritebackService(store, FailingExecutor())
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "chat-1"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)

    result = await service.confirm(proposal.id, "user-1")

    assert result.status == "failed"
    assert "权限" in result.message
    detail = await _last_audit_detail(store, "error")
    assert detail["kind"] == "writeback_execute_failed"
    assert "secret-token" not in detail["message"]
    assert "***REDACTED***" in detail["message"]
    saved = await store.get_pending_action(proposal.id)
    assert saved is not None
    assert saved["status"] == PendingActionStatus.FAILED.value


@pytest.mark.asyncio
async def test_confirm_blocks_oversized_writeback_before_execution(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    executor = FakeExecutor()
    settings = Settings(env="test", sqlite_path=tmp_path / "fcgo.sqlite3", max_writeback_chars=20)
    service = WritebackService(store, executor, settings)
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "chat-1"},
        payload={"text": "x" * 100},
        preview="oversized",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)

    result = await service.confirm(proposal.id, "user-1")

    assert result.status == "failed"
    assert "安全限制" in result.message
    assert executor.calls == []
    detail = await _last_audit_detail(store, "error")
    assert detail["kind"] == "writeback_safety_limit"


@pytest.mark.asyncio
async def test_confirm_records_reversible_bitable_create(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    executor = FakeExecutor({"record": {"record_id": "rec1"}})
    service = WritebackService(store, executor)
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.BITABLE_CREATE_RECORD,
        target={"app_token": "app1", "table_id": "tbl1"},
        payload={"fields": {"内容": "A"}},
        preview="create record",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)

    result = await service.confirm(proposal.id, "user-1")
    reversible = await store.find_latest_reversible_writeback("user-1")

    assert result.status == "executed"
    assert reversible is not None
    assert reversible["action_id"] == proposal.id
    assert reversible["undo_action_type"] == WriteActionType.BITABLE_DELETE_RECORD.value
    assert reversible["undo_target"] == {
        "app_token": "app1",
        "table_id": "tbl1",
        "record_id": "rec1",
    }


@pytest.mark.asyncio
async def test_confirm_marks_original_reverted_after_bitable_delete(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_writeback_execution(
        action_id="original-action",
        actor_id="user-1",
        action_type=WriteActionType.BITABLE_CREATE_RECORD.value,
        target={"app_token": "app1", "table_id": "tbl1"},
        payload={"fields": {"内容": "A"}},
        result={"record": {"record_id": "rec1"}},
        undo_action_type=WriteActionType.BITABLE_DELETE_RECORD.value,
        undo_target={"app_token": "app1", "table_id": "tbl1", "record_id": "rec1"},
        undo_payload={"undo_of_action_id": "original-action"},
        undo_preview="delete rec1",
    )
    service = WritebackService(store, FakeExecutor())
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.BITABLE_DELETE_RECORD,
        target={"app_token": "app1", "table_id": "tbl1", "record_id": "rec1"},
        payload={"undo_of_action_id": "original-action"},
        preview="delete rec1",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)

    result = await service.confirm(proposal.id, "user-1")

    assert result.status == "executed"
    assert await store.find_latest_reversible_writeback("user-1") is None


@pytest.mark.asyncio
async def test_confirm_records_reversible_sheet_write(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    executor = FakeExecutor(
        {
            "write_result": {"ok": True},
            "previous_values": [["旧值", ""], ["", ""]],
        }
    )
    service = WritebackService(store, executor)
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.SHEET_WRITE_RANGE,
        target={"spreadsheet_token": "sht1", "range": "Sheet1!A1:B2"},
        payload={"values": [["A", "B"], ["C", "D"]]},
        preview="write sheet",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)

    result = await service.confirm(proposal.id, "user-1")
    reversible = await store.find_latest_reversible_writeback("user-1")

    assert result.status == "executed"
    assert reversible is not None
    assert reversible["undo_action_type"] == WriteActionType.SHEET_WRITE_RANGE.value
    assert reversible["undo_target"] == {
        "spreadsheet_token": "sht1",
        "range": "Sheet1!A1:B2",
    }
    assert reversible["undo_payload"] == {
        "values": [["旧值", ""], ["", ""]],
        "undo_of_action_id": proposal.id,
    }


@pytest.mark.asyncio
async def test_confirm_records_reversible_doc_append_when_block_ids_returned(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    executor = FakeExecutor({"children": [{"block_id": "blk1"}, {"block_id": "blk2"}]})
    service = WritebackService(store, executor)
    proposal = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.DOC_APPEND,
        target={
            "document_id": "docx123",
            "title": "测试文档",
            "url": "https://my.feishu.cn/docx/docx123",
        },
        payload={"content": "新增内容"},
        preview="append doc",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(proposal)

    result = await service.confirm(proposal.id, "user-1")
    reversible = await store.find_latest_reversible_writeback("user-1")

    assert result.status == "executed"
    assert reversible is not None
    assert reversible["action_id"] == proposal.id
    assert reversible["undo_action_type"] == WriteActionType.DOC_DELETE_BLOCK.value
    assert reversible["undo_target"] == {
        "document_id": "docx123",
        "title": "测试文档",
        "url": "https://my.feishu.cn/docx/docx123",
    }
    assert reversible["undo_payload"] == {
        "block_ids": ["blk1", "blk2"],
        "undo_of_action_id": proposal.id,
    }
    assert reversible["undo_preview"] == (
        "撤回上一次写入：从《测试文档》删除刚刚新增的文字：\n新增内容"
    )


@pytest.mark.asyncio
async def test_sheet_undo_marks_original_reverted_without_creating_redo(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    await store.save_writeback_execution(
        action_id="original-sheet",
        actor_id="user-1",
        action_type=WriteActionType.SHEET_WRITE_RANGE.value,
        target={"spreadsheet_token": "sht1", "range": "Sheet1!A1"},
        payload={"values": [["新值"]]},
        result={"previous_values": [["旧值"]]},
        undo_action_type=WriteActionType.SHEET_WRITE_RANGE.value,
        undo_target={"spreadsheet_token": "sht1", "range": "Sheet1!A1"},
        undo_payload={"values": [["旧值"]], "undo_of_action_id": "original-sheet"},
        undo_preview="restore sheet",
    )
    service = WritebackService(
        store,
        FakeExecutor({"write_result": {"ok": True}, "previous_values": [["新值"]]}),
    )
    undo = ActionProposal(
        actor_id="user-1",
        action_type=WriteActionType.SHEET_WRITE_RANGE,
        target={"spreadsheet_token": "sht1", "range": "Sheet1!A1"},
        payload={"values": [["旧值"]], "undo_of_action_id": "original-sheet"},
        preview="restore sheet",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await store.save_pending_action(undo)

    result = await service.confirm(undo.id, "user-1")
    history = await store.list_recent_writeback_executions("user-1")

    assert result.status == "executed"
    assert await store.find_latest_reversible_writeback("user-1") is None
    assert history[0]["action_id"] == undo.id
    assert history[0]["reversible"] is False
    assert history[1]["action_id"] == "original-sheet"
    assert history[1]["reverted_at"] is not None
    detail = await _last_audit_detail(store, AuditEventType.WRITE_REVERTED.value)
    assert detail["undo_action_id"] == undo.id


async def _last_audit_detail(store: SQLiteStore, event_type: str) -> dict[str, Any]:
    async with aiosqlite.connect(store.path) as db:
        rows = await db.execute_fetchall(
            """
            SELECT detail_json
            FROM audit_events
            WHERE event_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (event_type,),
        )
    assert rows
    return json.loads(rows[0][0])
