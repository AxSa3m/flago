import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fcgo.agent.protocols import ModelProvider, ResourceReader
from fcgo.models import (
    ActionProposal,
    AssistantRequest,
    AssistantResponse,
    ResourceReadResult,
    ResourceRef,
    ResourceType,
    WriteActionType,
)
from fcgo.resources.parser import parse_resource_urls

logger = logging.getLogger(__name__)

_GENERATED_CONTENT_LABELS = (
    "待写内容预览",
    "待写内容",
    "写入内容预览",
    "写入内容",
    "生成内容",
    "生成结果",
    "最终内容",
    "新内容",
    "预览内容",
    "内容如下",
    "写回内容",
)

_EXPLICIT_VALUE_LABELS = (
    "写入值",
    "写入内容",
    "新值",
    "最终内容",
    "生成结果",
    "生成内容",
    "总结结果",
    "摘要内容",
)

_WRITEBACK_METADATA_LABELS = (
    "目标位置",
    "目标记录",
    "目标字段",
    "目标表格",
    "目标文档",
    "目标",
    "当前值",
    "原值",
    "操作",
    "动作",
    "字段",
    "范围",
)

_MODEL_OPERATION_LABELS = (
    "操作",
    "动作",
    "操作类型",
    "写回操作",
    "CRUD",
    "CRUD操作",
    "CRUD 操作",
)


@dataclass(frozen=True)
class _BitableFieldCandidate:
    name: str
    type_hint: str | None = None
    index: int = 0


@dataclass(frozen=True)
class _SheetTargetIntent:
    range_name: str
    source: str


@dataclass(frozen=True)
class _BitableRecordCandidate:
    index: int
    record_id: str | None
    fields: dict[str, str]


@dataclass(frozen=True)
class _BitableMatchSelector:
    field_name: str | None
    value: str | None
    row_index: int | None = None
    record_id: str | None = None


@dataclass(frozen=True)
class _BitableUpdatePlan:
    record_id: str
    field_name: str
    value: str
    matched_by: str


@dataclass(frozen=True)
class _ModelWritebackPlan:
    operation: str
    resource_type: str
    target: dict[str, Any]
    payload: dict[str, Any]
    preview: str | None = None


class Assistant:
    def __init__(
        self,
        model_provider: ModelProvider,
        resource_reader: ResourceReader | None = None,
        *,
        pending_action_ttl_seconds: int = 1800,
    ) -> None:
        self.model_provider = model_provider
        self.resource_reader = resource_reader
        self.pending_action_ttl_seconds = pending_action_ttl_seconds
        self.writeback_planner = WritebackPlanner(pending_action_ttl_seconds)

    async def handle(self, request: AssistantRequest) -> AssistantResponse:
        refs = parse_resource_urls(request.text)
        request.resource_refs[:] = refs
        request.resource_urls[:] = [ref.url for ref in refs]
        request.resource_results[:] = await self._read_resources(refs, request.actor_id)
        logger.info("assistant_request", extra={"request_id": request.request_id})
        response = await self.model_provider.generate(request)
        self.writeback_planner.apply(request, response)
        return response

    async def _read_resources(
        self,
        refs: list[ResourceRef],
        actor_id: str,
    ) -> list[ResourceReadResult]:
        if self.resource_reader is None:
            return []
        results: list[ResourceReadResult] = []
        for ref in refs:
            if ref.type not in {
                ResourceType.FEISHU_DOC,
                ResourceType.FEISHU_SHEET,
                ResourceType.FEISHU_BITABLE,
                ResourceType.WEB,
            }:
                continue
            results.append(await self.resource_reader.read(ref, actor_id))
        return results


class WritebackPlanner:
    def __init__(self, pending_action_ttl_seconds: int) -> None:
        self.pending_action_ttl_seconds = pending_action_ttl_seconds

    def apply(self, request: AssistantRequest, response: AssistantResponse) -> None:
        if not response.action_proposals:
            response.action_proposals.extend(
                self._infer_writeback_proposals(request, response.text)
            )
            if response.action_proposals:
                response.text = _writeback_proposal_text(
                    _remove_model_writeback_plan_blocks(response.text)
                )
        if response.action_proposals:
            self._normalize_explicit_writeback_targets(
                request,
                response.action_proposals,
                model_text=response.text,
            )

    def _infer_writeback_proposals(
        self,
        request: AssistantRequest,
        model_text: str,
    ) -> list[ActionProposal]:
        planned = self._proposals_from_model_plans(request, model_text)
        if planned:
            return planned
        if not _has_writeback_intent(request.text):
            return []
        content = _writeback_content(request.text, model_text)
        if not content:
            return []
        model_operation = _model_writeback_operation(model_text)
        now = datetime.now(UTC)
        doc_ref = _first_writable_doc_ref(request)
        if doc_ref is not None:
            if model_operation == "delete" or _has_replacement_or_delete_intent(request.text):
                return []
            return [
                _proposal(
                    actor_id=request.actor_id,
                    action_type=WriteActionType.DOC_APPEND,
                    target={"document_id": doc_ref.token},
                    payload={"content": content},
                    preview=f"向文档追加文本：\n{content}",
                    now=now,
                    ttl_seconds=self.pending_action_ttl_seconds,
                )
            ]
        sheet_ref = _first_writable_sheet_ref(request)
        if sheet_ref is not None:
            range_name = _sheet_write_range(sheet_ref, request, model_text, content)
            if not range_name:
                return []
            return [
                _proposal(
                    actor_id=request.actor_id,
                    action_type=WriteActionType.SHEET_WRITE_RANGE,
                    target={"spreadsheet_token": sheet_ref.token, "range": range_name},
                    payload={"values": [[content]]},
                    preview=f"向电子表格 {range_name} 写入：{content}",
                    now=now,
                    ttl_seconds=self.pending_action_ttl_seconds,
                )
            ]
        bitable_ref = _first_writable_bitable_ref(request)
        if bitable_ref is not None and bitable_ref.table_id:
            update_plan: _BitableUpdatePlan | None = None
            if model_operation == "update":
                update_plan = _bitable_update_plan(request, model_text, content, bitable_ref)
                if update_plan is None:
                    return []
            elif model_operation == "create":
                update_plan = None
            elif model_operation == "delete":
                return []
            elif _prefers_append_or_create(
                request.text,
                model_text,
            ) and not _has_replacement_or_delete_intent(request.text):
                update_plan = None
            else:
                update_plan = _bitable_update_plan(request, model_text, content, bitable_ref)
            if update_plan is not None:
                return [
                    _proposal(
                        actor_id=request.actor_id,
                        action_type=WriteActionType.BITABLE_UPDATE_RECORD,
                        target={
                            "app_token": bitable_ref.token,
                            "table_id": bitable_ref.table_id,
                            "record_id": update_plan.record_id,
                        },
                        payload={"fields": {update_plan.field_name: update_plan.value}},
                        preview=(
                            f"更新多维表格记录 {update_plan.record_id}："
                            f"{update_plan.field_name} = {update_plan.value}"
                            f"（匹配 {update_plan.matched_by}）"
                        ),
                        now=now,
                        ttl_seconds=self.pending_action_ttl_seconds,
                    )
                ]
            if model_operation in {"update", "delete"} or _has_replacement_or_delete_intent(
                request.text
            ):
                return []
            field_name = _infer_bitable_write_field(request, content, model_text)
            if not field_name:
                return []
            return [
                _proposal(
                    actor_id=request.actor_id,
                    action_type=WriteActionType.BITABLE_CREATE_RECORD,
                    target={
                        "app_token": bitable_ref.token,
                        "table_id": bitable_ref.table_id,
                    },
                    payload={"fields": {field_name: content}},
                    preview=f"向多维表格新增记录：{field_name} = {content}",
                    now=now,
                    ttl_seconds=self.pending_action_ttl_seconds,
                )
            ]
        return []

    def _proposals_from_model_plans(
        self,
        request: AssistantRequest,
        model_text: str,
    ) -> list[ActionProposal]:
        now = datetime.now(UTC)
        proposals: list[ActionProposal] = []
        for plan in _model_writeback_plans(model_text):
            proposal = self._proposal_from_model_plan(request, plan, now=now)
            if proposal is not None:
                proposals.append(proposal)
        return proposals

    def _proposal_from_model_plan(
        self,
        request: AssistantRequest,
        plan: _ModelWritebackPlan,
        *,
        now: datetime,
    ) -> ActionProposal | None:
        operation = plan.operation
        content = _model_plan_content(plan)
        if operation == "read":
            return None
        if plan.resource_type == "doc":
            return self._doc_proposal_from_model_plan(request, plan, content, now=now)
        if plan.resource_type == "sheet":
            return self._sheet_proposal_from_model_plan(request, plan, content, now=now)
        if plan.resource_type == "bitable":
            return self._bitable_proposal_from_model_plan(request, plan, content, now=now)
        if plan.resource_type == "message" and content:
            return _proposal(
                actor_id=request.actor_id,
                action_type=WriteActionType.MESSAGE_SEND,
                target={"chat_id": request.conversation_id},
                payload={"text": content},
                preview=plan.preview or f"发送消息：{content}",
                now=now,
                ttl_seconds=self.pending_action_ttl_seconds,
            )
        return None

    def _doc_proposal_from_model_plan(
        self,
        request: AssistantRequest,
        plan: _ModelWritebackPlan,
        content: str,
        *,
        now: datetime,
    ) -> ActionProposal | None:
        if not content:
            return None
        doc_ref = _first_writable_doc_ref(request)
        if doc_ref is None:
            return None
        return _proposal(
            actor_id=request.actor_id,
            action_type=WriteActionType.DOC_APPEND,
            target={"document_id": doc_ref.token},
            payload={"content": content},
            preview=plan.preview or f"向文档追加文本：\n{content}",
            now=now,
            ttl_seconds=self.pending_action_ttl_seconds,
        )

    def _sheet_proposal_from_model_plan(
        self,
        request: AssistantRequest,
        plan: _ModelWritebackPlan,
        content: str,
        *,
        now: datetime,
    ) -> ActionProposal | None:
        if not content:
            return None
        sheet_ref = _first_writable_sheet_ref(request)
        if sheet_ref is None:
            return None
        range_name = _sheet_range_from_plan(plan)
        if range_name:
            range_name = _qualify_sheet_range(sheet_ref, range_name)
        else:
            range_name = _sheet_write_range(sheet_ref, request, "", content)
        if not range_name:
            return None
        return _proposal(
            actor_id=request.actor_id,
            action_type=WriteActionType.SHEET_WRITE_RANGE,
            target={"spreadsheet_token": sheet_ref.token, "range": range_name},
            payload={"values": [[content]]},
            preview=plan.preview or f"向电子表格 {range_name} 写入：{content}",
            now=now,
            ttl_seconds=self.pending_action_ttl_seconds,
        )

    def _bitable_proposal_from_model_plan(
        self,
        request: AssistantRequest,
        plan: _ModelWritebackPlan,
        content: str,
        *,
        now: datetime,
    ) -> ActionProposal | None:
        bitable_ref = _first_writable_bitable_ref(request)
        if bitable_ref is None or not bitable_ref.table_id:
            return None
        operation = plan.operation
        if operation == "delete":
            record_id = _bitable_record_id_from_plan(request, plan, bitable_ref)
            if not record_id:
                return None
            return _proposal(
                actor_id=request.actor_id,
                action_type=WriteActionType.BITABLE_DELETE_RECORD,
                target={
                    "app_token": bitable_ref.token,
                    "table_id": bitable_ref.table_id,
                    "record_id": record_id,
                },
                payload={},
                preview=plan.preview or f"删除多维表格记录 {record_id}",
                now=now,
                ttl_seconds=self.pending_action_ttl_seconds,
            )
        if not content:
            return None
        field_name = _bitable_field_from_plan(request, plan, content)
        if not field_name:
            return None
        if operation == "update":
            record_id = _bitable_record_id_from_plan(request, plan, bitable_ref)
            if not record_id:
                return None
            return _proposal(
                actor_id=request.actor_id,
                action_type=WriteActionType.BITABLE_UPDATE_RECORD,
                target={
                    "app_token": bitable_ref.token,
                    "table_id": bitable_ref.table_id,
                    "record_id": record_id,
                },
                payload={"fields": {field_name: content}},
                preview=plan.preview
                or f"更新多维表格记录 {record_id}：{field_name} = {content}",
                now=now,
                ttl_seconds=self.pending_action_ttl_seconds,
            )
        if operation == "create":
            return _proposal(
                actor_id=request.actor_id,
                action_type=WriteActionType.BITABLE_CREATE_RECORD,
                target={"app_token": bitable_ref.token, "table_id": bitable_ref.table_id},
                payload={"fields": {field_name: content}},
                preview=plan.preview or f"向多维表格新增记录：{field_name} = {content}",
                now=now,
                ttl_seconds=self.pending_action_ttl_seconds,
            )
        return None

    def _normalize_explicit_writeback_targets(
        self,
        request: AssistantRequest,
        proposals: list[ActionProposal],
        *,
        model_text: str,
    ) -> None:
        for proposal in proposals:
            self._normalize_model_generated_content(
                request,
                proposal,
                model_text=model_text,
            )
            if proposal.action_type != WriteActionType.SHEET_WRITE_RANGE:
                self._normalize_bitable_update_target(request, proposal, model_text=model_text)
                continue
            self._normalize_sheet_write_target(request, proposal, model_text=model_text)

    def _normalize_model_generated_content(
        self,
        request: AssistantRequest,
        proposal: ActionProposal,
        *,
        model_text: str,
    ) -> None:
        content = _model_generated_writeback_content(model_text)
        if not content:
            return
        if proposal.action_type == WriteActionType.DOC_APPEND:
            original = str(proposal.payload.get("content", ""))
            if original == content:
                return
            logger.info("doc_writeback_content_corrected action_id=%s", proposal.id)
            proposal.payload["content"] = content
            proposal.preview = f"向文档追加文本：\n{content}"
            return
        if proposal.action_type == WriteActionType.SHEET_WRITE_RANGE:
            original = _proposal_first_value(proposal)
            if original == content:
                return
            logger.info("sheet_writeback_content_corrected action_id=%s", proposal.id)
            proposal.payload["values"] = [[content]]
            range_name = str(proposal.target.get("range", ""))
            proposal.preview = f"向电子表格 {range_name} 写入：{content}"
            return
        if proposal.action_type in {
            WriteActionType.BITABLE_CREATE_RECORD,
            WriteActionType.BITABLE_UPDATE_RECORD,
        }:
            fields = proposal.payload.get("fields")
            if not isinstance(fields, dict) or len(fields) != 1:
                return
            field_name = next(iter(fields))
            if str(fields[field_name]).strip() == content:
                return
            logger.info("bitable_writeback_content_corrected action_id=%s", proposal.id)
            proposal.payload["fields"] = {field_name: content}
            ref = _first_writable_bitable_ref(request)
            if proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD:
                record_id = str(proposal.target.get("record_id", ""))
                proposal.preview = f"更新多维表格记录 {record_id}：{field_name} = {content}"
            elif ref is not None:
                proposal.preview = f"向多维表格新增记录：{field_name} = {content}"

    def _normalize_sheet_write_target(
        self,
        request: AssistantRequest,
        proposal: ActionProposal,
        *,
        model_text: str,
    ) -> None:
        ref = _sheet_ref_for_proposal(request, proposal)
        if ref is None:
            return
        preview_value = _proposal_first_value(proposal)
        intent = _sheet_write_target_intent(
            ref,
            request.text,
            model_text=model_text,
            content=preview_value,
        )
        if intent is None:
            return
        original_range = str(proposal.target.get("range", ""))
        if original_range == intent.range_name:
            return
        logger.info(
            "sheet_write_range_corrected original_range=%s requested_range=%s source=%s",
            original_range,
            intent.range_name,
            intent.source,
        )
        proposal.target["range"] = intent.range_name
        proposal.preview = f"向电子表格 {intent.range_name} 写入：{preview_value}"

    def _normalize_bitable_update_target(
        self,
        request: AssistantRequest,
        proposal: ActionProposal,
        *,
        model_text: str,
    ) -> None:
        if proposal.action_type not in {
            WriteActionType.BITABLE_CREATE_RECORD,
            WriteActionType.BITABLE_UPDATE_RECORD,
        }:
            return
        ref = _first_writable_bitable_ref(request)
        if ref is None or not ref.table_id:
            return
        model_operation = _model_writeback_operation(model_text)
        if model_operation == "update":
            value = _proposal_bitable_value(proposal) or _extract_writeback_content(request.text)
            plan = _bitable_update_plan(request, model_text, value, ref)
            if plan is None:
                return
            proposal.action_type = WriteActionType.BITABLE_UPDATE_RECORD
            proposal.target = {
                "app_token": ref.token,
                "table_id": ref.table_id,
                "record_id": plan.record_id,
            }
            proposal.payload = {"fields": {plan.field_name: plan.value}}
            proposal.preview = (
                f"更新多维表格记录 {plan.record_id}：{plan.field_name} = {plan.value}"
                f"（匹配 {plan.matched_by}）"
            )
            return
        if model_operation == "create":
            if proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD:
                proposal.action_type = WriteActionType.BITABLE_CREATE_RECORD
                proposal.target = {"app_token": ref.token, "table_id": ref.table_id}
                create_value = _proposal_bitable_value(proposal)
                fields = proposal.payload.get("fields")
                if isinstance(fields, dict) and len(fields) == 1 and create_value:
                    field_name = next(iter(fields))
                    proposal.preview = f"向多维表格新增记录：{field_name} = {create_value}"
            return
        if model_operation == "delete":
            return
        if _prefers_append_or_create(
            request.text,
            model_text,
        ) and not _has_replacement_or_delete_intent(request.text):
            if proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD:
                proposal.action_type = WriteActionType.BITABLE_CREATE_RECORD
                proposal.target = {"app_token": ref.token, "table_id": ref.table_id}
                create_value = _proposal_bitable_value(proposal)
                fields = proposal.payload.get("fields")
                if isinstance(fields, dict) and len(fields) == 1 and create_value:
                    field_name = next(iter(fields))
                    proposal.preview = f"向多维表格新增记录：{field_name} = {create_value}"
            return
        value = _proposal_bitable_value(proposal) or _extract_writeback_content(request.text)
        plan = _bitable_update_plan(request, model_text, value, ref)
        if plan is None:
            return
        proposal.action_type = WriteActionType.BITABLE_UPDATE_RECORD
        proposal.target = {
            "app_token": ref.token,
            "table_id": ref.table_id,
            "record_id": plan.record_id,
        }
        proposal.payload = {"fields": {plan.field_name: plan.value}}
        proposal.preview = (
            f"更新多维表格记录 {plan.record_id}：{plan.field_name} = {plan.value}"
            f"（匹配 {plan.matched_by}）"
        )


def _proposal(
    *,
    actor_id: str,
    action_type: WriteActionType,
    target: dict[str, Any],
    payload: dict[str, Any],
    preview: str,
    now: datetime,
    ttl_seconds: int,
) -> ActionProposal:
    return ActionProposal(
        actor_id=actor_id,
        action_type=action_type,
        target=target,
        payload=payload,
        preview=preview,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )


def _has_writeback_intent(text: str) -> bool:
    normalized = text.strip().lower()
    return any(
        phrase in normalized
        for phrase in (
            "写入",
            "写到",
            "写进",
            "写回",
            "添加",
            "添加到",
            "添加进",
            "增加",
            "增加到",
            "增加进",
            "加入",
            "加入到",
            "加入进",
            "更新",
            "更新到",
            "更新为",
            "替换",
            "替换掉",
            "覆盖",
            "修改",
            "修改为",
            "改为",
            "改成",
            "追加到",
            "追加进",
            "保存到",
            "保存进",
            "记录到",
            "记录进",
            "填入",
            "填到",
            "append to",
            "write to",
            "write back",
            "save to",
        )
    )


def _extract_writeback_content(text: str) -> str:
    quoted_patterns = [
        r"“([^”]{1,4000})”",
        r'"([^"]{1,4000})"',
        r"'([^']{1,4000})'",
        r"`([^`]{1,4000})`",
    ]
    for pattern in quoted_patterns:
        match = re.search(pattern, text, flags=re.DOTALL)
        if match:
            return match.group(1).strip()
    before_link = re.split(r"https?://", text, maxsplit=1)[0]
    for marker in (
        "写入",
        "写到",
        "写进",
        "写回",
        "添加",
        "添加到",
        "添加进",
        "增加",
        "增加到",
        "增加进",
        "加入",
        "加入到",
        "加入进",
            "更新",
            "更新到",
            "更新为",
            "替换",
            "替换掉",
            "覆盖",
            "修改",
            "修改为",
            "改为",
        "改成",
        "追加到",
        "保存到",
        "记录到",
        "填入",
    ):
        if marker in before_link:
            candidate = before_link.split(marker, 1)[0]
            candidate = re.sub(r"^请|^帮我|^把|^将", "", candidate.strip())
            if _looks_like_generation_instruction(candidate):
                return ""
            if 0 < len(candidate) <= 4000:
                return candidate.strip("：:，,。 ")
    return ""


def _looks_like_generation_instruction(text: str) -> bool:
    normalized = text.strip().lower()
    markers = (
        "写一段",
        "生成",
        "总结",
        "改写",
        "润色",
        "翻译",
        "分析",
        "起草",
        "创作",
        "回答",
        "介绍",
        "generate",
        "summarize",
        "rewrite",
        "translate",
        "draft",
    )
    return any(marker in normalized for marker in markers)


def _writeback_content(request_text: str, model_text: str) -> str:
    model_content = _model_generated_writeback_content(model_text)
    if model_content:
        return model_content
    return _extract_writeback_content(request_text)


def _model_writeback_operation(text: str) -> str | None:
    plans = _model_writeback_plans(text)
    if len(plans) == 1:
        return plans[0].operation
    return (
        _model_writeback_operation_from_labeled_lines(text)
        or _model_writeback_operation_from_markdown_table(text)
        or _model_writeback_operation_from_natural_text(text)
    )


def _model_writeback_operation_from_labeled_lines(text: str) -> str | None:
    for line in text.splitlines():
        match = _structured_label_match(line, _MODEL_OPERATION_LABELS)
        if match is None:
            continue
        operation = _crud_operation_from_natural_text(match[1])
        if operation:
            return operation
    return None


def _model_writeback_operation_from_markdown_table(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    for index in range(len(lines) - 2):
        header = _markdown_row_cells(lines[index])
        separator = _markdown_row_cells(lines[index + 1])
        row = _markdown_row_cells(lines[index + 2])
        if not header or len(header) != len(row):
            continue
        if not all(set(cell.replace(":", "").strip()) <= {"-"} for cell in separator):
            continue
        headers = _normalize_table_headers(header)
        for header_index, header_name in enumerate(headers):
            if header_name not in _MODEL_OPERATION_LABELS:
                continue
            operation = _crud_operation_from_natural_text(row[header_index])
            if operation:
                return operation
    return None


def _model_writeback_operation_from_natural_text(text: str) -> str | None:
    normalized = _normalize_markdown_text(text)
    focused = _writeback_preview_section(normalized)
    operation = _crud_operation_from_natural_text(focused)
    if operation:
        return operation
    if re.search(
        r"(?:字段|记录|那一行|第\s*[0-9]+\s*(?:行|条|条记录)).{0,80}"
        r"(?:设置为|更新为|修改为|替换为|覆盖|覆盖原有值)",
        normalized,
    ):
        return "update"
    return None


def _writeback_preview_section(text: str) -> str:
    match = re.search(
        r"(?:待写内容预览|待写内容|写入内容预览|写回预览|预览)(.*)",
        text,
        flags=re.DOTALL,
    )
    if not match:
        return text[:2000]
    return match.group(1)[:2000]


def _crud_operation_from_natural_text(value: str) -> str | None:
    normalized = _normalize_markdown_text(value).strip().lower()
    if not normalized:
        return None
    exact = _normalize_crud_operation(normalized)
    if exact:
        return exact
    if any(marker in normalized for marker in ("删除", "移除", "清空", "delete", "remove")):
        return "delete"
    if any(
        marker in normalized
        for marker in (
            "更新",
            "修改",
            "替换",
            "覆盖",
            "设置为",
            "更新为",
            "修改为",
            "改为",
            "改成",
            "update",
            "replace",
            "overwrite",
            "set ",
        )
    ):
        return "update"
    if any(
        marker in normalized
        for marker in (
            "新增",
            "创建",
            "新建",
            "追加",
            "新增记录",
            "新增一条",
            "创建记录",
            "添加记录",
            "插入新",
            "create",
            "append",
            "insert",
            "new record",
            "new row",
        )
    ):
        return "create"
    return None


def _model_writeback_plans(text: str) -> list[_ModelWritebackPlan]:
    plans: list[_ModelWritebackPlan] = []
    for obj in _model_writeback_json_objects(text):
        plans.extend(_plans_from_json_object(obj))
    return plans


def _model_writeback_json_objects(text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    fenced_pattern = r"```(?:json|JSON)?\s*(.*?)```"
    for match in re.finditer(fenced_pattern, text, flags=re.DOTALL):
        block = match.group(1).strip()
        if "fcgo_writeback" not in block and "FCGO_WRITEBACK_PLAN" not in block:
            continue
        parsed = _json_object_from_text(block)
        if parsed is not None:
            objects.append(parsed)
    marker_match = re.search(
        r"FCGO_WRITEBACK_PLAN\s*[:：]\s*(\{.*\})",
        text,
        flags=re.DOTALL,
    )
    if marker_match:
        parsed = _json_object_from_text(marker_match.group(1))
        if parsed is not None:
            objects.append(parsed)
    return objects


def _json_object_from_text(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if candidate.startswith("FCGO_WRITEBACK_PLAN"):
        candidate = re.split(r"[:：]", candidate, maxsplit=1)[-1].strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _plans_from_json_object(obj: dict[str, Any]) -> list[_ModelWritebackPlan]:
    raw: Any
    if "fcgo_writeback" in obj:
        raw = obj["fcgo_writeback"]
    elif "fcgo_writeback_plan" in obj:
        raw = obj["fcgo_writeback_plan"]
    elif "proposals" in obj:
        raw = obj["proposals"]
    else:
        raw = obj
    items = raw if isinstance(raw, list) else [raw]
    plans: list[_ModelWritebackPlan] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        operation = _normalize_crud_operation(
            item.get("operation") or item.get("crud") or item.get("action")
        )
        resource_type = _normalize_plan_resource_type(
            item.get("resource_type") or item.get("resource") or item.get("target_type")
        )
        target: dict[str, Any] = {}
        payload: dict[str, Any] = {}
        target_raw = item.get("target")
        payload_raw = item.get("payload")
        if isinstance(target_raw, dict):
            target = dict(target_raw)
        if isinstance(payload_raw, dict):
            payload = dict(payload_raw)
        preview = _optional_str(item.get("preview") or item.get("summary"))
        if operation and resource_type:
            plans.append(
                _ModelWritebackPlan(
                    operation=operation,
                    resource_type=resource_type,
                    target=target,
                    payload=payload,
                    preview=preview,
                )
            )
    return plans


def _normalize_crud_operation(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {
        "create",
        "add",
        "append",
        "insert",
        "新增",
        "添加",
        "增加",
        "加入",
        "追加",
        "增",
    }:
        return "create"
    if normalized in {
        "update",
        "modify",
        "replace",
        "overwrite",
        "更新",
        "修改",
        "替换",
        "覆盖",
        "改",
    }:
        return "update"
    if normalized in {"delete", "remove", "clear", "删除", "移除", "清空", "删"}:
        return "delete"
    if normalized in {"read", "query", "select", "get", "读取", "查询", "查看", "查"}:
        return "read"
    return None


def _normalize_plan_resource_type(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"doc", "document", "docx", "wiki_doc", "文档", "飞书文档"}:
        return "doc"
    if normalized in {"sheet", "spreadsheet", "excel", "电子表格", "表格"}:
        return "sheet"
    if normalized in {"bitable", "base", "多维表格", "多维表"}:
        return "bitable"
    if normalized in {"message", "chat", "im", "消息", "会话"}:
        return "message"
    return None


def _model_plan_content(plan: _ModelWritebackPlan) -> str:
    for key in ("content", "value", "text", "new_value", "write_value", "summary"):
        value = plan.payload.get(key)
        if _has_text_value(value):
            return _clean_generated_content(str(value))
    fields = plan.payload.get("fields")
    if isinstance(fields, dict) and len(fields) == 1:
        value = next(iter(fields.values()))
        if _has_text_value(value):
            return _clean_generated_content(str(value))
    for key in ("content", "value", "text", "new_value", "write_value"):
        value = plan.target.get(key)
        if _has_text_value(value):
            return _clean_generated_content(str(value))
    return ""


def _sheet_range_from_plan(plan: _ModelWritebackPlan) -> str | None:
    for key in ("range", "cell", "cell_range", "target_range", "position"):
        value = plan.target.get(key)
        if _has_text_value(value):
            return _normalize_single_cell_range(str(value).strip())
    return None


def _bitable_field_from_plan(
    request: AssistantRequest,
    plan: _ModelWritebackPlan,
    content: str,
) -> str | None:
    fields = plan.payload.get("fields")
    if isinstance(fields, dict) and len(fields) == 1:
        return str(next(iter(fields))).strip()
    for key in ("field", "field_name", "target_field", "column", "字段", "目标字段"):
        value = plan.target.get(key)
        if _has_text_value(value):
            requested = _matching_bitable_field(request, str(value))
            return requested or str(value).strip()
    plan_text = json.dumps(
        {"target": plan.target, "payload": plan.payload},
        ensure_ascii=False,
    )
    return _infer_bitable_write_field(request, content, plan_text)


def _matching_bitable_field(request: AssistantRequest, value: str) -> str | None:
    wanted = value.strip()
    if not wanted:
        return None
    for result in request.resource_results:
        if result.ref.type != ResourceType.FEISHU_BITABLE:
            continue
        for candidate in _field_candidates_from_content(result.content):
            if candidate.name == wanted:
                return candidate.name
    return None


def _bitable_record_id_from_plan(
    request: AssistantRequest,
    plan: _ModelWritebackPlan,
    ref: ResourceRef,
) -> str | None:
    target = plan.target
    for key in ("record_id", "recordId"):
        value = target.get(key)
        if _has_text_value(value):
            return str(value).strip()
    record = target.get("record")
    if isinstance(record, dict):
        for key in ("record_id", "recordId", "id"):
            value = record.get(key)
            if _has_text_value(value):
                return str(value).strip()
        row_index = _row_index_from_plan_value(record.get("row_index") or record.get("index"))
    else:
        row_index = _row_index_from_plan_value(
            record or target.get("row_index") or target.get("index")
        )
    context = _bitable_context_for_ref(request, ref)
    if context is None:
        return None
    records = _bitable_records_from_content(context.content)
    if not records:
        return None
    if row_index == -1:
        record_match = max(records, key=lambda candidate: candidate.index, default=None)
        return record_match.record_id if record_match else None
    if row_index is not None:
        for candidate in records:
            if candidate.index == row_index:
                return candidate.record_id
    return None


def _row_index_from_plan_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"last", "latest", "最后", "最后一行", "最后一条", "最后一条记录"}:
        return -1
    match = re.search(r"([0-9]+)", normalized)
    return int(match.group(1)) if match else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _remove_model_writeback_plan_blocks(text: str) -> str:
    cleaned = re.sub(
        r"```(?:json|JSON)?\s*.*?fcgo_writeback.*?```",
        "",
        text,
        flags=re.DOTALL,
    )
    cleaned = re.sub(
        r"FCGO_WRITEBACK_PLAN\s*[:：]\s*\{.*\}\s*$",
        "",
        cleaned,
        flags=re.DOTALL,
    )
    return cleaned.strip()


def _model_generated_writeback_content(text: str) -> str:
    lines = text.splitlines()
    explicit_content = _explicit_generated_content_inside_block(lines, 0)
    if _is_usable_generated_content(explicit_content):
        return explicit_content
    for index, line in enumerate(lines):
        if not _line_has_generated_content_label(line):
            continue
        inline_content = _inline_content_after_label(line)
        if _is_usable_generated_content(inline_content):
            return inline_content
        block_content = _content_block_after_label(lines, index + 1)
        if _is_usable_generated_content(block_content):
            return block_content
        table_content = _generated_content_from_markdown_table(lines, index + 1)
        if _is_usable_generated_content(table_content):
            return table_content
    return ""


def _line_has_generated_content_label(line: str) -> bool:
    return _structured_label_match(line, _GENERATED_CONTENT_LABELS) is not None


def _inline_content_after_label(line: str) -> str:
    match = _structured_label_match(
        line,
        (*_EXPLICIT_VALUE_LABELS, *_GENERATED_CONTENT_LABELS),
    )
    return match[1] if match is not None else ""


def _content_block_after_label(lines: list[str], start_index: int) -> str:
    explicit_content = _explicit_generated_content_inside_block(lines, start_index)
    if explicit_content:
        return explicit_content
    table_content = _generated_content_from_markdown_table(lines, start_index)
    if table_content:
        return table_content
    return _content_block_after_value_label(lines, start_index)


def _generated_content_from_markdown_table(lines: list[str], start_index: int) -> str:
    for index in range(start_index, min(len(lines) - 2, start_index + 40)):
        header_line = lines[index].strip()
        separator_line = lines[index + 1].strip()
        if not _is_markdown_table_line(header_line):
            continue
        if not _is_markdown_table_separator(separator_line):
            continue
        headers = _normalize_table_headers(_markdown_row_cells(header_line))
        value_index = _value_column_index(headers)
        if value_index is None:
            continue
        for row_line in lines[index + 2 : min(len(lines), index + 12)]:
            stripped = row_line.strip()
            if not _is_markdown_table_line(stripped):
                break
            row = _markdown_row_cells(stripped)
            if value_index >= len(row):
                continue
            value = _clean_generated_content(row[value_index])
            if _is_usable_generated_content(value):
                return value
    return ""


def _value_column_index(headers: list[str]) -> int | None:
    value_headers = {
        "写入值",
        "写入内容",
        "新值",
        "最终内容",
        "生成结果",
        "生成内容",
        "总结结果",
        "摘要内容",
        "内容",
    }
    for index, header in enumerate(headers):
        if header.strip() in value_headers:
            return index
    return None


def _is_markdown_table_line(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _is_markdown_table_separator(line: str) -> bool:
    if not _is_markdown_table_line(line):
        return False
    cells = _markdown_row_cells(line)
    return bool(cells) and all(set(cell.replace(":", "").strip()) <= {"-"} for cell in cells)


def _content_block_after_value_label(lines: list[str], start_index: int) -> str:
    index = start_index
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return ""
    stripped = lines[index].strip()
    if stripped.startswith("```"):
        return _fenced_content(lines, index)
    if stripped.startswith(">"):
        return _blockquote_content(lines, index)
    if _line_looks_like_writeback_metadata(stripped):
        return ""
    return _plain_generated_content(lines, index)


def _explicit_generated_content_inside_block(lines: list[str], start_index: int) -> str:
    for index in range(start_index, min(len(lines), start_index + 30)):
        stripped = lines[index].strip()
        if not stripped:
            continue
        if _is_generated_content_stop_line(stripped):
            break
        if not _line_has_explicit_content_value_label(stripped):
            continue
        inline_content = _inline_content_after_label(stripped)
        if _is_usable_generated_content(inline_content):
            return inline_content
        block_content = _content_block_after_value_label(lines, index + 1)
        if _is_usable_generated_content(block_content):
            return block_content
    return ""


def _line_has_explicit_content_value_label(line: str) -> bool:
    return _structured_label_match(line, _EXPLICIT_VALUE_LABELS) is not None


def _fenced_content(lines: list[str], start_index: int) -> str:
    collected: list[str] = []
    for line in lines[start_index + 1 :]:
        if line.strip().startswith("```"):
            break
        collected.append(line)
    return _clean_generated_content("\n".join(collected))


def _blockquote_content(lines: list[str], start_index: int) -> str:
    collected: list[str] = []
    for line in lines[start_index:]:
        stripped = line.strip()
        if not stripped:
            if collected:
                break
            continue
        if not stripped.startswith(">"):
            break
        collected.append(stripped.removeprefix(">").strip())
    return _clean_generated_content("\n".join(collected))


def _plain_generated_content(lines: list[str], start_index: int) -> str:
    collected: list[str] = []
    for line in lines[start_index:]:
        stripped = line.strip()
        if not stripped:
            if collected:
                break
            continue
        if _is_generated_content_stop_line(stripped):
            break
        if _line_looks_like_writeback_metadata(stripped):
            break
        if _line_has_generated_content_label(stripped) and collected:
            break
        collected.append(line)
    return _clean_generated_content("\n".join(collected))


def _is_generated_content_stop_line(line: str) -> bool:
    normalized = _normalize_markdown_text(line).strip()
    if normalized in {"---", "----"}:
        return True
    stop_markers = (
        "请查看",
        "请确认",
        "请确认上述预览",
        "请在飞书卡片",
        "是否需要",
        "我已准备好写回预览",
        "待确认写回",
        "动作 ID",
        "操作说明",
        "确认后",
    )
    return any(normalized.startswith(marker) for marker in stop_markers)


def _clean_generated_content(content: str) -> str:
    cleaned = content.strip()
    cleaned = re.sub(r"^[-*]\s*", "", cleaned)
    cleaned = re.sub(r"^>\s*", "", cleaned)
    cleaned = cleaned.strip(" \t\r\n")
    if len(cleaned) >= 2:
        wrappers = (("`", "`"), ("“", "”"), ('"', '"'), ("'", "'"))
        for left, right in wrappers:
            if cleaned.startswith(left) and cleaned.endswith(right):
                return cleaned[1:-1].strip(" \t\r\n")
    return cleaned


def _structured_label_match(
    line: str,
    labels: tuple[str, ...],
) -> tuple[str, str] | None:
    normalized = _normalize_label_line(line)
    for label in labels:
        match = re.match(rf"^{re.escape(label)}\s*(?:[:：]\s*(.*))?$", normalized)
        if match:
            return label, _clean_generated_content(match.group(1) or "")
    return None


def _normalize_label_line(line: str) -> str:
    normalized = _normalize_markdown_text(line).strip()
    normalized = re.sub(r"^\s*#+\s*", "", normalized)
    normalized = re.sub(r"^\s*[-*]\s*", "", normalized)
    return normalized.strip()


def _line_looks_like_writeback_metadata(line: str) -> bool:
    if _structured_label_match(line, _WRITEBACK_METADATA_LABELS) is not None:
        return True
    normalized = _normalize_markdown_text(line).strip()
    target_markers = ("目标", "位置", "字段", "记录", "单元格", "范围")
    action_markers = ("写入", "写到", "覆盖", "替换", "更新")
    return any(marker in normalized for marker in target_markers) and any(
        marker in normalized for marker in action_markers
    )


def _is_usable_generated_content(content: str) -> bool:
    if not content.strip():
        return False
    stripped = content.strip()
    if stripped.startswith("|"):
        return False
    if _is_generated_content_stop_line(stripped):
        return False
    return len(stripped) <= 20_000


def _has_replacement_or_delete_intent(text: str) -> bool:
    normalized = _strip_quoted_content(text).strip().lower()
    return any(
        phrase in normalized
        for phrase in (
            "替换",
            "覆盖",
            "删除",
            "清空",
            "移除",
            "改成",
            "更新",
            "replace",
            "overwrite",
            "delete",
            "clear",
            "remove",
            "update",
        )
    )


def _strip_quoted_content(text: str) -> str:
    stripped = text
    for pattern in (
        r"“[^”]{0,4000}”",
        r'"[^"]{0,4000}"',
        r"'[^']{0,4000}'",
        r"`[^`]{0,4000}`",
    ):
        stripped = re.sub(pattern, "", stripped, flags=re.DOTALL)
    return stripped


def _writeback_proposal_text(original_text: str) -> str:
    prefix = "我已准备好写回预览，请在卡片中确认后执行。"
    if not original_text.strip():
        return prefix
    if "建议操作" in original_text or "手动写入" in original_text:
        return prefix
    return f"{original_text.strip()}\n\n{prefix}"


def _sheet_ref_for_proposal(
    request: AssistantRequest,
    proposal: ActionProposal,
) -> ResourceRef | None:
    spreadsheet_token = proposal.target.get("spreadsheet_token")
    refs = [result.ref for result in request.resource_results] + request.resource_refs
    for ref in refs:
        if ref.type != ResourceType.FEISHU_SHEET:
            continue
        if not spreadsheet_token or ref.token == spreadsheet_token:
            return ref
    return _first_writable_sheet_ref(request)


def _proposal_first_value(proposal: ActionProposal) -> str:
    values = proposal.payload.get("values")
    if isinstance(values, list) and values:
        first_row = values[0]
        if isinstance(first_row, list) and first_row:
            return str(first_row[0])
    return str(proposal.payload)


def _proposal_bitable_value(proposal: ActionProposal) -> str | None:
    fields = proposal.payload.get("fields")
    if not isinstance(fields, dict):
        return None
    for value in fields.values():
        if _has_text_value(value):
            return str(value).strip()
    return None


def _first_writable_doc_ref(request: AssistantRequest) -> ResourceRef | None:
    for result in request.resource_results:
        if result.error:
            continue
        if result.ref.type == ResourceType.FEISHU_DOC and result.ref.token:
            return result.ref
    return next(
        (
            ref
            for ref in request.resource_refs
            if ref.type == ResourceType.FEISHU_DOC and ref.token and ref.source_kind != "wiki"
        ),
        None,
    )


def _first_writable_sheet_ref(request: AssistantRequest) -> ResourceRef | None:
    for result in request.resource_results:
        if result.error:
            continue
        if result.ref.type == ResourceType.FEISHU_SHEET and result.ref.token:
            return result.ref
    return next(
        (
            ref
            for ref in request.resource_refs
            if ref.type == ResourceType.FEISHU_SHEET and ref.token and ref.source_kind != "wiki"
        ),
        None,
    )


def _first_writable_bitable_ref(request: AssistantRequest) -> ResourceRef | None:
    for result in request.resource_results:
        if result.ref.type == ResourceType.FEISHU_BITABLE and result.ref.token:
            return result.ref
    return next(
        (
            ref
            for ref in request.resource_refs
            if ref.type == ResourceType.FEISHU_BITABLE and ref.token and ref.source_kind != "wiki"
        ),
        None,
    )


def _sheet_write_range(
    ref: ResourceRef,
    request: AssistantRequest,
    model_text: str,
    content: str,
) -> str:
    range_hint = (ref.range_hint or "").strip()
    if range_hint:
        normalized = _normalize_single_cell_range(range_hint)
        if "!" in range_hint:
            return normalized
        if ref.sheet_id:
            return f"{ref.sheet_id}!{normalized}"
        return normalized
    intent = _sheet_write_target_intent(
        ref,
        request.text,
        model_text=model_text,
        content=content,
    )
    if intent is not None:
        return intent.range_name
    if _has_replacement_or_delete_intent(request.text):
        return ""
    append_row = _sheet_append_row(request, ref) or 1
    append_cell = f"A{append_row}"
    if ref.sheet_id:
        return f"{ref.sheet_id}!{append_cell}:{append_cell}"
    return f"{append_cell}:{append_cell}"


def _sheet_append_row(request: AssistantRequest, ref: ResourceRef) -> int | None:
    for result in request.resource_results:
        if result.ref.type != ResourceType.FEISHU_SHEET:
            continue
        if ref.token and result.ref.token and result.ref.token != ref.token:
            continue
        match = re.search(r"建议追加起始行\s*[:：]\s*([1-9][0-9]*)", result.content)
        if match:
            return int(match.group(1))
        row_count = _sheet_markdown_data_row_count(result.content)
        if row_count is not None:
            return row_count + 1
    return None


def _sheet_markdown_data_row_count(content: str) -> int | None:
    lines = [line.strip() for line in content.splitlines()]
    table_lines = [line for line in lines if line.startswith("|") and line.endswith("|")]
    if len(table_lines) < 2:
        return None
    body_lines = [
        line
        for line in table_lines[2:]
        if any(cell.strip() for cell in line.strip("|").split("|"))
    ]
    return len(body_lines) + 1 if table_lines else None


def _sheet_write_target_intent(
    ref: ResourceRef,
    request_text: str,
    *,
    model_text: str,
    content: str,
) -> _SheetTargetIntent | None:
    request_range = _sheet_range_from_text(request_text)
    if request_range:
        return _SheetTargetIntent(
            range_name=_qualify_sheet_range(ref, request_range),
            source="request",
        )
    model_range = _sheet_range_from_model_text(model_text, content)
    if model_range:
        return _SheetTargetIntent(
            range_name=_qualify_sheet_range(ref, model_range),
            source="model_text",
        )
    if _prefers_append_or_create(request_text, model_text):
        return None
    return None


def _qualify_sheet_range(ref: ResourceRef, range_name: str) -> str:
    normalized = _normalize_single_cell_range(range_name)
    if "!" in normalized:
        sheet_prefix, cell_range = normalized.split("!", 1)
        if ref.sheet_id:
            return f"{ref.sheet_id}!{cell_range}"
        return f"{sheet_prefix}!{cell_range}"
    if ref.sheet_id:
        return f"{ref.sheet_id}!{normalized}"
    return normalized


def _normalize_single_cell_range(range_name: str) -> str:
    if ":" in range_name:
        return range_name
    if "!" in range_name:
        sheet_id, cell = range_name.split("!", 1)
        if _is_cell_ref(cell):
            return f"{sheet_id}!{cell}:{cell}"
        return range_name
    if _is_cell_ref(range_name):
        return f"{range_name}:{range_name}"
    return range_name


def _sheet_range_from_text(text: str) -> str | None:
    command_text = _strip_quoted_content(re.split(r"https?://", text, maxsplit=1)[0])
    cell = r"[A-Za-z]{1,3}[1-9][0-9]*"
    range_ref = rf"{cell}(?:\s*[:：]\s*{cell})?"
    patterns = (
        rf"(?:写入|写到|写进|填入|填到|填进|加入|加入到|加入进|放到|放入|位置|单元格|格|range|cell)\s*[:：=]?\s*({range_ref})",
        rf"({range_ref})\s*(?:格|单元格|范围|range|cell)",
    )
    for pattern in patterns:
        match = re.search(pattern, command_text, flags=re.IGNORECASE)
        if not match:
            continue
        range_name = re.sub(r"\s+", "", match.group(1)).replace("：", ":").upper()
        return _normalize_single_cell_range(range_name)
    row_column_range = _sheet_row_column_range_from_text(command_text)
    if row_column_range:
        return row_column_range
    return None


def _sheet_range_from_model_text(text: str, content: str) -> str | None:
    if not text.strip():
        return None
    content = content.strip()
    for line in text.splitlines():
        if content and content in line:
            range_name = _sheet_range_from_qualified_text(line)
            if range_name:
                return range_name
    for line in text.splitlines():
        if not _line_looks_like_sheet_write_target(line):
            continue
        range_name = _sheet_range_from_qualified_text(line)
        if range_name:
            return range_name
    return _sheet_range_from_text(text)


def _line_looks_like_sheet_write_target(line: str) -> bool:
    normalized = line.strip().lower()
    return any(
        marker in normalized
        for marker in (
            "位置",
            "写入",
            "写到",
            "写进",
            "更新",
            "修改",
            "单元格",
            "新值",
            "target",
            "range",
            "cell",
            "write",
            "update",
        )
    )


def _sheet_range_from_qualified_text(text: str) -> str | None:
    cell = r"[A-Za-z]{1,3}[1-9][0-9]*"
    sheet = r"[A-Za-z0-9_\-\u4e00-\u9fff]+"
    pattern = (
        rf"(?<![A-Za-z0-9])(?:{sheet}!)?({cell})"
        rf"(?:\s*[:：]\s*(?:{sheet}!)?({cell}))?"
        rf"(?![A-Za-z0-9])"
    )
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        start = match.group(1).upper()
        end = (match.group(2) or start).upper()
        return _normalize_single_cell_range(f"{start}:{end}")
    return None


def _sheet_row_column_range_from_text(text: str) -> str | None:
    number = r"[0-9]+|[零〇一二两三四五六七八九十百千万]+"
    row_first_patterns = (
        rf"第?\s*({number})\s*行\s*第?\s*({number})\s*(?:列|栏)",
        rf"row\s*({number})\s*(?:column|col)\s*({number})",
    )
    for pattern in row_first_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            row = _parse_sheet_position_number(match.group(1))
            column = _parse_sheet_position_number(match.group(2))
            return _cell_range_from_row_column(row, column)
    column_first_patterns = (
        rf"第?\s*({number})\s*(?:列|栏)\s*第?\s*({number})\s*行",
        rf"(?:column|col)\s*({number})\s*row\s*({number})",
    )
    for pattern in column_first_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            column = _parse_sheet_position_number(match.group(1))
            row = _parse_sheet_position_number(match.group(2))
            return _cell_range_from_row_column(row, column)
    return None


def _cell_range_from_row_column(row: int | None, column: int | None) -> str | None:
    if row is None or column is None or row < 1 or column < 1:
        return None
    cell = f"{_column_number_to_letters(column)}{row}"
    return f"{cell}:{cell}"


def _column_number_to_letters(column: int) -> str:
    letters = ""
    while column > 0:
        column, remainder = divmod(column - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _parse_sheet_position_number(value: str) -> int | None:
    normalized = value.strip()
    if normalized.isdigit():
        return int(normalized)
    return _parse_chinese_integer(normalized)


def _parse_chinese_integer(value: str) -> int | None:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = 0
    section = 0
    number = 0
    seen = False
    for char in value:
        if char in digits:
            number = digits[char]
            seen = True
            continue
        unit = units.get(char)
        if unit is None:
            return None
        seen = True
        if unit == 10000:
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
            continue
        section += (number or 1) * unit
        number = 0
    if not seen:
        return None
    return total + section + number


def _is_cell_ref(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]+[1-9][0-9]*", value.strip()))


def _prefers_append_or_create(request_text: str, model_text: str = "") -> bool:
    normalized = _normalize_markdown_text(f"{request_text}\n{model_text}").lower()
    markers = (
        "最后一行",
        "最后一条",
        "最后一条记录",
        "表的最后",
        "末尾",
        "追加",
        "追加到",
        "新增记录",
        "新增一条",
        "新建记录",
        "添加记录",
        "增加",
        "增加到",
        "增加进",
        "插入新",
        "append",
        "new row",
        "new record",
    )
    return any(marker in normalized for marker in markers)


def _bitable_update_plan(
    request: AssistantRequest,
    model_text: str,
    content: str,
    ref: ResourceRef,
) -> _BitableUpdatePlan | None:
    context = _bitable_context_for_ref(request, ref)
    if context is None:
        return None
    candidates = _field_candidates_from_content(context.content)
    if not candidates:
        return None
    records = _bitable_records_from_content(context.content)
    if not records:
        return None
    field_name = _bitable_update_field(request.text, model_text, candidates, content)
    if not field_name:
        return None
    selector = _bitable_match_selector(request.text, model_text, field_name, content)
    if selector is None:
        return None
    record = _find_bitable_record(records, selector, field_name)
    if record is None or not record.record_id:
        return None
    matched_by = _bitable_matched_by(record, selector)
    return _BitableUpdatePlan(
        record_id=record.record_id,
        field_name=field_name,
        value=content,
        matched_by=matched_by,
    )


def _bitable_context_for_ref(
    request: AssistantRequest,
    ref: ResourceRef,
) -> ResourceReadResult | None:
    for result in request.resource_results:
        if result.ref.type != ResourceType.FEISHU_BITABLE:
            continue
        if ref.token and result.ref.token and ref.token != result.ref.token:
            continue
        return result
    return None


def _bitable_update_field(
    request_text: str,
    model_text: str,
    candidates: list[_BitableFieldCandidate],
    content: str,
) -> str | None:
    model_field = _bitable_update_field_from_model_text(model_text, candidates, content)
    if model_field:
        return model_field
    request_field = _bitable_update_field_from_request(request_text, candidates, content)
    return request_field


def _bitable_update_field_from_model_text(
    text: str,
    candidates: list[_BitableFieldCandidate],
    content: str,
) -> str | None:
    normalized = _normalize_markdown_text(text)
    explicit_field = _explicit_bitable_target_field(normalized, candidates)
    if explicit_field:
        return explicit_field
    scored: list[tuple[int, int, _BitableFieldCandidate]] = []
    for candidate in candidates:
        score = 0
        name = candidate.name.strip()
        for match in re.finditer(re.escape(name), normalized, flags=re.IGNORECASE):
            window = normalized[max(0, match.start() - 24) : match.end() + 36]
            after = normalized[match.end() : match.end() + 16]
            if re.match(r"^[\s”\"']*字段\s*值为", after):
                score -= 50
            if re.match(r"^[\s”\"']*字段\s*(?:设置为|更新为|修改为|写入)", after):
                score += 30
            update_markers = ("待更新", "设置为", "更新", "修改", "写入", "新值")
            if any(marker in window for marker in update_markers):
                score += 35
            if content and content in window:
                score += 10
        if score:
            scored.append((score, -candidate.index, candidate))
    if not scored:
        return None
    return max(scored, key=lambda item: (item[0], item[1]))[2].name


def _explicit_bitable_target_field(
    text: str,
    candidates: list[_BitableFieldCandidate],
) -> str | None:
    label_candidates = ("目标字段", "目标列", "写入字段", "更新字段", "字段")
    for line in text.splitlines():
        match = _structured_label_match(line, label_candidates)
        if match is None or not match[1]:
            continue
        field_name = _matching_bitable_field_name(candidates, match[1])
        if field_name:
            return field_name
    patterns = (
        r"的\s*[「“\"`]?([^」”\"`\s]+)[」”\"`]?\s*字段",
        r"目标字段\s*[:：]\s*[「“\"`]?([^」”\"`\n]+)[」”\"`]?",
        r"目标列\s*[:：]\s*[「“\"`]?([^」”\"`\n]+)[」”\"`]?",
    )
    for pattern in patterns:
        for regex_match in re.finditer(pattern, text):
            field_name = _matching_bitable_field_name(candidates, regex_match.group(1))
            if field_name:
                return field_name
    return None


def _matching_bitable_field_name(
    candidates: list[_BitableFieldCandidate],
    value: str,
) -> str | None:
    normalized = _normalize_match_value(re.sub(r"[（(].*?[）)]", "", value).strip())
    for candidate in candidates:
        if candidate.name == normalized:
            return candidate.name
    for candidate in candidates:
        if candidate.name in normalized:
            return candidate.name
    return None


def _bitable_update_field_from_request(
    text: str,
    candidates: list[_BitableFieldCandidate],
    content: str,
) -> str | None:
    clean = _strip_quoted_content(re.split(r"https?://", text, maxsplit=1)[0])
    scored: list[tuple[int, int, _BitableFieldCandidate]] = []
    for candidate in candidates:
        name = candidate.name.strip()
        if name not in clean:
            continue
        score = 5
        for match in re.finditer(re.escape(name), clean, flags=re.IGNORECASE):
            window = clean[max(0, match.start() - 16) : match.end() + 20]
            request_markers = ("字段", "列", "行", "写入", "添加", "增加", "加入", "更新")
            if any(marker in window for marker in request_markers):
                score += 15
            if content and content in window:
                score += 5
        scored.append((score, -candidate.index, candidate))
    if not scored:
        requested = _requested_bitable_field(text, candidates)
        return requested.name if requested else None
    return max(scored, key=lambda item: (item[0], item[1]))[2].name


def _bitable_match_selector(
    request_text: str,
    model_text: str,
    target_field: str,
    content: str,
) -> _BitableMatchSelector | None:
    selector = _bitable_match_selector_from_model_text(model_text, target_field, content)
    if selector is not None:
        return selector
    return _bitable_match_selector_from_request(request_text, target_field, content)


def _bitable_match_selector_from_model_text(
    text: str,
    target_field: str,
    content: str,
) -> _BitableMatchSelector | None:
    normalized = _normalize_markdown_text(text)
    record_id = _bitable_record_id_from_text(normalized)
    if record_id:
        return _BitableMatchSelector(
            field_name=None,
            value=None,
            row_index=_bitable_row_index_from_text(normalized),
            record_id=record_id,
        )
    field_value_match = re.search(
        r"[“\"]([^”\"]+)[”\"]字段值为[“\"]([^”\"]+)[”\"]",
        normalized,
    )
    if field_value_match:
        return _BitableMatchSelector(
            field_name=field_value_match.group(1).strip(),
            value=field_value_match.group(2).strip(),
            row_index=_bitable_row_index_from_text(normalized),
        )
    table_selector = _bitable_match_selector_from_model_table(
        normalized,
        target_field,
        content,
    )
    if table_selector is not None:
        return table_selector
    row_index = _bitable_row_index_from_text(normalized)
    if row_index is not None:
        return _BitableMatchSelector(field_name=None, value=None, row_index=row_index)
    return None


def _bitable_match_selector_from_model_table(
    text: str,
    target_field: str,
    content: str,
) -> _BitableMatchSelector | None:
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    for index in range(len(lines) - 2):
        header = _markdown_row_cells(lines[index])
        separator = _markdown_row_cells(lines[index + 1])
        row = _markdown_row_cells(lines[index + 2])
        if not header or len(header) != len(row):
            continue
        if not all(set(cell.replace(":", "").strip()) <= {"-"} for cell in separator):
            continue
        if target_field not in _normalize_table_headers(header):
            continue
        if content and content not in row:
            continue
        for field_name, value in zip(_normalize_table_headers(header), row, strict=False):
            if field_name == target_field or not value or value == content:
                continue
            return _BitableMatchSelector(field_name=field_name, value=value)
    return None


def _bitable_match_selector_from_request(
    text: str,
    target_field: str,
    content: str,
) -> _BitableMatchSelector | None:
    clean = _strip_quoted_content(re.split(r"https?://", text, maxsplit=1)[0])
    escaped_target = re.escape(target_field)
    patterns = (
        rf"(?:添加到|加入到|写入到|填入到|更新到|修改到|添加|加入|写入|填入)\s*(.+?)\s*的\s*{escaped_target}",
        rf"(.+?)\s*的\s*{escaped_target}\s*(?:行|列|字段)?",
    )
    for pattern in patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if not match:
            continue
        value = _normalize_match_value(match.group(1))
        if value and value != content:
            return _BitableMatchSelector(field_name=None, value=value)
    row_index = _bitable_row_index_from_text(clean)
    if row_index is not None:
        return _BitableMatchSelector(field_name=None, value=None, row_index=row_index)
    return None


def _find_bitable_record(
    records: list[_BitableRecordCandidate],
    selector: _BitableMatchSelector,
    target_field: str,
) -> _BitableRecordCandidate | None:
    if selector.record_id:
        for record in records:
            if record.record_id == selector.record_id:
                return record
        return _BitableRecordCandidate(
            index=selector.row_index or 0,
            record_id=selector.record_id,
            fields={},
        )
    if selector.row_index is not None:
        if selector.row_index == -1:
            return max(records, key=lambda record: record.index, default=None)
        for record in records:
            if record.index == selector.row_index:
                return record
    if not selector.value:
        return None
    matches: list[_BitableRecordCandidate] = []
    for record in records:
        if selector.field_name:
            value = record.fields.get(selector.field_name)
            if value == selector.value:
                matches.append(record)
            continue
        if any(
            field_name != target_field and value == selector.value
            for field_name, value in record.fields.items()
        ):
            matches.append(record)
    return matches[0] if len(matches) == 1 else None


def _bitable_matched_by(
    record: _BitableRecordCandidate,
    selector: _BitableMatchSelector,
) -> str:
    if selector.record_id:
        return f"record_id = {selector.record_id}"
    if selector.field_name and selector.value:
        return f"{selector.field_name} = {selector.value}"
    if selector.row_index == -1:
        return "最后一条记录"
    if selector.row_index is not None:
        return f"第 {record.index} 条记录"
    if selector.value:
        return f"记录值 = {selector.value}"
    return f"record_id = {record.record_id}"


def _bitable_records_from_content(content: str) -> list[_BitableRecordCandidate]:
    records: list[_BitableRecordCandidate] = []
    for line in content.splitlines():
        match = re.match(r"^-\s*第\s*([0-9]+)\s*条\s*[:：]\s*(.+)$", line.strip())
        if not match:
            continue
        index = int(match.group(1))
        fields: dict[str, str] = {}
        record_id: str | None = None
        for part in re.split(r"[；;]", match.group(2)):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key == "record_id":
                record_id = value
            elif key:
                fields[key] = value
        records.append(_BitableRecordCandidate(index=index, record_id=record_id, fields=fields))
    return records


def _bitable_record_id_from_text(text: str) -> str | None:
    patterns = (
        r"record_id\s*[:：=]\s*`?([A-Za-z0-9_-]{3,})`?",
        r"记录\s*ID\s*[:：=]\s*`?([A-Za-z0-9_-]{3,})`?",
        r"目标记录\s*[:：]\s*`?([A-Za-z0-9_-]{3,})`?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip("`")
    return None


def _bitable_row_index_from_text(text: str) -> int | None:
    match = re.search(r"第\s*([0-9]+)\s*(?:行|条|条记录|行记录)", text)
    if match:
        return int(match.group(1))
    if any(marker in text for marker in ("最后一行", "最后一条", "最后一条记录")):
        return -1
    return None


def _markdown_row_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _normalize_table_headers(headers: list[str]) -> list[str]:
    return [re.sub(r"[（(].*?[）)]", "", header).strip() for header in headers]


def _normalize_markdown_text(text: str) -> str:
    return text.replace("**", "").replace("__", "")


def _normalize_match_value(value: str) -> str:
    return re.sub(r"^[\s：:，,。\"'`“”]+|[\s：:，,。\"'`“”]+$", "", value)


def _has_text_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _infer_bitable_write_field(
    request: AssistantRequest,
    content: str,
    model_text: str,
) -> str | None:
    candidates: list[_BitableFieldCandidate] = []
    for result in request.resource_results:
        if result.ref.type != ResourceType.FEISHU_BITABLE:
            continue
        candidates.extend(_field_candidates_from_content(result.content))
    requested = _requested_bitable_field(request.text, candidates)
    if requested:
        return requested.name
    model_requested = _requested_bitable_field(model_text, candidates)
    if model_requested:
        return model_requested.name
    selected = _best_bitable_field(candidates, request.text, content)
    return selected.name if selected else None


def _field_candidates_from_content(content: str) -> list[_BitableFieldCandidate]:
    candidates: list[_BitableFieldCandidate] = []
    lines = content.splitlines()
    in_field_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "字段：":
            in_field_section = True
            continue
        if stripped in {"记录索引：", "记录摘录："}:
            in_field_section = False
        bullet_match = re.match(
            r"^-\s+(.+?)\s*(?:[（(]\s*类型\s*[:：]?\s*([^）)]+)\s*[）)])?\s*$",
            stripped,
        )
        if bullet_match and in_field_section:
            name = bullet_match.group(1).strip()
            candidates.append(
                _BitableFieldCandidate(
                    name=name,
                    type_hint=_normalize_optional_text(bullet_match.group(2)),
                    index=index,
                )
            )
            continue
        table_match = re.match(r"^\|\s*(.+?)\s*\|$", stripped)
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        is_header_row = next_line.startswith("|") and set(next_line.replace("|", "").strip()) <= {
            "-",
            ":",
            " ",
        }
        if table_match and is_header_row:
            candidates.extend(
                _BitableFieldCandidate(name=cell.strip(), index=index)
                for cell in table_match.group(1).split("|")
            )
    return _dedupe_bitable_fields(
        candidate
        for candidate in candidates
        if _is_valid_bitable_field_name(candidate.name)
    )


def _best_bitable_field(
    candidates: list[_BitableFieldCandidate],
    request_text: str,
    content: str,
) -> _BitableFieldCandidate | None:
    if not candidates:
        return None
    scored = [
        (
            _bitable_field_score(candidate, request_text, content),
            -candidate.index,
            candidate,
        )
        for candidate in candidates
    ]
    return max(scored, key=lambda item: (item[0], item[1]))[2]


def _requested_bitable_field(
    text: str,
    candidates: list[_BitableFieldCandidate],
) -> _BitableFieldCandidate | None:
    if not candidates:
        return None
    normalized = text.replace("：", ":")
    for candidate in candidates:
        escaped = re.escape(candidate.name)
        patterns = (
            rf"(?:字段|列|field)\s*[:=]?\s*{escaped}",
            rf"{escaped}\s*(?:字段|列)",
            rf"(?:写入|写到|写进|填入|填到|记录到|增加|增加到|增加进)\s*{escaped}",
        )
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns):
            return candidate
    return None


def _bitable_field_score(
    candidate: _BitableFieldCandidate,
    request_text: str,
    content: str,
) -> int:
    name = candidate.name.strip()
    normalized_name = name.lower()
    type_hint = (candidate.type_hint or "").lower()
    score = 0
    if _is_text_like_bitable_type(type_hint):
        score += 35
    if _is_general_text_field_name(normalized_name):
        score += 18
    if _field_name_matches_task(name, request_text):
        score += 16
    if _field_name_matches_task(name, content):
        score += 8
    if _is_likely_system_or_non_text_field(normalized_name, type_hint):
        score -= 30
    return score


def _is_text_like_bitable_type(type_hint: str) -> bool:
    if not type_hint:
        return False
    text_type_markers = ("text", "string", "文本", "多行", "1")
    return any(marker in type_hint for marker in text_type_markers)


def _is_general_text_field_name(normalized_name: str) -> bool:
    text_name_markers = (
        "内容",
        "文本",
        "备注",
        "说明",
        "描述",
        "标题",
        "名称",
        "关键词",
        "需求",
        "任务",
        "name",
        "title",
        "description",
        "note",
    )
    return any(marker in normalized_name for marker in text_name_markers)


def _field_name_matches_task(field_name: str, text: str) -> bool:
    normalized_field = field_name.strip().lower()
    if not normalized_field:
        return False
    normalized_text = text.lower()
    if normalized_field in normalized_text:
        return True
    tokens = [
        token
        for token in re.split(r"[\s/_\-:：,，.。()（）]+", normalized_field)
        if len(token) >= 2
    ]
    return any(token in normalized_text for token in tokens)


def _is_likely_system_or_non_text_field(normalized_name: str, type_hint: str) -> bool:
    non_text_name_markers = (
        "日期",
        "时间",
        "附件",
        "图片",
        "文件",
        "链接",
        "人员",
        "用户",
        "状态",
        "进度",
        "公式",
        "编号",
        "创建",
        "修改",
        "更新时间",
        "创建时间",
    )
    non_text_type_markers = (
        "date",
        "datetime",
        "attachment",
        "file",
        "image",
        "url",
        "user",
        "person",
        "formula",
    )
    return any(marker in normalized_name for marker in non_text_name_markers) or any(
        marker in type_hint for marker in non_text_type_markers
    )


def _dedupe_bitable_fields(
    candidates: Iterable[_BitableFieldCandidate],
) -> list[_BitableFieldCandidate]:
    seen: set[str] = set()
    deduped: list[_BitableFieldCandidate] = []
    for candidate in candidates:
        key = candidate.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _is_valid_bitable_field_name(name: str) -> bool:
    stripped = name.strip()
    return bool(stripped) and stripped not in {"未命名字段", "---"}


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
