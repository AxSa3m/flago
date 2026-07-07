import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from flago.agent.protocols import ResourceReader, ResourceSearcher
from flago.models import (
    ActionProposalDraft,
    AssistantRequest,
    ConversationType,
    EmptyToolRequest,
    MemoryDraftRequest,
    ModelToolSpec,
    ResourceReadRequest,
    ResourceRef,
    ResourceSearchRequest,
    ResourceType,
    ToolName,
    ToolResult,
    WebReadRequest,
    WriteActionType,
    WritebackConfirmationMode,
    WritebackProposalRequest,
)
from flago.resources.parser import parse_resource_url

ToolHandler = Callable[[BaseModel, "ToolExecutionContext"], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolDefinition:
    name: ToolName
    description: str
    parameters_model: type[BaseModel]
    handler: ToolHandler
    read_only: bool = True
    required_permissions: tuple[str, ...] = ()
    allow_group: bool = True
    timeout_seconds: float = 30.0
    audit_event_type: str | None = None

    def spec(self) -> ModelToolSpec:
        return ModelToolSpec(
            name=self.name,
            description=self.description,
            parameters=_json_schema(self.parameters_model),
            read_only=self.read_only,
            required_permissions=list(self.required_permissions),
            allow_group=self.allow_group,
            timeout_seconds=self.timeout_seconds,
            audit_event_type=self.audit_event_type,
        )


@dataclass
class ToolExecutionContext:
    actor_id: str
    conversation_type: ConversationType
    writeback_enabled: bool
    writeback_confirmation_mode: WritebackConfirmationMode
    request: AssistantRequest | None = None
    resource_reader: ResourceReader | None = None
    resource_searcher: ResourceSearcher | None = None
    store: Any | None = None
    model_router: Any | None = None


class ToolRegistry:
    def __init__(self, definitions: list[ToolDefinition]) -> None:
        self._definitions = {definition.name: definition for definition in definitions}

    def specs(self) -> list[ModelToolSpec]:
        return [definition.spec() for definition in self._definitions.values()]

    def get(self, name: ToolName) -> ToolDefinition | None:
        return self._definitions.get(name)

    async def execute(
        self,
        *,
        call_id: str,
        name: ToolName,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        definition = self.get(name)
        if definition is None:
            return ToolResult(
                call_id=call_id,
                name=name,
                ok=False,
                error=f"未知工具：{name.value}",
                user_message=f"当前版本没有可执行工具：{name.value}",
            )
        if context.conversation_type == ConversationType.GROUP and not definition.allow_group:
            return ToolResult(
                call_id=call_id,
                name=name,
                ok=False,
                error="tool_not_allowed_in_group",
                user_message="该操作暂不支持在群聊中执行，请在私聊里使用。",
            )
        if not definition.read_only and not context.writeback_enabled:
            return ToolResult(
                call_id=call_id,
                name=name,
                ok=False,
                error="writeback_disabled",
                user_message="写入功能当前未启用，无法准备写回动作。",
            )
        try:
            payload = definition.parameters_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                call_id=call_id,
                name=name,
                ok=False,
                error=f"tool_arguments_invalid: {exc.errors()}",
                user_message="工具参数不完整或格式不正确。",
            )
        try:
            result = await asyncio.wait_for(
                definition.handler(payload, context),
                timeout=definition.timeout_seconds,
            )
            return result.model_copy(update={"call_id": call_id, "name": name})
        except TimeoutError:
            return ToolResult(
                call_id=call_id,
                name=name,
                ok=False,
                error="tool_timeout",
                user_message="工具执行超时，请缩小范围或稍后重试。",
            )


def default_tool_registry(default_timeout_seconds: float = 30.0) -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDefinition(
                name=ToolName.SEARCH_RESOURCES,
                description="Search user-visible Feishu resources by query.",
                parameters_model=ResourceSearchRequest,
                handler=_search_resources,
                required_permissions=("drive:drive.search:readonly", "search:docs:read"),
                allow_group=False,
                timeout_seconds=default_timeout_seconds,
                audit_event_type="resource_searched",
            ),
            ToolDefinition(
                name=ToolName.READ_RESOURCE,
                description="Read user-authorized Feishu or web resources.",
                parameters_model=ResourceReadRequest,
                handler=_read_resource,
                required_permissions=("docx:document:readonly",),
                timeout_seconds=default_timeout_seconds,
                audit_event_type="resource_read",
            ),
            ToolDefinition(
                name=ToolName.INSPECT_DOC_STRUCTURE,
                description="Inspect readable document structure metadata without writing.",
                parameters_model=ResourceReadRequest,
                handler=_inspect_doc_structure,
                required_permissions=("docx:document:readonly",),
                timeout_seconds=default_timeout_seconds,
                audit_event_type="resource_read",
            ),
            ToolDefinition(
                name=ToolName.PREPARE_WRITEBACK,
                description="Prepare writeback drafts for confirmation or configured execution.",
                parameters_model=WritebackProposalRequest,
                handler=_prepare_writeback,
                read_only=False,
                required_permissions=("docx:document:write",),
                allow_group=True,
                timeout_seconds=default_timeout_seconds,
                audit_event_type="action_created",
            ),
            ToolDefinition(
                name=ToolName.GET_WRITEBACK_POLICY,
                description="Return current writeback confirmation policy.",
                parameters_model=EmptyToolRequest,
                handler=_get_writeback_policy,
                timeout_seconds=default_timeout_seconds,
            ),
            ToolDefinition(
                name=ToolName.LIST_MEMORY,
                description="List current user's saved long-term memory summaries.",
                parameters_model=EmptyToolRequest,
                handler=_list_memory,
                allow_group=False,
                timeout_seconds=default_timeout_seconds,
            ),
            ToolDefinition(
                name=ToolName.UPSERT_MEMORY_DRAFT,
                description="Prepare a memory item draft; saving still needs confirmation.",
                parameters_model=MemoryDraftRequest,
                handler=_upsert_memory_draft,
                read_only=False,
                allow_group=False,
                timeout_seconds=default_timeout_seconds,
                audit_event_type="memory_updated",
            ),
            ToolDefinition(
                name=ToolName.GET_MODEL_STATUS,
                description="Return current configured model provider status.",
                parameters_model=EmptyToolRequest,
                handler=_get_model_status,
                timeout_seconds=default_timeout_seconds,
            ),
            ToolDefinition(
                name=ToolName.WEB_READ,
                description="Read a normal web page URL when available.",
                parameters_model=WebReadRequest,
                handler=_web_read,
                timeout_seconds=default_timeout_seconds,
                audit_event_type="resource_read",
            ),
        ]
    )


def model_tool_specs() -> list[ModelToolSpec]:
    return default_tool_registry().specs()


async def _search_resources(payload: BaseModel, context: ToolExecutionContext) -> ToolResult:
    request = _typed(payload, ResourceSearchRequest)
    if context.resource_searcher is None:
        return _tool_error(ToolName.SEARCH_RESOURCES, "resource_search_unavailable")
    queries = _expanded_search_queries(request)
    if not queries:
        return _tool_error(ToolName.SEARCH_RESOURCES, "missing_query", "请提供搜索关键词。")
    limit = min(max(request.limit, 1), 20)
    refs: list[ResourceRef] = []
    seen: set[tuple[ResourceType, str, str | None]] = set()
    for query in queries:
        for ref in await context.resource_searcher.search(
            query,
            context.actor_id,
            limit=max(limit - len(refs), 0),
        ):
            key = (ref.type, ref.url, ref.token)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
            if len(refs) >= limit:
                break
        if len(refs) >= limit:
            break
    return ToolResult(
        call_id="",
        name=ToolName.SEARCH_RESOURCES,
        ok=True,
        content={
            "refs": [ref.model_dump(mode="json") for ref in refs],
            "count": len(refs),
            "queries": queries,
        },
        user_message="" if refs else "没有搜索到匹配的飞书资源。",
        audit_metadata={"query_count": len(queries), "result_count": len(refs)},
    )


def _expanded_search_queries(request: ResourceSearchRequest) -> list[str]:
    raw_queries = [request.query, *request.queries]
    queries: list[str] = []
    for raw_query in raw_queries:
        query = _normalize_search_query(raw_query)
        if not query:
            continue
        queries.append(query)
        queries.extend(_search_query_variants(query))
    return _unique([query for query in queries if len(query) <= 80])[:6]


def _search_query_variants(query: str) -> list[str]:
    variants: list[str] = []
    without_generic = query
    for word in (
        "飞书云文档",
        "飞书文档",
        "飞书文件",
        "云文档",
        "请帮我",
        "帮我",
        "请",
        "搜索",
        "查找",
        "找到",
        "找一下",
        "找一篇",
        "找一个",
        "文档",
        "文件",
        "资料",
        "相关的",
        "相关",
        "包含",
        "标题",
        "名字",
        "名称",
        "内容",
    ):
        without_generic = without_generic.replace(word, " ")
    without_generic = _normalize_search_query(without_generic)
    compact_without_generic = re.sub(r"\s+", "", without_generic)
    if compact_without_generic and compact_without_generic != query:
        variants.append(compact_without_generic)
    if without_generic and without_generic != query:
        variants.append(without_generic)

    for base in [without_generic, query]:
        if not base:
            continue
        compact = re.sub(r"\s+", "", base)
        for suffix in ("资料测试", "测试资料", "测试", "剧本", "教程", "案例"):
            if compact.endswith(suffix):
                shortened = compact[: -len(suffix)].strip()
                if len(shortened) >= 2:
                    variants.append(shortened)

    tokenized = [
        token
        for token in re.split(r"[\s,，。:：;；!?！？/\\|]+", without_generic or query)
        if len(token) >= 2
    ]
    variants.extend(tokenized[:3])
    return _unique(variants)


def _normalize_search_query(value: str) -> str:
    cleaned = re.sub(r"https?://\S+", " ", value or "")
    cleaned = re.sub(r"[“”\"'`《》（）()\[\]【】]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ：:，,。？?！!；;")


async def _read_resource(payload: BaseModel, context: ToolExecutionContext) -> ToolResult:
    request = _typed(payload, ResourceReadRequest)
    if context.resource_reader is None:
        return _tool_error(ToolName.READ_RESOURCE, "resource_reader_unavailable")
    results = [
        (await context.resource_reader.read(ref, context.actor_id)).model_dump(mode="json")
        for ref in request.refs
    ]
    return ToolResult(
        call_id="",
        name=ToolName.READ_RESOURCE,
        ok=all(result.get("error") is None for result in results),
        content={"results": results},
        audit_metadata={"count": len(results)},
    )


async def _inspect_doc_structure(payload: BaseModel, context: ToolExecutionContext) -> ToolResult:
    read_result = await _read_resource(payload, context)
    structures = []
    for result in read_result.content.get("results", []):
        if isinstance(result, dict):
            structures.append(
                {
                    "ref": result.get("ref"),
                    "title": result.get("title"),
                    "metadata": result.get("metadata") or {},
                    "error": result.get("error"),
                }
            )
    return ToolResult(
        call_id="",
        name=ToolName.INSPECT_DOC_STRUCTURE,
        ok=read_result.ok,
        content={"structures": structures},
        error=read_result.error,
        user_message=read_result.user_message,
        audit_metadata=read_result.audit_metadata,
    )


async def _prepare_writeback(payload: BaseModel, context: ToolExecutionContext) -> ToolResult:
    request = _typed(payload, WritebackProposalRequest)
    if not request.proposals:
        return _tool_error(ToolName.PREPARE_WRITEBACK, "missing_writeback_drafts")
    normalized = [_normalize_writeback_draft(draft, context) for draft in request.proposals]
    missing_required = [
        reason for draft in normalized if (reason := _missing_writeback_required_reason(draft))
    ]
    if missing_required:
        return _tool_error(
            ToolName.PREPARE_WRITEBACK,
            "; ".join(missing_required),
            "我还不能确定写回目标或写入内容，请明确目标文档和要写入的文字。",
        )
    unsupported = [_unsupported_writeback_reason(draft) for draft in normalized]
    unsupported = [reason for reason in unsupported if reason]
    if unsupported:
        return _tool_error(
            ToolName.PREPARE_WRITEBACK,
            "; ".join(unsupported),
            "当前版本只稳定支持写到文档开头或文档末尾；文档中间位置暂不生成写回卡片。",
        )
    return ToolResult(
        call_id="",
        name=ToolName.PREPARE_WRITEBACK,
        ok=True,
        content={
            "writeback_drafts": [
                draft.model_dump(mode="json") for draft in normalized
            ],
            "confirmation_mode": context.writeback_confirmation_mode.value,
        },
        audit_metadata={"draft_count": len(normalized)},
    )


async def _get_writeback_policy(payload: BaseModel, context: ToolExecutionContext) -> ToolResult:
    _typed(payload, EmptyToolRequest)
    return ToolResult(
        call_id="",
        name=ToolName.GET_WRITEBACK_POLICY,
        ok=True,
        content={
            "writeback_enabled": context.writeback_enabled,
            "confirmation_mode": context.writeback_confirmation_mode.value,
        },
    )


async def _list_memory(payload: BaseModel, context: ToolExecutionContext) -> ToolResult:
    _typed(payload, EmptyToolRequest)
    if context.store is None:
        return _tool_error(ToolName.LIST_MEMORY, "memory_store_unavailable")
    items = await context.store.list_memory_items(context.actor_id)
    return ToolResult(
        call_id="",
        name=ToolName.LIST_MEMORY,
        ok=True,
        content={"items": [item.model_dump(mode="json") for item in items]},
        user_message="" if items else "暂时没有保存长期记忆。",
    )


async def _upsert_memory_draft(payload: BaseModel, context: ToolExecutionContext) -> ToolResult:
    request = _typed(payload, MemoryDraftRequest)
    return ToolResult(
        call_id="",
        name=ToolName.UPSERT_MEMORY_DRAFT,
        ok=True,
        content={"draft": request.model_dump(mode="json")},
        user_message="已准备记忆草案，保存前仍需要用户确认。",
    )


async def _get_model_status(payload: BaseModel, context: ToolExecutionContext) -> ToolResult:
    _typed(payload, EmptyToolRequest)
    router = context.model_router
    if router is None:
        return _tool_error(ToolName.GET_MODEL_STATUS, "model_router_unavailable")
    current_provider = getattr(router, "default_provider", "")
    current_model = getattr(router, "default_model", None)
    catalog = [
        {
            "provider": getattr(item, "provider", ""),
            "model": getattr(item, "model", None),
            "configured": getattr(item, "configured", False),
        }
        for item in getattr(router, "catalog", [])
    ]
    return ToolResult(
        call_id="",
        name=ToolName.GET_MODEL_STATUS,
        ok=True,
        content={
            "default_provider": current_provider,
            "default_model": current_model,
            "catalog": catalog,
        },
    )


async def _web_read(payload: BaseModel, context: ToolExecutionContext) -> ToolResult:
    request = _typed(payload, WebReadRequest)
    if context.resource_reader is None:
        return _tool_error(ToolName.WEB_READ, "resource_reader_unavailable")
    ref = ResourceRef(type=ResourceType.WEB, url=request.url)
    result = await context.resource_reader.read(ref, context.actor_id)
    return ToolResult(
        call_id="",
        name=ToolName.WEB_READ,
        ok=result.error is None,
        content={"result": result.model_dump(mode="json")},
        error=result.error,
        user_message=result.error,
    )


def _normalize_writeback_draft(
    draft: ActionProposalDraft,
    context: ToolExecutionContext,
) -> ActionProposalDraft:
    if draft.action_type != WriteActionType.DOC_APPEND:
        return draft
    target = dict(draft.target)
    payload = dict(draft.payload)
    document_id = _doc_id_from_target(target)
    if document_id:
        target["document_id"] = document_id
    url = _target_url(target)
    if url:
        target["url"] = url
    title = _target_title(target, context.request)
    if title:
        target["title"] = title
    content = _followup_writeback_content(context.request) or _doc_append_content(payload)
    if content:
        payload["content"] = content
    payload.pop("text", None)
    position = _write_position(target, payload, draft.preview)
    target.pop("position", None)
    payload.pop("position", None)
    if position == "start" and document_id:
        target["block_id"] = document_id
        target["index"] = 0
    elif position == "end":
        if target.get("block_id") == document_id:
            target.pop("block_id", None)
        if _int_or_none(target.get("index")) in {-1, None}:
            target.pop("index", None)
    preview = _normalized_doc_append_preview(
        content=content or str(payload.get("content") or ""),
        position=position,
        fallback=draft.preview,
    )
    return draft.model_copy(update={"target": target, "payload": payload, "preview": preview})


def _missing_writeback_required_reason(draft: ActionProposalDraft) -> str:
    if draft.action_type != WriteActionType.DOC_APPEND:
        return ""
    if not str(draft.target.get("document_id") or "").strip():
        return "missing_document_id"
    if not str(draft.payload.get("content") or "").strip():
        return "missing_doc_append_content"
    return ""


def _unsupported_writeback_reason(draft: ActionProposalDraft) -> str:
    if draft.action_type != WriteActionType.DOC_APPEND:
        return ""
    document_id = str(draft.target.get("document_id") or "").strip()
    block_id = str(draft.target.get("block_id") or "").strip()
    index = _int_or_none(draft.target.get("index", -1))
    if not block_id and index == -1:
        return ""
    if document_id and block_id == document_id and index == 0:
        return ""
    return "unsupported_doc_middle_insert"


def _doc_id_from_target(target: dict[str, Any]) -> str:
    direct = _first_text(target, "document_id", "doc_id", "token")
    if direct:
        return direct
    url = _target_url(target)
    if not url:
        return ""
    ref = parse_resource_url(url)
    if ref.type == ResourceType.FEISHU_DOC and ref.token:
        return ref.token
    return ""


def _target_url(target: dict[str, Any]) -> str:
    return _first_text(target, "target_url", "url", "link")


def _target_title(target: dict[str, Any], request: AssistantRequest | None) -> str:
    title = _first_text(target, "target_title", "title", "name", "document_title")
    if title and not _is_generic_target_title(title):
        return title
    document_id = _doc_id_from_target(target)
    url = _target_url(target)
    if request is not None:
        matched = _title_from_known_resources(request, document_id=document_id, url=url)
        if matched:
            return matched
        from_text = _title_from_request_text(request.text)
        if from_text:
            return from_text
        for message in reversed(request.chat_context_messages):
            from_context = _title_from_request_text(message.text)
            if from_context:
                return from_context
    return "" if _is_generic_target_title(title) else title


def _title_from_known_resources(
    request: AssistantRequest,
    *,
    document_id: str,
    url: str,
) -> str:
    for result in request.resource_results:
        if _resource_matches(result.ref, document_id=document_id, url=url):
            title = (result.title or result.ref.title or "").strip()
            if title and not _is_generic_target_title(title):
                return title
    for ref in request.resource_refs:
        if _resource_matches(ref, document_id=document_id, url=url):
            title = (ref.title or "").strip()
            if title and not _is_generic_target_title(title):
                return title
    return ""


def _resource_matches(ref: ResourceRef, *, document_id: str, url: str) -> bool:
    if document_id and ref.token == document_id:
        return True
    return bool(url and ref.url == url)


def _title_from_request_text(text: str) -> str:
    for pattern in (r"《([^》]{2,60})》",):
        for match in re.finditer(pattern, text):
            candidate = _clean_target_title(match.group(1))
            if candidate:
                return candidate
    marker_match = re.search(
        r"(?:到|进|写到|写进)\s*([^，。！？\n]{2,80}?)(?:的)?(?:开头|末尾|文末|里|里面|中|之前|前面|后面|$)",
        text,
    )
    if marker_match:
        return _clean_target_title(marker_match.group(1))
    return ""


def _clean_target_title(value: str) -> str:
    cleaned = re.sub(r"https?://\S+", " ", value)
    cleaned = re.sub(
        r"(?:开头|末尾|文末|最后|最前面|最开始|里面|里|中|之前|前面|后面|的多维表格|多维表格)$",
        "",
        cleaned.strip(),
    )
    cleaned = cleaned.strip(" ：:，,。？?！!；;\"'`“”《》")
    if not cleaned or _is_generic_target_title(cleaned):
        return ""
    return cleaned[:80]


def _is_generic_target_title(value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "").casefold()
    return compact in {"", "目标", "目标文档", "目标资源", "文档", "打开目标资源"}


def _doc_append_content(payload: dict[str, Any]) -> str:
    return _first_text(payload, "content", "text", "value")


def _followup_writeback_content(request: AssistantRequest | None) -> str:
    if request is None or not _is_location_only_writeback_followup(request.text):
        return ""
    for message in reversed(request.chat_context_messages):
        text = message.text.strip()
        if not text or text == request.text.strip():
            continue
        if message.sender_id and request.actor_id and message.sender_id != request.actor_id:
            continue
        content = _content_from_writeback_request(text)
        if content:
            return content
        if _is_bare_writeback_content(text):
            return text.strip()
    return ""


def _content_from_writeback_request(text: str) -> str:
    for pattern in (
        r"[“\"']([^”\"']{1,500})[”\"']",
        r"写\s*一句\s*([^，。！？\n]{1,500}?)(?:到|进|写到|写进)",
    ):
        match = re.search(pattern, text)
        if match:
            content = match.group(1).strip(" ：:，,。？?！!；;\"'`“”")
            if content:
                return content
    return ""


def _is_bare_writeback_content(text: str) -> bool:
    normalized = text.strip()
    if not normalized or len(normalized) > 200:
        return False
    if normalized.startswith(("/", "\\")):
        return False
    if _is_location_only_writeback_followup(normalized):
        return False
    return not any(
        marker in normalized for marker in ("？", "?", "吗", "帮我", "请", "写到", "写进")
    )


def _is_location_only_writeback_followup(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text.strip().casefold())
    if not normalized:
        return False
    location_markers = ("开头", "结尾", "末尾", "文末", "最后", "top", "bottom")
    if not any(marker in normalized for marker in location_markers):
        return False
    if not any(marker in normalized for marker in ("写", "加", "追加", "插入", "放")):
        return False
    return len(normalized) <= 24


def _write_position(
    target: dict[str, Any],
    payload: dict[str, Any],
    preview: str,
) -> str:
    raw = f"{target.get('position') or ''} {payload.get('position') or ''} {preview}"
    lowered = raw.casefold()
    if any(marker in lowered for marker in ("start", "top", "开头", "最前", "最开始")):
        return "start"
    if any(marker in lowered for marker in ("end", "bottom", "结尾", "末尾", "文末", "最后")):
        return "end"
    return ""


def _normalized_doc_append_preview(*, content: str, position: str, fallback: str) -> str:
    cleaned = content.strip()
    if not cleaned:
        return fallback
    if position == "start":
        return f"向文档开头插入文本：\n{cleaned}"
    if position == "end":
        return f"向文档追加文本：\n{cleaned}"
    return fallback or f"向文档追加文本：\n{cleaned}"


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool_error(name: ToolName, error: str, user_message: str | None = None) -> ToolResult:
    return ToolResult(
        call_id="",
        name=name,
        ok=False,
        error=error,
        user_message=user_message or error,
    )


def _typed[T: BaseModel](payload: BaseModel, model: type[T]) -> T:
    if isinstance(payload, model):
        return payload
    return model.model_validate(payload.model_dump())


def _json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None
