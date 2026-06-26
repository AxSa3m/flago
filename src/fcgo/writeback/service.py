import json
from datetime import UTC, datetime
from typing import Any

from fcgo.config import Settings
from fcgo.logging import redact
from fcgo.models import (
    AuditEventType,
    ConfirmationResult,
    PendingActionStatus,
    WriteActionType,
)
from fcgo.storage import SQLiteStore
from fcgo.writeback.executor import WriteExecutor


class WritebackService:
    def __init__(
        self,
        store: SQLiteStore,
        executor: WriteExecutor,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.settings = settings or Settings()

    async def confirm(self, action_id: str, actor_id: str) -> ConfirmationResult:
        action = await self.store.get_pending_action(action_id)
        if not action:
            return ConfirmationResult(status="not_found", message="待确认动作不存在")
        if action["actor_id"] != actor_id:
            return ConfirmationResult(status="forbidden", message="只能由创建该动作的用户确认")
        if action["status"] != PendingActionStatus.PENDING.value:
            return ConfirmationResult(status="duplicate", message="该动作已处理")
        expires_at = datetime.fromisoformat(action["expires_at"])
        if expires_at <= datetime.now(UTC):
            await self.store.set_pending_action_status(action_id, PendingActionStatus.EXPIRED)
            await self.store.audit(
                AuditEventType.ACTION_EXPIRED,
                actor_id=actor_id,
                action_id=action_id,
            )
            return ConfirmationResult(status="expired", message="待确认动作已过期")
        action_type = WriteActionType(action["action_type"])
        safety_error = _validate_writeback_limits(
            action_type=action_type,
            target=action["target"],
            payload=action["payload"],
            settings=self.settings,
        )
        if safety_error:
            await self.store.audit(
                AuditEventType.ERROR,
                actor_id=actor_id,
                action_id=action_id,
                detail={
                    "kind": "writeback_safety_limit",
                    "action_type": action["action_type"],
                    "message": safety_error,
                },
            )
            return ConfirmationResult(
                status="failed",
                message=f"写回被安全限制拦截：{safety_error}",
            )
        await self.store.set_pending_action_status(action_id, PendingActionStatus.CONFIRMED)
        await self.store.audit(
            AuditEventType.ACTION_CONFIRMED,
            actor_id=actor_id,
            action_id=action_id,
        )
        try:
            result = await self.executor.execute(
                action_type=action_type,
                actor_id=actor_id,
                target=action["target"],
                payload=action["payload"],
            )
        except Exception as exc:  # noqa: BLE001 - convert executor failures to card responses
            detail = str(redact(str(exc)))
            await self.store.set_pending_action_status(action_id, PendingActionStatus.FAILED)
            await self.store.audit(
                AuditEventType.ERROR,
                actor_id=actor_id,
                action_id=action_id,
                detail={
                    "kind": "writeback_execute_failed",
                    "action_type": action["action_type"],
                    "message": detail,
                },
            )
            return ConfirmationResult(
                status="failed",
                message=_writeback_failure_message(detail),
            )
        await self.store.set_pending_action_status(action_id, PendingActionStatus.EXECUTED)
        undo = _undo_metadata(
            action_id=action_id,
            action_type=action_type,
            target=action["target"],
            payload=action["payload"],
            result=result,
        )
        await self.store.save_writeback_execution(
            action_id=action_id,
            actor_id=actor_id,
            action_type=action["action_type"],
            target=action["target"],
            payload=action["payload"],
            result=result,
            undo_action_type=undo.get("action_type"),
            undo_target=undo.get("target"),
            undo_payload=undo.get("payload"),
            undo_preview=undo.get("preview"),
        )
        undo_of_action_id = _optional_text(action["payload"].get("undo_of_action_id"))
        if undo_of_action_id:
            await self.store.mark_writeback_reverted(undo_of_action_id)
            await self.store.audit(
                AuditEventType.WRITE_REVERTED,
                actor_id=actor_id,
                action_id=undo_of_action_id,
                detail={
                    "undo_action_id": action_id,
                    "undo_action_type": action["action_type"],
                },
            )
        await self.store.audit(
            AuditEventType.WRITE_EXECUTED,
            actor_id=actor_id,
            action_id=action_id,
            detail={"action_type": action["action_type"]},
        )
        return ConfirmationResult(status="executed", message="写回已执行")

    async def cancel(self, action_id: str, actor_id: str) -> ConfirmationResult:
        action = await self.store.get_pending_action(action_id)
        if not action:
            return ConfirmationResult(status="not_found", message="待确认动作不存在")
        if action["actor_id"] != actor_id:
            return ConfirmationResult(status="forbidden", message="只能由创建该动作的用户取消")
        if action["status"] != PendingActionStatus.PENDING.value:
            return ConfirmationResult(status="duplicate", message="该动作已处理")
        await self.store.set_pending_action_status(action_id, PendingActionStatus.CANCELED)
        await self.store.audit(
            AuditEventType.ACTION_CANCELED,
            actor_id=actor_id,
            action_id=action_id,
        )
        return ConfirmationResult(status="canceled", message="已取消")


def _validate_writeback_limits(
    *,
    action_type: WriteActionType,
    target: dict[str, Any],
    payload: dict[str, Any],
    settings: Settings,
) -> str | None:
    serialized_size = _json_size({"target": target, "payload": payload})
    if serialized_size > settings.max_writeback_chars:
        return (
            f"写回内容过大，当前 {serialized_size} 字符，"
            f"限制 {settings.max_writeback_chars} 字符"
        )

    if action_type == WriteActionType.DOC_CREATE:
        content = _optional_text(payload.get("content"))
        if content and len(content) > settings.max_writeback_chars:
            return f"文档正文过长，限制 {settings.max_writeback_chars} 字符"
    if action_type == WriteActionType.DOC_APPEND:
        content = _optional_text(payload.get("content"))
        if content and len(content) > settings.max_writeback_chars:
            return f"追加文本过长，限制 {settings.max_writeback_chars} 字符"
    if action_type == WriteActionType.SHEET_WRITE_RANGE:
        values = payload.get("values")
        cell_count = _sheet_cell_count(values)
        if cell_count > settings.max_writeback_cells:
            return (
                f"表格写入范围过大，当前 {cell_count} 个单元格，"
                f"限制 {settings.max_writeback_cells} 个"
            )
    if action_type == WriteActionType.MESSAGE_SEND:
        text = _optional_text(payload.get("text"))
        if text and len(text) > settings.max_writeback_chars:
            return f"消息文本过长，限制 {settings.max_writeback_chars} 字符"
    if action_type == WriteActionType.DOC_DELETE_BLOCK:
        block_ids = payload.get("block_ids")
        if not isinstance(block_ids, list) or not block_ids:
            return "文档撤回缺少新增块信息"
    return None


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except TypeError:
        return len(str(value))


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _sheet_cell_count(values: Any) -> int:
    if not isinstance(values, list):
        return 0
    count = 0
    for row in values:
        if isinstance(row, list):
            count += len(row)
        else:
            count += 1
    return count


def _writeback_failure_message(detail: str) -> str:
    lowered = detail.lower()
    if "缺少所需权限" in detail or "missing oauth scopes" in lowered:
        return "写回失败：当前飞书授权缺少所需权限。请重新发送 /授权 后再生成新的写回卡片。"
    if "permission" in lowered or "forbidden" in lowered or "99991679" in detail:
        return "写回失败：你或应用没有目标资源的写入权限。请确认文档权限、应用权限和 OAuth 授权。"
    if "invalid param" in lowered or "1770001" in detail:
        return "写回失败：飞书接口不接受当前写入参数。请重新生成写回卡片后再试。"
    if "wrong range" in lowered or "90202" in detail:
        return "写回失败：飞书电子表格写入范围格式不正确。请重新生成写回卡片后再试。"
    if "fieldnamenotfound" in lowered or "1254045" in detail:
        return "写回失败：多维表格字段名不存在。请确认字段配置，或重新生成写回卡片后再试。"
    if "base:record:delete" in detail:
        return "撤回失败：当前飞书授权缺少删除多维表格记录权限。请重新发送 /授权 后再试。"
    if "bad request" in lowered or "400" in detail:
        return "写回失败：飞书接口返回请求格式错误。请重新生成写回卡片后再试。"
    if "token" in lowered:
        return "写回失败：飞书授权可能已过期或无效。请重新发送 /授权 后再试。"
    return "写回执行失败，请重新生成写回卡片后再试。"


def _undo_metadata(
    *,
    action_id: str,
    action_type: WriteActionType,
    target: dict[str, Any],
    payload: dict[str, Any],  # noqa: ARG001 - future undo metadata may need payload snapshots
    result: dict[str, Any],
) -> dict[str, Any]:
    if _optional_text(payload.get("undo_of_action_id")):
        return {}
    if action_type == WriteActionType.DOC_APPEND:
        document_id = _optional_text(target.get("document_id"))
        block_ids = _doc_created_block_ids(result)
        if not document_id or not block_ids:
            return {}
        return {
            "action_type": WriteActionType.DOC_DELETE_BLOCK.value,
            "target": {"document_id": document_id},
            "payload": {
                "block_ids": block_ids,
                "undo_of_action_id": action_id,
            },
            "preview": f"撤回上一次写回：删除文档中新追加的 {len(block_ids)} 个内容块",
        }
    if action_type == WriteActionType.BITABLE_CREATE_RECORD:
        record_id = _bitable_record_id(result)
        if not record_id:
            return {}
        app_token = _optional_text(target.get("app_token"))
        table_id = _optional_text(target.get("table_id"))
        if not app_token or not table_id:
            return {}
        return {
            "action_type": WriteActionType.BITABLE_DELETE_RECORD.value,
            "target": {
                "app_token": app_token,
                "table_id": table_id,
                "record_id": record_id,
            },
            "payload": {"undo_of_action_id": action_id},
            "preview": f"撤回上一次写回：删除多维表格记录 {record_id}",
        }
    if action_type == WriteActionType.SHEET_WRITE_RANGE:
        previous_values = result.get("previous_values")
        spreadsheet_token = _optional_text(target.get("spreadsheet_token"))
        range_name = _optional_text(target.get("range"))
        if not isinstance(previous_values, list) or not spreadsheet_token or not range_name:
            return {}
        return {
            "action_type": WriteActionType.SHEET_WRITE_RANGE.value,
            "target": {
                "spreadsheet_token": spreadsheet_token,
                "range": range_name,
            },
            "payload": {
                "values": previous_values,
                "undo_of_action_id": action_id,
            },
            "preview": f"撤回上一次写回：恢复电子表格范围 {range_name}",
        }
    return {}


def _bitable_record_id(result: dict[str, Any]) -> str | None:
    candidates = (
        result.get("record_id"),
        result.get("id"),
        _nested(result, "record", "record_id"),
        _nested(result, "record", "id"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _doc_created_block_ids(result: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in _doc_created_block_candidates(result):
        if not isinstance(item, dict):
            continue
        block_id = item.get("block_id") or item.get("id")
        if isinstance(block_id, str) and block_id.strip() and block_id not in ids:
            ids.append(block_id.strip())
    return ids


def _doc_created_block_candidates(result: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = []
    for key in ("children", "items", "blocks"):
        value = result.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    children = _nested(result, "block", "children")
    if isinstance(children, list):
        candidates.extend(children)
    return candidates


def _nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value
