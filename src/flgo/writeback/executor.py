from typing import Any, Protocol

from flgo.feishu.client import FeishuClient
from flgo.feishu.openapi import FeishuOpenAPI
from flgo.models import WriteActionType


class WriteExecutor(Protocol):
    async def execute(
        self,
        *,
        action_type: WriteActionType,
        actor_id: str,
        target: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a confirmed write."""


class FeishuWriteExecutor:
    def __init__(self, api: FeishuOpenAPI, feishu_client: FeishuClient | None = None) -> None:
        self.api = api
        self.feishu_client = feishu_client

    async def execute(
        self,
        *,
        action_type: WriteActionType,
        actor_id: str,
        target: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if action_type == WriteActionType.DOC_CREATE:
            return await self.api.create_doc(
                _require_str(payload, "title"),
                actor_id,
                folder_token=_optional_str(target, "folder_token"),
                content=_optional_str(payload, "content"),
            )
        if action_type == WriteActionType.DOC_APPEND:
            blocks = payload.get("blocks")
            if isinstance(blocks, list):
                return await self.api.append_doc_blocks(
                    _require_str(target, "document_id"),
                    blocks,
                    actor_id,
                    block_id=_optional_str(target, "block_id"),
                    revision_id=int(target.get("revision_id", -1)),
                )
            return await self.api.append_doc_text(
                _require_str(target, "document_id"),
                _require_str(payload, "content"),
                actor_id,
                block_id=_optional_str(target, "block_id"),
                index=int(target.get("index", -1)),
            )
        if action_type == WriteActionType.DOC_DELETE_BLOCK:
            document_id = _require_str(target, "document_id")
            block_ids = _require_str_list(payload, "block_ids")
            results = []
            for block_id in reversed(block_ids):
                results.append(
                    await self.api.delete_doc_block(
                        document_id,
                        block_id,
                        actor_id,
                        revision_id=int(target.get("revision_id", -1)),
                    )
                )
            return {"deleted_block_ids": block_ids, "delete_results": results}
        if action_type == WriteActionType.SHEET_WRITE_RANGE:
            spreadsheet_token = _require_str(target, "spreadsheet_token")
            range_name = _require_str(target, "range")
            values = _require_list(payload, "values")
            previous = await self.api.read_sheet_range(
                spreadsheet_token,
                range_name,
                actor_id,
            )
            result = await self.api.write_sheet_range(
                spreadsheet_token,
                range_name,
                values,
                actor_id,
            )
            return {
                "write_result": result,
                "previous_values": _sheet_snapshot(previous, values),
            }
        if action_type == WriteActionType.BITABLE_CREATE_RECORD:
            return await self.api.create_bitable_record(
                _require_str(target, "app_token"),
                _require_str(target, "table_id"),
                _require_dict(payload, "fields"),
                actor_id,
            )
        if action_type == WriteActionType.BITABLE_UPDATE_RECORD:
            return await self.api.update_bitable_record(
                _require_str(target, "app_token"),
                _require_str(target, "table_id"),
                _require_str(target, "record_id"),
                _require_dict(payload, "fields"),
                actor_id,
            )
        if action_type == WriteActionType.BITABLE_DELETE_RECORD:
            return await self.api.delete_bitable_record(
                _require_str(target, "app_token"),
                _require_str(target, "table_id"),
                _require_str(target, "record_id"),
                actor_id,
            )
        if action_type == WriteActionType.MESSAGE_SEND:
            if not self.feishu_client:
                raise RuntimeError("FeishuClient is required for message writes")
            await self.feishu_client.reply_text(
                _require_str(target, "chat_id"),
                _require_str(payload, "text"),
            )
            return {"ok": True}
        raise NotImplementedError(f"{action_type.value} executor is not implemented yet")


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"writeback missing required string field: {key}")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"writeback field must be a non-empty string: {key}")
    return value


def _require_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"writeback missing required object field: {key}")
    return value


def _require_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"writeback missing required list field: {key}")
    return value


def _require_str_list(data: dict[str, Any], key: str) -> list[str]:
    value = _require_list(data, key)
    result = [str(item).strip() for item in value if str(item).strip()]
    if not result:
        raise ValueError(f"writeback missing required string list field: {key}")
    return result


def _sheet_snapshot(data: dict[str, Any], written_values: list[Any]) -> list[list[Any]]:
    value_range = data.get("valueRange")
    existing = value_range.get("values") if isinstance(value_range, dict) else None
    existing_rows = existing if isinstance(existing, list) else []
    row_count = len(written_values)
    column_count = max(
        (
            len(row) if isinstance(row, list) else 1
            for row in written_values
        ),
        default=0,
    )
    snapshot: list[list[Any]] = []
    for row_index in range(row_count):
        existing_row = (
            existing_rows[row_index]
            if row_index < len(existing_rows) and isinstance(existing_rows[row_index], list)
            else []
        )
        snapshot.append(
            [
                existing_row[column_index] if column_index < len(existing_row) else ""
                for column_index in range(column_count)
            ]
        )
    return snapshot
