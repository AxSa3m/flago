from typing import Any, Protocol

from fcgo.feishu.client import FeishuClient
from fcgo.feishu.openapi import FeishuOpenAPI
from fcgo.models import WriteActionType


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
            )
        if action_type == WriteActionType.SHEET_WRITE_RANGE:
            return await self.api.write_sheet_range(
                _require_str(target, "spreadsheet_token"),
                _require_str(target, "range"),
                _require_list(payload, "values"),
                actor_id,
            )
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
