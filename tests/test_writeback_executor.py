import pytest

from flgo.models import WriteActionType
from flgo.writeback.executor import FeishuWriteExecutor


class FakeOpenAPI:
    def __init__(self) -> None:
        self.calls = []

    async def create_doc(self, title, actor_id, *, folder_token=None, content=None):
        self.calls.append(("create_doc", title, actor_id, folder_token, content))
        return {"document_id": "docx123"}

    async def append_doc_text(
        self,
        document_id,
        content,
        actor_id,
        *,
        block_id=None,
        index=-1,
    ):
        self.calls.append(("append_doc_text", document_id, content, actor_id, block_id, index))
        return {"ok": True}

    async def append_doc_blocks(
        self,
        document_id,
        blocks,
        actor_id,
        *,
        block_id=None,
        revision_id=-1,
    ):
        self.calls.append(
            ("append_doc_blocks", document_id, blocks, actor_id, block_id, revision_id)
        )
        return {"ok": True}

    async def write_sheet_range(self, spreadsheet_token, range_name, values, actor_id):
        self.calls.append(("write_sheet_range", spreadsheet_token, range_name, values, actor_id))
        return {"ok": True}

    async def read_sheet_range(self, spreadsheet_token, range_name, actor_id):
        self.calls.append(("read_sheet_range", spreadsheet_token, range_name, actor_id))
        return {
            "valueRange": {
                "range": range_name,
                "values": [["旧值"]],
            }
        }

    async def create_bitable_record(self, app_token, table_id, fields, actor_id):
        self.calls.append(("create_bitable_record", app_token, table_id, fields, actor_id))
        return {"ok": True}

    async def update_bitable_record(self, app_token, table_id, record_id, fields, actor_id):
        self.calls.append(
            ("update_bitable_record", app_token, table_id, record_id, fields, actor_id)
        )
        return {"ok": True}

    async def delete_bitable_record(self, app_token, table_id, record_id, actor_id):
        self.calls.append(("delete_bitable_record", app_token, table_id, record_id, actor_id))
        return {"ok": True}

    async def delete_doc_block(self, document_id, block_id, actor_id, *, revision_id=-1):
        self.calls.append(("delete_doc_block", document_id, block_id, actor_id, revision_id))
        return {"ok": True}


class FakeFeishuClient:
    def __init__(self) -> None:
        self.replies = []

    async def reply_text(self, chat_id, text):
        self.replies.append((chat_id, text))


@pytest.mark.asyncio
async def test_executor_creates_doc() -> None:
    api = FakeOpenAPI()
    executor = FeishuWriteExecutor(api)

    result = await executor.execute(
        action_type=WriteActionType.DOC_CREATE,
        actor_id="ou_user",
        target={"folder_token": "fld123"},
        payload={"title": "标题", "content": "正文"},
    )

    assert result == {"document_id": "docx123"}
    assert api.calls == [("create_doc", "标题", "ou_user", "fld123", "正文")]


@pytest.mark.asyncio
async def test_executor_appends_doc_text() -> None:
    api = FakeOpenAPI()
    executor = FeishuWriteExecutor(api)

    await executor.execute(
        action_type=WriteActionType.DOC_APPEND,
        actor_id="ou_user",
        target={"document_id": "docx123"},
        payload={"content": "新增内容"},
    )

    assert api.calls == [("append_doc_text", "docx123", "新增内容", "ou_user", None, -1)]


@pytest.mark.asyncio
async def test_executor_inserts_doc_text_at_document_start() -> None:
    api = FakeOpenAPI()
    executor = FeishuWriteExecutor(api)

    await executor.execute(
        action_type=WriteActionType.DOC_APPEND,
        actor_id="ou_user",
        target={"document_id": "docx123", "block_id": "docx123", "index": 0},
        payload={"content": "开头文字"},
    )

    assert api.calls == [
        ("append_doc_text", "docx123", "开头文字", "ou_user", "docx123", 0)
    ]


@pytest.mark.asyncio
async def test_executor_appends_doc_blocks() -> None:
    api = FakeOpenAPI()
    executor = FeishuWriteExecutor(api)
    blocks = [{"block_type": 2}]

    await executor.execute(
        action_type=WriteActionType.DOC_APPEND,
        actor_id="ou_user",
        target={"document_id": "docx123", "block_id": "blk1", "revision_id": 3},
        payload={"blocks": blocks},
    )

    assert api.calls == [("append_doc_blocks", "docx123", blocks, "ou_user", "blk1", 3)]


@pytest.mark.asyncio
async def test_executor_deletes_doc_blocks_for_undo() -> None:
    api = FakeOpenAPI()
    executor = FeishuWriteExecutor(api)

    result = await executor.execute(
        action_type=WriteActionType.DOC_DELETE_BLOCK,
        actor_id="ou_user",
        target={"document_id": "docx123"},
        payload={"block_ids": ["blk1", "blk2"]},
    )

    assert result == {
        "deleted_block_ids": ["blk1", "blk2"],
        "delete_results": [{"ok": True}, {"ok": True}],
    }
    assert api.calls == [
        ("delete_doc_block", "docx123", "blk2", "ou_user", -1),
        ("delete_doc_block", "docx123", "blk1", "ou_user", -1),
    ]


@pytest.mark.asyncio
async def test_executor_writes_sheet_range() -> None:
    api = FakeOpenAPI()
    executor = FeishuWriteExecutor(api)

    await executor.execute(
        action_type=WriteActionType.SHEET_WRITE_RANGE,
        actor_id="ou_user",
        target={"spreadsheet_token": "sht123", "range": "Sheet1!A1"},
        payload={"values": [["A"]]},
    )

    assert api.calls == [
        ("read_sheet_range", "sht123", "Sheet1!A1", "ou_user"),
        ("write_sheet_range", "sht123", "Sheet1!A1", [["A"]], "ou_user"),
    ]


@pytest.mark.asyncio
async def test_executor_sheet_snapshot_covers_newly_filled_empty_cells() -> None:
    api = FakeOpenAPI()
    executor = FeishuWriteExecutor(api)

    result = await executor.execute(
        action_type=WriteActionType.SHEET_WRITE_RANGE,
        actor_id="ou_user",
        target={"spreadsheet_token": "sht123", "range": "Sheet1!A1:B2"},
        payload={"values": [["A", "B"], ["C", "D"]]},
    )

    assert result["previous_values"] == [["旧值", ""], ["", ""]]


@pytest.mark.asyncio
async def test_executor_creates_and_updates_bitable_records() -> None:
    api = FakeOpenAPI()
    executor = FeishuWriteExecutor(api)

    await executor.execute(
        action_type=WriteActionType.BITABLE_CREATE_RECORD,
        actor_id="ou_user",
        target={"app_token": "app123", "table_id": "tbl1"},
        payload={"fields": {"关键词": "A"}},
    )
    await executor.execute(
        action_type=WriteActionType.BITABLE_UPDATE_RECORD,
        actor_id="ou_user",
        target={"app_token": "app123", "table_id": "tbl1", "record_id": "rec1"},
        payload={"fields": {"关键词": "B"}},
    )

    assert api.calls == [
        ("create_bitable_record", "app123", "tbl1", {"关键词": "A"}, "ou_user"),
        ("update_bitable_record", "app123", "tbl1", "rec1", {"关键词": "B"}, "ou_user"),
    ]


@pytest.mark.asyncio
async def test_executor_deletes_bitable_record() -> None:
    api = FakeOpenAPI()
    executor = FeishuWriteExecutor(api)

    result = await executor.execute(
        action_type=WriteActionType.BITABLE_DELETE_RECORD,
        actor_id="ou_user",
        target={"app_token": "app123", "table_id": "tbl1", "record_id": "rec1"},
        payload={},
    )

    assert result == {"ok": True}
    assert api.calls == [("delete_bitable_record", "app123", "tbl1", "rec1", "ou_user")]


@pytest.mark.asyncio
async def test_executor_sends_message() -> None:
    api = FakeOpenAPI()
    feishu = FakeFeishuClient()
    executor = FeishuWriteExecutor(api, feishu)

    result = await executor.execute(
        action_type=WriteActionType.MESSAGE_SEND,
        actor_id="ou_user",
        target={"chat_id": "oc_chat"},
        payload={"text": "hello"},
    )

    assert result == {"ok": True}
    assert feishu.replies == [("oc_chat", "hello")]


@pytest.mark.asyncio
async def test_executor_validates_required_fields() -> None:
    executor = FeishuWriteExecutor(FakeOpenAPI())

    with pytest.raises(ValueError, match="spreadsheet_token"):
        await executor.execute(
            action_type=WriteActionType.SHEET_WRITE_RANGE,
            actor_id="ou_user",
            target={"range": "Sheet1!A1"},
            payload={"values": [["A"]]},
        )
